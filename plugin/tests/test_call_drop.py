"""The voice provider dropping a live call is not a hang-up (call_drop).

OpenAI resets the realtime connection now and then ("Connection reset without closing handshake");
Codex then reports `thread/realtime/closed` though nobody asked to end. Speakeasy holds such a call,
tells the app to reconnect (`reconnect_required`), and the app's resume continues the same
conversation (its tasks, transcript and voice) in a fresh session. Only End ends a call at once.
All data here is invented.
"""
from __future__ import annotations

import logging
import queue

import pytest

from fakes import SDP, FakeTransport, http, wait_for
from speakeasy import calls as calls_mod
from speakeasy.calls import BackendRun


class DropTransport(FakeTransport):
    """A Codex app-server stand-in whose notifications the test sends."""

    def __init__(self) -> None:
        super().__init__()
        self.notifications: queue.Queue = queue.Queue()

    def say(self, role: str, text: str) -> None:
        self.notifications.put({"method": "thread/realtime/transcript/done",
                                "params": {"threadId": "thr_fake", "role": role, "text": text}})

    def provider_reset(self) -> None:
        """What Codex sends after OpenAI reset the socket and its own reconnect failed."""
        self.notifications.put({"method": "thread/realtime/closed", "params": {"threadId": "thr_fake"}})


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


def live_call_with_a_task(server, service, codex, key="req_drop_1", status="running"):
    session = open_call(server, key)
    interaction = service.interaction(session["interaction_id"])
    wait_for(lambda: interaction.status == "listening", 5)
    with interaction.lock:
        interaction.runs["dl_invented_1"] = BackendRun("dl_invented_1", 1, "idem_invented_1", "run_invented_1",
                                                       status=status)
        interaction.latest_delegation_id = "dl_invented_1"
    codex[-1].say("user", "Find me a quiet cafe near the office")
    codex[-1].say("assistant", "Sure.")
    return interaction


def test_a_provider_reset_holds_the_call_and_the_resume_continues_it(server, service, codex, caplog):
    caplog.set_level(logging.INFO, logger="speakeasy")
    first = live_call_with_a_task(server, service, codex)
    voice = codex[0].started[3]
    codex[0].provider_reset()
    wait_for(lambda: first.drop_held, 5)
    # Held, not ended: no "closed" for the app, which is told to reconnect instead.
    assert first.finalization == "open" and first.end_reason is None
    assert first.snapshot()["call_state"] == "live" and first.snapshot()["reconnecting"] is True
    assert events(first, "closed") == []
    [ask] = events(first, "reconnect_required")
    assert ask["interaction_id"] == first.interaction_id and ask["reason"] == "provider"
    assert ask["attempt"] == 1 and ask["max"] == calls_mod.MAX_AUTO_RESUMES
    assert "call.drop" in caplog.text and "held=true" in caplog.text

    # The app reconnects into it: the same conversation goes on with its task, transcript and voice.
    second = service.interaction(open_call(server, "req_drop_2", resume_from=first.interaction_id)["interaction_id"])
    assert first.successor is second and second.resumed_from == first.interaction_id
    assert "dl_invented_1" in second.runs and second.runs["dl_invented_1"].run_id == "run_invented_1"
    seed_text = repr(codex[1].started[2]) + codex[1].started[1]
    assert "quiet cafe near the office" in seed_text
    assert codex[1].started[3] == voice
    assert second.drop_count == 1 and not first.drop_held
    assert first.snapshot()["call_state"] == "resumed"
    assert "resumed=true" in caplog.text and "gap_ms=" in caplog.text
    # Nothing was logged as an ended call: the call never ended.
    assert "call.end" not in caplog.text


def test_end_still_ends_the_call_at_once(server, service, codex, caplog):
    caplog.set_level(logging.INFO, logger="speakeasy")
    interaction = live_call_with_a_task(server, service, codex, status="completed")
    # End: the app asks the plugin to stop the voice session, Codex then reports it closed.
    status, _ = http(server.base_url, "POST", f"/voice/interactions/{interaction.interaction_id}/end", {},
                     server.token)
    assert status == 200
    assert ("thread/realtime/stop", {"threadId": "thr_fake"}) in codex[0].requests
    codex[0].provider_reset()  # the closed that answers the stop
    wait_for(lambda: interaction.finalization == "confirmed", 5)
    assert not interaction.drop_held and events(interaction, "reconnect_required") == []
    wait_for(lambda: events(interaction, "closed"), 5)
    assert events(interaction, "closed")[0]["reason"] == "user"
    assert interaction.end_reason == "user"
    assert "call.end" in caplog.text and "reason=user" in caplog.text
    logged = service.call_log.get(interaction.interaction_id)
    assert logged and logged.get("end_reason") == "user"


