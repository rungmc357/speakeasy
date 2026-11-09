"""SPK-308: a job Hermes finished never stays "working" because its follower lost the result.

Seen live: a long turn finished in Hermes with its full answer, but the plugin was backed up and the
run's follower never recorded it. The row stayed ``working`` with no result for the rest of the call,
and nothing settled it, because the stuck-task sweeps skip every run this process still follows.
The periodic sweep now asks Hermes about followed runs; one Hermes finished a while ago is settled
through its worker's normal path, or straight from Hermes' record when that worker is stalled too.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from fakes import http, wait_for
from speakeasy import recovery
from speakeasy import service as service_mod
from speakeasy.store import StateStore

ANSWER = ("Three things stand out on the phone screens: the composer crowds the keyboard, the model "
          "chip wraps, and the task card repeats its title.\nSPOKEN: Three layout issues, all on screen.")


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(recovery, "FOLLOWED_CHECK_AFTER_S", 0)
    monkeypatch.setattr(recovery, "FOLLOWED_DONE_GRACE_S", 0)


def _stalled_follower(server, service, hermes, *, release: threading.Event, stall_every_time: bool):
    """A running job whose follower stalls on the run's final event, as a backed-up plugin did."""
    hermes.hold = True
    hermes.responder = lambda prompt, sid: ANSWER
    status, body = http(server.base_url, "POST", "/voice/work",
                        {"text": "Outline three layout issues on the phone screens"}, server.token)
    assert status == 202, body
    wait_for(lambda: len(hermes.calls) == 1)
    key, run_id = hermes.calls[0]["idem"], hermes.calls[0]["run_id"]
    wait_for(lambda: service.store.run_status(key) in {"running", "working"})
    worker = service._text_worker
    original = worker._handle_hermes_event
    stalls = {"n": 0}

    async def stalled(backend, event):
        if str(event.get("event") or "").startswith("run.") and (stall_every_time or not stalls["n"]):
            stalls["n"] += 1
            while not release.is_set():
                await asyncio.sleep(0.05)
        return await original(backend, event)

    worker._handle_hermes_event = stalled
    # Hermes finishes the turn: its status and final event both carry the answer.
    hermes.runs[run_id]["status"] = "completed"
    hermes.runs[run_id]["done"].set()
    wait_for(lambda: stalls["n"] >= 1)
    return key, run_id, worker


def _backend(worker, key):
    with worker.interaction.lock:
        return next(r for r in worker.interaction.runs.values() if r.idem_key == key)


def test_a_finished_run_whose_follower_stalled_settles_with_its_answer(server, service, hermes, quick):
    release = threading.Event()
    try:
        key, run_id, worker = _stalled_follower(server, service, hermes, release=release, stall_every_time=False)
        time.sleep(0.3)
        assert service.store.run_status(key) in {"running", "working"}   # the bug: done in Hermes, not here
        settled = service.settle_stuck_tasks()                             # the once-a-minute sweep
        assert (key, "completed") in settled
        work = service.store.work(idem_key=key) or {}
        assert work["status"] == "completed"
        assert "composer crowds the keyboard" in work["result"]["full"]
        assert work["result"]["spoken"] == "Three layout issues, all on screen."
        assert _backend(worker, key).status == "completed"                 # memory agrees with the row
        assert service.settle_stuck_tasks() == []                          # settled once
    finally:
        release.set()
    # The stalled follower catching up later changes nothing: the answer is stored once.
    time.sleep(0.4)
    work = service.store.work(idem_key=key) or {}
    assert work["status"] == "completed" and "composer crowds the keyboard" in work["result"]["full"]


def test_a_stalled_worker_still_gets_its_run_settled_from_hermes(server, service, hermes, quick, monkeypatch,
                                                                 caplog):
    monkeypatch.setattr(service_mod, "LOST_RESULT_HANDOFF_S", 0.5)
    release = threading.Event()
    try:
        key, run_id, worker = _stalled_follower(server, service, hermes, release=release, stall_every_time=True)
        with caplog.at_level(logging.WARNING, logger="speakeasy"):
            settled = service.settle_stuck_tasks()
        assert (key, "completed") in settled
        work = service.store.work(idem_key=key) or {}
        assert work["status"] == "completed" and "composer crowds the keyboard" in work["result"]["full"]
        assert _backend(worker, key).status == "completed"
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert run_id in logged and "never recorded" in logged    # visible in errors.log next time
    finally:
        release.set()


