"""A plugin reload (or a Hermes restart) carries on with open work instead of throwing it away.

SPK-296: a job held behind another job on the same project stays held across a reload and starts on
its own when that job ends, or at once when it already has. SPK-270: running jobs no longer hold an
update back; the new code follows each running Hermes run on from where the old code left off, and
only a live call (or state that lives only in memory) defers the automatic reload.
"""
from __future__ import annotations

import time

import pytest

from fakes import FakeLiveWorker, FakeTransport, http, wait_for
from speakeasy import calls
from speakeasy.calls import BackendRun, Interaction


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "FENCE_POLL_S", 0.05)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "shopfront"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    return root


def _service(home):
    """A second VoiceService on the same home: the code a reload (or a restart) brings up."""
    from speakeasy.service import VoiceService
    return VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                        codex_login=lambda binary: (True, "Signed in to Codex."),
                        openai_negotiate=lambda key, payload: {"session": {"id": "s"}, "transport": {"sdp": "v=0\r\n"}},
                        openai_worker=lambda rt, interaction: FakeLiveWorker(rt, interaction))


def _job(server, text):
    status, body = http(server.base_url, "POST", "/voice/work", {"text": text}, server.token)
    assert status == 202, body
    return body


def _row(service, status):
    return next((r for r in service.store.open_runs() if r["status"] == status), None)


def _held_job(server, service, hermes, repo):
    hermes.hold = True
    _job(server, f"Fix the checkout bug in {repo}")
    wait_for(lambda: len(hermes.calls) == 1)
    _job(server, f"refactor the settings page in {repo}/src")
    return wait_for(lambda: _row(service, "waiting_on_task"))


def _reload(service, home):
    """What a reload does: the old service hands its runs over and closes, the new one starts."""
    service.close()
    new = _service(home)
    new.resume_open_work()
    new.settle_stuck_tasks(startup=True)
    return new


def test_a_held_job_stays_held_across_a_reload_and_starts_when_its_blocker_ends(server, service, hermes, home, repo):
    held = _held_job(server, service, hermes, repo)
    first = hermes.calls[0]["run_id"]
    new = _reload(service, home)
    try:
        # Still held (not "interrupted" by the startup sweep), still saying what it waits for.
        assert new.store.run_status(held["key"]) == "waiting_on_task"
        assert (new.store.waiting_of(held["key"]) or {}).get("project") == "shopfront"
        assert len(hermes.calls) == 1                                  # not started on top of it
        # The blocker is followed by the new code: when it finishes, the held job starts.
        hermes.runs[first]["done"].set()
        wait_for(lambda: new.store.run_status(hermes.calls[0]["idem"]) == "completed")
        wait_for(lambda: len(hermes.calls) == 2)
        assert "settings page" in str(hermes.calls[1]["input"])
        assert hermes.calls[1]["idem"] != hermes.calls[0]["idem"]
        wait_for(lambda: new.store.run_status(held["key"]) in {"running", "working", "completed"})
    finally:
        new.close()


def test_a_held_job_whose_blocker_is_gone_starts_right_after_a_restart(server, service, hermes, home, repo):
    held = _held_job(server, service, hermes, repo)
    first = hermes.calls[0]["run_id"]
    # Hermes restarted: the blocker's run is gone from Hermes and the plugin starts fresh.
    service.close()
    hermes.runs.pop(first)
    new = _service(home)
    try:
        new.resume_open_work()
        new.settle_stuck_tasks(startup=True)
        wait_for(lambda: len(hermes.calls) == 2)
        assert "settings page" in str(hermes.calls[1]["input"])
        assert new.store.run_status(held["key"]) != "interrupted"
    finally:
        new.close()


def test_a_running_job_carries_on_through_a_reload_and_its_answer_lands_once(server, service, hermes, home):
    hermes.hold = True
    _job(server, "Find a quiet cafe near the office")
    wait_for(lambda: len(hermes.calls) == 1)
    key, run_id = hermes.calls[0]["idem"], hermes.calls[0]["run_id"]
    wait_for(lambda: service.store.run_status(key) == "running")
    assert service.busy() is False                      # a running job never holds an update back
    new = _reload(service, home)
    try:
        assert new.store.run_status(key) == "running"   # followed, not settled as lost
        hermes.runs[run_id]["done"].set()
        wait_for(lambda: new.store.run_status(key) == "completed")
        work = new.store.work(idem_key=key) or {}
        assert work.get("result", {}).get("full") or work.get("result", {}).get("spoken") or work["status"] == "completed"
        assert len(hermes.calls) == 1                   # never started again
    finally:
        new.close()


def test_only_a_live_call_or_memory_only_state_holds_back_a_reload(service):
    assert service.busy() is False
    call = Interaction("int_a", "sess_a", started_at=time.time() - 120)   # connected over CALL_SETTLE_S ago
    service.interactions["int_a"] = call
    assert service.busy() is True                       # a live call
    call.call_closed = True
    assert service.busy() is False
    call.runs["d1"] = BackendRun("d1", 1, "idem_1", run_id="run_0001", status="running")
    assert service.busy() is False                      # a running job: carries on through it
    call.runs["d2"] = BackendRun("d2", 1, "idem_2", status="waiting_on_task")
    assert service.busy() is False                      # a held job: stays held through it
    call.runs["d3"] = BackendRun("d3", 1, "idem_3", status="admitting")
    assert service.busy() is True                       # being handed to Hermes right now
    call.runs["d3"].status = "waiting_for_approval"
    assert service.busy() is True                       # its question's request lives in memory
    call.runs["d3"] = BackendRun("d3", 1, "idem_3", run_id=calls.LOCAL_RUN_PREFIX + "x", status="running")
    assert service.busy() is True                       # a chat-thread turn: followed by a thread wait
    call.runs["d3"].status = "completed"
    assert service.busy() is False


def test_the_handover_saves_where_each_stream_got_to(service):
    worker = service.text_worker()
    run = BackendRun("d1", 0, "idem_seq", run_id="run_0009", status="working")
    worker.interaction.runs["d1"] = run
    worker._saw_seq(run, {"event": "tool.started", "seq": 7})
    worker._saw_seq(run, {"event": "tool.started", "seq": 3})  # out of order: the newest stays
    assert service.hand_over_runs() == {"idem_seq": 7}
    assert worker.detached is True
