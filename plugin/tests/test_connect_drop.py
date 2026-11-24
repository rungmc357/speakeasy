"""SPK-581: a provider drop before a call ever went live is a failed connect, not a dropped call.

A call attempt stalled while connecting: the phone's audio never came up and nobody said a word.
Codex then reported its realtime session closed. The plugin treated it like a drop under a live
conversation: it held the call 45 s for a reconnect, published "Reconnecting…", got no resume and
logged ``call.end reason=provider``, so the call watch counted a failed connect as a provider drop.

Now a drop that arrives before the call went live ends the attempt at once as ``connect_failed``
with a plain "Couldn't connect" line. A never-live call nobody is attached to (no event stream, no
request from the app) is closed with its voice session as ``unattended``. A drop on a live call
still holds, reconnects and resumes (test_call_drop.py). All data here is invented.
"""
from __future__ import annotations

import logging
import queue
import time

import pytest

from fakes import SDP, FakeTransport, http, wait_for
from speakeasy import calls as calls_mod
from speakeasy import service as service_mod
from speakeasy.plain_errors import CONNECT_FAILED


class DropTransport(FakeTransport):
    """A Codex app-server stand-in whose notifications the test sends."""

    def __init__(self) -> None:
        super().__init__()
        self.notifications: queue.Queue = queue.Queue()
        self.stops = 0

    def say(self, role: str, text: str) -> None:
        self.notifications.put({"method": "thread/realtime/transcript/done",
                                "params": {"threadId": "thr_fake", "role": role, "text": text}})

    def provider_reset(self) -> None:
        self.notifications.put({"method": "thread/realtime/closed", "params": {"threadId": "thr_fake"}})

    def stop(self) -> None:
        self.stops += 1
        super().stop()


@pytest.fixture
def codex(service):
    service.settings.patch({"voice": {"provider": "codex"}})
    transports: list[DropTransport] = []
    service._codex_factory = lambda: transports.append(DropTransport()) or transports[-1]
    return transports


def open_call(server, key: str, resume_from: str | None = None) -> dict:
    body = {"sdp": SDP, **({"resume_from": resume_from} if resume_from else {})}
    status, session = http(server.base_url, "POST", "/voice/sessions", body, server.token, {"Idempotency-Key": key})
    assert status == 201, session
    return session


def events(interaction, kind: str) -> list:
    return [payload for _, k, payload in list(interaction.feed.ring) if k == kind]


def test_a_drop_during_connect_ends_the_attempt_at_once(server, service, codex, caplog):
    caplog.set_level(logging.INFO, logger="speakeasy")
    interaction = service.interaction(open_call(server, "req_connect_drop_1")["interaction_id"])
    wait_for(lambda: interaction.status == "listening", 5)  # the voice side is up; the phone's audio isn't
    began = time.monotonic()
    codex[0].provider_reset()
    wait_for(lambda: interaction.finalization == "incomplete", 5)
    wait_for(lambda: events(interaction, "closed"), 5)
    assert time.monotonic() - began < 3  # seconds, not the 45 s reconnect hold
    # Never held and never told to reconnect: there was no conversation to come back to.
    assert not interaction.drop_held and events(interaction, "reconnect_required") == []
    assert interaction.drop_count == 0
    [closed] = events(interaction, "closed")
    assert closed["reason"] == "connect_failed" and closed["error"] == CONNECT_FAILED
    assert interaction.end_reason == "connect_failed"
    assert interaction.snapshot()["call_state"] == "ended"
    # The call watch reads these lines: no provider drop, one failed connect.
    assert "before_live=true" in caplog.text and "reason=connect_failed" in caplog.text
    assert "held=true" not in caplog.text and "call.end id=" in caplog.text
    assert "reason=provider duration" not in caplog.text
    # The attempt is over: it can't be resumed as if it were a dropped live call.
    status, refused = http(server.base_url, "POST", "/voice/sessions",
                           {"sdp": SDP, "resume_from": interaction.interaction_id},
                           server.token, {"Idempotency-Key": "req_connect_drop_resume"})
    assert status == 409
    # A fresh try is a new call, which connects normally.
    again = service.interaction(open_call(server, "req_connect_drop_2")["interaction_id"])
    wait_for(lambda: again.status == "listening", 5)
    assert again.finalization == "open"


