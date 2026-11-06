"""Release 1: what the voice says happened, happened. Replays of real failure shapes, with invented
wording: an answer to a task's question reaching a task running in a thread, a stop reaching it,
"show me … and do X" doing both, a new subject after "and also" becoming its own task, one question
staying one task, a thread task's progress reaching the card, and the tour decided by the server."""
from __future__ import annotations

import json
import sqlite3
import time

from fakes import SDP, http, wait_for
from speakeasy import channels, router, threads
from speakeasy.calls import whole_request
from speakeasy.prompt import builder as P
from speakeasy.text import thread_commentary

WORK = {"target": "discord:111", "label": "#errands", "topic": "appointments and bookings", "new_thread": True}


def start_call(server, service, key="req_ft_1"):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def tasks(server):
    return http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]


def notes(worker, kind):
    return [c for k, _, c in worker.sent if k == kind]


class SlowThreads:
    """A thread task that keeps working until released, reporting progress on the way."""

    def __init__(self, accept=True):
        self.accept, self.posted, self.release, self.opened = accept, [], False, []
        self.activity = [("commentary", "The dentist's calendar is open. Looking at Thursday now."),
                         ("tool", "browser step done")]

    def available(self, target):
        return True

    def open(self, target, *, message, title, delivery_id):
        self.opened.append(message)
        return threads.Opened("speakeasy-x", "777", "discord")

    def wait(self, opened, on_session, on_title=None, on_activity=None):
        on_session("thread_sess_1")
        for kind, text in self.activity:
            if on_activity:
                on_activity(kind, text)
        while not self.release:
            time.sleep(0.05)
        return "Booked Thursday at 4:15."

    def post(self, platform, thread_id, *, message, delivery_id):
        self.posted.append((platform, thread_id, message))
        return self.accept


RUNNERS: list = []


import pytest


@pytest.fixture(autouse=True)
def _release_runners():
    yield
    for runner in RUNNERS:
        runner.release = True
    RUNNERS.clear()


def run_thread_task(server, service, runner):
    RUNNERS.append(runner)
    service.rt.threads = runner
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [WORK]}})
    service.rt.topical_channel = lambda label: channels.Choice(
        channels.Channel(**{k: WORK[k] for k in ("target", "label", "topic")}, new_thread=True), "topic")
    _, worker = start_call(server, service)
    worker.delegate("call_book", "Get me a dentist cleaning on the 9th")
    wait_for(lambda: runner.opened)
    wait_for(lambda: service.store.continued_for(next(iter(worker.interaction.runs.values())).idem_key))
    return worker


def test_an_answer_to_a_thread_tasks_question_reaches_its_thread(server, service, hermes):
    runner = SlowThreads()
    worker = run_thread_task(server, service, runner)
    task_id = next(iter(worker.interaction.runs))
    worker.delegate("call_ans", "Go with the 4:15", task_id=task_id)
    wait_for(lambda: runner.posted)
    platform, thread_id, message = runner.posted[0]
    assert (platform, thread_id) == ("discord", "777") and "Go with the 4:15" in message
    # The voice's "Delivered" note follows the post.
    wait_for(lambda: any(n.startswith("Delivered") for n in notes(worker, "session.thinking.append")))
    assert hermes.calls == []  # never a fresh task asking "what does 4:15 refer to?"
    runner.release = True


def test_a_follow_up_that_cannot_be_delivered_is_never_claimed(server, service, hermes):
    runner = SlowThreads(accept=False)
    worker = run_thread_task(server, service, runner)
    task_id = next(iter(worker.interaction.runs))
    worker.delegate("call_hold", "Hold off on that for a minute", task_id=task_id)
    wait_for(lambda: notes(worker, "session.commentary.append"))
    said = " ".join(notes(worker, "session.commentary.append"))
    assert "couldn't get that through" in said
    assert not any(n.startswith("Delivered") for n in notes(worker, "session.thinking.append"))
    runner.release = True


def test_show_me_on_a_thread_task_without_a_picture_goes_into_the_thread(server, service, hermes):
    runner = SlowThreads()
    worker = run_thread_task(server, service, runner)
    task_id = next(iter(worker.interaction.runs))
    worker.delegate("call_show", ". Show me the openings when you have them", task_id=task_id)
    wait_for(lambda: runner.posted)
    assert "openings" in runner.posted[0][2] and "SEE" in runner.posted[0][2]
    assert P.SHOW_ME_NOTHING not in notes(worker, "session.commentary.append")
    runner.release = True


