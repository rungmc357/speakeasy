"""Look at this, spoken: a request that needs the screen while sharing is off waits for the button
(and runs with a capture once it's on, or ends "Not sent"), "stop looking at my screen" turns sharing
off whether or not the voice hands it off (both voice backends), and "share my screen" explains the
button. The Mac is played by FakeMac over real HTTP; Hermes by the fake API server."""
from __future__ import annotations

import asyncio
import base64
import queue
import threading
import time

import pytest

from fakes import SDP, FakeMac, FakeTransport, http, upload_attachment, wait_for
from speakeasy import calls, router
from speakeasy.prompt import builder as P

JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"


def jpeg(marker: bytes, size: int = 8_000) -> bytes:
    return JPEG_HEAD + (marker * (size // len(marker) + 1))[:size - len(JPEG_HEAD)]


WINDOW = jpeg(b"terminal-window ")
PICTURE = jpeg(b"dropped-picture ")


def images_in(run_input) -> list[bytes]:
    if isinstance(run_input, str):
        return []
    return [base64.b64decode(p["image_url"]["url"].split(",", 1)[1])
            for p in run_input[0]["content"] if p["type"] == "image_url"]


def text_of(run_input) -> str:
    return run_input if isinstance(run_input, str) else run_input[0]["content"][0]["text"]


def window(app: str = "Terminal", data: bytes = WINDOW):
    return lambda capture_id: {"data": data, "app": app}


class Call:
    """One call from a Mac that declared ``screen`` (or not)."""

    def __init__(self, server, service, key: str, screen: str | None = "ready"):
        body = {"sdp": SDP, **({"screen": screen} if screen else {})}
        status, session = http(server.base_url, "POST", "/voice/sessions", body, server.token, {"Idempotency-Key": key})
        assert status == 201, session
        self.server, self.service, self.iid = server, service, session["interaction_id"]
        self.interaction = service.interaction(self.iid)
        self.worker = self.interaction.worker
        self.seq = 0
        self.mac: FakeMac | None = None

    def sharing(self, on: bool, seq: int | None = None) -> tuple[int, dict]:
        """The panel's button, as the Mac sends it: the next sequence number after any it has seen."""
        self.seq = seq if seq is not None else max([self.seq] + [e["seq"] for e in self.events("screen.state")]) + 1
        return http(self.server.base_url, "POST", f"/voice/interactions/{self.iid}/screen",
                    {"on": on, "seq": self.seq}, self.server.token)

    def mac_replies(self, reply) -> FakeMac:
        self.mac = FakeMac(self.server.base_url, self.server.token, self.interaction, reply)
        return self.mac

    def hear(self, words: str) -> None:
        """Only the user's transcript: no handoff from the voice."""
        self.worker.feed({"type": "session.input_transcript.delta", "delta": words, "start_ms": 1, "end_ms": 2})

    def say(self, delegation_id: str, words: str, **extra) -> None:
        """One spoken request after the voice's last reply, handed off."""
        self.worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 1, "end_ms": 2})
        self.worker.delegate(delegation_id, words, **extra)

    def snap(self) -> dict:
        return http(self.server.base_url, "GET", f"/voice/interactions/{self.iid}", token=self.server.token)[1]

    def events(self, kind: str) -> list:
        return [p for _, k, p in list(self.interaction.feed.ring) if k == kind]

    def holds(self) -> list:
        """Every hold the snapshot showed, in order (consecutive repeats once)."""
        seen: list = []
        for snap in self.events("interaction"):
            if not seen or seen[-1] != snap["hold"]:
                seen.append(snap["hold"])
        return seen

    def captures(self) -> list[str]:
        return [p["capture_id"] for p in self.events("capture")]

    def spoken(self) -> list[str]:
        return [c for k, _, c in self.worker.sent if k == "session.commentary.append"]

    def notes(self) -> list[str]:
        return [c for k, _, c in self.worker.sent if k == "session.thinking.append"]

    def run(self, delegation_id: str):
        return self.interaction.runs.get(delegation_id)


