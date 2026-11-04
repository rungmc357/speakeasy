"""Live activity and openable ids on every path a task really runs (all data invented):

- a job in a call over the runs API (/v1/runs events);
- a job continued in a chat's own session (session chat stream events);
- a job in a new chat thread, and a request typed into an existing thread through Hermes' webhook
  platform: no event stream, so tool calls are read back from the session's transcript;
- typed follow-ups to a finished task, with a call open and with none.

A typed follow-up's reply ``run_id`` must open the task it started (GET /voice/work/{id}), even when
the run id Hermes handed out was refused a moment later and the task went into a chat thread.
"""
import json
import sqlite3
import time

import pytest

from fakes import http, wait_for
from speakeasy import channels, continuity, router, threads
from speakeasy import recovery
from speakeasy.store import StateStore
from test_routing import WORK, start_call, tasks

SESSION = "s_garden"
THREAD = "4242"


# -- an invented Hermes state DB --------------------------------------------------------------------

def make_state_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, source TEXT, chat_type TEXT, "
               "thread_id TEXT, origin_json TEXT, title TEXT, started_at REAL, ended_at REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
               "role TEXT, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)")
    db.commit()
    db.close()


def add_row(path, session, role, content="", *, calls=None, call_id=None, tool=None, at=None):
    db = sqlite3.connect(path)
    db.execute("INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp) "
               "VALUES (?,?,?,?,?,?,?)", (session, role, content, call_id,
                                          json.dumps(calls) if calls else None, tool, at or time.time()))
    db.commit()
    db.close()


def tool_call(call_id, name, args):
    return {"id": call_id, "call_id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def write_turn(path, session, request, *, steps, answer=None, pause=0.0):
    """One webhook turn as Hermes records it: the user row, then per step an assistant row with the
    tool call and the tool's result row, then (if given) the final plain reply."""
    add_row(path, session, "user", request)
    for call_id, name, args, result in steps:
        start = time.time()
        add_row(path, session, "assistant", "", calls=[tool_call(call_id, name, args)], at=start)
        add_row(path, session, "tool", result, call_id=call_id, tool=name, at=start + 0.25)
        if pause:
            time.sleep(pause)
    if answer is not None:
        add_row(path, session, "assistant", answer)


def activity_of(server, run_id):
    return (http(server.base_url, "GET", f"/voice/work/{run_id}", token=server.token)[1] or {}).get("work") or {}


# -- the transcript reader ------------------------------------------------------------------------

def test_tool_calls_are_read_back_from_a_session_transcript(tmp_path):
    db = tmp_path / "state.db"
    make_state_db(db)
    add_row(db, SESSION, "user", "count the lines")
    add_row(db, SESSION, "assistant", "", calls=[tool_call("c1", "terminal", {"command": "wc -l notes.txt"})], at=100.0)
    add_row(db, SESSION, "tool", '{"output": "12 notes.txt", "exit_code": 0}', call_id="c1", tool="terminal", at=101.5)
    add_row(db, SESSION, "assistant", "", calls=[tool_call("c2", "read_file", {"path": "/tmp/x/plan.md"})], at=102.0)
    add_row(db, SESSION, "tool", '{"success": false, "error": "not found"}', call_id="c2", tool="read_file", at=102.1)
    add_row(db, "other", "assistant", "", calls=[tool_call("c9", "terminal", {"command": "ls"})])
    events, newest = threads.session_tool_events(db, SESSION, 0)
    assert [(e["event"], e["tool"], e.get("tool_call_id")) for e in events] == [
        ("tool.started", "terminal", "c1"), ("tool.completed", "terminal", "c1"),
        ("tool.started", "read_file", "c2"), ("tool.completed", "read_file", "c2")]
    assert events[0]["preview"] == "wc -l notes.txt" and events[0]["transcript_at"] == 100.0
    assert events[1]["error"] is False and events[3]["error"] is True
    assert threads.session_tool_events(db, SESSION, newest) == ([], newest)

    from speakeasy import activity as A
    job = A.JobActivity()
    for e in events:
        job.observe(e)
    items = A.public(job.state, detail=True)["activity"]
    assert [(i["tool"], i["status"]) for i in items] == [("terminal", "done"), ("read_file", "failed")]
    assert items[0]["started_at"] == 100.0 and items[0]["ended_at"] == 101.5  # the real duration


def test_a_missing_or_odd_transcript_reads_as_nothing(tmp_path):
    assert threads.session_tool_events(tmp_path / "none.db", SESSION, 5) == ([], 5)
    db = tmp_path / "old.db"
    sqlite3.connect(db).execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT)").connection.close()
    assert threads.session_tool_events(db, SESSION, 0) == ([], 0)


