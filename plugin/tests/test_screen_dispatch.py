"""Look at this, dispatch: which spoken requests ask the Mac for a capture, what each Hermes run
carries (images as image parts, files by saved path), where requests that carry something may go
(never a group chat, a thread or a channel nobody named), and what the voice hears about it. The
Mac is played by FakeMac over real HTTP; Hermes by the fake API server."""
from __future__ import annotations

import base64
import itertools
import json
import threading
import time

import pytest

from fakes import SDP, FakeMac, http, upload_attachment, wait_for
from speakeasy import attachments as A
from speakeasy import calls, channels, continuity, router, threads
from speakeasy.home_control import Reply
from speakeasy.prompt import builder as P

JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
WORK = {"target": "discord:111", "label": "#work", "topic": "my job: meetings, email, projects", "new_thread": False}
RESEARCH = {"target": "discord:222", "label": "#research", "topic": "reading up on a subject", "new_thread": False}


def jpeg(marker: bytes, size: int = 8_000) -> bytes:
    return JPEG_HEAD + (marker * (size // len(marker) + 1))[:size - len(JPEG_HEAD)]


WINDOW = jpeg(b"xcode-window ")
PICTURE = jpeg(b"header-picture ")


def images_in(run_input) -> list[bytes]:
    if isinstance(run_input, str):
        return []
    return [base64.b64decode(p["image_url"]["url"].split(",", 1)[1])
            for p in run_input[0]["content"] if p["type"] == "image_url"]


def text_of(run_input) -> str:
    return run_input if isinstance(run_input, str) else run_input[0]["content"][0]["text"]


class Call:
    """One call from a Mac that declared ``screen`` (or not), with the helpers a test needs."""

    def __init__(self, server, service, key: str, screen: str | None = "ready"):
        body = {"sdp": SDP, **({"screen": screen} if screen else {})}
        status, session = http(server.base_url, "POST", "/voice/sessions", body, server.token, {"Idempotency-Key": key})
        assert status == 201, session
        self.server, self.service, self.iid = server, service, session["interaction_id"]
        self.worker = service.workers[-1]
        self.interaction = service.interaction(self.iid)
        self.seq = 0
        self.mac: FakeMac | None = None

    def sharing(self, on: bool) -> None:
        self.seq += 1
        status, _ = http(self.server.base_url, "POST", f"/voice/interactions/{self.iid}/screen",
                         {"on": on, "seq": self.seq}, self.server.token)
        assert status == 200

    def mac_replies(self, reply) -> FakeMac:
        self.mac = FakeMac(self.server.base_url, self.server.token, self.interaction, reply)
        return self.mac

    def drop(self, data: bytes, kind: str = "picture", ctype: str = "image/jpeg", name: str | None = None) -> dict:
        status, body = upload_attachment(self.server.base_url, self.server.token, self.iid, data, kind, ctype,
                                         {"X-Speakeasy-Filename": name} if name else None)
        assert status == 200, body
        return body

    def say(self, delegation_id: str, words: str, **extra) -> None:
        """One spoken request after the voice's last reply, so it reads as a request of its own."""
        self.worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 1, "end_ms": 2})
        self.worker.delegate(delegation_id, words, **extra)

    def captures(self) -> list[str]:
        return [p["capture_id"] for _, kind, p in list(self.interaction.feed.ring) if kind == "capture"]

    def spoken(self) -> list[str]:
        return [c for k, _, c in self.worker.sent if k == "session.commentary.append"]

    def notes(self) -> list[str]:
        return [c for k, _, c in self.worker.sent if k == "session.thinking.append"]

    def run(self, delegation_id: str):
        return self.worker.interaction.runs.get(delegation_id)


@pytest.fixture
def open_call(server, service, monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)
    service.hermes.image_support = {"images": True, "vision": "native"}  # Hermes here reads images
    made: list[Call] = []

    def make(key: str, screen: str | None = "ready") -> Call:
        made.append(Call(server, service, key, screen))
        return made[-1]
    yield make
    for call in made:
        if call.mac is not None:
            call.mac.stop()


def window(app: str = "Xcode", data: bytes = WINDOW):
    return lambda capture_id: {"data": data, "app": app}


def delegation_of(service, run_id: str) -> str:
    return service.store.task_id_for(service.store.key_for_run(run_id))


def runs_started(service, hermes, count: int) -> dict:
    """The first ``count`` Hermes runs by task id, once each run's id is recorded against its task
    (the fake Hermes sees the request a moment before the plugin stores the run id it got back)."""
    return wait_for(lambda: (lambda by: by if len(hermes.calls) == count and len(by) == count and None not in by
                             else None)({delegation_of(service, run["run_id"]): run for run in hermes.calls}))


def tasks(server):
    return http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]


# -- the matcher ----------------------------------------------------------------------------------

@pytest.mark.parametrize("request_", [
    "What's this error?", "what's this", "Hmm, what is this?", "what does this say", "look at this",
    "can you take a look at this for me", "is this layout right?", "what's on my screen", "fix these errors",
    "reply to this saying I'm out until Monday", "what does this line do", "summarize this page",
    "help me with what I'm looking at", "check this out", "stop looking at my screen", "share my screen"])
