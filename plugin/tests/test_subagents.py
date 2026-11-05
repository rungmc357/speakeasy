"""Helpers a task started (delegate_task subagents; docs/API.md "Helpers", capability job_subagents).
Invented data only."""
import json
import sqlite3
import time

import pytest

from fakes import http, wait_for
from speakeasy import activity as A
from speakeasy import subagents as S
from speakeasy.store import StateStore
from test_project_fence import run_of, say
from test_routing import start_call
from test_thread_activity import add_row, make_state_db, tool_call


def start(sid, goal, index=0, session=None, **extra):
    event = {"event": "subagent.start", "subagent_id": sid, "goal": goal, "task_index": index, "task_count": 3,
             "delegation_id": "dg1", "depth": 1, "tool_count": 0, **extra}
    if session:
        event["child_session_id"] = session
    return event


def complete(sid, status, summary="", index=0, **extra):
    return {"event": "subagent.complete", "subagent_id": sid, "status": status, "summary": summary,
            "task_index": index, "delegation_id": "dg1", "duration_seconds": 12.5, "tool_count": 4, **extra}


def run(events, start_at=1000.0):
    job = A.JobActivity()
    for i, event in enumerate(events):
        job.observe(event, now=start_at + i)
    return job


def helpers(job, detail=False):
    return A.public(job.state, detail=detail).get("subagents") or []


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.sqlite3", tmp_path)


def add_child(db, session, parent, goal, started=None, ended=None):
    conn = sqlite3.connect(db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)")]
    for col in ("parent_session_id", "model_config"):
        if col not in cols:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
    conn.execute("INSERT INTO sessions (id, source, started_at, ended_at, parent_session_id, model_config) "
                 "VALUES (?,?,?,?,?,?)", (session, "subagent", started or time.time(), ended, parent,
                                          json.dumps({"_delegate_from": parent})))
    conn.commit()
    conn.close()
    add_row(db, session, "user", goal)


def end_child(db, session, answer=None):
    if answer is not None:
        add_row(db, session, "assistant", answer)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (time.time(), session))
    conn.commit()
    conn.close()


# -- events -> the model ---------------------------------------------------------------------------

def test_parallel_helpers_each_get_a_row_and_settle_by_their_own_id():
    job = run([
        {"event": "tool.started", "tool": "delegate_task", "preview": "3 parallel tasks", "tool_call_id": "d1"},
        start("sa-0-aaaa1111", "Compare the return policies of the three invented shops", 0),
        start("sa-1-bbbb2222", "Find the cheapest shipping option for a small parcel", 1),
        start("sa-2-cccc3333", "Check which shop ships to the islands", 2),
        complete("sa-0-aaaa1111", "completed", "Shop B has the longest return window (60 days).", 0),
        complete("sa-2-cccc3333", "failed", "The shop's page did not load.", 2),
    ])
    rows = helpers(job)
    assert [r["id"] for r in rows] == ["sa-0-aaaa1111", "sa-1-bbbb2222", "sa-2-cccc3333"]
    assert [r["status"] for r in rows] == ["done", "running", "failed"]
    assert rows[0]["summary"].startswith("Shop B has the longest return window")
    assert rows[0]["tool_count"] == 4 and rows[0]["ended_at"] >= rows[0]["started_at"]
    assert "ended_at" not in rows[1]
    for r in rows:
        assert 2 <= len(r["title"].split()) <= 8 and "…" not in r["title"]
    # the parent's feed: one helper item each, settled by its own id (not "the newest running one")
    items = [i for i in A.public(job.state)["activity"] if i.get("tool") == "delegate_task" and i["id"] != "a1"]
    assert [i["status"] for i in items] == ["done", "running", "failed"]


def test_a_complete_without_its_start_still_names_the_helper():
    job = run([complete("sa-0-dddd4444", "completed", "Found two invented candidates.", goal="Shortlist venues")])
    (row,) = helpers(job)
    assert row["status"] == "done" and row["summary"] == "Found two invented candidates."