@pytest.fixture
def open_call(server, service, monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)
    monkeypatch.setattr(calls, "HOLD_S", 5.0)            # every hold ends while the test still runs
    monkeypatch.setattr(calls, "HOLD_OUTCOME_S", 0.5)
    monkeypatch.setattr(calls, "STOP_HEARD_SETTLE_S", 0.2)
    service.hermes.image_support = {"images": True, "vision": "native"}
    service.rt.quick_call = lambda request, today: None  # no web lookups: questions go on to Hermes
    made: list[Call] = []

    def make(key: str, screen: str | None = "ready") -> Call:
        made.append(Call(server, service, key, screen))
        return made[-1]
    yield make
    for call in made:
        if call.mac is not None:
            call.mac.stop()
        call.worker.end_hold("ended")


def never_held(call: Call) -> bool:
    return all(h is None for h in call.holds()) and call.worker.held is None and call.snap()["hold"] is None


def waiting(call: Call) -> dict:
    return wait_for(lambda: (lambda h: h if h and h["state"] == "waiting" else None)(call.snap()["hold"]))


# -- the matchers ----------------------------------------------------------------------------------

@pytest.mark.parametrize("words", [
    "stop looking at my screen", "Okay, stop sharing my screen please", "don't look at my screen",
    "Don’t look at my screen", "turn off screen sharing", "turn screen sharing off", "stop screen sharing",
    "no more looking at my screen", "can you stop looking at my screen", "you can stop looking at my screen now",
    "turn off the eye", "stop watching my screen", "unshare my screen"])
def test_stop_phrases_match(words):
    assert router.stops_looking(words) and router.screen_intent(words) == "stop"


@pytest.mark.parametrize("words", [
    "how do I stop sharing my screen", "stop sharing my screen in Zoom", "stop sharing my screen on the call",
    "remind me to turn off screen sharing", "don't stop looking at my screen", "tell the kids to stop looking at the screen",
    "what happens if I stop sharing my screen", "stop looking for flights", "stop sharing", "stop", "look at my screen"])
def test_stop_phrases_never_match(words):
    assert not router.stops_looking(words)


@pytest.mark.parametrize("words", [
    "share my screen", "Can you share my screen?", "I want to share my screen", "turn on screen sharing",
    "turn screen sharing on", "enable screen sharing", "look at my screen while we talk", "turn on the eye",
    "I'd like you to watch my screen while we're talking", "ok, share my screen with you"])
def test_share_requests_match(words):
    assert router.screen_intent(words) == "share"


@pytest.mark.parametrize("words", [
    "share my screen with Sam in Zoom", "look at my screen", "how do I share my screen", "share this page with Sam",
    "share my screen and tell me what's wrong", "what's on my screen"])
def test_share_requests_never_match(words):
    assert router.screen_intent(words) != "share"


@pytest.mark.parametrize("words", [
    "look at my screen", "What's this error?", "what's on my screen", "is this layout right?", "what does this say",
    "can you see my screen", "help me with what I'm looking at", "how do I fix this error"])
def test_requests_that_need_the_screen(words):
    assert router.needs_screen(words)


# The U3 must-not-match list, plus talk about a screen itself and the spoken intents: none waits.
@pytest.mark.parametrize("words", [
    "look at this weekend's forecast", "show me what you're looking at", "what are you looking at",
    "what's on your screen", "how much screen time did I have this week", "fix the screen door",
    "add sunscreen to the list", "let me look at this", "let me take a look at this", "what's that",
    "draft this email to my landlord", "remind me to call Sam", "make this the header image", "summarize this",
    "look at this year's budget", "what time is it in Nairobi", "show me", "can I see this",
    "my screen keeps flickering", "my screen went black", "how do I share my screen in Zoom",
    "how do I take a screenshot of my screen", "stop looking at my screen", "share my screen",
    # commands for the device itself
    "lock my screen", "turn off my screen", "dim my display", "set my screen brightness to 50 percent",
    "turn my screen off", "wake up my display", "change my display resolution"])
def test_requests_that_never_wait_for_the_screen(words):
    assert not router.needs_screen(words)