def test_screen_references_match(request_):
    assert router.refers_to_screen(request_)


@pytest.mark.parametrize("request_", [
    "look at this weekend's forecast", "show me what you're looking at", "what are you looking at",
    "what's on your screen", "how much screen time did I have this week", "fix the screen door",
    "add sunscreen to the list", "let me look at this", "let me take a look at this", "what's that",
    "draft this email to my landlord", "remind me to call Sam", "make this the header image", "summarize this",
    "look at this year's budget", "what time is it in Tokyo", "show me", "can I see this"])
def test_screen_references_never_match(request_):
    assert not router.refers_to_screen(request_)


def test_quick_answers_never_take_a_screen_question():
    from speakeasy import quick  # in a call that can share; an undeclared call is as before (see below)
    assert not quick.eligible("What's this error?", can_share=True)
    assert not quick.eligible("is this layout right?", can_share=True)
    assert quick.eligible("Who won the hockey game last night", can_share=True)
    assert quick.eligible("What's this error?")  # a call that can't share: "this" is never their screen


def test_the_task_view_stays_the_tasks():
    assert router.about_task_view("show me what you're looking at") and router.about_task_view("what's on your screen")
    assert not router.about_task_view("look at my screen") and not router.about_task_view("show me this")


def test_curly_apostrophes_read_as_straight_ones():
    # Transcripts use both: "What’s this?" points at the screen exactly like "What's this?".
    assert router.refers_to_screen("What’s this?") and router.refers_to_screen("help me with what I’m looking at")
    assert router.about_task_view("show me what you’re looking at")
    assert router.needs_screen("What’s this?") and not router.needs_screen("show me what you’re looking at")


def test_a_chat_with_no_recorded_type_is_never_private():
    origin = json.dumps({"chat_id": "555", "user_id": "42"})
    unknown = continuity._from_row("s1", "telegram", None, None, origin, "Chat", 1.0)
    assert unknown.chat_type == "dm" and not unknown.private  # text work reads it as before
    assert continuity._from_row("s2", "telegram", "dm", None, origin, "Chat", 1.0).private
    assert not continuity._from_row("s3", "discord", "group", None, origin, "Chat", 1.0).private


# -- what a run carries ---------------------------------------------------------------------------

def test_whats_this_error_carries_one_capture(open_call, server, service, hermes):
    call = open_call("req_err")
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_err", "What's this error?")
    wait_for(lambda: hermes.calls)
    assert len(call.captures()) == 1
    run = hermes.calls[0]
    assert images_in(run["input"]) == [WINDOW]
    assert "a screenshot of their Xcode window (saved at " in text_of(run["input"])
    task = wait_for(lambda: next((t for t in tasks(server) if t.get("shared")), None))
    assert task["shared"] == [{"kind": "screen", "app": "Xcode"}]
    # The app shows it from Speakeasy's copy; paths never leave the server.
    status, data = http(server.base_url, "GET", f"/voice/shared-image/{run['run_id']}/1", token=server.token)
    assert status == 200 and data == WINDOW
    assert http(server.base_url, "GET", f"/voice/shared-image/{run['run_id']}/2", token=server.token)[0] == 404
    assert http(server.base_url, "GET", f"/voice/shared-image/{run['run_id']}/1")[0] == 401
    assert "/cache/" not in json.dumps(tasks(server))
    assert P.shared_note("Xcode", True, 0, 0) in call.notes()
    assert not any("I'm taking your" in s for s in call.spoken())  # they asked about the screen: no notice


def test_a_dropped_picture_goes_with_the_next_request(open_call, server, service, hermes):
    call = open_call("req_pic")
    call.drop(PICTURE)
    call.say("call_pic", "Make this the header image")
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == [PICTURE]
    assert call.captures() == []  # sharing is off
    snap = http(server.base_url, "GET", f"/voice/interactions/{call.iid}", token=server.token)[1]
    assert snap["attachments"] == []
    task = wait_for(lambda: next((t for t in tasks(server) if t.get("shared")), None))
    assert task["shared"] == [{"kind": "picture"}]
    status, data = http(server.base_url, "GET", f"/voice/shared-image/{task['run_id']}/1", token=server.token)
    assert status == 200 and data == PICTURE


def test_a_dropped_file_is_named_by_its_saved_path(open_call, server, service, hermes, home):
    call = open_call("req_pdf")
    report = b"%PDF-1.7\nQuarterly numbers, private " * 50
    call.drop(report, "file", "application/pdf", "report.pdf")
    call.say("call_pdf", "Summarize this")
    wait_for(lambda: hermes.calls)
    prompt = hermes.calls[0]["input"]
    assert isinstance(prompt, str), "a file is never an image part"
    saved = next(p for p in (home / "cache" / "speakeasy" / "shared").glob("*/report.pdf"))
    assert f"the file report.pdf (saved at {saved})" in prompt and "file tools" in prompt
    assert "Quarterly numbers" not in prompt and saved.read_bytes() == report
    task = wait_for(lambda: next((t for t in tasks(server) if t.get("shared")), None))
    assert task["shared"] == [{"kind": "file", "name": "report.pdf"}]
    assert http(server.base_url, "GET", f"/voice/shared-image/{task['run_id']}/1", token=server.token)[0] == 404