def test_end_while_reconnecting_ends_it_as_the_users(server, service, codex):
    interaction = live_call_with_a_task(server, service, codex, status="completed")
    codex[0].provider_reset()
    wait_for(lambda: interaction.drop_held, 5)
    status, body = http(server.base_url, "POST", f"/voice/interactions/{interaction.interaction_id}/end", {},
                        server.token)
    assert status == 200 and body["finalization"] == "confirmed"
    assert not interaction.drop_held and interaction.end_reason == "user"
    assert events(interaction, "closed")[-1]["reason"] == "user"
    status, refused = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, "resume_from": interaction.interaction_id},
                           server.token, {"Idempotency-Key": "req_drop_after_end"})
    assert status == 409  # ended means ended
    # SPK-458: with a reason the app ends on, instead of starting a fresh call in the ended one's place.
    assert refused.get("reason") == "ended"


def test_a_late_drop_report_after_a_hang_up_never_holds_the_call(server, service, codex, caplog):
    """SPK-458, the other arrival order: End landed first, then the provider's drop is reported.
    The hang-up wins: no hold, no reconnect_required, still ended as the user's."""
    caplog.set_level(logging.INFO, logger="speakeasy")
    interaction = live_call_with_a_task(server, service, codex, status="completed")
    status, _ = http(server.base_url, "POST", f"/voice/interactions/{interaction.interaction_id}/end", {},
                     server.token)
    assert status == 200
    codex[0].provider_reset()
    wait_for(lambda: interaction.finalization == "confirmed", 5)
    interaction.worker.call_drop("provider")  # a drop report that lost the race with End
    assert not interaction.drop_held and interaction.status != "reconnecting"
    assert events(interaction, "reconnect_required") == []
    assert interaction.end_reason == "user" and interaction.finalization == "confirmed"
    assert "ignored: the call already ended" in caplog.text
    status, refused = http(server.base_url, "POST", "/voice/sessions",
                           {"sdp": SDP, "resume_from": interaction.interaction_id},
                           server.token, {"Idempotency-Key": "req_late_drop_resume"})
    assert status == 409 and refused.get("reason") == "ended"


def test_at_most_two_automatic_resumes_per_call(server, service, codex, caplog):
    caplog.set_level(logging.INFO, logger="speakeasy")
    current = live_call_with_a_task(server, service, codex, status="completed")
    for n in range(calls_mod.MAX_AUTO_RESUMES):
        codex[-1].provider_reset()
        wait_for(lambda: current.drop_held, 5)
        current = service.interaction(open_call(server, f"req_cap_{n}", resume_from=current.interaction_id)["interaction_id"])
        wait_for(lambda: current.status == "listening", 5)
    assert current.drop_count == calls_mod.MAX_AUTO_RESUMES
    codex[-1].provider_reset()
    wait_for(lambda: current.finalization == "incomplete", 5)
    assert not current.drop_held and events(current, "reconnect_required") == []
    wait_for(lambda: events(current, "closed"), 5)
    assert events(current, "closed")[0]["reason"] == "provider"
    assert current.end_reason == "provider"
    assert "resumed=false" in caplog.text and "reason=provider" in caplog.text


def test_a_drop_nobody_reconnects_into_ends_as_dropped(server, service, codex, monkeypatch):
    monkeypatch.setattr(calls_mod, "DROP_HOLD_S", 0.2)
    interaction = live_call_with_a_task(server, service, codex, status="completed")
    codex[0].provider_reset()
    wait_for(lambda: interaction.finalization == "incomplete", 5)
    assert not interaction.drop_held and interaction.end_reason == "provider"
    assert events(interaction, "closed")[-1]["reason"] == "provider"
    logged = service.call_log.get(interaction.interaction_id)
    assert logged and logged.get("end_reason") == "provider"


def test_a_pause_is_not_a_drop(server, service, codex):
    interaction = live_call_with_a_task(server, service, codex)
    status, _ = http(server.base_url, "POST", f"/voice/interactions/{interaction.interaction_id}/pause", {},
                     server.token)
    assert status == 200
    codex[0].provider_reset()
    wait_for(lambda: interaction.paused and interaction.call_closed, 5)
    assert not interaction.drop_held and events(interaction, "reconnect_required") == []
