"""A task's run status has one list of states and one checked way to change it (runstate.py,
Store.transition): late events can't reopen a finished task, a restart leaves no task stuck half-way,
and every way a task settles also settles its card's steps and helpers. Invented data only."""
from __future__ import annotations

import asyncio
import time

import pytest

from speakeasy import activity as A
from speakeasy import calls, recovery, router, runstate as RS, service as service_mod
from speakeasy.calls import BackendRun
from speakeasy.store import StateStore
from test_routing import _progress_worker


def row(store, key):
    return store._db.execute("SELECT status, run_id, settled_at FROM runs WHERE idem_key=?", (key,)).fetchone()


def force(store, key, status, run_id=None, age_s=0.0):
    """Put a row in any state, as an older build or a crash left it."""
    with store._lock, store._db:
        store._db.execute("UPDATE runs SET status=?, run_id=?, updated=? WHERE idem_key=?",
                          (status, run_id, time.time() - age_s, key))


def task(store, key, request="Find why the checkout test fails"):
    store.reserve_run(key, "int_x", "call_" + key, 1)
    store.progress(key, "request", request)


class Hermes:
    """get_run answers from a table; anything else is a run Hermes doesn't know (after its restart)."""

    def __init__(self, runs=None):
        self.runs, self.asked = dict(runs or {}), []

    def get_run(self, run_id):
        self.asked.append(run_id)
        if run_id not in self.runs:
            raise RuntimeError("404")
        return self.runs[run_id]


# -- one list of states ----------------------------------------------------------------------------

def test_every_state_is_named_once_and_every_group_comes_from_it():
    assert len(RS.ALL) == 13 and {"ambiguous", "rejected", "waiting_on_task", "resolving_approval"} <= RS.ALL
    assert RS.TERMINAL | RS.OPEN | {"rejected"} == RS.ALL and not RS.TERMINAL & RS.OPEN
    # The hand-written sets in calls, service and router are the shared ones now.
    assert calls.ACTIVE_RUN_STATES is RS.LIVE
    assert service_mod.QUEUE_BEHIND == RS.LIVE - {"cancel_requested"}
    assert router._LIVE == {"admitting", "working", "running"}
    # Every state that isn't final has a way in and a way out.
    for state in RS.OPEN:
        assert RS.allowed_from(state), state
        assert any(state in RS.allowed_from(end) for end in RS.TERMINAL), state


def test_the_store_transition_is_a_compare_and_set(tmp_path):
    store = StateStore(tmp_path / "s.sqlite3")
    task(store, "k1")
    run = BackendRun("t1", 1, "k1")
    assert store.transition("k1", "running", run_id="run_1", mirror=run)
    assert (run.status, run.run_id) == ("running", "run_1") and row(store, "k1")[:2] == ("running", "run_1")
    assert store.transition("k1", "working", mirror=run)
    # The caller names what it expects; anything else is refused and nothing changes.
    assert not store.transition("k1", "completed", expect={"waiting_for_approval"})
    assert row(store, "k1")[0] == "working"
    assert store.transition("k1", "completed", mirror=run)
    assert run.status == "completed" and row(store, "k1")[2]  # settled stamp
    # Finished is final: nothing from the live states may follow it, nor the same end twice.
    for late in ("admitting", "running", "working", "waiting_for_approval", "cancel_requested",
                 "ambiguous", "waiting_on_task", "completed"):
        assert not store.update_run("k1", None, late), late
    assert row(store, "k1")[0] == "completed"
    # A memory copy that drifted is put back to the stored final status by a refused write.
    run.status = "working"
    assert not store.transition("k1", "working", mirror=run)
    assert run.status == "completed"
    with pytest.raises(ValueError):
        store.transition("k1", "finished")  # not a state


def test_a_final_event_corrects_an_interrupted_the_sweep_guessed(tmp_path):
    store = StateStore(tmp_path / "s.sqlite3")
    task(store, "k1")
    store.update_run("k1", "run_1", "running")
    assert store.transition("k1", "interrupted")
    assert store.transition("k1", "completed")  # Hermes' own answer arrived late
    assert not store.transition("k1", "interrupted")
    assert row(store, "k1")[0] == "completed"


# -- late events on a live call --------------------------------------------------------------------

def live(service, key="idem_r1", delegation="t_r1"):
    worker = _progress_worker(service)
    task(service.store, key)
    service.store.update_run(key, "run_" + key, "running")
    backend = BackendRun(delegation, 1, key, status="running")
    backend.run_id = "run_" + key
    worker.interaction.runs[delegation] = backend
    worker.interaction.latest_delegation_id = delegation
    return worker, backend