def test_the_app_saying_its_audio_is_up_makes_a_later_drop_a_live_drop(server, service, codex, caplog):
    """A silent live call (nobody has spoken yet): the app's POST .../live is what marks it live."""
    caplog.set_level(logging.INFO, logger="speakeasy")
    interaction = service.interaction(open_call(server, "req_connect_live_1")["interaction_id"])
    wait_for(lambda: interaction.status == "listening", 5)
    status, body = http(server.base_url, "POST", f"/voice/interactions/{interaction.interaction_id}/live", {},
                        server.token)
    assert status == 200 and body["live"] is True
    assert interaction.went_live and "call.live" in caplog.text and "by=app" in caplog.text
    codex[0].provider_reset()
    wait_for(lambda: interaction.drop_held, 5)
    [ask] = events(interaction, "reconnect_required")
    assert ask["hold_s"] == calls_mod.DROP_HOLD_S
    assert interaction.finalization == "open" and events(interaction, "closed") == []


def test_speech_marks_a_call_live(server, service, codex):
    interaction = service.interaction(open_call(server, "req_connect_speech_1")["interaction_id"])
    wait_for(lambda: interaction.status == "listening", 5)
    assert not interaction.went_live
    codex[0].say("user", "What's on my calendar this afternoon")
    wait_for(lambda: interaction.went_live, 5)
    codex[0].provider_reset()
    wait_for(lambda: interaction.drop_held, 5)
    assert events(interaction, "closed") == []


def test_a_resume_continues_a_live_call(server, service, codex):
    """A resumed leg carries on a conversation that was live, so its drop still holds."""
    first = service.interaction(open_call(server, "req_connect_resume_1")["interaction_id"])
    wait_for(lambda: first.status == "listening", 5)
    codex[0].say("user", "Read me the last message from the plumber")
    wait_for(lambda: first.went_live, 5)
    codex[0].provider_reset()
    wait_for(lambda: first.drop_held, 5)
    second = service.interaction(open_call(server, "req_connect_resume_2", resume_from=first.interaction_id)
                                 ["interaction_id"])
    assert second.went_live
    wait_for(lambda: second.status == "listening", 5)
    codex[1].provider_reset()
    wait_for(lambda: second.drop_held, 5)
    assert second.end_reason is None


@pytest.fixture
def quick_reaper(monkeypatch):
    monkeypatch.setattr(calls_mod, "UNATTENDED_S", 0.4)
    monkeypatch.setattr(service_mod, "UNATTENDED_CHECK_S", 0.1)


def test_a_connect_nobody_is_attached_to_is_reaped_with_its_voice(server, service, codex, quick_reaper, caplog):
    """The app went away mid-connect (killed, or never opened the call's stream): its voice is closed."""
    caplog.set_level(logging.INFO, logger="speakeasy")
    interaction = service.interaction(open_call(server, "req_unattended_1")["interaction_id"])
    wait_for(lambda: interaction.status == "listening", 5)
    wait_for(lambda: interaction.end_reason == "unattended", 5)
    wait_for(lambda: interaction.finalization == "incomplete", 5)
    assert codex[0].stops >= 1 and codex[0].closed  # the voice session is gone
    assert interaction.snapshot()["call_state"] == "ended"
    assert not interaction.drop_held and events(interaction, "reconnect_required") == []
    assert interaction.error == CONNECT_FAILED
    assert "call.unattended" in caplog.text and "reason=unattended" in caplog.text
    assert "reason=provider" not in caplog.text


def test_an_attached_app_keeps_a_slow_connect_open(server, service, codex, quick_reaper):
    """Someone still on the call (its event stream open) is never reaped, however slow the connect."""
    interaction = service.interaction(open_call(server, "req_attended_1")["interaction_id"])
    service.stream_attached(interaction, +1)
    time.sleep(1.0)
    assert interaction.end_reason is None and interaction.finalization == "open" and not codex[0].closed
    # The stream closes (the app went away): now it is reaped.
    service.stream_attached(interaction, -1)
    wait_for(lambda: interaction.end_reason == "unattended", 5)


def test_requests_about_the_call_count_as_attached(server, service, codex, quick_reaper):
    """An app on the polling fallback has no stream open, but keeps asking about the call."""
    interaction = service.interaction(open_call(server, "req_polling_1")["interaction_id"])
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        status, _ = http(server.base_url, "GET", f"/voice/interactions/{interaction.interaction_id}", None,
                         server.token)
        assert status == 200
        time.sleep(0.1)
    assert interaction.end_reason is None and interaction.finalization == "open"


def test_a_live_call_is_never_reaped(server, service, codex, quick_reaper):
    interaction = service.interaction(open_call(server, "req_live_unattended_1")["interaction_id"])
    wait_for(lambda: interaction.status == "listening", 5)
    codex[0].say("user", "Turn the porch light on")
    wait_for(lambda: interaction.went_live, 5)
    time.sleep(1.0)
    assert interaction.end_reason is None and interaction.finalization == "open" and not codex[0].closed