@pytest.mark.parametrize("raw,status", [("completed", "done"), ("failed", "failed"), ("error", "failed"),
                                        ("timeout", "failed"), ("interrupted", "stopped")])
def test_hermes_statuses_map_to_four(raw, status):
    job = run([start("sa-0-eeee5555", "Do a thing"), complete("sa-0-eeee5555", raw, "x")])
    assert helpers(job)[0]["status"] == status


def test_helpers_still_running_stop_with_a_stopped_or_failed_task():
    job = run([start("sa-0-ffff6666", "Draft the invented report", session="child_a"),
               {"event": "run.cancelled"}])
    (row,) = helpers(job)
    assert row["status"] == "stopped" and row["note"] == "Stopped with the task"


def test_a_helper_with_nothing_to_follow_is_stopped_when_the_task_completes():
    job = run([start("sa-0-gggg7777", "Check the invented calendar"), {"event": "run.completed"}])
    assert helpers(job)[0]["status"] == "stopped"


def test_a_helper_with_a_transcript_keeps_running_after_the_task_completes():
    job = run([start("sa-0-hhhh8888", "Keep watching the invented build", session="child_b"),
               {"event": "run.completed"}])
    assert helpers(job)[0]["status"] == "running"


def test_secrets_never_reach_a_helper():
    fake_key = "sk" + "-" + "test-1234567890abcdef"  # built at runtime so the secret scan doesn't flag the fixture
    job = run([start("sa-0-iiii9999", "Deploy with API_TOKEN=abc123secretvalue to /srv/apps/shop/deploy.sh"),
               complete("sa-0-iiii9999", "completed", f"Used bearer {fake_key} and it worked")])
    text = json.dumps(helpers(job, detail=True))
    assert "abc123secretvalue" not in text and fake_key not in text and "/srv/apps" not in text


def test_no_helpers_no_field_and_older_fields_unchanged():
    job = run([{"event": "tool.started", "tool": "read_file", "preview": "notes.md", "tool_call_id": "t1"}])
    out = A.public(job.state)
    assert "subagents" not in out and out["activity"][0]["text"] == "Read notes.md"


def test_nested_helpers_carry_depth_and_parent():
    job = run([start("sa-0-jjjj0000", "Plan the invented trip"),
               start("sa-0-kkkk1111", "Find invented train times", depth=2, parent_id="sa-0-jjjj0000")])
    rows = helpers(job)
    assert "depth" not in rows[0] and rows[1]["depth"] == 2 and rows[1]["parent_id"] == "sa-0-jjjj0000"


def test_helpers_are_capped():
    job = run([start(f"sa-{i}-{i:08x}", f"Invented subtask number {i} of many", i) for i in range(S.MAX_SUBAGENTS + 4)])
    assert len(helpers(job)) == S.MAX_SUBAGENTS


# -- the transcript watch --------------------------------------------------------------------------

def watch(store, db, parent=None, titler=None):
    return S.SubagentWatch(store, "k1", db, (lambda: parent) if parent else None, titler=titler, since=0)


def test_a_helpers_own_tool_calls_are_read_from_its_transcript(store, tmp_path, monkeypatch):
    monkeypatch.setattr(A, "FLUSH_EVERY_S", 0)
    db = tmp_path / "hermes.db"
    make_state_db(db)
    add_child(db, "child_c", "parent_1", "Compare the invented plans")
    t = time.time()
    add_row(db, "child_c", "assistant", "", calls=[tool_call("c1", "web_search", {"query": "invented plan prices"})], at=t)
    add_row(db, "child_c", "tool", '{"results": 5}', call_id="c1", tool="web_search", at=t + 1)
    add_row(db, "child_c", "assistant", "", calls=[tool_call("c2", "read_file", {"path": "/tmp/x/plans.md"})], at=t + 2)
    A.observe(store, "k1", start("sa-0-llll2222", "Compare the invented plans", session="child_c"))
    w = watch(store, db)
    assert w.poll() is True
    (row,) = A.public(store.activity_state("k1"), detail=True)["subagents"]
    assert [s["text"] for s in row["activity"]] == ["Searched the web for “invented plan prices”", "Read plans.md"]
    assert [s["status"] for s in row["activity"]] == ["done", "running"]
    assert row["now"] == "Read plans.md" and row["steps_total"] == 2
    stream = A.public(store.activity_state("k1"))["subagents"][0]
    assert "activity" not in stream and stream["now"] == "Read plans.md"  # the stream stays small
    A.observe(store, "k1", complete("sa-0-llll2222", "completed", "Plan B is cheaper."))
    w.poll()
    row = A.public(store.activity_state("k1"), detail=True)["subagents"][0]
    assert row["status"] == "done" and "now" not in row and row["activity"][-1]["status"] == "done"


