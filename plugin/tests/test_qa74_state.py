"""QA 74 lane A state-machine fixes (plugin half).

#1  a dropped call (its connection closed, nobody paused it) resumes as the same conversation.
#11 an approval answered anywhere closes its Home stack row as done, never "skipped".
#12 a reply belongs to the user message it answers (reply_to), never the next one.
#10 after a restart, a still-waiting approval is armed again, or says it expired: never dead buttons.
#23 re-pairing the same phone leaves one push target, not two.
"""
from __future__ import annotations

import pytest

from fakes import SDP, http, wait_for
from speakeasy import calls
from speakeasy import home_state as HS
from speakeasy import recovery
from test_b73_home_state import NOON, _msg, _now, frozen  # noqa: F401
from test_reload_keeps_work import _reload
from test_tasks import start_call
from test_voice_approvals import _waiting, say

NOW = 1_800_000_000.0


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)


# -- #12 ---------------------------------------------------------------------------------------------

def test_a_late_answer_to_an_older_message_is_not_the_reply_to_the_newer_one():
    entries = [
        _msg("m_a", "user", NOW, None),
        _msg("r_ack", "assistant", NOW + 1, None, part="ack", reply_to="m_a"),
        _msg("m_b", "user", NOW + 10, None),
        _msg("r_b", "assistant", NOW + 11, None, reply_to="m_b"),
        _msg("r_a", "assistant", NOW + 30, "run_a", reply_to="m_a"),   # A's slow answer, and A's job
    ]
    cur = HS.current_exchange(entries, NOW + 40)
    assert cur["message_id"] == "m_b"
    assert cur["reply_ids"] == ["r_b"]
    assert "run_a" not in cur["run_ids"]


def test_replies_without_a_reply_to_still_count_for_the_latest_message():
    entries = [_msg("m_a", "user", NOW, None), _msg("r_a", "assistant", NOW + 2, "run_a")]
    cur = HS.current_exchange(entries, NOW + 5)
    assert cur["reply_ids"] == ["r_a"] and cur["run_ids"] == ["run_a"]


def test_home_chat_rows_say_which_message_they_answer():
    from speakeasy.home_chat import HomeChat
    base = {"text": "x", "at": NOW, "run_id": None, "part": None, "client_id": None, "pending": None}
    answer = HomeChat.entry({**base, "id": "h_2", "role": "assistant", "reply_to": "h_1"})
    asked = HomeChat.entry({**base, "id": "h_1", "role": "user", "reply_to": None})
    assert answer["reply_to"] == "m:h_1"
    assert "reply_to" not in asked


# -- #1 ----------------------------------------------------------------------------------------------

def test_reconnect_of_a_dropped_call_resumes_the_same_conversation(server, service, hermes):
    session, worker = start_call(server, service, "qa74_drop_1")
    worker.feed({"type": "session.input_transcript.delta", "delta": "Plan the Rome trip", "start_ms": 1, "end_ms": 2})
    worker.call_closed()                       # transport dropped; nobody pressed pause
    status, body = http(server.base_url, "POST", "/voice/sessions",
                        {"sdp": SDP, "resume_from": session["interaction_id"]}, server.token,
                        {"Idempotency-Key": "qa74_drop_2"})
    assert status == 201, (status, body)
    assert body["resumed_from"] == session["interaction_id"]
    history = service.pause_history(service.interaction(session["interaction_id"]))
    assert any("Rome trip" in t["text"] for t in history)
    # Resumed once: a second resume of the same call is refused.
    status, _ = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, "resume_from": session["interaction_id"]},
                     server.token, {"Idempotency-Key": "qa74_drop_3"})
    assert status == 409


def test_a_reconnect_before_the_plugin_saw_the_drop_pauses_the_old_leg_and_resumes(server, service, hermes):
    session, worker = start_call(server, service, "qa74_drop_live_1")
    worker.feed({"type": "session.input_transcript.delta", "delta": "Book the dentist", "start_ms": 1, "end_ms": 2})
    status, body = http(server.base_url, "POST", "/voice/sessions",
                        {"sdp": SDP, "resume_from": session["interaction_id"]}, server.token,
                        {"Idempotency-Key": "qa74_drop_live_2"})
    assert status == 201, (status, body)
    source = service.interaction(session["interaction_id"])
    assert source.successor is not None and body["resumed_from"] == session["interaction_id"]


def test_a_call_that_was_ended_is_not_resumed(server, service, hermes):
    session, worker = start_call(server, service, "qa74_end_1")
    worker.handle({"type": "session.closed"}) if hasattr(worker, "handle") else None
    interaction = service.interaction(session["interaction_id"])
    with interaction.lock:
        interaction.call_closed = True
        interaction.finalization = "confirmed"     # End, not a drop
    status, _ = http(server.base_url, "POST", "/voice/sessions",
                     {"sdp": SDP, "resume_from": session["interaction_id"]}, server.token,
                     {"Idempotency-Key": "qa74_end_2"})
    assert status == 409


# -- #11 ---------------------------------------------------------------------------------------------

def _decision_row(service):
    body = _now(service)
    decisions = [i for i in body["stack"]["items"] if i.get("kind") == "decision"]
    assert decisions, body["stack"]
    return decisions[0]["id"]


