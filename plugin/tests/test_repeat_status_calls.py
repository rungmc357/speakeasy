"""Through a live (fake) call: a repeat status ask about a task that just finished is answered from
its result with no new run; the same ask hours later continues the old task's session; a different
subject still starts its own task. Invented subjects only."""
from __future__ import annotations

import time

from fakes import SDP, http, wait_for
from speakeasy import calls


def start_call(server, service, key):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def board(server):
    return http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]


def notes(worker):
    return [c for k, _, c in worker.sent if k == "session.commentary.append"]


def first_task(server, service, hermes, monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)
    hermes.responder = lambda prompt, sid: ("Build 12 report.\nDONE: Build 12 checked\nSPOKEN: Three fixes are merged and two are still running.")
    _, worker = start_call(server, service, "req_rs_1")
    worker.delegate("call_b12", "Check how build 12 is going: what's merged and what's left")
    wait_for(lambda: [t for t in board(server) if t["status"] == "completed"])
    wait_for(lambda: len(hermes.calls) == 1)
    key = next(iter(worker.interaction.runs.values())).idem_key
    return worker, key


def age(service, key, minutes):
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE runs SET settled_at=?, created=? WHERE idem_key=?",
                                  (time.time() - minutes * 60, time.time() - minutes * 60 - 120, key))


def test_a_repeat_ask_minutes_later_is_answered_from_the_result(server, service, hermes, monkeypatch):
    _, key = first_task(server, service, hermes, monkeypatch)
    age(service, key, 5)
    _, worker = start_call(server, service, "req_rs_2")
    worker.delegate("call_again", "where are we on build 12?")
    wait_for(lambda: any("Same as 5 minutes ago" in n for n in notes(worker)))
    time.sleep(0.4)
    assert len(hermes.calls) == 1  # no fresh investigation
    said = next(n for n in notes(worker) if "Same as" in n)
    assert "Three fixes are merged" in said and "check again fresh" in said
    # "Yeah, do it" to the offered fresh check continues the task's own session.
    old_session = service.store.session_for(key)
    worker.feed({"type": "session.output_transcript.delta", "start_ms": 3, "end_ms": 4,
                 "delta": "Same as five minutes ago: three fixes merged, two running. Want a fresh check?"})
    worker.delegate("call_yes", "yeah, do it")
    wait_for(lambda: len(hermes.calls) == 2)
    assert hermes.calls[-1]["session_id"] == old_session


def test_the_same_ask_hours_later_continues_the_old_session(server, service, hermes, monkeypatch):
    _, key = first_task(server, service, hermes, monkeypatch)
    old_session = service.store.session_for(key)
    assert old_session
    age(service, key, 180)
    _, worker = start_call(server, service, "req_rs_3")
    worker.delegate("call_later", "what's the status of build 12?")
    wait_for(lambda: len(hermes.calls) == 2)
    assert hermes.calls[-1]["session_id"] == old_session  # the session that already has the work
    assert not any("Same as" in n for n in notes(worker))


def test_a_different_subject_still_starts_its_own_task(server, service, hermes, monkeypatch):
    _, key = first_task(server, service, hermes, monkeypatch)
    old_session = service.store.session_for(key)
    age(service, key, 5)
    _, worker = start_call(server, service, "req_rs_4")
    worker.delegate("call_other", "where are we on the kitchen quote?")
    wait_for(lambda: len(hermes.calls) == 2)
    assert hermes.calls[-1]["session_id"] != old_session
    assert not any("Same as" in n for n in notes(worker))


def test_an_answer_that_came_in_after_hang_up_opens_the_next_ask_on_it(server, service, hermes, monkeypatch):
    _, key = first_task(server, service, hermes, monkeypatch)
    age(service, key, 40)  # finished 40 min ago: too old for "same as", but the user never heard it
    with service.store._lock:
        settled = service.store._db.execute("SELECT settled_at FROM runs WHERE idem_key=?", (key,)).fetchone()[0]
    hung_up = settled - 60
    monkeypatch.setattr(calls, "_prior_call_end", lambda store: hung_up)
    _, worker = start_call(server, service, "req_rs_5")
    worker.delegate("call_late", "where are we on build 12?")
    wait_for(lambda: any("came in after you hung up" in n for n in notes(worker)))
    time.sleep(0.3)
    assert len(hermes.calls) == 1