def test_acknowledgements():
    assert router.acknowledges("ok, one sec") and router.acknowledges("There, it's on.")
    assert not router.acknowledges("ok, draft the release notes") and not router.acknowledges("what's this?")


# What the user says after "Turn on the eye in my panel": about doing that, so a waiting request keeps waiting.
@pytest.mark.parametrize("words", [
    "I'm turning on the eye", "turning it on now", "how do I turn it on", "where's the eye", "where’s the eye?",
    "it's on", "take a look now", "ok go", "okay I turned on screen sharing", "it's on now, take a look",
    "ok turning on the eye", "I can't find the eye", "go ahead", "can you see it now?", "the eye is on now"])
def test_talk_about_turning_sharing_on_keeps_a_hold(words):
    assert router.acknowledges(words) and router.about_sharing_on(words)


@pytest.mark.parametrize("words", [
    "turn on the lights", "take a look at the weather in Boston", "draft the release notes", "what's this?",
    "how do I share my screen in Zoom", "look at my screen", "turn on the kitchen lights and the eye",
    "find the receipt from Amazon"])
def test_requests_are_never_talk_about_sharing(words):
    assert not router.about_sharing_on(words) and not router.acknowledges(words)


# -- the hold --------------------------------------------------------------------------------------

def test_look_at_my_screen_waits_for_sharing_then_runs_with_it(open_call, hermes):
    call = open_call("hold_happy")
    call.mac_replies(window())
    call.say("call_look", "Look at my screen")
    hold = waiting(call)
    assert hold["text"] == "Waiting for your screen" and abs(hold["since"] - time.time()) < 10
    assert call.events("screen.hint") == [{"reason": "screen_off"}]
    wait_for(lambda: P.screen_off_hint() in call.spoken())
    assert "Control-Option-S" in P.screen_off_hint()
    assert call.captures() == [] and hermes.calls == []
    assert call.worker.handoff_at == {} and call.run("call_look") is None  # no stale waiting line, no task yet
    assert call.sharing(True)[0] == 200
    wait_for(lambda: hermes.calls)
    assert len(call.captures()) == 1 and images_in(hermes.calls[0]["input"]) == [WINDOW]
    assert "Look at my screen" in text_of(hermes.calls[0]["input"])
    assert call.snap()["hold"] is None and P.HOLD_RELEASED_NOTE in call.notes()
    assert call.run("call_look").run_id == hermes.calls[0]["run_id"]


def test_the_hold_runs_with_pictures_dropped_while_it_waited(open_call, server, hermes):
    call = open_call("hold_picture")
    call.mac_replies(window())
    call.say("call_look", "What's this error?")
    waiting(call)
    status, _ = upload_attachment(server.base_url, server.token, call.iid, PICTURE)
    assert status == 200
    call.sharing(True)
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == [WINDOW, PICTURE]


def test_an_unanswered_hold_ends_unsent(open_call, hermes, monkeypatch):
    monkeypatch.setattr(calls, "HOLD_S", 0.4)
    call = open_call("hold_expire")
    call.say("call_look", "What's this error?")
    waiting(call)
    wait_for(lambda: call.snap()["hold"] is None and len(call.holds()) >= 3)
    waiting_line, ended, cleared = call.holds()[-3:]
    assert waiting_line["state"] == "waiting" and cleared is None
    assert ended["state"] == "not_sent" and ended["text"] == "Not sent: screen sharing was off"
    wait_for(lambda: P.HOLD_DROPPED_NOTE in call.notes())
    call.sharing(True)  # too late: nothing runs, nothing is captured
    time.sleep(0.4)
    assert hermes.calls == [] and call.captures() == [] and call.run("call_look") is None