def test_a_follow_up_to_a_finished_task_carries_the_capture_in_its_session(open_call, server, service, hermes):
    call = open_call("req_follow")
    call.say("call_first", "Draft the release notes")
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_more", "Add what's on my screen to it", task_id="call_first")
    wait_for(lambda: len(hermes.calls) == 2)
    assert hermes.calls[1]["session_id"] == hermes.calls[0]["session_id"]
    assert images_in(hermes.calls[1]["input"]) == [WINDOW] and images_in(hermes.calls[0]["input"]) == []


def test_a_later_run_in_that_session_hears_the_screenshot_is_old(open_call, server, service, hermes):
    call = open_call("req_stale")
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_shot", "What's this error?")
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    call.sharing(False)
    call.say("call_after", "And how do I fix it", task_id="call_shot")
    wait_for(lambda: len(hermes.calls) == 2)
    second = hermes.calls[1]
    assert second["session_id"] == hermes.calls[0]["session_id"] and isinstance(second["input"], str)
    assert "No new screenshot with this message; the last one is from " in second["input"]
    assert "No new screenshot" not in text_of(hermes.calls[0]["input"])


def test_an_undeclared_call_looking_at_the_screen_runs_text_only(open_call, server, service, hermes):
    call = open_call("req_iphone", screen=None)
    call.say("call_look", "Look at my screen")
    wait_for(lambda: hermes.calls)
    assert isinstance(hermes.calls[0]["input"], str) and call.captures() == []
    snap = http(server.base_url, "GET", f"/voice/interactions/{call.iid}", token=server.token)[1]
    assert snap["hold"] is None and snap["captures"] == []


def test_this_page_in_an_undeclared_call_is_still_the_tasks(open_call, server, service, hermes, home):
    # An iPhone can't point at its own screen: "what does this page look like?" about a running task
    # brings up the task's picture, as before Look at this.
    folder = home / "cache" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    photo = folder / "hotel.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    hermes.hold = True
    hermes.live_events = [{"event": "media.seen", "path": str(photo), "source": "screenshot"}]
    call = open_call("req_iphone_page", screen=None)
    call.say("call_hotel", "Research the Lisbon hotels")
    running = wait_for(lambda: (lambda r: r if r and r.run_id else None)(call.run("call_hotel")))
    wait_for(lambda: service.store.live_image(running.idem_key))
    hermes.hold, hermes.live_events = False, []
    service.rt.route_call = lambda m: ('{"follow_up_task_id": "call_hotel", "parts": ["x"], "channel": null, "show": %s}'
                                       % str("look like" in m[-1]["content"].rsplit("Request: ", 1)[-1]).lower())
    call.say("call_page", "What does this page look like?")  # routing says: show the task's picture
    show = wait_for(lambda: [p for _, k, p in list(call.interaction.feed.ring) if k == "show"])
    assert show[-1]["task_id"] == "call_hotel" and show[-1]["image"] == "live"
    call.say("call_put", "Put it on my screen")  # "show me" in words that name a screen: still the task's
    show = wait_for(lambda: (lambda s: s if len(s) == 2 else None)(
        [p for _, k, p in list(call.interaction.feed.ring) if k == "show"]))
    assert show[-1]["task_id"] == "call_hotel"
    assert hermes.steers == [] and len(hermes.calls) == 1 and call.captures() == []
    hermes.runs[running.run_id]["done"].set()


def test_a_screen_question_in_an_undeclared_call_keeps_the_quick_lane(open_call, server, service, hermes):
    asked: list[str] = []
    service.rt.quick_call = lambda request, today: asked.append(request) or "A 404 means the page wasn't found."
    call = open_call("req_iphone_quick", screen=None)
    call.say("call_err", "What's this error?")
    wait_for(lambda: any("404" in c for c in call.spoken()))
    assert asked == ["What's this error?"] and hermes.calls == [] and call.captures() == []


# -- requests that never carry anything -----------------------------------------------------------

def test_home_control_never_asks_for_a_capture(open_call, server, service, hermes):
    service.rt.home = type("Home", (), {"enabled": True, "respond": staticmethod(
        lambda request, notes, answering=None, following=False:
        Reply(True, "Kitchen lights at 30%.") if "kitchen" in request else None)})()
    call = open_call("req_home")
    call.sharing(True)
    call.say("call_home", "kitchen lights to 30%")
    wait_for(lambda: "Kitchen lights at 30%." in call.spoken())
    time.sleep(0.3)
    assert call.captures() == [] and hermes.calls == []


def test_the_instant_lane_answers_and_drops_its_capture(open_call, server, service, hermes):
    call = open_call("req_tokyo")
    call.sharing(True)
    call.say("call_tokyo", "what time is it in Tokyo")
    wait_for(lambda: any("Tokyo" in c for c in call.spoken()))
    [capture_id] = call.captures()
    wait_for(lambda: call.interaction.attachments.open_captures() == [])
    assert call.interaction.attachments.wait_capture_blocking(capture_id, 0.1).reason == "cancelled"
    assert hermes.calls == [] and not any("screen" in s for s in call.spoken())