def test_a_late_working_event_never_reopens_a_completed_task(service):
    worker, backend = live(service)
    asyncio.run(worker._handle_hermes_event(backend, {"event": "run.completed", "output": "The test waits on a slow fake."}))
    assert backend.status == "completed" and row(service.store, backend.idem_key)[0] == "completed"
    # A tool event and a progress note that were still on their way when the run ended.
    asyncio.run(worker._handle_hermes_event(backend, {"event": "tool.started", "tool": "read_file",
                                                      "preview": "notes.md", "tool_call_id": "c9"}))
    asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim", "text": "STATUS: Checking"}))
    assert backend.status == "completed"
    assert row(service.store, backend.idem_key)[0] == "completed"
    assert service.store.work(idem_key=backend.idem_key)["status"] == "completed"


def test_a_second_final_event_changes_nothing(service):
    worker, backend = live(service)
    asyncio.run(worker._handle_hermes_event(backend, {"event": "run.completed", "output": "First answer."}))
    asyncio.run(worker._handle_hermes_event(backend, {"event": "run.failed", "error": "late"}))
    work = service.store.work(idem_key=backend.idem_key)
    assert work["status"] == "completed" and "First answer" in work["result"]["full"]
    assert backend.status == "completed"


def test_a_tool_event_after_stop_does_not_undo_the_stop(service):
    worker, backend = live(service)
    assert worker.move(backend, "cancel_requested")
    asyncio.run(worker._handle_hermes_event(backend, {"event": "tool.started", "tool": "terminal",
                                                      "preview": "pytest -q", "tool_call_id": "c1"}))
    assert backend.status == "cancel_requested" and row(service.store, backend.idem_key)[0] == "cancel_requested"
    asyncio.run(worker._handle_hermes_event(backend, {"event": "run.cancelled"}))
    assert backend.status == "cancelled" and row(service.store, backend.idem_key)[0] == "cancelled"


def test_stopping_a_task_that_just_finished_is_refused(service):
    worker, backend = live(service)
    force(service.store, backend.idem_key, "completed", backend.run_id)  # settled by another path
    with pytest.raises(calls.ServiceError):
        service._cancel(worker.interaction, backend, "int_x")
    assert backend.status == "completed" and row(service.store, backend.idem_key)[0] == "completed"


# -- restart: every half-way status is swept -------------------------------------------------------

OPEN_LEFT_BEHIND = sorted(RS.OPEN - {"ambiguous"})


@pytest.mark.parametrize("status", OPEN_LEFT_BEHIND)
def test_after_a_restart_a_task_hermes_finished_settles_with_its_answer(tmp_path, status):
    store = StateStore(tmp_path / "s.sqlite3")
    task(store, "k1")
    force(store, "k1", status, "run_1", age_s=5)
    hermes = Hermes({"run_1": {"status": "completed", "output": "The fake clock was never advanced."}})
    assert recovery.sweep_orphans(store, hermes, set(), started_at=time.time()) == [("k1", "completed")]
    work = store.work(idem_key="k1")
    assert work["status"] == "completed" and "fake clock" in work["result"]["full"]
    assert store.dismiss_key("k1")  # settled: the x clears it


@pytest.mark.parametrize("status", OPEN_LEFT_BEHIND)
def test_after_a_restart_a_run_hermes_forgot_is_unconfirmed_and_can_be_cleared(tmp_path, status):
    store = StateStore(tmp_path / "s.sqlite3")
    task(store, "k1")
    force(store, "k1", status, "run_1", age_s=5)
    assert recovery.sweep_orphans(store, Hermes(), set(), started_at=time.time()) == [("k1", "ambiguous")]
    assert row(store, "k1")[0] == "ambiguous"
    assert store.dismiss_key("k1")


@pytest.mark.parametrize("status", OPEN_LEFT_BEHIND)
def test_after_a_restart_a_task_that_never_reached_hermes_is_interrupted(tmp_path, status):
    store = StateStore(tmp_path / "s.sqlite3")
    task(store, "k1")
    force(store, "k1", status, None, age_s=5)
    assert recovery.sweep_orphans(store, Hermes(), set(), started_at=time.time()) == [("k1", "interrupted")]
    status_now, run_id, settled = row(store, "k1")
    assert status_now == "interrupted" and run_id and settled  # a local id: the x can clear it
    assert store.dismiss_key("k1")


