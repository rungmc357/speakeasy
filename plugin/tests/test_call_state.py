"""A call's life in one place (SPK-81): CallState and pause_reason, one pause per call, the idle and
cap timers judged on the reconciled run state, and closing a session the app gave up on before it
went live. Invented requests throughout."""
from __future__ import annotations

import time

from fakes import SDP, http, wait_for
from speakeasy import calls
from speakeasy.calls import BackendRun, CallState, run_may_be_working


def start_call(server, service, key):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def say(worker, delegation_id, words, at):
    worker.feed({"type": "session.input_transcript.delta", "delta": words, "start_ms": at, "end_ms": at + 1})
    worker.feed({"type": "session.delegation.created", "offset_ms": at + 5,
                 "delegation": {"id": delegation_id, "target": "client"}})


# -- the reconciled run state ----------------------------------------------------------------------

def test_a_dropped_stream_may_still_be_working():
    run = BackendRun("d1", 1, "k1", run_id="run_a", status="ambiguous")
    assert run_may_be_working(run, "ambiguous") is True
    assert run_may_be_working(run, "completed") is False  # recovery settled it


def test_memory_active_but_stored_finished_is_not_working():
    run = BackendRun("d1", 1, "k1", run_id="run_a", status="working")
    assert run_may_be_working(run, "working") is True
    assert run_may_be_working(run, "completed") is False
    assert run_may_be_working(run, "interrupted") is False


def test_a_stuck_admission_stops_keeping_the_call_up():
    run = BackendRun("d1", 1, "k1", status="admitting")
    assert run_may_be_working(run, None, now=run.started + 5) is True
    assert run_may_be_working(run, None, now=run.started + calls.ADMITTING_KEEPS_CALL_S + 1) is False
    run.run_id = "run_b"  # admitted: Hermes has it
    assert run_may_be_working(run, None, now=run.started + calls.ADMITTING_KEEPS_CALL_S + 1) is True


def test_idle_pause_waits_while_a_dropped_stream_may_still_be_working(server, service, hermes):
    hermes.hold = True
    session, worker = start_call(server, service, "req_cs_1")
    say(worker, "call_draft", "Draft the quarterly garden newsletter", 10)
    run = wait_for(lambda: next((r for r in worker.interaction.runs.values() if r.run_id), None))
    # The run's stream dropped: memory and the store both say ambiguous until recovery settles it.
    run.status = "ambiguous"
    service.store.update_run(run.idem_key, run.run_id, "ambiguous")
    interaction = service.interaction(session["interaction_id"])
    minutes = service.settings.get()["idle_pause_minutes"]
    interaction.last_activity = time.monotonic() - minutes * 60 - 5
    assert service.idle_check(now=time.monotonic()) == []  # Hermes may still be answering
    assert worker.cap_reached(1900, 1800) is False
    # Recovery settles it: the call is quiet now.
    service.store.update_run(run.idem_key, run.run_id, "completed")
    assert session["interaction_id"] in service.idle_check(now=time.monotonic())
    assert interaction.pause_reason == "idle" and interaction.call_state is CallState.PAUSED


def test_idle_pause_ignores_a_task_memory_still_thinks_runs(server, service, hermes):
    hermes.hold = True
    session, worker = start_call(server, service, "req_cs_2")
    say(worker, "call_tidy", "Tidy the shared photo folder", 10)
    run = wait_for(lambda: next((r for r in worker.interaction.runs.values() if r.run_id), None))
    service.store.update_run(run.idem_key, run.run_id, "completed")  # stored finished, memory stale
    interaction = service.interaction(session["interaction_id"])
    minutes = service.settings.get()["idle_pause_minutes"]
    interaction.last_activity = time.monotonic() - minutes * 60 - 5
    assert session["interaction_id"] in service.idle_check(now=time.monotonic())


# -- CallState and one pause per call --------------------------------------------------------------

def test_call_state_and_pause_reason_through_pause_and_resume(server, service, hermes):
    session, worker = start_call(server, service, "req_cs_3")
    interaction = service.interaction(session["interaction_id"])
    assert interaction.call_state in (CallState.CONNECTING, CallState.LIVE)
    reply = service.pause(session["interaction_id"], limit=True)
    assert reply["pause_reason"] == "cap" and reply["limit_paused"] is True
    snap = interaction.snapshot()
    assert snap["call_state"] == "paused" and snap["pause_reason"] == "cap"
    # A second pause (the idle check racing the cap) keeps the first reason and starts nothing new.
    again = service.pause(session["interaction_id"], idle_minutes=15)
    assert again["pause_reason"] == "cap" and "idle_paused_minutes" not in again
    assert interaction.idle_paused_minutes is None and interaction.limit_paused is True


def test_repeat_pause_starts_one_expiry_timer(server, service, hermes, monkeypatch):
    session, _ = start_call(server, service, "req_cs_4")
    started = []

    class CountingTimer:
        def __init__(self, interval, fn, args=()):
            started.append(interval)
            self.daemon = True

        def start(self):
            pass

    import speakeasy.service as service_mod
    monkeypatch.setattr(service_mod.threading, "Timer", CountingTimer)
    service.pause(session["interaction_id"])
    service.pause(session["interaction_id"])
    service.pause(session["interaction_id"], idle_minutes=10)
    assert len(started) == 1


# -- a session the app gave up on --------------------------------------------------------------------

def test_abandon_closes_a_call_that_never_went_live(server, service, hermes):
    session, worker = start_call(server, service, "req_cs_5")
    stopped = []
    worker.stop_transport = lambda: stopped.append(True)
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/end",
                        {"abandon": True}, server.token)
    assert status == 200 and body["abandoned"] is True
    assert stopped == [True]
    interaction = service.interaction(session["interaction_id"])
    assert interaction.call_state is CallState.ENDED
    assert interaction.snapshot()["call_state"] == "ended"


def test_abandon_leaves_a_paused_call_alone(server, service, hermes):
    session, worker = start_call(server, service, "req_cs_6")
    service.pause(session["interaction_id"])
    stopped = []
    worker.stop_transport = lambda: stopped.append(True)
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/end",
                        {"abandon": True}, server.token)
    assert status == 200 and body["abandoned"] is False
    assert stopped == []
    service.resumable(session["interaction_id"])  # still resumable


def test_plain_end_still_finishes_the_transport(server, service, hermes):
    session, _ = start_call(server, service, "req_cs_7")
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/end",
                        {}, server.token)
    assert status == 200 and "finalization" in body