def test_a_screen_question_skips_the_quick_lane(open_call, server, service, hermes):
    asked: list[str] = []
    service.rt.quick_call = lambda request, today: asked.append(request) or "The Otters won 5 to 2."
    call = open_call("req_quick")
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_err", "What's this error?")  # a question a quick lookup would otherwise try
    wait_for(lambda: hermes.calls)
    assert asked == [] and images_in(hermes.calls[0]["input"]) == [WINDOW]
    other = open_call("req_quick_2")
    other.sharing(True)
    other.say("call_score", "Who won the hockey game last night")  # not about the screen: quick as ever
    wait_for(lambda: any("Otters" in c for c in other.spoken()))
    assert asked == ["Who won the hockey game last night"] and len(hermes.calls) == 1


def test_my_screen_is_captured_but_what_youre_looking_at_is_the_tasks(open_call, server, service, hermes, home):
    folder = home / "cache" / "screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    photo = folder / "hotel.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    hermes.hold = True
    hermes.live_events = [{"event": "media.seen", "path": str(photo), "source": "screenshot"}]
    call = open_call("req_views")
    call.say("call_hotel", "Research the Lisbon hotels")
    running = wait_for(lambda: (lambda r: r if r and r.run_id else None)(call.run("call_hotel")))
    wait_for(lambda: service.store.live_image(running.idem_key))
    hermes.hold, hermes.live_events = False, []
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_view", "Show me what you're looking at")
    show = wait_for(lambda: [p for _, k, p in list(call.interaction.feed.ring) if k == "show"])
    assert show[-1]["task_id"] == "call_hotel" and call.captures() == [] and len(hermes.calls) == 1
    call.say("call_mine", "What's on my screen")
    wait_for(lambda: len(hermes.calls) == 2)
    assert images_in(hermes.calls[1]["input"]) == [WINDOW]
    hermes.runs[running.run_id]["done"].set()


class Threads:
    """A channel that opens a thread per task; ``finish`` answers at once, else it works until released."""

    def __init__(self, finish: bool = True):
        self.opened, self.posted, self.release = [], [], finish

    def available(self, target):
        return True

    def open(self, target, *, message, title, delivery_id):
        self.opened.append(message)
        return threads.Opened("speakeasy-x", "777", "discord")

    def wait(self, opened, on_session, on_title=None, on_activity=None):
        on_session("thread_sess_1")
        while not self.release:
            time.sleep(0.05)
        return "Booked Thursday at 4:15."

    def post(self, platform, thread_id, *, message, delivery_id):
        self.posted.append(message)
        return True


def thread_channel(service, runner: Threads) -> None:
    service.rt.threads = runner
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [dict(WORK, new_thread=True)]}})
    service.rt.topical_channel = lambda label: channels.Choice(
        channels.Channel(WORK["target"], WORK["label"], WORK["topic"], new_thread=True), "topic")


def test_look_at_my_screen_never_asks_for_the_tasks_picture(open_call, server, service, hermes):
    service.rt.route_call = lambda m: '{"follow_up_task_id": null, "parts": ["x"], "channel": null, "show": true}'
    call = open_call("req_noshow")
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_look", "Look at my screen and tell me what you think")
    wait_for(lambda: hermes.calls)
    assert P.SHOW_IT_FOCUS.strip() not in text_of(hermes.calls[0]["input"])
    assert not call.worker.wants_show and not [p for _, k, p in list(call.interaction.feed.ring) if k == "show"]
    # A running thread task, in a call that can share but isn't sharing: words only, and no picture asked
    # for. (A call that can't share at all asks for the task's picture, as before: see message_thread_task.)
    runner = Threads(finish=False)
    thread_channel(service, runner)
    other = open_call("req_noshow_thread")
    other.say("call_book", "Get me a dentist cleaning on the 9th")
    wait_for(lambda: runner.opened)
    wait_for(lambda: service.store.continued_for(other.run("call_book").idem_key))
    other.say("call_mine", "Look at my screen", task_id="call_book")
    wait_for(lambda: runner.posted)
    assert "SEE" not in runner.posted[0] and "Look at my screen" in runner.posted[0]
    runner.release = True


# -- one capture per request, never another's ----------------------------------------------------

def test_close_handoffs_never_swap_captures(open_call, server, service, hermes, monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0)
    shots = {}
    call = open_call("req_ab")
    call.sharing(True)

    def reply(capture_id):
        first = call.mac.seen.index(capture_id) == 0  # A's request came first ...
        shots[capture_id] = jpeg(b"window-A " if first else b"window-B ")
        # ... B's capture lands first: A's waits until B's upload is answered (not a fixed delay, which a
        # busy machine outlasts before B's request is even asked).
        b_answered = lambda: len(call.mac.seen) > 1 and call.mac.seen[1] in call.mac.answers
        return {"data": shots[capture_id], "app": "Xcode", **({"after": b_answered} if first else {})}
    mac = call.mac_replies(reply)
    call.say("call_a", "Fix the login bug")
    call.say("call_b", "Rename the release notes file")
    by = runs_started(service, hermes, 2)
    a_shot, b_shot = (shots[c] for c in mac.seen)
    assert list(mac.answers) == mac.seen[::-1], "B's capture was uploaded before A's"
    assert images_in(by["call_a"]["input"]) == [a_shot] and images_in(by["call_b"]["input"]) == [b_shot]