def test_the_sweep_leaves_followed_new_and_thread_tasks_alone(tmp_path):
    store = StateStore(tmp_path / "s.sqlite3")
    for key in ("followed", "fresh", "thread", "quiet"):
        task(store, key)
    force(store, "followed", "working", "run_a", age_s=3600)
    force(store, "fresh", "admitting", None)          # this process's own, being set up
    force(store, "thread", "running", None, age_s=3600)
    store.set_thread("thread", "discord", "333", "a thread in #main")
    force(store, "quiet", "working", "run_q", age_s=3600)
    hermes = Hermes({"run_q": {"status": "failed", "error": "provider down"}})
    started = time.time() - 1
    assert recovery.sweep_orphans(store, hermes, {"followed"}, started_at=started) == [("quiet", "failed")]
    assert hermes.asked == ["run_q"]
    assert [row(store, k)[0] for k in ("followed", "fresh", "thread")] == ["working", "admitting", "running"]
    # Later passes only take rows whose follower went quiet for a while.
    force(store, "fresh", "running", "run_f", age_s=60)
    assert recovery.sweep_orphans(store, hermes, {"followed"}) == []
    force(store, "fresh", "running", "run_f", age_s=recovery.ORPHAN_QUIET_S + 5)
    assert recovery.sweep_orphans(store, hermes, {"followed"}) == [("fresh", "ambiguous")]


def test_the_service_sweeps_at_startup_and_skips_what_it_follows(service, monkeypatch):
    store = service.store
    worker, backend = live(service)
    task(store, "left")
    force(store, "left", "waiting_on_task", None, age_s=30)
    force(store, backend.idem_key, "working", backend.run_id, age_s=30)
    service._started_at = time.time()
    with service.lock:
        service.interactions["int_x"] = worker.interaction  # the followed task belongs to a live call
    monkeypatch.setattr(service.hermes, "get_run", lambda run_id: {"status": "running"}, raising=False)
    settled = service.settle_stuck_tasks(startup=True)
    assert ("left", "interrupted") in settled
    assert row(store, backend.idem_key)[0] == "working"


# -- every settle path settles the card ------------------------------------------------------------

def busy_card(store, key, now):
    A.observe(store, key, {"event": "tool.started", "tool": "terminal", "preview": "pytest -q tests",
                           "tool_call_id": "c1"}, now=now)
    A.observe(store, key, {"event": "subagent.start", "subagent_id": "sa1", "goal": "Read the build logs",
                           "child_session_id": "child_1"}, now=now)
    A.observe(store, key, {"event": "subagent.start", "subagent_id": "sa2", "goal": "Check the fixtures"}, now=now)
    public = A.public(A.tracker(store, key).state, detail=True)
    assert any(i["status"] == "running" for i in public["activity"])


def spinning(store, key):
    state = A.tracker(store, key).state
    items = [i for i in state.get("items") or [] if i.get("status") == "running"]
    helpers = [s for s in state.get("subagents") or [] if s.get("status") == "running"]
    return items, helpers


def test_the_orphan_sweep_settles_the_cards_steps_and_helpers(tmp_path):
    store = StateStore(tmp_path / "s.sqlite3")
    task(store, "k1")
    busy_card(store, "k1", time.time() - 20)
    force(store, "k1", "working", "run_1", age_s=5)
    hermes = Hermes({"run_1": {"status": "completed", "output": "Done: the fixture was stale."}})
    assert recovery.sweep_orphans(store, hermes, set(), started_at=time.time()) == [("k1", "completed")]
    assert spinning(store, "k1") == ([], [])
    # What the apps read (store.work) says the same.
    work = store.work(idem_key="k1", activity_detail=True) or {}
    assert not any(i.get("status") == "running" for i in work.get("activity") or [])


def test_the_thread_sweep_settles_the_cards_steps_and_helpers(tmp_path):
    from test_recovery import hermes_db, thread_task
    db = tmp_path / "state.db"
    hermes_db(db, [])
    store = StateStore(tmp_path / "s.sqlite3")
    thread_task(store, "k1", "Check the fixtures", age_s=recovery.NO_ANSWER_AFTER_S + 60)
    busy_card(store, "k1", time.time() - 20)
    force(store, "k1", "running", None, age_s=recovery.NO_ANSWER_AFTER_S + 60)
    assert recovery.sweep(store, db, lambda: (), {}) == [("k1", "interrupted")]
    assert spinning(store, "k1") == ([], [])


def test_a_late_sweep_never_overwrites_an_answer_that_arrived(tmp_path):
    store = StateStore(tmp_path / "s.sqlite3")
    task(store, "k1")
    force(store, "k1", "working", "run_1", age_s=5)
    hermes = Hermes({"run_1": {"status": "completed", "output": "Sweep's copy."}})
    real = hermes.get_run

    def finished_meanwhile(run_id):
        reply = real(run_id)
        store.set_result("k1", {"spoken": "Live answer.", "full": "Live answer."})
        store.transition("k1", "completed")  # the live follower got there first
        return reply
    hermes.get_run = finished_meanwhile
    assert recovery.sweep_orphans(store, hermes, set(), started_at=time.time()) == []
    assert store.work(idem_key="k1")["result"]["full"] == "Live answer."