def test_a_helper_that_never_reports_settles_from_its_closed_session(store, tmp_path, monkeypatch):
    monkeypatch.setattr(A, "FLUSH_EVERY_S", 0)
    db = tmp_path / "hermes.db"
    make_state_db(db)
    add_child(db, "child_d", "parent_1", "Summarize the invented notes")
    add_child(db, "child_e", "parent_1", "Rename the invented files")
    A.observe(store, "k1", start("sa-0-mmmm3333", "Summarize the invented notes", 0, session="child_d"))
    A.observe(store, "k1", start("sa-1-nnnn4444", "Rename the invented files", 1, session="child_e"))
    A.observe(store, "k1", {"event": "run.completed"})
    w = watch(store, db)
    assert w.poll() is True  # still running: a background helper
    end_child(db, "child_d", answer="The notes cover three invented topics.")
    end_child(db, "child_e")
    assert w.poll() is False
    rows = A.public(store.activity_state("k1"))["subagents"]
    assert rows[0]["status"] == "done" and rows[0]["summary"] == "The notes cover three invented topics."
    assert rows[1]["status"] == "stopped" and rows[1]["note"] == "It ended without an answer"


def test_a_quiet_helper_is_stopped_a_while_after_the_task_ends(store, tmp_path, monkeypatch):
    monkeypatch.setattr(A, "FLUSH_EVERY_S", 0)
    db = tmp_path / "hermes.db"
    make_state_db(db)
    add_child(db, "child_f", "parent_1", "Wait for the invented reply")
    t = time.time()
    A.observe(store, "k1", start("sa-0-oooo5555", "Wait for the invented reply", session="child_f"), now=t)
    A.observe(store, "k1", {"event": "run.completed"}, now=t + 1)
    clock = [t + 60]
    w = S.SubagentWatch(store, "k1", db, None, since=0, clock=lambda: clock[0])
    w.poll()
    assert A.public(store.activity_state("k1"))["subagents"][0]["status"] == "running"
    clock = [t + S.IDLE_SETTLE_S + 5]
    w = S.SubagentWatch(store, "k1", db, None, since=0, clock=lambda: clock[0])
    w.poll()
    assert A.public(store.activity_state("k1"))["subagents"][0]["status"] == "stopped"


def test_children_are_found_by_their_parent_session_and_named_by_the_titler(store, tmp_path, monkeypatch):
    """A thread task has no run event stream: its helpers come from the sessions Hermes made under it."""
    monkeypatch.setattr(A, "FLUSH_EVERY_S", 0)
    db = tmp_path / "hermes.db"
    make_state_db(db)
    add_child(db, "child_g", "parent_9", "Look up the opening hours of the invented museum and the cafe next door")
    add_child(db, "child_other", "someone_else", "Not ours")
    seen = []
    w = watch(store, db, parent="parent_9", titler=lambda goal: seen.append(goal) or "Check museum and cafe hours")
    w.poll()
    rows = A.public(store.activity_state("k1"))["subagents"]
    assert len(rows) == 1 and rows[0]["status"] == "running"
    assert rows[0]["title"] == "Check museum and cafe hours" and seen
    assert rows[0]["goal"].startswith("Look up the opening hours")
    # an event naming the same child later doesn't make a second row
    A.observe(store, "k1", start("sa-0-pppp6666", "Look up the opening hours", session="child_g"))
    assert len(A.public(store.activity_state("k1"))["subagents"]) == 1