def test_a_newer_request_ends_the_hold_and_routes_as_usual(open_call, hermes):
    call = open_call("hold_newer")
    call.say("call_look", "Look at my screen")
    waiting(call)
    call.say("call_ack", "Okay, one sec")  # turning it on: still waiting, and no task for it
    call.say("call_again", "Look at my screen")  # the same words handed off again
    time.sleep(0.4)
    assert call.snap()["hold"]["state"] == "waiting" and call.worker.held.delegation_id == "call_look"
    assert call.spoken().count(P.screen_off_hint()) == 1 and hermes.calls == []
    call.say("call_notes", "Draft the release notes")
    wait_for(lambda: hermes.calls)
    assert [h["state"] for h in call.holds() if h] == ["waiting", "not_sent"]
    assert "Draft the release notes" in text_of(hermes.calls[0]["input"]) and images_in(hermes.calls[0]["input"]) == []
    assert P.HOLD_DROPPED_NOTE in call.notes()
    call.sharing(True)  # the old request is gone: nothing else runs
    time.sleep(0.4)
    assert len(hermes.calls) == 1 and call.captures() == []


def test_a_second_screen_request_takes_over_the_hold(open_call, hermes):
    call = open_call("hold_second")
    call.mac_replies(window())
    call.say("call_first", "Look at my screen")
    first = waiting(call)
    call.say("call_second", "What does this say")
    wait_for(lambda: call.worker.held and call.worker.held.delegation_id == "call_second")
    assert "not_sent" in [h["state"] for h in call.holds() if h] and call.snap()["hold"]["since"] >= first["since"]
    call.sharing(True)
    wait_for(lambda: hermes.calls)
    time.sleep(0.3)
    assert len(hermes.calls) == 1 and "What does this say" in text_of(hermes.calls[0]["input"])
    assert call.run("call_second") is not None and call.run("call_first") is None


def test_pausing_ends_the_hold(open_call, server, hermes):
    call = open_call("hold_pause")
    call.say("call_look", "look at this")
    waiting(call)
    assert http(server.base_url, "POST", f"/voice/interactions/{call.iid}/pause", {}, server.token)[0] == 200
    assert call.interaction.snapshot()["hold"]["state"] == "not_sent"
    wait_for(lambda: call.interaction.snapshot()["hold"] is None)
    assert call.sharing(True)[0] == 409  # a paused call takes no toggle; nothing runs
    time.sleep(0.3)
    assert hermes.calls == []


def test_the_call_ending_ends_the_hold(open_call, hermes):
    call = open_call("hold_end")
    call.say("call_look", "look at this")
    waiting(call)
    call.worker.feed({"type": "session.closed"})
    assert call.interaction.snapshot()["hold"]["state"] == "not_sent"
    assert call.worker.held is None and hermes.calls == []


def test_without_screen_recording_the_voice_points_to_settings(open_call, hermes):
    call = open_call("hold_noperm", screen="no_permission")
    call.say("call_look", "Look at my screen")
    wait_for(lambda: P.SCREEN_PERMISSION_HINT in call.spoken())
    assert call.events("screen.hint") == [{"reason": "no_permission"}]
    time.sleep(0.3)
    assert call.snap()["hold"] is None and never_held(call) and hermes.calls == []
    assert call.worker.held is None and call.run("call_look") is None


def test_an_undeclared_call_is_unchanged(open_call, hermes):
    call = open_call("hold_iphone", screen=None)
    call.say("call_look", "Look at my screen")
    wait_for(lambda: hermes.calls)
    assert isinstance(hermes.calls[0]["input"], str) and call.events("screen.hint") == []
    assert never_held(call) and call.captures() == []
    wait_for(lambda: call.run("call_look").status == "completed")
    call.say("call_stop", "Stop looking at my screen")  # an iPhone call: words for Hermes, as before
    wait_for(lambda: len(hermes.calls) == 2)
    assert call.events("screen.state") == [] and not {P.SCREEN_STOPPED, P.SCREEN_ALREADY_OFF} & set(call.spoken())


def test_a_dropped_picture_goes_instead_of_waiting(open_call, server, hermes):
    call = open_call("hold_dropped")
    assert upload_attachment(server.base_url, server.token, call.iid, PICTURE)[0] == 200
    call.say("call_what", "What's this?")  # "this" is the picture they dropped
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == [PICTURE] and never_held(call)