def test_a_picture_dropped_while_a_request_routes_goes_with_the_next(open_call, server, service, hermes):
    routing, go = threading.Event(), threading.Event()

    def route_call(messages):
        if "offsite" in messages[-1]["content"].rsplit("Request: ", 1)[-1]:
            routing.set()
            go.wait(15)  # released by the test once B has started, never by the clock
        return None
    service.rt.route_call = route_call
    call = open_call("req_late_drop")
    call.say("call_a", "Plan the offsite agenda and the dinner")
    assert routing.wait(5)
    call.drop(PICTURE)
    call.say("call_b", "Make this the header image")
    wait_for(lambda: hermes.calls)
    go.set()
    by = runs_started(service, hermes, 2)
    assert images_in(by["call_b"]["input"]) == [PICTURE] and images_in(by["call_a"]["input"]) == []


# -- running tasks -------------------------------------------------------------------------------

def test_a_plain_follow_up_to_a_running_task_is_words_only(open_call, server, service, hermes):
    hermes.hold = True
    call = open_call("req_plain")
    call.say("call_draft", "Draft a reply to the vendor")
    running = wait_for(lambda: (lambda r: r if r and r.run_id else None)(call.run("call_draft")))
    call.sharing(True)
    call.say("call_short", "Make it shorter", task_id="call_draft")
    wait_for(lambda: hermes.steers)
    assert hermes.steers[-1][1] == P.steer_text(service.rt.names, "Make it shorter")
    [capture_id] = call.captures()
    wait_for(lambda: call.interaction.attachments.open_captures() == [])
    assert call.interaction.attachments.wait_capture_blocking(capture_id, 0.1).reason == "cancelled"
    assert len(hermes.calls) == 1 and not any("screen" in s for s in call.spoken())  # dropped silently
    hermes.runs[running.run_id]["done"].set()


def test_whats_this_steers_a_running_task_with_the_saved_screenshot(open_call, server, service, hermes):
    hermes.hold = True
    numbers = itertools.count(1)
    call = open_call("req_steer")
    call.say("call_draft", "Draft a reply to the vendor")
    running = wait_for(lambda: (lambda r: r if r and r.run_id else None)(call.run("call_draft")))
    call.sharing(True)
    call.mac_replies(lambda capture_id: {"data": jpeg(f"window-{next(numbers)} ".encode()), "app": "Mail"})
    call.say("call_this", "What's this?", task_id="call_draft")
    wait_for(lambda: hermes.steers)
    steer = hermes.steers[-1][1]
    assert "a screenshot of their Mail window (saved at " in steer and "vision_analyze" in steer
    path = steer.split("(saved at ", 1)[1].split(")", 1)[0]
    assert open(path, "rb").read() == jpeg(b"window-1 ") and len(hermes.calls) == 1
    row = wait_for(lambda: next((t for t in tasks(server) if t["task_id"] == "call_draft" and t.get("shared")), None))
    assert row["shared"] == [{"kind": "screen", "app": "Mail"}]
    # The run stops taking guidance: a new task that references it carries the capture.
    hermes.runs[running.run_id]["status"] = "completed"
    call.say("call_that", "And what's this?", task_id="call_draft")
    wait_for(lambda: len(hermes.calls) == 2)
    assert images_in(hermes.calls[1]["input"]) == [jpeg(b"window-2 ")]
    assert "Draft a reply to the vendor" in text_of(hermes.calls[1]["input"])
    hermes.runs[running.run_id]["done"].set()


# -- where it may go -----------------------------------------------------------------------------

def group_candidate(monkeypatch, conv) -> list:
    streamed = []
    monkeypatch.setattr(continuity, "conversations_with_context", lambda db, request, **kw: [continuity.Candidate(conv, ())])
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **kw: False)

    def stream(base, key, c, message, callback, **kw):
        streamed.append(message)
        callback("run.started", {"run_id": "run_chat1"})
        callback("assistant.completed", {"content": "Picked up.\nSPOKEN: Picked up."})
        callback("run.completed", {})
        return True
    monkeypatch.setattr(continuity, "stream_session_chat", stream)
    return streamed


CONTINUE = '{"follow_up_task_id": null, "conversation": "c1", "parts": ["x"], "channel": null}'


@pytest.mark.parametrize("kind", ["group", "unknown"])
def test_a_group_chat_never_gets_what_was_shared(open_call, server, service, hermes, monkeypatch, kind):
    origin = json.dumps({"chat_id": "555", "user_id": "42", **({"chat_type": "group"} if kind == "group" else {})})
    conv = continuity._from_row("s_budget", "telegram", None, None, origin, "Budget", time.time())
    streamed = group_candidate(monkeypatch, conv)
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": []}})
    service.rt.route_call = lambda m: CONTINUE
    # Words only: the chat is continued as before.
    plain = open_call(f"req_group_plain_{kind}")
    plain.say("call_plain", "Keep going on the budget in that chat")
    wait_for(lambda: streamed)
    assert hermes.calls == []
    # With the screen: a new task, answered home, that knows which conversation it was about.
    call = open_call(f"req_group_{kind}")
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_shared", "Keep going on the budget in that chat with this chart")
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == [WINDOW] and len(streamed) == 1
    assert "It's a shared chat" in text_of(hermes.calls[0]["input"])
    assert call.run("call_shared").deliver_to is None


