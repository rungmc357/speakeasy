"""Committed outcomes are complete at the first acknowledgement; invented data only."""
import asyncio
import json
import sqlite3

import pytest

from speakeasy.store import StateStore
from test_run_state import live, task
from test_email_drafts import DRAFT, answer_with


def test_completion_listener_reads_draft_at_first_ack(service):
    worker, backend = live(service)
    seen = []
    service.store.settle_listeners.append(lambda key, status: seen.append(
        (service.store.run_row(key), service.store.work(idem_key=key), service.store.drafts_for(key))))
    asyncio.run(worker._handle_hermes_event(backend, {"event": "run.completed", "output": answer_with(DRAFT)}))
    row, work, drafts = seen[0]
    assert len(drafts) == 1 and drafts[0]["subject"] == DRAFT["subject"]
    assert row["status"] == "completed" and row["settled_at"]
    assert work["result"]["spoken"]


def test_failure_listener_reads_reason_at_first_ack(service):
    worker, backend = live(service)
    seen = []
    service.store.settle_listeners.append(lambda key, status: seen.append(
        service.store._db.execute("SELECT failure FROM runs WHERE idem_key=?", (key,)).fetchone()[0]))
    asyncio.run(worker._handle_hermes_event(backend, {"event": "run.failed", "error": "unrecognized fake failure"}))
    assert seen and seen[0] is not None


def test_outcome_and_drafts_commit_together(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    task(store, "checkout")
    reader = sqlite3.connect(path)
    observed = []
    original = store._add_draft_locked

    def insert(key, session, draft, now):
        observed.append(reader.execute("SELECT status,result_json,settled_at FROM runs").fetchone())
        return original(key, session, draft, now)

    monkeypatch.setattr(store, "_add_draft_locked", insert)
    result = {"full": "A report", "question": {"question": "Which color?"}, "images": [{"path": "chart.png"}]}
    assert store.settle("checkout", "completed", result=result, drafts=[DRAFT])
    assert observed == [("admitting", None, None)]
    status, raw, marker = reader.execute("SELECT status,result_json,settled_at FROM runs").fetchone()
    assert status == "completed" and marker and json.loads(raw) == result
    assert reader.execute("SELECT COUNT(*) FROM email_drafts").fetchone()[0] == 1
    reader.close()


def test_draft_failure_rolls_back_outcome_and_superseding(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    task(store, "checkout")
    original = store.add_draft("checkout", None, DRAFT)
    seen = []
    store.settle_listeners.append(lambda *args: seen.append(args))
    with pytest.raises(TypeError):
        store.settle("checkout", "completed", result={"full": "Report"}, drafts=[DRAFT, {"bad": object()}])
    assert store.run_row("checkout")["status"] == "admitting"
    assert store.run_row("checkout")["settled_at"] is None
    assert store.draft(original["draft_id"])["status"] == "pending"
    assert len(store.drafts_for("checkout")) == 1 and not seen


def test_repeated_settle_never_replaces_payload_or_draft(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    task(store, "checkout")
    assert store.settle("checkout", "completed", result={"full": "First"}, drafts=[DRAFT])
    original = store.drafts_for("checkout")
    assert not store.settle("checkout", "failed", result={"full": "Late"}, failure=("unknown", None), drafts=[DRAFT])
    assert store.drafts_for("checkout") == original
    assert store.work(idem_key="checkout")["result"]["full"] == "First"


def test_queue_does_not_drain_status_without_committed_marker(service, monkeypatch):
    store = service.store
    task(store, "checkout")
    store.queue_message("held", "task", "checkout", None, "checkout", "Use blue")
    with store._lock, store._db:
        store._db.execute("UPDATE runs SET status='completed', settled_at=NULL WHERE idem_key='checkout'")
    sent = []
    monkeypatch.setattr(service, "_send_held", lambda row: sent.append(row))
    assert service.drain_queued() == 0 and not sent
    with store._lock, store._db:
        store._db.execute("UPDATE runs SET status='running' WHERE idem_key='checkout'")
    assert store.settle("checkout", "completed", result={"full": "Report"})
    service.drain_queued()
    assert len(sent) == 1