@pytest.mark.parametrize("number,words", list(enumerate([
    "look at this year's budget", "what's that", "summarize this", "remind me to call Sam",
    "my screen keeps flickering", "how do I share my screen in Zoom", "lock my screen"])))
def test_requests_that_dont_need_the_screen_never_wait(open_call, hermes, number, words):
    call = open_call(f"hold_never_{number}")
    call.say("call_words", words)
    wait_for(lambda: hermes.calls)
    assert never_held(call) and call.events("screen.hint") == [] and call.worker.held is None


# -- stop looking ----------------------------------------------------------------------------------

def test_stop_looking_heard_in_the_transcript_turns_sharing_off(open_call, server, hermes):
    call = open_call("stop_openai")
    assert call.sharing(True)[0] == 200  # seq 1
    for piece in ["Stop", " looking", " at", " my", " scr", "een", "."]:  # OpenAI's pieces, no handoff
        call.hear(piece)
    state = wait_for(lambda: call.events("screen.state"))
    assert state == [{"on": False, "seq": 2}]
    assert call.snap()["screen"] == {"on": False, "seq": 2, "declared": "ready"}
    wait_for(lambda: P.SCREEN_STOPPED in call.spoken())
    assert P.SCREEN_OFF_NOTE in call.notes()
    # The same words handed off too: acted on once, no task.
    call.worker.feed({"type": "session.delegation.created", "offset_ms": 5,
                      "delegation": {"id": "call_stop", "target": "client"}})
    time.sleep(0.4)
    assert call.spoken().count(P.SCREEN_STOPPED) == 1 and P.SCREEN_ALREADY_OFF not in call.spoken()
    assert hermes.calls == [] and call.run("call_stop") is None
    # The app adopts seq 2: its toggle with seq 2 is stale, its next one (3) goes through.
    assert call.sharing(True, seq=2)[1] == {"on": False, "seq": 2}
    assert call.sharing(True)[1] == {"on": True, "seq": 3}


def test_stop_looking_closes_a_capture_on_its_way(open_call, hermes):
    call = open_call("stop_capture")
    call.sharing(True)
    call.mac_replies(lambda capture_id: None)  # the Mac never answers
    call.say("call_err", "What's this error?")
    [capture_id] = wait_for(call.captures)
    call.hear("don't look at my screen")
    wait_for(lambda: P.SCREEN_STOPPED in call.spoken())
    assert call.interaction.attachments.open_captures() == []
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == []


def test_stop_looking_heard_on_codex_turns_sharing_off(server, service, hermes, monkeypatch):
    class Transport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.notifications: queue.Queue = queue.Queue()

    monkeypatch.setattr(calls, "STOP_HEARD_SETTLE_S", 0.2)
    service._codex_factory = Transport
    service.settings.patch({"voice": {"provider": "codex"}})
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, "screen": "ready"},
                           server.token, {"Idempotency-Key": "stop_codex"})
    assert status == 201, session
    iid = session["interaction_id"]
    interaction = service.interaction(iid)
    transport = interaction.worker.transport
    try:
        wait_for(lambda: interaction.worker.loop is not None)
        assert http(server.base_url, "POST", f"/voice/interactions/{iid}/screen", {"on": True, "seq": 1},
                    server.token)[0] == 200
        transport.notifications.put({"method": "thread/realtime/transcript/done",
                                     "params": {"threadId": transport.thread_id, "role": "user",
                                                "text": "Please stop sharing my screen now."}})
        state = wait_for(lambda: [p for _, k, p in list(interaction.feed.ring) if k == "screen.state"])
        assert state == [{"on": False, "seq": 2}] and not interaction.attachments.screen_on
        wait_for(lambda: ("thread/realtime/appendSpeech", P.SCREEN_STOPPED) in
                 [(m, p.get("text")) for m, p in transport.requests])
        # Codex hands the same words off: once is enough, and nothing reaches Hermes.
        transport.notifications.put({"method": "thread/realtime/itemAdded", "params": {
            "threadId": transport.thread_id, "item": {
                "type": "handoff_request", "handoff_id": "h_stop", "input_transcript": "Please stop sharing my screen now.",
                "active_transcript": [{"role": "user", "text": "Please stop sharing my screen now."}]}}})
        time.sleep(0.5)
        speech = [p.get("text") for m, p in transport.requests if m == "thread/realtime/appendSpeech"]
        assert speech.count(P.SCREEN_STOPPED) == 1 and P.SCREEN_ALREADY_OFF not in speech and hermes.calls == []
    finally:
        transport.stop()