def test_a_running_job_is_left_to_its_follower(server, service, hermes, quick):
    hermes.hold = True
    status, body = http(server.base_url, "POST", "/voice/work", {"text": "Find a quiet cafe near the office"},
                        server.token)
    assert status == 202, body
    wait_for(lambda: len(hermes.calls) == 1)
    key, run_id = hermes.calls[0]["idem"], hermes.calls[0]["run_id"]
    wait_for(lambda: service.store.run_status(key) in {"running", "working"})
    assert service.settle_stuck_tasks() == []
    assert service.store.run_status(key) in {"running", "working"}
    hermes.runs[run_id]["status"] = "completed"
    hermes.runs[run_id]["done"].set()
    wait_for(lambda: service.store.run_status(key) == "completed")  # the follower's own settle, as always


# -- the decision, unit level ---------------------------------------------------------------------

class Hermes:
    def __init__(self, runs=None, forgotten=()):
        self.runs, self.forgotten, self.asked = dict(runs or {}), set(forgotten), []

    def get_run(self, run_id):
        self.asked.append(run_id)
        if run_id in self.forgotten:
            exc = RuntimeError("Hermes HTTP 404")
            exc.status = 404
            raise exc
        return self.runs[run_id]


def _open(store, key, run_id, status="working", age_s=600.0, quiet_s=0.0):
    store.reserve_run(key, "int_x", "call_" + key, 1)
    store.progress(key, "request", "Find why the checkout test fails")
    with store._lock, store._db:
        store._db.execute("UPDATE runs SET status=?, run_id=?, created=?, updated=? WHERE idem_key=?",
                          (status, run_id, time.time() - age_s, time.time() - quiet_s, key))


def test_only_runs_hermes_finished_a_while_ago_are_handed_back(tmp_path):
    store = StateStore(tmp_path / "s.sqlite3")
    _open(store, "done", "run_done")
    _open(store, "going", "run_going")
    _open(store, "young", "run_young", age_s=10)
    _open(store, "thread", "lr_abc")
    hermes = Hermes({"run_done": {"status": "completed", "output": "All set."},
                     "run_going": {"status": "running"}, "run_young": {"status": "completed", "output": "x"}})
    followed = {"done": "run_done", "going": "run_going", "young": "run_young", "thread": "lr_abc"}
    seen: dict[str, float] = {}
    # First sighting: a follower a moment behind gets the grace period to settle it itself.
    assert recovery.finished_followed(store, hermes, followed, seen, mono=100.0) == ([], [])
    assert set(hermes.asked) == {"run_done", "run_going"}   # too young and thread tasks aren't asked
    done, _ = recovery.finished_followed(store, hermes, followed, seen,
                                         mono=100.0 + recovery.FOLLOWED_DONE_GRACE_S)
    assert [(k, f) for k, f, _ in done] == [("done", "completed")]
    assert recovery.settle_from_hermes(store, "done", "working", "completed", done[0][2])
    assert store.run_status("done") == "completed" and store.work(idem_key="done")["result"]["full"] == "All set."
    recovery.finished_followed(store, hermes, followed, seen, mono=500.0)
    assert "done" not in seen                                # settled: forgotten
    assert store.run_status("going") == "working"


def test_a_failed_run_settles_failed_with_its_reason(tmp_path):
    store = StateStore(tmp_path / "s.sqlite3")
    _open(store, "k", "run_k")
    reply = {"status": "failed", "error": "Error code: 429 - rate limit exceeded"}
    assert recovery.settle_from_hermes(store, "k", "working", "failed", reply)
    row = store._db.execute("SELECT status, failure, settled_at FROM runs WHERE idem_key='k'").fetchone()
    assert row[0] == "failed" and row[1] and row[2]
    assert not recovery.settle_from_hermes(store, "k", "working", "completed", {"output": "late"})  # checked


def test_a_followed_run_hermes_forgot_becomes_unconfirmed_once_quiet(tmp_path):
    store = StateStore(tmp_path / "s.sqlite3")
    _open(store, "fresh", "run_fresh", quiet_s=60)
    _open(store, "quiet", "run_quiet", quiet_s=recovery.ORPHAN_QUIET_S + 5)
    hermes = Hermes(forgotten={"run_fresh", "run_quiet"})
    done, forgotten = recovery.finished_followed(store, hermes, {"fresh": "run_fresh", "quiet": "run_quiet"}, {})
    assert done == [] and forgotten == ["quiet"]