def test_the_users_own_dm_takes_the_capture(open_call, server, service, hermes, monkeypatch):
    origin = json.dumps({"chat_id": "555", "user_id": "42", "chat_type": "dm"})
    streamed = group_candidate(monkeypatch, continuity._from_row("s_dm", "telegram", "dm", None, origin, "Budget",
                                                                 time.time()))
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": []}})
    service.rt.route_call = lambda m: CONTINUE
    call = open_call("req_dm")
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_dm", "Keep going on the budget in that chat with this chart")
    wait_for(lambda: streamed)
    message = streamed[0]
    assert isinstance(message, list) and base64.b64decode(message[1]["image_url"]["url"].split(",", 1)[1]) == WINDOW
    assert "a screenshot of their Xcode window (saved at " in message[0]["text"] and hermes.calls == []


def test_a_screen_follow_up_to_a_finished_thread_task_goes_home(open_call, server, service, hermes, monkeypatch):
    runner = Threads(finish=True)
    thread_channel(service, runner)
    conv = continuity.Conversation("thread_sess_1", "discord", "777", "thread", "777", "42", "111",
                                   "Server / #work / Dentist", "Dentist", time.time())
    monkeypatch.setattr(continuity, "conversation_by_session", lambda db, sid: conv if sid == "thread_sess_1" else None)
    monkeypatch.setattr(continuity, "stream_session_chat",
                        lambda *a, **kw: pytest.fail("a shared screen went into a thread"))
    call = open_call("req_thread")
    call.say("call_book", "Get me a dentist cleaning on the 9th")
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_fix", "And fix the time there, look at this error on my screen", task_id="call_book")
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == [WINDOW] and runner.posted == []
    assert call.run("call_fix").deliver_to is None


def test_a_riding_along_capture_is_given_up_and_the_thread_continues(open_call, server, service, hermes, monkeypatch):
    runner = Threads(finish=True)
    thread_channel(service, runner)
    conv = continuity.Conversation("thread_sess_1", "discord", "777", "thread", "777", "42", "111",
                                   "Server / #work / Dentist", "Dentist", time.time())
    monkeypatch.setattr(continuity, "conversation_by_session", lambda db, sid: conv if sid == "thread_sess_1" else None)
    streamed: list = []
    monkeypatch.setattr(continuity, "stream_session_chat",
                        lambda base, key, c, message, callback, **kw: streamed.append(message) or True)
    call = open_call("req_thread_ride")
    call.say("call_book", "Get me a dentist cleaning on the 9th")
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    calls_before = len(hermes.calls)
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_fix", "And make it the 10th instead", task_id="call_book")
    wait_for(lambda: streamed)
    assert isinstance(streamed[0], str)  # words only: the thread's group session never gets the screen
    assert len(hermes.calls) == calls_before
    assert "base64" not in json.dumps(streamed)


def test_shared_things_go_home_unless_a_channel_is_named(open_call, server, service, hermes):
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [WORK, RESEARCH], "mode": "topic"}})
    service.rt.route_call = lambda m: '{"follow_up_task_id": null, "parts": ["x"], "channel": "#work"}'
    call = open_call("req_topic")
    call.say("call_text", "Write up notes from the standup")
    wait_for(lambda: hermes.calls)
    assert call.run("call_text").deliver_to == "discord:111"  # words only: the topic decides as before
    call.drop(PICTURE)
    call.say("call_pic", "Write up notes from this whiteboard photo")
    wait_for(lambda: len(hermes.calls) == 2)
    assert call.run("call_pic").deliver_to is None and images_in(hermes.calls[1]["input"]) == [PICTURE]
    call.drop(PICTURE)
    call.say("call_named", "Post it in #work: a summary of this photo")
    wait_for(lambda: len(hermes.calls) == 3)
    assert call.run("call_named").deliver_to == "discord:111" and images_in(hermes.calls[2]["input"]) == [PICTURE]


def test_split_parts_share_one_capture_and_the_pictures(open_call, server, service, hermes):
    service.rt.route_call = lambda m: ('{"follow_up_task_id": null, "parts": ["Fix the chart colors", '
                                       '"Email the chart to Sam"], "channel": null}')
    call = open_call("req_split")
    call.sharing(True)
    call.mac_replies(window())
    call.drop(PICTURE)
    call.say("call_split", "Fix the chart colors and also email the chart to Sam")
    wait_for(lambda: len(hermes.calls) == 2)
    assert len(call.captures()) == 1
    assert [images_in(c["input"]) for c in hermes.calls] == [[WINDOW, PICTURE], [WINDOW, PICTURE]]
    assert sum(P.shared_note("Xcode", True, 1, 0) == n for n in call.notes()) == 1  # said once


