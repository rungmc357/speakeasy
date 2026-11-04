"""Mic-aware narration: with the mic open the user hears a progress line at least every
UNMUTED_EVERY_S, news or not; muted or unreported keeps the classic gate (news only, one per
PROGRESS_EVERY_S) byte for byte."""
from __future__ import annotations

import asyncio
import time

import pytest

from fakes import SDP, FakeLiveWorker, http
from speakeasy import calls
from speakeasy.calls import BackendRun, Interaction


def worker(service, muted=None):
    interaction = Interaction("int_x", "sess_x")
    interaction.mic_muted = muted
    return FakeLiveWorker(service.rt, interaction), interaction


def spoken(fake):
    return [c for k, _, c in fake.sent if k == "session.commentary.append"]


def running(age: float) -> BackendRun:
    backend = BackendRun("t1", 1, "idem_1", status="running")
    backend.started -= age
    return backend


def say(fake, backend, milestone="Pulling this week's events"):
    return asyncio.run(fake.maybe_speak_progress(backend, milestone))


# -- unmuted: first line early ---------------------------------------------------------------------

def test_unmuted_speaks_after_twelve_seconds(service):
    fake, _ = worker(service, muted=False)
    backend = running(calls.UNMUTED_FIRST_S + 1)
    backend.activity, backend.activity_count = ["Pulling this week's events"], 1
    assert say(fake, backend) is True
    assert len(spoken(fake)) == 1


def test_unmuted_still_quiet_before_twelve_seconds(service):
    fake, _ = worker(service, muted=False)
    backend = running(calls.UNMUTED_FIRST_S - 2)
    backend.activity, backend.activity_count = ["Pulling this week's events"], 1
    assert say(fake, backend) is False
    assert spoken(fake) == []


def test_unmuted_never_speaks_twice_inside_the_spacing(service):
    fake, interaction = worker(service, muted=False)
    backend = running(calls.UNMUTED_FIRST_S + 1)
    backend.activity, backend.activity_count = ["Pulling this week's events"], 1
    assert say(fake, backend) is True
    backend.activity_count = 2
    assert say(fake, backend, "Comparing four venues") is False
    assert len(spoken(fake)) == 1


# -- the change: a guaranteed line at least every UNMUTED_EVERY_S while listening --------------------

def test_unmuted_speaks_again_after_the_gap_with_no_news(service):
    fake, interaction = worker(service, muted=False)
    backend = running(calls.UNMUTED_FIRST_S + 1)
    backend.activity, backend.activity_count = ["Pulling this week's events"], 1
    assert say(fake, backend) is True
    # Nothing new reported for another UNMUTED_EVERY_S: a listening user still hears a line.
    interaction.progress_spoken_at = time.monotonic() - (calls.UNMUTED_EVERY_S + 1)
    assert say(fake, backend) is True
    assert len(spoken(fake)) == 2


def test_unmuted_heartbeat_is_optional_news_is_not_required(service):
    """A task whose activity is long stale still narrates while the mic is open."""
    fake, interaction = worker(service, muted=False)
    backend = running(120)
    backend.activity, backend.activity_count, backend.told_count = ["Reading the calendar"], 1, 1
    assert say(fake, backend) is True
    assert spoken(fake)[0]


# -- muted / unknown: today's behaviour, unchanged ---------------------------------------------------

def test_muted_keeps_the_forty_five_second_first_line(service):
    fake, _ = worker(service, muted=True)
    backend = running(20)
    backend.activity, backend.activity_count = ["Pulling this week's events"], 1
    assert say(fake, backend) is False


def test_muted_needs_news_even_after_the_gap(service):
    fake, interaction = worker(service, muted=True)
    backend = running(120)
    backend.activity, backend.activity_count, backend.told_count = ["Reading the calendar"], 1, 1
    interaction.progress_spoken_at = time.monotonic() - (calls.PROGRESS_EVERY_S + 1)
    assert say(fake, backend) is False
    assert spoken(fake) == []


def test_unknown_mic_behaves_exactly_like_muted(service):
    fake, interaction = worker(service, muted=None)
    backend = running(120)
    backend.activity, backend.activity_count, backend.told_count = ["Reading the calendar"], 1, 1
    interaction.progress_spoken_at = time.monotonic() - (calls.PROGRESS_EVERY_S + 1)
    assert say(fake, backend) is False
    backend.activity_count = 2
    assert say(fake, backend, "Comparing four venues") is True


def test_progress_setting_still_mutes_everything(service):
    service.settings.patch({"speech": {"progress": False}})
    fake, _ = worker(service, muted=False)
    backend = running(120)
    backend.activity, backend.activity_count = ["Pulling this week's events"], 1
    assert say(fake, backend) is False
    assert spoken(fake) == []


# -- the route ---------------------------------------------------------------------------------------

def start_call(server, service, key="req_mic_1"):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


@pytest.mark.parametrize("muted", [True, False])
def test_mic_report_lands_on_the_interaction(server, service, muted):
    session, _ = start_call(server, service, key=f"req_mic_{muted}")
    status, body = http(server.base_url, "POST",
                        f"/voice/interactions/{session['interaction_id']}/mic", {"muted": muted}, server.token)
    assert status == 200, body
    assert body["mic_muted"] is muted
    snapshot = http(server.base_url, "GET", f"/voice/interactions/{session['interaction_id']}", token=server.token)[1]
    assert snapshot["mic_muted"] is muted


@pytest.mark.parametrize("body", [{"muted": "yes"}, {"muted": 1}, {}, {"muted": True, "extra": 1}, {"on": True}])
def test_mic_report_rejects_anything_but_a_bool(server, service, body):
    session, _ = start_call(server, service)
    status, reply = http(server.base_url, "POST",
                         f"/voice/interactions/{session['interaction_id']}/mic", body, server.token)
    assert status == 400, reply


def test_mic_report_for_an_unknown_interaction_is_404(server, service):
    start_call(server, service)
    status, _ = http(server.base_url, "POST", "/voice/interactions/se_missing/mic", {"muted": True}, server.token)
    assert status == 404
