"""QA74 #20: an app relaunched while a call is paused had no way to learn about it (``call`` names
only live calls), so the paused conversation was lost. Home now names the paused, still-resumable
call in ``paused_call``; never one that was resumed, carried on by a later call or expired.
Invented data only."""
from __future__ import annotations

import time
import types

from speakeasy.calls import Interaction
from test_history import get, state  # noqa: F401


def _paused(service, interaction_id="vi_paused", device="dev_phone", reason="user", minutes=None, at=None):
    call = Interaction(interaction_id=interaction_id, live_session_id="live_" + interaction_id)
    call.status = "active"
    call.worker = types.SimpleNamespace(loop=None, pending_approvals=lambda: [])
    call.device_id = device
    call.call_closed = True
    call.paused = True
    call.pause_reason = reason
    call.idle_paused_minutes = minutes
    call.paused_at = at if at is not None else time.time()
    with service.lock:
        service.interactions[interaction_id] = call
    return call


def test_home_names_a_paused_call_so_a_relaunched_app_can_resume_it(server, service, state):
    assert get(server, "/voice/now")[1]["paused_call"] is None
    _paused(service)
    body = get(server, "/voice/now")[1]
    assert body["call"] is None
    assert body["paused_call"]["interaction_id"] == "vi_paused"
    assert body["paused_call"]["pause_reason"] == "user"
    assert body["paused_call"]["device_id"] == "dev_phone"
    assert "idle_paused_minutes" not in body["paused_call"]


def test_a_quiet_pause_says_how_long_and_the_newest_pause_wins(server, service, state):
    now = time.time()
    _paused(service, "vi_old", at=now - 120)
    _paused(service, "vi_quiet", reason="idle", minutes=4, at=now - 5)
    paused = get(server, "/voice/now")[1]["paused_call"]
    assert paused["interaction_id"] == "vi_quiet"
    assert paused["pause_reason"] == "idle" and paused["idle_paused_minutes"] == 4


def test_a_resumed_carried_expired_or_abandoned_call_is_not_offered(server, service, state):
    resumed = _paused(service, "vi_resumed")
    resumed.successor = Interaction(interaction_id="vi_next", live_session_id="live_next")
    carried = _paused(service, "vi_carried")
    carried.carried_by = Interaction(interaction_id="vi_later", live_session_id="live_later")
    expired = _paused(service, "vi_expired")
    expired.pause_bookkept = True
    gave_up = _paused(service, "vi_abandoned")
    gave_up.abandoned = True
    assert get(server, "/voice/now")[1]["paused_call"] is None


def test_a_pause_records_when_it_happened(service):
    call = Interaction(interaction_id="vi_live", live_session_id="live_vi_live")
    call.status = "active"
    with service.lock:
        service.interactions["vi_live"] = call
    before = time.time()
    service.pause("vi_live")
    assert call.paused and call.paused_at is not None and call.paused_at >= before