def test_the_watch_polls_at_most_once_a_second_and_drains_on_stop(tmp_path):
    db = tmp_path / "state.db"
    make_state_db(db)
    seen = []
    watch = threads.SessionToolWatch(db, lambda: SESSION, 0, seen.append, poll_s=0.1)
    assert watch.poll_s == 1.0
    add_row(db, SESSION, "assistant", "", calls=[tool_call("c1", "terminal", {"command": "ls"})])
    watch.poll()
    add_row(db, SESSION, "tool", "a\nb", call_id="c1", tool="terminal")
    watch.stop()  # never started: stop still reads what is left
    assert [e["event"] for e in seen] == ["tool.started", "tool.completed"]
    watch.poll()
    assert len(seen) == 2


# -- recovery never settles a task on its thread's previous reply ---------------------------------

def test_recovery_ignores_a_reply_written_before_the_task_began(tmp_path):
    db = tmp_path / "state.db"
    make_state_db(db)
    add_row(db, SESSION, "assistant", "Yesterday's answer.", at=time.time() - 3600)
    store = StateStore(tmp_path / "speakeasy.sqlite3")
    store.reserve_run("se_follow", "voice_1", "typed_1", 0)
    store.update_run("se_follow", None, "running")
    store.set_continued("se_follow", SESSION, "a thread")
    assert recovery.sweep(store, db, lambda: (), {}) == []
    assert store.work(idem_key="se_follow")["status"] == "running"
    add_row(db, SESSION, "user", "follow-up")
    add_row(db, SESSION, "assistant", "Today's answer.")
    assert recovery.sweep(store, db, lambda: (), {}) == [("se_follow", "completed")]


# -- typed follow-up ids --------------------------------------------------------------------------

def test_a_refused_run_id_already_handed_out_still_opens_the_task(tmp_path):
    store = StateStore(tmp_path / "speakeasy.sqlite3")
    store.reserve_run("se_t", "voice_1", "typed_1", 0)
    store.update_run("se_t", "run_refused1", "running")
    assert store.openable_run_id("se_t") == "run_refused1"
    store.forget_run_id("se_t")                      # Hermes refused it; the task went to a thread
    local = store.openable_run_id("se_t")
    assert local.startswith("lr_")
    for rid in ("run_refused1", local):
        assert store.work(rid)["status"] == "running" and store.key_for_run(rid) == "se_t"
    store.update_run("se_t", None, "completed")
    assert store.work(idem_key="se_t")["run_id"] == local  # it settles under the id it was given
    assert store.work("run_refused1")["run_id"] == local


# -- end to end: every path a real job takes --------------------------------------------------------

CONV = continuity.Conversation(SESSION, "discord", THREAD, "thread", THREAD, "42", "111",
                               "Server / #work / Garden", "Garden plan", time.time())


class WebhookThread:
    """Hermes' webhook platform: a message posted into the thread starts a turn there; the turn is
    written to the state DB as it runs. Waits (bounded) until the card shows a tool call, so the
    test proves activity arrives while the task runs, not only at the end."""

    def __init__(self, service, steps_by_turn, answers):
        self.service, self.steps_by_turn, self.answers = service, steps_by_turn, answers
        self.posted, self.live_seen = [], []

    def available(self, target):
        return True

    def post(self, platform, thread_id, *, message, delivery_id):
        self.posted.append((platform, thread_id, message))
        return True

    def wait_turn(self, platform, thread_id, after_id, on_activity=None):
        turn = len(self.posted) - 1
        db = self.service.rt.state_db
        write_turn(db, SESSION, self.posted[-1][2], steps=self.steps_by_turn[turn])
        key = self._running_key()
        deadline = time.time() + 6
        while time.time() < deadline:
            state = self.service.store.activity_state(key) if key else None
            if state and state.get("items"):
                self.live_seen.append(turn)
                break
            time.sleep(0.1)
        add_row(db, SESSION, "assistant", self.answers[turn])
        return self.answers[turn]

    def _running_key(self):
        rows = self.service.store._db.execute(
            "SELECT idem_key FROM runs WHERE status='running' ORDER BY rowid DESC LIMIT 1").fetchall()
        return rows[0][0] if rows else None