def test_a_child_found_first_keeps_its_row_when_hermes_names_it(store, tmp_path, monkeypatch):
    monkeypatch.setattr(A, "FLUSH_EVERY_S", 0)
    db = tmp_path / "hermes.db"
    make_state_db(db)
    add_child(db, "child_k", "parent_2", "Count the invented votes")
    watch(store, db, parent="parent_2").poll()
    row_id = A.public(store.activity_state("k1"))["subagents"][0]["id"]
    A.observe(store, "k1", start("sa-0-wwww2222", "Count the invented votes", session="child_k"))
    A.observe(store, "k1", complete("sa-0-wwww2222", "completed", "42 invented votes."))
    (row,) = A.public(store.activity_state("k1"))["subagents"]
    assert row["id"] == row_id and row["status"] == "done"


def test_the_watch_ends_once_the_task_ends_with_no_helper_running(store, tmp_path, monkeypatch):
    monkeypatch.setattr(A, "FLUSH_EVERY_S", 0)
    db = tmp_path / "hermes.db"
    make_state_db(db)
    w = watch(store, db, parent="parent_3")
    assert w.poll() is True
    A.observe(store, "k1", {"event": "run.completed"})
    assert w.poll() is False


def test_a_bad_title_keeps_the_instant_one(store, tmp_path, monkeypatch):
    monkeypatch.setattr(A, "FLUSH_EVERY_S", 0)
    db = tmp_path / "hermes.db"
    make_state_db(db)
    A.observe(store, "k1", start("sa-0-qqqq7777", "Sort the invented receipts by month"))
    first = A.public(store.activity_state("k1"))["subagents"][0]["title"]
    watch(store, db, titler=lambda goal: None).poll()
    assert A.public(store.activity_state("k1"))["subagents"][0]["title"] == first == "Sort the invented receipts by month"


def test_a_missing_state_db_reads_as_nothing(store, tmp_path):
    A.observe(store, "k1", start("sa-0-rrrr8888", "Do an invented thing", session="child_h"))
    assert watch(store, tmp_path / "missing.db", parent="p").poll() is True
    assert A.public(store.activity_state("k1"))["subagents"][0]["status"] == "running"


def test_helpers_survive_a_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "FLUSH_EVERY_S", 0)
    first = StateStore(tmp_path / "state.sqlite3", tmp_path)
    A.observe(first, "k1", start("sa-0-ssss9999", "Tidy the invented inbox"))
    A.observe(first, "k1", complete("sa-0-ssss9999", "completed", "Archived 12 invented emails."))
    again = StateStore(tmp_path / "state.sqlite3", tmp_path)
    assert A.public(again.activity_state("k1"))["subagents"][0]["summary"] == "Archived 12 invented emails."


# -- end to end: the work object and the flag ------------------------------------------------------

def test_a_running_task_shows_its_helpers_and_status_has_the_flag(server, service, hermes):
    status = http(server.base_url, "GET", "/voice/status", token=server.token)[1]
    assert status["capabilities"]["job_subagents"] is True
    hermes.hold = True
    hermes.live_events = [
        {"event": "tool.started", "tool": "delegate_task", "preview": "2 parallel tasks", "tool_call_id": "d1"},
        start("sa-0-tttt0000", "Compare the invented gym prices", 0),
        start("sa-1-uuuu1111", "Check the invented gym opening hours", 1),
        complete("sa-1-uuuu1111", "failed", "The site was down.", 1),
    ]
    _, worker = start_call(server, service)
    say(worker, "call_a", "Find me a gym near the invented office")
    task = run_of(worker, "call_a")

    def work():
        got = http(server.base_url, "GET", f"/voice/work/{task.run_id}", token=server.token)[1].get("work") or {}
        subs = got.get("subagents") or []
        return got if len(subs) == 2 and subs[1]["status"] == "failed" else None
    got = wait_for(work, timeout=10)
    assert [s["status"] for s in got["subagents"]] == ["running", "failed"]
    assert got["status"] not in {"failed"}  # a failed helper never fails the task
    for r in hermes.runs.values():
        r["done"].set()
