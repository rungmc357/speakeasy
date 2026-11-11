"""Honest receipts for what the user adds to a running task: queued -> landed | not read."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from fakes import wait_for
from speakeasy import calls, receipts
from speakeasy.prompt import builder as P
from test_routing import start_call, tasks

MARKER = ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; "
          "not tool output and not a new delivery when replayed from conversation history]")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)
    monkeypatch.setattr(calls, "FOLLOW_UP_POLL_S", 0.1)
    monkeypatch.setattr(calls, "FOLLOW_UP_SETTLE_S", 0.3)


def state_db(path: Path) -> Path:
    """Hermes' state.db, with only the columns receipts.py reads."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, parent_session_id TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
               "role TEXT, content TEXT, tool_calls TEXT, timestamp REAL, display_kind TEXT)")
    db.commit()
    db.close()
    return path


def write(path: Path, session_id: str, role: str, content: str, kind: str | None = None) -> None:
    db = sqlite3.connect(path)
    db.execute("INSERT INTO messages (session_id, role, content, timestamp, display_kind) VALUES (?,?,?,?,?)",
               (session_id, role, content, time.time(), kind))
    db.commit()
    db.close()


def steer_row(text: str) -> str:
    """What Hermes writes when the agent takes a steer (agent.prompt_builder.steer_user_row)."""
    return f"{MARKER}\n{text}\n[/OUT-OF-BAND USER MESSAGE]"


def say(worker, delegation_id, words, **extra):
    worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 1, "end_ms": 2})
    worker.delegate(delegation_id, words, **extra)


def _running(server, service, hermes, home):
    hermes.hold = True
    db = state_db(Path(home) / "state.db")
    session, worker = start_call(server, service)
    say(worker, "call_trip", "Plan a four day trip to Lisbon")
    run = wait_for(lambda: next((r for r in worker.interaction.runs.values()
                                 if r.delegation_id == "call_trip" and r.run_id), None))
    say(worker, "call_more", "make it lighter, fewer museums", task_id="call_trip")
    wait_for(lambda: hermes.steers)
    return worker, run, db


def _follow_ups(server):
    return wait_for(lambda: next((t.get("follow_ups") for t in tasks(server) if t.get("follow_ups")), None))


def _state(server):
    return [f["state"] for f in _follow_ups(server)]


def _notes(worker, kind="session.thinking.append"):
    return [c for k, _, c in worker.sent if k == kind]


# -- receipts.py ---------------------------------------------------------------------------

def test_landed_only_when_the_text_is_in_a_later_user_row(tmp_path):
    db = state_db(tmp_path / "state.db")
    write(db, "s1", "user", steer_row("Sam just added: fewer museums"), "steer")
    first = 1
    assert receipts.landed(db, "s1", 0, "Sam just added: fewer museums")
    assert not receipts.landed(db, "s1", first, "Sam just added: fewer museums")  # older than the steer
    assert not receipts.landed(db, "s2", 0, "Sam just added: fewer museums")      # another session
    write(db, "s1", "assistant", "Sam just added: fewer museums")                  # echoed by the model
    assert not receipts.landed(db, "s1", first, "Sam just added: fewer museums")
    assert not receipts.landed(tmp_path / "missing.db", "s1", 0, "x")              # fails closed
    assert receipts.still_pending("one\nSam  just added:\nfewer museums", "Sam just added: fewer museums")
    assert not receipts.still_pending(None, "x")


def test_a_compacted_child_session_counts(tmp_path):
    db = state_db(tmp_path / "state.db")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sessions VALUES ('child', 'parent')")
    conn.commit()
    conn.close()
    write(db, "child", "user", steer_row("added text"), "steer")
    assert receipts.landed(db, "parent", 0, "added text")


# -- the call ------------------------------------------------------------------------------

