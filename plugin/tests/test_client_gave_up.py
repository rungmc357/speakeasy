"""SPK-557: the phone gave up on POST /voice/sessions before the reply arrived.

A slow connect (spare missed, a long Codex prepare) outlasted the app's 15 s request timeout. The
plugin went on anyway: it started the realtime voice for a call nobody was on, held it as a provider
drop ~55 s later, failed to resume it and logged ``call.end reason=provider``. The call watch then
counted a failed connect as a provider drop.

Now a reply that can't be delivered cancels the call: its voice session is closed, it ends as
``client_gave_up`` (never held, dropped or resumed) and the drop counters don't see it.
All data here is invented.
"""
from __future__ import annotations

import json
import logging
import queue
import re
import socket
import threading
import time

import pytest

from fakes import SDP, FakeTransport, http, wait_for


class SlowTransport(FakeTransport):
    """A Codex transport whose prepare outlasts the client's patience (a missed spare)."""

    prepare_s = 1.2

    def __init__(self) -> None:
        super().__init__()
        self.notifications: queue.Queue = queue.Queue()
        self.stops = 0

    def prepare(self) -> None:
        time.sleep(self.prepare_s)

    def stop(self) -> None:
        self.stops += 1
        super().stop()


@pytest.fixture
def slow_codex(service):
    service.settings.patch({"voice": {"provider": "codex"}})
    service.codex_spare = None  # the spare missed: every connect prepares cold
    transports: list[SlowTransport] = []
    service._codex_factory = lambda: transports.append(SlowTransport()) or transports[-1]
    return transports


def give_up_on_session(server, key: str, patience_s: float = .3) -> None:
    """POST /voice/sessions, then hang up before the reply (the app's request timeout)."""
    host, port = server.base_url.split("//", 1)[1].split(":")
    body = json.dumps({"sdp": SDP}).encode()
    head = (f"POST /voice/sessions HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
            f"Authorization: Bearer {server.token}\r\nIdempotency-Key: {key}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode()
    sock = socket.create_connection((host, int(port)), timeout=5)
    sock.sendall(head + body)
    time.sleep(patience_s)
    sock.close()


def only_interaction(service):
    with service.lock:
        found = list(service.interactions.values())
    return found[0] if len(found) == 1 else None


def test_a_client_that_hangs_up_before_the_reply_cancels_the_call(server, service, slow_codex, caplog):
    caplog.set_level(logging.INFO, logger="speakeasy")
    give_up_on_session(server, "req_gave_up_1")
    interaction = wait_for(lambda: only_interaction(service), 5)
    wait_for(lambda: interaction.end_reason is not None and interaction.call_closed, 5)
    transport = slow_codex[0]
    # The voice session for nobody is closed at once, not left to the provider to drop.
    assert transport.closed and transport.stops >= 1
    assert interaction.end_reason == "client_gave_up"
    assert interaction.abandoned is True
    assert interaction.snapshot()["call_state"] == "ended"
    # Never held for a reconnect, never counted as a drop or a resume.
    assert interaction.drop_held is False and interaction.dropped_at is None and interaction.drop_count == 0
    assert "call.drop" not in caplog.text
    assert "reason=client_gave_up" in caplog.text and "reason=provider" not in caplog.text
    # The call watch's drop counters (call.drop ... resumed=false, call.end reason=provider) see nothing.
    assert provider_drops(caplog.text) == 0
    # No one was on it: nothing goes in the call log (the call watch's count of calls).
    assert service.call_log.get(interaction.interaction_id) is None
    settle(interaction)


def provider_drops(log: str) -> int:
    """What the quality loop's call watch counts as provider drops ending a call, from agent.log lines."""
    fatal = sum(1 for m in re.finditer(r"call\.drop id=\S+ reason=(\w+) resumed=(\w+)", log) if m.group(2) != "true")
    ends = sum(1 for m in re.finditer(r"call\.end id=\S+ reason=(\w+)", log) if m.group(1) == "provider")
    return max(fatal, ends)


def settle(interaction) -> None:
    """Let the call's worker thread finish before the service closes under it."""
    for thread in threading.enumerate():
        if thread.name == f"speakeasy-call-{interaction.interaction_id[:12]}":
            thread.join(5)


def test_a_gave_up_call_is_not_offered_again_by_an_idempotent_retry(server, service, slow_codex):
    give_up_on_session(server, "req_gave_up_2")
    interaction = wait_for(lambda: only_interaction(service), 5)
    wait_for(lambda: interaction.end_reason == "client_gave_up", 5)
    status, body = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                        {"Idempotency-Key": "req_gave_up_2"})
    assert status == 409, body  # a fresh offer starts a fresh call; the dead one is not replayed
    settle(interaction)


def test_a_delivered_reply_still_starts_the_call(server, service, slow_codex, caplog):
    caplog.set_level(logging.INFO, logger="speakeasy")
    SlowTransport.prepare_s = 0
    try:
        status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                               {"Idempotency-Key": "req_gave_up_3"})
    finally:
        SlowTransport.prepare_s = 1.2
    assert status == 201, session
    interaction = service.interaction(session["interaction_id"])
    wait_for(lambda: interaction.status == "listening", 5)
    time.sleep(.3)
    assert not slow_codex[0].closed and interaction.end_reason is None and not interaction.abandoned
    assert "client_gave_up" not in caplog.text
    slow_codex[0].stop()  # End (as the app would) so the worker finishes before teardown
    settle(interaction)