def test_stop_looking_handed_off_before_the_transcript_check(open_call, hermes, monkeypatch):
    monkeypatch.setattr(calls, "STOP_HEARD_SETTLE_S", 1.0)  # the handoff gets there first
    call = open_call("stop_handoff")
    call.sharing(True)
    call.say("call_stop", "Stop looking at my screen")
    wait_for(lambda: P.SCREEN_STOPPED in call.spoken())
    assert call.events("screen.state") == [{"on": False, "seq": 2}]
    time.sleep(1.3)  # the transcript check wakes up: nothing more to do
    assert call.spoken().count(P.SCREEN_STOPPED) == 1 and len(call.events("screen.state")) == 1
    assert hermes.calls == [] and call.run("call_stop") is None


def test_stop_looking_while_sharing_was_off_is_answered_briefly(open_call, hermes):
    call = open_call("stop_off")
    call.say("call_stop", "Turn off screen sharing")
    wait_for(lambda: P.SCREEN_ALREADY_OFF in call.spoken())
    assert call.events("screen.state") == [] and hermes.calls == [] and call.run("call_stop") is None


def test_stop_looking_calls_off_a_waiting_request(open_call, hermes):
    call = open_call("stop_hold")
    call.say("call_look", "Look at my screen")
    waiting(call)
    call.hear("Actually, don't look at my screen")
    wait_for(lambda: P.SCREEN_HOLD_CANCELLED in call.spoken())
    assert call.snap()["hold"]["state"] == "not_sent" and call.worker.held is None
    call.sharing(True)
    time.sleep(0.3)
    assert hermes.calls == [] and call.captures() == []


def test_words_after_the_phrase_can_rule_it_out(open_call, hermes):
    call = open_call("stop_zoom")
    call.sharing(True)
    call.hear("Stop sharing my screen")
    call.hear(" in Zoom.")  # within STOP_HEARD_SETTLE_S: about Zoom, not Speakeasy
    time.sleep(0.5)
    assert call.interaction.attachments.screen_on and call.events("screen.state") == []


def test_spent_stop_words_never_turn_sharing_off_again(open_call, hermes):
    call = open_call("stop_spent")
    call.sharing(True)
    call.hear("stop looking at my screen")
    wait_for(lambda: call.events("screen.state"))
    call.sharing(True)  # they turn it back on right away
    call.hear(" thanks")
    time.sleep(0.4)
    assert call.interaction.attachments.screen_on and len(call.events("screen.state")) == 1


# -- share my screen -------------------------------------------------------------------------------

def test_share_my_screen_explains_the_button(open_call, hermes):
    call = open_call("share_off")
    call.say("call_share", "Share my screen")
    wait_for(lambda: P.share_hint() in call.spoken())
    assert call.events("screen.hint") == [{"reason": "share_request"}]
    time.sleep(0.3)
    assert hermes.calls == [] and call.run("call_share") is None
    assert call.snap()["screen"]["on"] is False and never_held(call)


def test_share_my_screen_handed_off_twice_hints_once(open_call, hermes):
    """The voice sometimes hands the same words off twice: the button is explained once."""
    call = open_call("share_twice")
    call.say("call_share_a", "Please share my screen now")
    wait_for(lambda: P.share_hint() in call.spoken())
    call.say("call_share_b", "Please share my screen now")
    time.sleep(0.6)
    assert call.events("screen.hint") == [{"reason": "share_request"}]
    assert call.spoken().count(P.share_hint()) == 1 and hermes.calls == []