def test_a_thread_tasks_progress_reaches_its_card(server, service, hermes):
    runner = SlowThreads()
    run_thread_task(server, service, runner)

    def live():
        rows = [t for t in tasks(server) if t["status"] in {"running", "working"}]
        return rows and "Thursday" in (rows[0].get("detail") or "")
    wait_for(live)
    row = [t for t in tasks(server) if t["status"] in {"running", "working"}][0]
    assert row["short_status"] != "Status unconfirmed" and not row.get("stale")
    runner.release = True


def test_only_looking_vs_looking_and_instructing():
    assert router.only_looking("Show me")
    assert router.only_looking("A picture of that page")
    assert not router.only_looking("A picture of that page. OK, the 4:15 then")
    assert not router.only_looking("Show me the openings and book the earliest")
    assert router.wants_to_see("Show me the openings when you have them")


def test_and_also_with_a_new_subject_is_a_new_task():
    running = [router.OpenTask("t1", "Is the reader app deploy finished", "running", "", 3.0)]
    for request in ("And also are we downloading the 70B model right now", "also did the SQL backup run"):
        assert router.continues_newest(request, running) is None, request
    for tail in ("and is it live yet", "or did it fail"):
        assert router.continues_newest(tail, running) is not None, tail


def test_a_repeat_said_while_waiting_never_runs_twice():
    running = [router.OpenTask("t1", "turn the hallway lights back on", "running", "", 20.0)]
    part = router.quick_intent("turn the hallway lights back on", running)
    assert part is not None and part.kind == "follow_up" and part.task_id == "t1"


def test_the_routing_prompt_decides_by_topic_and_keeps_one_question_whole():
    system = router.route_messages("x", [], [])[0]["content"]
    assert "Only the subject decides against it" in system and "different subject" in system and "is ONE part" in system


def test_a_split_sentence_is_handed_on_whole():
    context = "Assistant: Sure.\nUser: Hallway lamps at thirty percent\nUser: green"
    assert whole_request(context) == "Hallway lamps at thirty percent green"
    assert whole_request("User: Check the forecast for Friday in Chicago please") == \
        "Check the forecast for Friday in Chicago please"


def test_thread_commentary_is_one_clean_line():
    text = "Voice: book a cleaning\n\n**The dentist** has Thursday open. Pulling times now.\n```\nls\n```"
    assert thread_commentary(text) == "The dentist has Thursday open. Pulling times now."


def test_the_tour_is_only_for_a_first_ever_call(server, service, hermes):
    status, _ = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, "tour": {}}, server.token,
                     {"Idempotency-Key": "req_tour_1"})
    assert status == 201
    first = service.workers[-1].instructions if hasattr(service.workers[-1], "instructions") else None
    service.store.set_meta("last_call_end", repr(time.time()))
    seen = {}
    real = service.instructions

    def spy(**kw):
        seen.update(kw)
        return real(**kw)
    service.instructions = spy
    status, _ = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, "tour": {}}, server.token,
                     {"Idempotency-Key": "req_tour_2"})
    assert status == 201 and seen.get("tour") is None


def test_thread_routes_key_like_a_typed_message(tmp_path):
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT, source TEXT, chat_type TEXT, thread_id TEXT, origin_json TEXT, "
               "title TEXT, started_at REAL, ended_at REAL)")
    db.execute("INSERT INTO sessions VALUES ('s','discord','thread','888',?, 't', ?, NULL)",
               (json.dumps({"user_id": "42", "user_name": "sam", "chat_id": "888", "parent_chat_id": "111"}), time.time()))
    db.commit()
    db.close()
    (tmp_path / "webhook_subscriptions.json").write_text(json.dumps({"mine": {"secret": "keep"}}))
    name = threads.ensure_thread_route(tmp_path, "discord", "888")
    routes = json.loads((tmp_path / "webhook_subscriptions.json").read_text())
    assert routes["mine"] == {"secret": "keep"}
    route = routes[name]
    assert route["source_chat_id"] == "888" and route["source_chat_type"] == "thread"
    assert route["source_thread_id"] == "888" and route["source_user_id"] == "42"
    assert "source_new_thread" not in route
    # a channel-route resync keeps the thread routes
    threads.sync_routes(tmp_path, [], supported=True)
    assert name in json.loads((tmp_path / "webhook_subscriptions.json").read_text())


def test_home_follow_ups_and_partial_names_stay_on_the_instant_path():
    from speakeasy.home_control import Device, HomeControl
    devices = [Device("light.a", "Den Ceiling Lights", "on", {}), Device("light.b", "Den Arc Pendants", "on", {}),
               Device("light.c", "Office Desk Lamp", "on", {})]
    home = HomeControl(lambda: {}, lambda: None)
    assert home.wants("turn on the pendants", devices)
    assert not home.wants("turn them back on", devices)
    assert home.wants("turn them back on", devices, following=True)
    assert not home.wants("what's the capital of Peru", devices, following=True)