def test_an_approval_answered_on_the_call_leaves_the_stack_as_done(server, service, hermes, frozen):  # noqa: F811
    _, worker = _waiting(server, service, hermes)
    sid = _decision_row(service)
    say(worker, "call_ok", "approved")
    wait_for(lambda: hermes.approvals)
    wait_for(lambda: not worker.pending_approvals())
    frozen(NOON + 5)
    _now(service)
    assert service.home_state.row(sid)["state"] == HS.DONE


def test_an_approval_answered_from_the_call_screen_leaves_the_stack_as_done(server, service, hermes, frozen):  # noqa: F811
    session, worker = _waiting(server, service, hermes)
    sid = _decision_row(service)
    _, run = worker.pending_approvals()[0]
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/approval",
                        {"choice": "once", "run_id": run.run_id, "request_id": run.approval["request_id"]},
                        server.token)
    assert status == 200, body
    frozen(NOON + 5)
    _now(service)
    assert service.home_state.row(sid)["state"] == HS.DONE
    assert not [r for r in service.home_state._rows() if r["state"] == HS.SKIPPED]


# -- #10 ---------------------------------------------------------------------------------------------

def _waiting_job(server, service, hermes, text="Tidy the old garden photos"):
    hermes.approval_first = True
    status, started = http(server.base_url, "POST", "/voice/work", {"text": text}, server.token)
    assert status == 202, started
    run_id = started["run_id"]
    key = wait_for(lambda: service.store.key_for_run(run_id))
    wait_for(lambda: service.store.run_status(key) == "waiting_for_approval")
    return run_id, key


def test_after_a_restart_a_still_waiting_approval_is_armed_again(server, service, hermes, home):
    run_id, key = _waiting_job(server, service, hermes)
    request = {"event": "approval.request", "request_id": "apr_" + run_id, "description": "Delete an old file",
               "choices": ["once", "deny"]}
    hermes.runs[run_id].update(status="waiting_for_approval", approval=request)
    new = _reload(service, home)
    try:
        assert new.store.run_status(key) == "waiting_for_approval"
        armed = wait_for(lambda: new._session_approvals([key]))
        assert armed[0]["run_id"] == run_id and armed[0]["choices"] == ["once", "deny"]
        sid = new.store.session_for(key)
        got = new.history_approval(sid, {"run_id": run_id, "choice": "once"})
        assert got["choice"] == "once"
        assert hermes.approvals[-1]["request_id"] == "apr_" + run_id
    finally:
        new.close()


def test_after_a_restart_an_approval_hermes_no_longer_holds_says_it_expired(server, service, hermes, home):
    run_id, key = _waiting_job(server, service, hermes, "Clear the old build logs")
    hermes.runs.pop(run_id)                     # Hermes restarted too: the request is gone
    new = _reload(service, home)
    try:
        assert new.store.run_status(key) == "interrupted"
        items = [i for i in new.now({"tz": "America/New_York"}).get("items", []) if i.get("run_id") == run_id]
        assert all(not (i.get("question") or {}).get("shape") == "approval" for i in items), items
    finally:
        new.close()


def test_a_waiting_row_with_no_held_approval_never_shows_buttons(server, service, hermes):
    run_id, key = _waiting_job(server, service, hermes, "Archive the March invoices")
    for worker in service._workers():
        with worker.interaction.lock:
            for run in worker.interaction.runs.values():
                run.approval = None          # what a restart left: the request id is gone
    items = [i for i in service.now({"tz": "America/New_York"}).get("items", []) if i.get("run_id") == run_id]
    assert items, "the job is still on Home"
    assert all(i["kind"] != "question" for i in items), items
    assert any(i.get("line") == __import__("speakeasy.now", fromlist=["x"]).APPROVAL_EXPIRED_LINE for i in items)


def test_the_orphan_sweep_settles_an_unanswerable_approval_as_expired(service):
    class Gone:
        def get_run(self, run_id):
            raise RuntimeError("no run")
    store = service.store
    rows = [{"key": "k_appr", "status": "waiting_for_approval", "run_id": "run_gone", "updated": 0.0}]
    seen = []
    store_open_runs, store.open_runs = store.open_runs, lambda: rows
    store_transition = store.transition
    store.transition = lambda key, to, expect=None, **kw: seen.append((key, to)) or True
    store_progress, store.progress = store.progress, lambda *a, **k: seen.append(a)
    try:
        recovery.sweep_orphans(store, Gone(), set(), started_at=10.0, now=20.0)
    finally:
        store.open_runs, store.transition, store.progress = store_open_runs, store_transition, store_progress
    assert ("k_appr", "interrupted") in seen
    assert ("k_appr", "result", recovery.APPROVAL_EXPIRED_NOTE) in seen


# -- #23 ---------------------------------------------------------------------------------------------

def test_one_phone_token_is_one_push_target_after_re_pairing(service):
    devices = service.devices
    a, _ = devices.redeem(devices.new_pairing_code(), "iPhone")
    b, _ = devices.redeem(devices.new_pairing_code(), "iPhone")
    token = "a1b2c3d4" * 8
    assert devices.set_push(a, token, "sandbox", ["needs_you"])
    assert devices.set_push(b, token, "sandbox", ["needs_you"])
    assert list(devices.push_targets()) == [b]
    other = "f0e1d2c3" * 8
    assert devices.set_push(a, other, "sandbox", ["needs_you"])
    assert sorted(devices.push_targets()) == sorted([a, b])