def test_share_my_screen_while_sharing_is_on(open_call, hermes):
    call = open_call("share_on")
    call.sharing(True)
    call.say("call_share", "turn on screen sharing")
    wait_for(lambda: P.SCREEN_ALREADY_SHARED in call.spoken())
    time.sleep(0.3)
    assert hermes.calls == [] and call.captures() == [] and call.events("screen.hint") == []


def test_share_my_screen_without_screen_recording(open_call, hermes):
    call = open_call("share_noperm", screen="no_permission")
    call.say("call_share", "Can you share my screen?")
    wait_for(lambda: P.SCREEN_PERMISSION_HINT in call.spoken())
    assert call.events("screen.hint") == [{"reason": "no_permission"}] and hermes.calls == []


def test_share_my_screen_keeps_a_waiting_request_waiting(open_call, hermes):
    call = open_call("share_hold")
    call.mac_replies(window())
    call.say("call_look", "What's on my screen")
    waiting(call)
    call.say("call_share", "share my screen")  # how do I turn it on? The request keeps waiting
    wait_for(lambda: P.share_hint() in call.spoken())
    assert call.snap()["hold"]["state"] == "waiting" and call.worker.held.delegation_id == "call_look"
    assert call.events("screen.hint") == [{"reason": "screen_off"}, {"reason": "share_request"}]
    call.sharing(True)
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == [WINDOW] and "What's on my screen" in text_of(hermes.calls[0]["input"])


@pytest.mark.parametrize("words,rest", [
    ("stop looking at my screen and set a timer for ten minutes", "set a timer for ten minutes"),
    ("Stop looking at my screen, then check the weather in Boston", "check the weather in Boston"),
    ("Can you set a timer for ten minutes and stop looking at my screen", "Can you set a timer for ten minutes"),
    ("stop looking at my screen please", ""),
    ("okay stop sharing my screen now thanks", ""),
    ("how do I stop sharing my screen", ""),
    # leftovers that belong to the stop itself never become a request
    ("you can stop looking at my screen", ""),
    ("you can stop looking at my screen now", ""),
    ("I'm done, stop looking at my screen", ""),
    ("that's enough, stop looking at my screen", ""),
    ("please stop looking at my screen for a bit", ""),
    ("okay, stop looking at my screen for now", ""),
    ("stop looking at my screen, that's enough for now", ""),
    # a whole clause of its own, or one joined after a pause
    ("Stop looking at my screen. What's the weather in Boston?", "What's the weather in Boston?"),
    ("stop looking at my screen for now and set a timer for ten minutes", "set a timer for ten minutes"),
])
def test_what_else_was_asked_with_a_stop(words, rest):
    assert router.after_stop(words) == rest


def test_a_stop_with_another_request_still_runs_the_request(open_call, hermes, monkeypatch):
    monkeypatch.setattr(calls, "STOP_HEARD_SETTLE_S", 1.0)  # the handoff gets there first
    call = open_call("stop_and_more")
    call.sharing(True)
    call.say("call_stop_more", "Stop looking at my screen and draft a note to Sam about Friday")
    wait_for(lambda: hermes.calls)
    assert call.events("screen.state") == [{"on": False, "seq": 2}] and P.SCREEN_STOPPED in call.spoken()
    sent = hermes.calls[0]["input"]
    assert isinstance(sent, str) and "draft a note to Sam about Friday" in sent  # words only: sharing is off
    assert "stop looking" not in sent.lower().split("draft a note")[1]


# -- around the hold: talk about the button, follow-ups, the release -------------------------------

def test_talk_about_the_button_keeps_the_request_waiting(open_call, hermes):
    call = open_call("hold_talk")
    call.mac_replies(window())
    call.say("call_look", "What's this error?")
    waiting(call)
    call.say("call_doing", "I'm turning on the eye now")
    call.say("call_where", "Where's the eye?")
    time.sleep(0.4)
    assert call.snap()["hold"]["state"] == "waiting" and call.worker.held.delegation_id == "call_look"
    assert hermes.calls == [] and call.run("call_doing").status == call.run("call_where").status == "rejected"
    assert P.HOLD_DROPPED_NOTE not in call.notes()
    call.sharing(True)
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == [WINDOW] and "What's this error?" in text_of(hermes.calls[0]["input"])
    # Asking to look again just as it went: that request already has the screen.
    call.say("call_now", "Now look at my screen")
    call.say("call_again", "What's this error?")
    call.say("call_go", "ok, take a look")
    time.sleep(0.5)
    assert len(hermes.calls) == 1 and len(call.captures()) == 1
    assert {call.run(d).error for d in ("call_now", "call_again", "call_go")} == {"Already on its way with your screen"}