def refusing_stream(base, key, c, message, callback, **k):
    callback("run.started", {"run_id": "run_refused_" + str(int(time.time() * 1000) % 100000)})
    time.sleep(0.4)  # the follow-up's reply goes out while its run id is still the refused one
    callback("run.failed", {"error": "permission_denied"})
    return True


@pytest.fixture
def thread_hermes(service, monkeypatch):
    make_state_db(service.rt.state_db)
    monkeypatch.setattr(continuity, "conversations_with_context", lambda db, request, **kw: [continuity.Candidate(CONV, ())])
    monkeypatch.setattr(continuity, "conversation_by_session", lambda db, sid: CONV if sid == SESSION else None)
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **k: False)
    monkeypatch.setattr(continuity, "stream_session_chat", refusing_stream)
    monkeypatch.setattr(threads, "thread_session", lambda db, platform, thread_id: SESSION)
    monkeypatch.setattr(router, "aux_call", lambda messages, timeout=router.ROUTE_TIMEOUT_S:
                        '{"follow_up_task_id": null, "conversation": "c1", "parts": ["x"], "channel": null}')
    monkeypatch.setattr(service.rt.notices, "post", lambda key, text, limit=600, target=None: True)
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [
        {"target": "discord:111", "label": "#work", "topic": "work"}]}})
    runner = WebhookThread(service, [
        [("c1", "terminal", {"command": "ls beds/"}, "north.txt\nsouth.txt")],
        [("c2", "terminal", {"command": "wc -l beds/north.txt"}, "14 beds/north.txt"),
         ("c3", "read_file", {"path": "beds/south.txt"}, "tomatoes\nbasil")],
        [("c4", "search_files", {"pattern": "basil"}, "beds/south.txt:2")],
    ], ["Two beds.\nSPOKEN: Two beds.", "North has 14 lines.\nSPOKEN: Fourteen lines.",
        "Basil is in the south bed.\nSPOKEN: South bed."])
    service.rt.threads = runner
    return runner


def first_thread_task(server, service):
    _, worker = start_call(server, service)
    worker.delegate("call_g", "In the garden plan thread, which beds do I have?")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed" and t.get("run_id")])[0]
    return worker, done


def check_follow_up(server, service, runner, body, turn, tools):
    assert body["ok"] and body["delivered_as"] == "follow_up", body
    rid = body["run_id"]
    # The id in the reply opens the task it started, while it runs and once it is done.
    assert activity_of(server, rid), f"GET /voice/work/{rid} is null"
    work = wait_for(lambda: (lambda w: w if w.get("status") == "completed" else None)(activity_of(server, rid)), timeout=15)
    assert work["run_id"].startswith("lr_")
    assert [i.get("tool") for i in work["activity"]] == tools
    assert all(i["status"] == "done" and i["ended_at"] >= i["started_at"] for i in work["activity"])
    assert work["activity_total"] == len(tools)
    assert turn in runner.live_seen  # the card had the tool calls before the turn ended
    feed = [t for t in tasks(server) if t.get("run_id") == work["run_id"]]
    assert feed and feed[0]["activity"]  # the task feed shows the same task, with its activity


def test_a_task_typed_into_an_existing_thread_shows_its_tool_calls(server, service, thread_hermes):
    _, done = first_thread_task(server, service)
    assert thread_hermes.posted and 0 in thread_hermes.live_seen
    work = activity_of(server, done["run_id"])
    assert [i.get("tool") for i in work["activity"]] == ["terminal"] and work["activity"][0]["status"] == "done"


def test_typed_follow_up_in_a_call_that_goes_into_the_thread(server, service, thread_hermes):
    _, done = first_thread_task(server, service)
    status, body = http(server.base_url, "POST", f"/voice/work/{done['run_id']}/message",
                        {"text": "How many lines in the north bed?", "client_id": "c_n"}, server.token)
    assert status == 200, body
    check_follow_up(server, service, thread_hermes, body, 1, ["terminal", "read_file"])


def test_typed_follow_up_with_no_call_open_that_goes_into_the_thread(server, service, thread_hermes):
    worker, done = first_thread_task(server, service)
    worker.feed({"type": "session.closed"})
    status, body = http(server.base_url, "POST", f"/voice/work/{done['run_id']}/message",
                        {"text": "How many lines in the north bed?"}, server.token)
    assert status == 200, body
    check_follow_up(server, service, thread_hermes, body, 1, ["terminal", "read_file"])