def test_accepted_is_only_queued_until_the_task_reads_it(server, service, hermes, home):
    worker, run, db = _running(server, service, hermes, home)
    assert _state(server) == ["queued"]
    note = wait_for(lambda: [c for c in _notes(worker) if c.startswith("Lined up, not read yet:")])[0]
    assert "has not read it yet" in note
    assert not any(c.startswith("Delivered:") for c in _notes(worker))     # never claimed as passed on
    # The agent drains it at its next tool batch: Hermes writes it into the task's transcript.
    session = service.store.session_for(run.idem_key)
    write(db, session, "user", steer_row(hermes.steers[-1][1]), "steer")
    wait_for(lambda: _state(server) == ["landed"])
    hermes.runs[run.run_id]["done"].set()
    wait_for(lambda: run.status == "completed")
    assert not any("before it read" in c for c in _notes(worker, "session.commentary.append"))


def test_a_run_that_ends_with_it_unread_says_so_and_offers_it(server, service, hermes, home):
    worker, run, _ = _running(server, service, hermes, home)
    pending = hermes.steers[-1][1]
    hermes.runs[run.run_id]["events"] = [{"event": "run.completed", "output": "Done.\nDONE: Trip\nSPOKEN: Planned.",
                                          "completed": True, "partial": False, "interrupted": False,
                                          "pending_steer": pending}]
    hermes.runs[run.run_id]["done"].set()
    wait_for(lambda: _state(server) == ["not_read"])
    spoken = wait_for(lambda: [c for c in _notes(worker, "session.commentary.append") if "before it read" in c])[0]
    assert "make it lighter, fewer museums" in spoken and "new work" in spoken
    assert any("hand off exactly this as a new request" in c for c in _notes(worker))


def test_a_stopped_run_with_no_transcript_evidence_did_not_read_it(server, service, hermes, home):
    worker, run, _ = _running(server, service, hermes, home)
    hermes.runs[run.run_id]["events"] = [{"event": "run.cancelled", "completed": False, "partial": False,
                                          "interrupted": True}]
    hermes.runs[run.run_id]["done"].set()
    wait_for(lambda: _state(server) == ["not_read"])
    wait_for(lambda: [c for c in _notes(worker, "session.commentary.append") if "stopped before it read" in c])


def test_a_completed_run_that_handed_nothing_back_drained_it(server, service, hermes, home):
    """Hermes' finalizer returns any still-queued steer as pending_steer: none back means it was taken."""
    worker, run, _ = _running(server, service, hermes, home)
    hermes.runs[run.run_id]["done"].set()
    wait_for(lambda: _state(server) == ["landed"])


def test_an_old_hermes_without_terminal_fields_proves_nothing(server, service, hermes, home):
    worker, run, _ = _running(server, service, hermes, home)
    hermes.runs[run.run_id]["events"] = [{"event": "run.completed", "output": "Done."}]
    hermes.runs[run.run_id]["done"].set()
    wait_for(lambda: _state(server) == ["not_read"])


def test_the_voice_rule_names_queued():
    rules = Path(P.__file__).with_name("rules.md").read_text()
    assert '"Lined up" means not read yet' in rules and "never a one-word status" in rules


def test_a_follow_up_taken_just_as_the_run_ends_is_still_settled(server, service, hermes, home, monkeypatch):
    """Hermes takes what they added, and the run finishes before Speakeasy has recorded it: the
    run's own settling has passed, so the receipt must not stay "queued" for good."""
    hermes.hold = True
    state_db(Path(home) / "state.db")
    _, worker = start_call(server, service)
    say(worker, "call_trip", "Plan a four day trip to Lisbon")
    run = wait_for(lambda: next((r for r in worker.interaction.runs.values()
                                 if r.delegation_id == "call_trip" and r.run_id), None))
    real_steer = worker.hermes.steer

    def steer_then_end(run_id, text):
        accepted = real_steer(run_id, text)
        hermes.runs[run_id]["events"] = [{"event": "run.cancelled", "completed": False, "partial": False,
                                          "interrupted": True}]
        hermes.runs[run_id]["done"].set()
        wait_for(lambda: run.status == "cancelled")  # the end is handled before the steer returns
        time.sleep(0.2)
        return accepted
    monkeypatch.setattr(worker.hermes, "steer", steer_then_end)
    say(worker, "call_more", "make it lighter, fewer museums", task_id="call_trip")
    wait_for(lambda: _state(server) == ["not_read"])
    wait_for(lambda: [c for c in _notes(worker, "session.commentary.append") if "stopped before it read" in c])