def test_a_new_request_right_after_the_release_still_runs(open_call, hermes):
    call = open_call("hold_then_more")
    call.mac_replies(window())
    call.say("call_look", "Look at my screen")
    waiting(call)
    call.sharing(True)
    wait_for(lambda: hermes.calls)
    call.say("call_more", "Look at my screen and tell me which tab has the invoice")  # it asks more than a look
    wait_for(lambda: len(hermes.calls) == 2)
    assert "which tab has the invoice" in text_of(hermes.calls[1]["input"])


def test_a_follow_up_about_a_tasks_result_never_waits(open_call, hermes):
    hermes.hold = True
    call = open_call("hold_marked")
    call.say("call_build", "Run the release build")
    running = wait_for(lambda: (lambda r: r if r and r.run_id else None)(call.run("call_build")))
    # The build reported an error; the voice ties the question to that task. Sharing is off.
    call.say("call_err", "What does this error mean", task_id="call_build")
    wait_for(lambda: hermes.steers)
    assert hermes.steers[-1][1] == P.steer_text(call.service.rt.names, "What does this error mean")
    assert never_held(call) and call.events("screen.hint") == [] and len(hermes.calls) == 1
    hermes.runs[running.run_id]["done"].set()


def test_sharing_on_while_the_hint_is_on_its_way_keeps_the_capture(open_call, service, hermes, monkeypatch):
    call = open_call("hold_race")
    call.mac_replies(window())
    worker = call.worker
    real_append, real_wait = worker.append, worker.wait_for_screen
    hint_out, routing, held_done = threading.Event(), threading.Event(), threading.Event()

    async def append(kind, delegation_id, content):
        if content == P.screen_off_hint():
            while not hint_out.is_set():  # the hint's round trip (Codex: a JSON-RPC request) takes a while
                await asyncio.sleep(0.02)
        await real_append(kind, delegation_id, content)

    async def wait_for_screen(*args):
        held = await real_wait(*args)
        if held:
            held_done.set()  # the held handoff's own dispatch finishes right after this
        return held
    monkeypatch.setattr(worker, "append", append)
    monkeypatch.setattr(worker, "wait_for_screen", wait_for_screen)
    service.rt.route_call = lambda m: routing.wait(2.5) and None  # the release waits in routing meanwhile
    call.say("call_look", "Look at my screen")
    waiting(call)
    call.sharing(True)  # while the hint is still on its way
    wait_for(call.captures)  # the release bound the handoff and asked for a capture
    hint_out.set()
    assert held_done.wait(5)
    time.sleep(0.3)  # the held handoff's dispatch has finished: it must not have given the capture back
    routing.set()
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == [WINDOW] and len(call.captures()) == 1


def test_a_stop_with_only_leftover_words_starts_nothing(open_call, hermes, monkeypatch):
    monkeypatch.setattr(calls, "STOP_HEARD_SETTLE_S", 1.0)  # the handoff gets there first
    call = open_call("stop_leftover")
    call.sharing(True)
    call.say("call_stop", "You can stop looking at my screen now")
    wait_for(lambda: P.SCREEN_STOPPED in call.spoken())
    time.sleep(0.5)
    assert hermes.calls == [] and call.events("screen.state") == [{"on": False, "seq": 2}]


@pytest.mark.parametrize("words", ["Put it on my screen", "put the chart up on my screen", "show that on my screen"])
def test_asking_for_the_tasks_picture_on_my_screen_never_waits(words):
    assert router.about_task_view(words) and not router.needs_screen(words)