def test_spoken_follow_up_in_a_call_that_goes_into_the_thread(server, service, thread_hermes):
    worker, done = first_thread_task(server, service)
    worker.feed({"type": "session.output_transcript.delta", "delta": "Okay.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_f", "And how long is the north bed's list?", task_id=done["task_id"])
    work = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed" and len(t.get("activity") or []) == 2])[0]
    detail = activity_of(server, work["run_id"])
    assert [i.get("tool") for i in detail["activity"]] == ["terminal", "read_file"]


class TranscriptThreads:
    """A new thread opened through the webhook platform; its turn is written to the state DB."""

    def __init__(self, service):
        self.service, self.opened, self.live_seen = service, [], False

    def available(self, target):
        return True

    def open(self, target, *, message, title, delivery_id):
        self.opened.append(target)
        return threads.Opened("speakeasy-x", THREAD, "discord")

    def wait(self, opened, on_session, on_title=None, on_activity=None):
        on_session(SESSION)
        db = self.service.rt.state_db
        write_turn(db, SESSION, "book it", steps=[("n1", "web_search", {"query": "dentist near me"}, "3 results")])
        deadline = time.time() + 6
        while time.time() < deadline and not self.live_seen:
            rows = self.service.store._db.execute("SELECT state_json FROM activity").fetchall()
            self.live_seen = any(json.loads(r[0]).get("items") for r in rows)
            time.sleep(0.1)
        add_row(db, SESSION, "assistant", "Booked the 9th.\nSPOKEN: Booked.")
        return "Voice: x\nBooked the 9th."


def test_a_job_in_a_new_thread_shows_its_tool_calls(server, service, monkeypatch):
    make_state_db(service.rt.state_db)
    monkeypatch.setattr(threads, "thread_session", lambda db, platform, thread_id: SESSION)
    runner = TranscriptThreads(service)
    service.rt.threads = runner
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [dict(WORK, new_thread=True)]}})
    _, worker = start_call(server, service)
    worker.delegate("call_t", "Put this in work: book a dentist cleaning")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    work = activity_of(server, done["run_id"])
    assert runner.live_seen and [i.get("tool") for i in work["activity"]] == ["web_search"]


def test_a_job_continued_in_a_chats_own_session_shows_its_tool_calls(server, service, monkeypatch):
    monkeypatch.setattr(continuity, "conversations_with_context", lambda db, request, **kw: [continuity.Candidate(CONV, ())])
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **k: False)
    monkeypatch.setattr(router, "aux_call", lambda messages, timeout=router.ROUTE_TIMEOUT_S:
                        '{"follow_up_task_id": null, "conversation": "c1", "parts": ["x"], "channel": null}')
    monkeypatch.setattr(service.rt.notices, "post", lambda key, text, limit=600, target=None: True)

    def stream(base, key, c, message, callback, **k):
        callback("run.started", {"run_id": "run_chat1"})
        callback("tool.started", {"tool_name": "read_file", "preview": "beds.md", "tool_call_id": "s1"})
        callback("tool.completed", {"tool_name": "read_file", "preview": "tomatoes", "tool_call_id": "s1"})
        callback("assistant.completed", {"content": "Tomatoes.\nSPOKEN: Tomatoes."})
        callback("run.completed", {})
        return True
    monkeypatch.setattr(continuity, "stream_session_chat", stream)
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [
        {"target": "discord:111", "label": "#work", "topic": "work"}]}})
    _, worker = start_call(server, service)
    worker.delegate("call_c", "In the garden plan thread, what's in the beds?")
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    work = activity_of(server, "run_chat1")
    assert [(i.get("tool"), i["status"]) for i in work["activity"]] == [("read_file", "done")]


def test_typed_follow_up_over_the_runs_api_shows_its_tool_calls(server, service, hermes):
    hermes.live_events = [
        {"event": "tool.started", "tool": "terminal", "preview": "ls beds/", "tool_call_id": "r1"},
        {"event": "tool.completed", "tool": "terminal", "tool_call_id": "r1", "duration": 0.2, "error": False},
    ]
    _, worker = start_call(server, service)
    worker.delegate("call_r", "Compare three bike locks")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed" and t.get("run_id")])[0]
    worker.feed({"type": "session.closed"})
    status, body = http(server.base_url, "POST", f"/voice/work/{done['run_id']}/message",
                        {"text": "Which is lightest?"}, server.token)
    assert status == 200 and body["delivered_as"] == "follow_up", body
    work = wait_for(lambda: (lambda w: w if w.get("status") == "completed" else None)(activity_of(server, body["run_id"])))
    assert "terminal" in [i.get("tool") for i in work["activity"]]  # (the fake run adds its own web_search)