def test_a_question_card_answer_never_waits_for_a_capture(open_call, server, service, hermes):
    call = open_call("req_card")
    call.say("call_garden", "Plan the garden beds for spring")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    call.sharing(True)
    call.mac_replies(window())
    started = time.monotonic()
    status, body = http(server.base_url, "POST", f"/voice/interactions/{call.iid}/answer",
                        {"task_id": done["task_id"], "text": "One long bed"}, server.token)
    assert status == 200 and body["sent"]
    wait_for(lambda: len(hermes.calls) == 2)
    assert time.monotonic() - started < A.CAPTURE_WAIT_S
    assert call.captures() == [] and isinstance(hermes.calls[1]["input"], str)


def test_a_clarifying_question_keeps_the_capture_for_its_answer(open_call, server, service, hermes, monkeypatch):
    asked = []

    def explicit(request):
        if asked:
            return None
        asked.append(request)
        return channels.Choice(None, "clarify", clarify="Should that go in #work or #research?")
    monkeypatch.setattr(service.rt, "explicit_channel", explicit)
    call = open_call("req_clarify")
    call.sharing(True)
    mac = call.mac_replies(window())
    call.say("call_ask", "Post this chart for the team")
    wait_for(lambda: "Should that go in #work or #research?" in call.spoken())
    wait_for(lambda: mac.answers)
    call.say("call_answer", "Just keep it with me")
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == [WINDOW] and len(call.captures()) == 1


def test_a_kept_capture_never_goes_with_an_unrelated_request(open_call, server, service, hermes, monkeypatch):
    asked = []

    def explicit(request):
        if asked:
            return None
        asked.append(request)
        return channels.Choice(None, "clarify", clarify="Should that go in #work or #research?")
    monkeypatch.setattr(service.rt, "explicit_channel", explicit)
    chart, mail = jpeg(b"xcode-chart "), jpeg(b"mail-window ")
    call = open_call("req_clarify_other")
    call.sharing(True)
    mac = call.mac_replies(lambda capture_id: {"data": chart if call.mac.seen.index(capture_id) == 0 else mail,
                                               "app": "Xcode" if call.mac.seen.index(capture_id) == 0 else "Mail"})
    call.say("call_ask", "Post this chart for the team")
    wait_for(lambda: "Should that go in #work or #research?" in call.spoken())
    wait_for(lambda: mac.answers)
    # Not an answer to the question: a request of its own, with what's on screen now.
    call.say("call_other", "Summarize the quarterly report for the board meeting tomorrow")
    wait_for(lambda: hermes.calls)
    kept, fresh = call.captures()
    assert images_in(hermes.calls[0]["input"]) == [mail] and kept != fresh
    assert call.interaction.attachments.wait_capture_blocking(kept, 0.1).reason == "cancelled"


# -- limits and what the voice hears -------------------------------------------------------------

def test_a_capture_over_the_image_budget_is_dropped_with_a_reason(open_call, server, service, hermes, monkeypatch):
    call = open_call("req_budget")
    pictures = [jpeg(f"photo-{i} ".encode(), 1_000_000) for i in range(3)]
    for data in pictures:
        call.drop(data)
    monkeypatch.setattr(A, "MAX_REQUEST_IMAGE_BYTES", 3_500_000)  # three pictures fit; a fourth image doesn't
    call.sharing(True)
    call.mac_replies(window(data=jpeg(b"big-window ", 1_000_000)))
    call.say("call_pick", "Pick the best of these photos")
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == pictures
    assert P.capture_missing_line("budget") in call.spoken()


def test_the_first_capture_the_request_didnt_ask_for_is_said_once(open_call, server, service, hermes):
    call = open_call("req_notice")
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_sam", "Remind me to call Sam")
    wait_for(lambda: hermes.calls)
    wait_for(lambda: P.screen_sent_line("Xcode") in call.spoken())
    call.say("call_plants", "Remind me to water the plants")
    wait_for(lambda: len(hermes.calls) == 2)
    assert images_in(hermes.calls[1]["input"]) == [WINDOW]
    assert call.spoken().count(P.screen_sent_line("Xcode")) == 1


def test_no_upload_in_time_runs_with_words_and_says_why(open_call, server, service, hermes, monkeypatch):
    monkeypatch.setattr(A, "CAPTURE_WAIT_S", 0.3)
    call = open_call("req_timeout")
    call.sharing(True)
    call.mac_replies(lambda capture_id: None)  # the Mac never answers
    call.say("call_late", "Summarize the quarterly report")
    wait_for(lambda: hermes.calls)
    assert isinstance(hermes.calls[0]["input"], str)
    wait_for(lambda: P.capture_missing_line("timeout") in call.spoken())


def test_a_failed_capture_runs_at_once_and_says_why(open_call, server, service, hermes):
    call = open_call("req_secure")
    call.sharing(True)
    call.mac_replies(lambda capture_id: {"fail": "secure_input"})
    started = time.monotonic()
    call.say("call_pw", "Log me in to the bank portal")
    wait_for(lambda: hermes.calls)
    assert time.monotonic() - started < A.CAPTURE_WAIT_S  # no waiting out a capture that won't come
    assert isinstance(hermes.calls[0]["input"], str)
    assert P.capture_missing_line("secure_input") in call.spoken()


def test_sharing_turned_off_before_attach_drops_the_capture(open_call, server, service, hermes):
    go = threading.Event()
    service.rt.route_call = lambda m: go.wait(2.5) and None
    call = open_call("req_off")
    call.sharing(True)
    mac = call.mac_replies(window())
    call.say("call_off", "Check the build and the logs")
    wait_for(lambda: mac.answers and list(mac.answers.values())[0][1] == 200)  # the capture arrived
    call.sharing(False)
    go.set()
    wait_for(lambda: hermes.calls)
    assert isinstance(hermes.calls[0]["input"], str)
    assert P.capture_missing_line("sharing_off") in call.spoken()


def test_a_riding_along_capture_never_goes_to_a_topical_channel(open_call, server, service, hermes):
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [WORK, RESEARCH], "mode": "topic"}})
    service.rt.route_call = lambda m: '{"follow_up_task_id": null, "parts": ["x"], "channel": "#work"}'
    call = open_call("req_topic_ride")
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_notes", "Write up notes from the standup")
    wait_for(lambda: hermes.calls)
    assert call.run("call_notes").deliver_to == "discord:111"  # the topic decides as before
    assert images_in(hermes.calls[0]["input"]) == []          # and the screen stays out of it


# -- follow-ups to tasks that post somewhere else ------------------------------------------------

def topical_work(service) -> None:
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [WORK, RESEARCH], "mode": "topic"}})
    service.rt.route_call = lambda m: '{"follow_up_task_id": null, "parts": ["x"], "channel": "#work"}'


def test_a_screen_follow_up_to_a_running_channel_task_goes_home(open_call, server, service, hermes):
    topical_work(service)
    hermes.hold = True
    call = open_call("req_topic_running")
    call.say("call_notes", "Write up notes from the standup")
    running = wait_for(lambda: (lambda r: r if r and r.run_id else None)(call.run("call_notes")))
    assert running.deliver_to == "discord:111"  # routing put it in #work; the user never named it
    hermes.hold = False
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_this", "What's this?", task_id="call_notes")
    wait_for(lambda: len(hermes.calls) == 2)
    # Never steered into the run that posts to #work: a new task that references it, answered home.
    assert hermes.steers == [] and images_in(hermes.calls[1]["input"]) == [WINDOW]
    assert "Write up notes from the standup" in text_of(hermes.calls[1]["input"])
    assert call.run("call_this").deliver_to is None and call.run("call_notes").deliver_to == "discord:111"
    hermes.runs[running.run_id]["done"].set()


def test_a_screen_follow_up_to_a_chat_that_cant_be_looked_up_goes_home(open_call, server, service, hermes,
                                                                        monkeypatch):
    origin = json.dumps({"chat_id": "555", "user_id": "42", "chat_type": "group"})
    conv = continuity._from_row("s_budget", "telegram", None, None, origin, "Budget", time.time())
    release = threading.Event()
    monkeypatch.setattr(continuity, "conversations_with_context", lambda db, request, **kw: [continuity.Candidate(conv, ())])
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **kw: False)

    def stream(base, key, c, message, callback, **kw):  # the group chat's turn keeps running
        callback("run.started", {"run_id": "run_chat1"})
        release.wait(10)
        callback("assistant.completed", {"content": "Picked up.\nSPOKEN: Picked up."})
        callback("run.completed", {})
        return True
    monkeypatch.setattr(continuity, "stream_session_chat", stream)
    monkeypatch.setattr(continuity, "conversation_by_session", lambda db, sid: None)  # state.db can't tell now
    steered: list = []
    monkeypatch.setattr(service.hermes, "steer", lambda run_id, text: steered.append((run_id, text)) or True)
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": []}})
    service.rt.route_call = lambda m: CONTINUE
    call = open_call("req_group_running")
    try:
        call.say("call_budget", "Keep going on the budget in that chat")
        wait_for(lambda: (lambda r: r if r and r.run_id == "run_chat1" else None)(call.run("call_budget")))
        call.sharing(True)
        call.mac_replies(window())
        call.say("call_this", "What's this?", task_id="call_budget")
        wait_for(lambda: hermes.calls)
        assert steered == [] and images_in(hermes.calls[0]["input"]) == [WINDOW]
        assert call.run("call_this").deliver_to is None
    finally:
        release.set()


def test_a_plain_follow_up_to_a_channel_task_gives_up_its_capture(open_call, server, service, hermes):
    topical_work(service)
    call = open_call("req_topic_follow")
    call.say("call_notes", "Write up notes from the standup")
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    call.sharing(True)
    call.mac_replies(window())
    call.say("call_more", "Also add the action items", task_id="call_notes")
    wait_for(lambda: len(hermes.calls) == 2)
    # Words only: it goes to #work as before, and the riding-along screen never goes with it.
    assert images_in(hermes.calls[1]["input"]) == [] and call.run("call_more").deliver_to == "discord:111"
    assert P.screen_sent_line("Xcode") not in call.spoken()
