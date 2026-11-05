"""Grok voice (beta): the plugin owns the xAI realtime socket and relays audio to the app.

A fake xAI realtime server (a real WebSocket server on loopback) stands in for xAI, and a real
WebSocket client stands in for the app's audio relay."""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from typing import Any

import pytest
import websockets
from websockets.sync.client import connect as ws_connect

from fakes import FakeTransport, http, wait_for
from speakeasy import grok_live
from speakeasy.settings import GROK_BETA_OFF, Settings, SettingsError

TOKEN = "test-xai-token-not-real"


class FakeXAI:
    """Loopback stand-in for wss://api.x.ai/v1/realtime: records what the plugin sends and lets the
    test push server events."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.auth: list[str] = []
        self.paths: list[str] = []
        self.conn: Any = None
        self.closed = threading.Event()
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()

        async def handler(ws: Any) -> None:
            self.auth.append(ws.request.headers.get("Authorization", ""))
            self.paths.append(ws.request.path)
            self.conn = ws
            try:
                async for raw in ws:
                    self.received.append(json.loads(raw))
            except websockets.ConnectionClosed:
                pass
            finally:
                self.closed.set()

        async def main() -> None:
            self.server = await websockets.serve(handler, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]
            ready.set()
            try:
                await self.server.serve_forever()
            except asyncio.CancelledError:
                pass

        threading.Thread(target=lambda: self.loop.run_until_complete(main()), daemon=True).start()
        ready.wait(5)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def send(self, event: dict[str, Any]) -> None:
        wait_for(lambda: self.conn is not None)
        asyncio.run_coroutine_threadsafe(self.conn.send(json.dumps(event)), self.loop).result(5)

    def of(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in list(self.received) if m.get("type") == kind]

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.server.close)


@pytest.fixture
def xai():
    fake = FakeXAI()
    yield fake
    fake.close()


@pytest.fixture
def grok_service(home, xai):
    from speakeasy.service import VoiceService
    service = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                           codex_login=lambda binary: (True, "Signed in to Codex."),
                           grok_credential=lambda: (TOKEN, xai.base),
                           grok_signin=lambda: (True, False, "Signed in to xAI (Grok)."))
    service.settings.patch({"beta": {"grok_voice": True}})
    service.settings.patch({"voice": {"provider": "grok", "voice": "rex"}})
    yield service
    service.close()


@pytest.fixture
def grok_server(grok_service):
    from speakeasy.server import SpeakeasyServer
    srv = SpeakeasyServer(grok_service, "127.0.0.1", 0)
    srv.start()
    code = grok_service.devices.new_pairing_code()
    status, paired = http(srv.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Test Mac"})
    assert status == 201
    srv.token = paired["token"]  # type: ignore[attr-defined]
    yield srv
    srv.httpd.shutdown()


def start_call(srv) -> dict[str, Any]:
    status, body = http(srv.base_url, "POST", "/voice/sessions", {}, srv.token, {"Idempotency-Key": "grok-call-1"})
    assert status == 201, body
    return body


def relay(srv, body: dict[str, Any]):
    url = srv.base_url.replace("http://", "ws://") + body["transport"]["path"]
    return ws_connect(url, additional_headers={"Authorization": f"Bearer {srv.token}"}, open_timeout=5)


def recv_json(ws, kind: str, timeout: float = 5) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = ws.recv(timeout=max(.05, deadline - time.monotonic()))
        if isinstance(message, str):
            event = json.loads(message)
            if event.get("type") == kind:
                return event
    raise AssertionError(f"no {kind} from the relay")


def recv_audio(ws, timeout: float = 5) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = ws.recv(timeout=max(.05, deadline - time.monotonic()))
        if isinstance(message, bytes):
            return message
    raise AssertionError("no audio from the relay")


def worker_of(service, body):
    return service.interaction(body["interaction_id"]).worker


def test_session_start_returns_relay_transport_and_configures_xai(grok_server, grok_service, xai):
    body = start_call(grok_server)
    iid = body["interaction_id"]
    assert body["voice_provider"] == "grok"
    assert body["transport"] == {"type": "relay", "path": f"/voice/interactions/{iid}/audio",
                                 "format": {"encoding": "pcm16le", "rate": 24000, "channels": 1}}
    assert "sdp" not in body["transport"]
    update = wait_for(lambda: xai.of("session.update"))[0]["session"]
    assert xai.auth == [f"Bearer {TOKEN}"]
    assert xai.paths[0] == "/v1/realtime?model=grok-voice-latest"
    assert update["voice"] == "Rex"
    assert update["turn_detection"] == {"type": "server_vad", "silence_duration_ms": 1000}
    assert update["audio"] == {"input": {"format": {"type": "audio/pcm", "rate": 24000}},
                               "output": {"format": {"type": "audio/pcm", "rate": 24000}}}
    [tool] = update["tools"]
    assert tool["type"] == "function" and tool["name"] == "handoff"
    assert tool["parameters"]["required"] == ["request"] and "task_id" in tool["parameters"]["properties"]
    assert "`handoff`" in update["instructions"] and len(update["instructions"]) > 500
    # The token never reaches the app, the snapshot or the worker after connecting.
    assert TOKEN not in json.dumps(body)
    worker = worker_of(grok_service, body)
    wait_for(lambda: worker.connected)
    assert worker._token == ""
    status, snap = http(grok_server.base_url, "GET", f"/voice/interactions/{iid}", None, grok_server.token)
    assert status == 200 and TOKEN not in json.dumps(snap)


def test_audio_flows_both_ways_and_mute_holds_the_mic(grok_server, grok_service, xai):
    body = start_call(grok_server)
    wait_for(lambda: xai.of("session.update"))
    pcm = bytes(range(256)) * 4  # ~21 ms of PCM16 at 24 kHz
    with relay(grok_server, body) as app:
        app.send(pcm)
        sent = wait_for(lambda: xai.of("input_audio_buffer.append"))
        assert base64.b64decode(sent[0]["audio"]) == pcm
        voice = b"\x01\x02" * 480
        xai.send({"type": "response.output_audio.delta", "response_id": "r1", "delta": base64.b64encode(voice).decode()})
        assert recv_audio(app) == voice
        xai.send({"type": "response.done", "response": {"id": "r1"}})
        recv_json(app, "response_done")
        app.send(json.dumps({"type": "mute", "muted": True}))
        time.sleep(.2)
        app.send(pcm)
        time.sleep(.3)
        assert len(xai.of("input_audio_buffer.append")) == 1
        app.send(json.dumps({"type": "mute", "muted": False}))
        time.sleep(.1)
        app.send(pcm)
        wait_for(lambda: len(xai.of("input_audio_buffer.append")) == 2)


def test_barge_in_flushes_the_app_and_cancels_the_reply(grok_server, grok_service, xai):
    body = start_call(grok_server)
    wait_for(lambda: xai.of("session.update"))
    with relay(grok_server, body) as app:
        wait_for(lambda: worker_of(grok_service, body).client is not None)
        xai.send({"type": "response.created", "response": {"id": "r7"}})
        xai.send({"type": "input_audio_buffer.speech_started"})
        recv_json(app, "speech_started")
        wait_for(lambda: xai.of("response.cancel"))
        # Late audio from the cancelled reply never reaches the app; a new reply's does.
        xai.send({"type": "response.output_audio.delta", "response_id": "r7", "delta": base64.b64encode(b"\x09" * 64).decode()})
        xai.send({"type": "response.output_audio.delta", "response_id": "r8", "delta": base64.b64encode(b"\x07" * 64).decode()})
        assert recv_audio(app) == b"\x07" * 64


def test_transcripts_and_handoff_start_a_task(grok_server, grok_service, xai, hermes):
    body = start_call(grok_server)
    worker = worker_of(grok_service, body)
    wait_for(lambda: xai.of("session.update"))
    xai.send({"type": "input_audio_buffer.speech_started"})
    xai.send({"type": "conversation.item.input_audio_transcription.completed", "item_id": "i1",
              "transcript": "Check my inbox for anything urgent from Sam"})
    xai.send({"type": "response.output_audio_transcript.delta", "response_id": "r1", "delta": "On it."})
    xai.send({"type": "response.function_call_arguments.done", "call_id": "call_1", "name": "handoff",
              "arguments": json.dumps({"request": "Check the inbox for urgent email from Sam"})})
    out = wait_for(lambda: [m for m in xai.of("conversation.item.create")
                            if m["item"].get("type") == "function_call_output"])[0]["item"]
    assert out["call_id"] == "call_1" and json.loads(out["output"])["status"] == "started"
    wait_for(lambda: xai.of("response.create"))
    call = wait_for(lambda: hermes.calls)[0]
    assert "inbox" in json.dumps(call["input"]).lower()
    turns = worker.turns()
    assert turns[0] == {"role": "user", "text": "Check my inbox for anything urgent from Sam"}
    assert {"role": "assistant", "text": "On it."} in turns
    # The same call id again (a replay) starts nothing new.
    xai.send({"type": "response.function_call_arguments.done", "call_id": "call_1", "name": "handoff",
              "arguments": json.dumps({"request": "Check the inbox for urgent email from Sam"})})
    time.sleep(.5)
    assert len(hermes.calls) == 1


def test_handoff_after_spoken_ack_asks_for_no_second_reply(grok_server, grok_service, xai, hermes):
    body = start_call(grok_server)
    wait_for(lambda: xai.of("session.update"))
    xai.send({"type": "conversation.item.input_audio_transcription.completed", "transcript": "Rerun the nightly build"})
    xai.send({"type": "response.created", "response": {"id": "r5"}})
    xai.send({"type": "response.output_audio.delta", "response_id": "r5", "delta": base64.b64encode(b"\x01" * 64).decode()})
    xai.send({"type": "response.function_call_arguments.done", "response_id": "r5", "call_id": "c5", "name": "handoff",
              "arguments": json.dumps({"request": "Rerun the nightly build"})})
    xai.send({"type": "response.done", "response": {"id": "r5"}})
    wait_for(lambda: hermes.calls)
    wait_for(lambda: [m for m in xai.of("conversation.item.create") if m["item"].get("type") == "function_call_output"])
    time.sleep(.3)
    assert xai.of("response.create") == []  # it already acknowledged in the turn that called the tool


def test_handoff_without_transcript_uses_the_request(grok_server, grok_service, xai, hermes):
    body = start_call(grok_server)
    wait_for(lambda: xai.of("session.update"))
    xai.send({"type": "response.function_call_arguments.done", "call_id": "call_9", "name": "handoff",
              "arguments": json.dumps({"request": "Find a good time to call the plumber this week"})})
    call = wait_for(lambda: hermes.calls)[0]
    assert "plumber" in json.dumps(call["input"]).lower()


def test_unknown_tool_is_answered_not_dispatched(grok_server, grok_service, xai, hermes):
    body = start_call(grok_server)
    wait_for(lambda: xai.of("session.update"))
    xai.send({"type": "response.function_call_arguments.done", "call_id": "c2", "name": "launch",
              "arguments": "{}"})
    out = wait_for(lambda: [m for m in xai.of("conversation.item.create")
                            if m["item"].get("type") == "function_call_output"])[0]["item"]
    assert json.loads(out["output"])["status"] == "error"
    time.sleep(.3)
    assert hermes.calls == []
    assert worker_of(grok_service, body).delegations == set()


def test_thinking_is_silent_and_commentary_is_spoken(grok_server, grok_service, xai):
    body = start_call(grok_server)
    worker = worker_of(grok_service, body)
    wait_for(lambda: xai.of("session.update"))
    wait_for(lambda: worker.connected)

    def send(kind: str, text: str) -> None:
        asyncio.run_coroutine_threadsafe(worker._send(kind, None, text), worker.loop).result(5)

    send("session.thinking.append", "Background: the report finished.")
    items = wait_for(lambda: [m for m in xai.of("conversation.item.create") if m["item"].get("role") == "system"])
    assert items[0]["item"]["content"] == [{"type": "input_text", "text": "Background: the report finished."}]
    time.sleep(.2)
    assert xai.of("response.create") == []
    # Spoken lines are said verbatim (xAI force_message), with no response.create.
    send("session.commentary.append", "The report is ready.")

    def forced():
        return [m["item"] for m in xai.of("conversation.item.create") if m["item"].get("type") == "force_message"]
    item = wait_for(forced)[0]
    assert item["role"] == "assistant" and item["content"] == [{"type": "output_text", "text": "The report is ready."}]
    xai.send({"type": "response.created", "response": {"id": "r3"}})
    # While a reply is playing, the next spoken line waits for it rather than colliding.
    time.sleep(.1)
    send("session.commentary.append", "One more thing.")
    time.sleep(.3)
    assert len(forced()) == 1
    xai.send({"type": "response.done", "response": {"id": "r3"}})
    wait_for(lambda: len(forced()) == 2)
    assert forced()[1]["content"][0]["text"] == "One more thing."
    assert xai.of("response.create") == []


def test_end_from_the_app_closes_the_call(grok_server, grok_service, xai):
    body = start_call(grok_server)
    interaction = grok_service.interaction(body["interaction_id"])
    wait_for(lambda: xai.of("session.update"))
    with relay(grok_server, body) as app:
        wait_for(lambda: worker_of(grok_service, body).client is not None)
        app.send(json.dumps({"type": "end"}))
        assert recv_json(app, "closed")["reason"] == "ended"
    wait_for(lambda: interaction.finalization == "confirmed")
    assert xai.closed.wait(5)
    status, _ = http(grok_server.base_url, "GET", body["transport"]["path"], None, grok_server.token)
    assert status == 409  # no relay into an ended call


def test_second_relay_replaces_the_first(grok_server, grok_service, xai):
    body = start_call(grok_server)
    wait_for(lambda: xai.of("session.update"))
    first = relay(grok_server, body)
    wait_for(lambda: worker_of(grok_service, body).client is not None)
    with relay(grok_server, body) as second:
        assert recv_json(first, "closed")["reason"] == "replaced"
        second.send(b"\x00\x01" * 240)
        wait_for(lambda: xai.of("input_audio_buffer.append"))
    first.close()


def test_relay_lost_ends_the_call(grok_server, grok_service, xai, monkeypatch):
    monkeypatch.setattr(grok_live, "RELAY_LOST_S", 0.6)
    body = start_call(grok_server)
    interaction = grok_service.interaction(body["interaction_id"])
    with relay(grok_server, body):
        wait_for(lambda: worker_of(grok_service, body).client is not None)
    wait_for(lambda: interaction.call_closed, timeout=6)
    assert worker_of(grok_service, body).end_reason == "relay lost"


def test_relay_needs_the_device_token_and_an_upgrade(grok_server, grok_service, xai):
    body = start_call(grok_server)
    url = grok_server.base_url.replace("http://", "ws://") + body["transport"]["path"]
    with pytest.raises(Exception):
        ws_connect(url, open_timeout=5)
    status, _ = http(grok_server.base_url, "GET", body["transport"]["path"], None, grok_server.token)
    assert status == 426
    status, _ = http(grok_server.base_url, "GET", "/voice/interactions/vi_nope/audio", None, grok_server.token)
    assert status == 404


def test_missing_credential_is_a_clear_409(home, xai):
    from speakeasy.service import VoiceService
    service = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                           codex_login=lambda binary: (True, "ok"), grok_credential=lambda: ("", ""),
                           grok_signin=lambda: (False, False, grok_live.SIGN_IN_HINT))
    try:
        service.settings.patch({"beta": {"grok_voice": True}})
        service.settings.patch({"voice": {"provider": "grok"}})
        with pytest.raises(Exception) as err:
            service.create_session({}, "grok-nokey-1")
        assert err.value.status == 409 and "hermes auth add xai-oauth" in err.value.message
        status = service.status()
        assert status["voice_ready"] is False and status["grok_ready"] is False
        assert "hermes auth add xai-oauth" in status["grok_message"]
        assert service.onboarding()["steps"]["voice_ready"] is False
        assert xai.received == []
    finally:
        service.close()


def test_signin_state_asks_hermes_credential_pool_offline(home):
    assert grok_live.signin_state(home)[0] is False
    (home / "auth.json").write_text(json.dumps({"providers": {"xai-oauth": {"tokens": {"access_token": "x"}}}}))
    assert grok_live.signin_state(home)[0] is True
    (home / "auth.json").write_text(json.dumps({"credential_pool": {"xai-oauth": [{"access_token": "y"}]}}))
    assert grok_live.signin_state(home)[0] is True
    (home / "auth.json").write_text("{}")
    with open(home / ".env", "a") as f:
        f.write("XAI_API_KEY=fake-key-for-test\n")
    signed_in, key_set, message = grok_live.signin_state(home)
    assert (signed_in, key_set) == (False, True) and "fake-key" not in message


def test_resolve_credential_falls_back_to_the_env_key(home, monkeypatch):
    import builtins
    real_import = builtins.__import__

    def no_hermes(name, *a, **k):
        if name == "tools.xai_http":
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_hermes)
    assert grok_live.resolve_credential(home) == ("", "")
    with open(home / ".env", "a") as f:
        f.write("XAI_API_KEY=fake-key-for-test\n")
    assert grok_live.resolve_credential(home) == ("fake-key-for-test", "https://api.x.ai/v1")


def test_beta_gate(home):
    settings = Settings(home)
    with pytest.raises(SettingsError) as err:
        settings.patch({"voice": {"provider": "grok"}})
    assert str(err.value) == GROK_BETA_OFF
    assert settings.get()["beta"]["grok_voice"] is False
    settings.patch({"beta": {"grok_voice": True}})
    assert settings.patch({"voice": {"provider": "grok"}})["voice"]["provider"] == "grok"
    with pytest.raises(SettingsError):
        settings.patch({"beta": {"grok_voice": False}})  # not out from under a Grok provider
    settings.patch({"voice": {"provider": "openai"}})
    settings.patch({"beta": {"grok_voice": False}})


def test_cli_refuses_grok_without_beta(home, capsys):
    from argparse import Namespace
    from speakeasy.cli import cmd_config
    rc = cmd_config(Namespace(config_command="set", key="voice.provider", value="grok"), home)
    assert rc == 2 and "beta.grok_voice true" in capsys.readouterr().out
    assert cmd_config(Namespace(config_command="set", key="beta.grok_voice", value="true"), home) == 0
    assert cmd_config(Namespace(config_command="set", key="voice.provider", value="grok"), home) == 0
    assert Settings(home).get()["voice"]["provider"] == "grok"


def test_hand_edited_grok_without_beta_refuses_calls(home, xai):
    from speakeasy.service import ServiceError, VoiceService
    raw = Settings(home).get()
    raw["voice"]["provider"] = "grok"
    (home / "speakeasy").mkdir(exist_ok=True)
    (home / "speakeasy" / "settings.json").write_text(json.dumps(raw))
    service = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                           codex_login=lambda binary: (True, "ok"), grok_credential=lambda: (TOKEN, xai.base),
                           grok_signin=lambda: (True, False, "Signed in to xAI (Grok)."))
    try:
        assert service.settings.get()["voice"]["provider"] == "grok"
        with pytest.raises(ServiceError) as err:
            service.create_session({}, "grok-gated-1")
        assert err.value.status == 409 and "beta" in err.value.message
        assert service.status()["voice_ready"] is False
    finally:
        service.close()


def test_voices_and_default(home):
    from speakeasy.settings import default_voice
    settings = Settings(home)
    settings.patch({"beta": {"grok_voice": True}})
    s = settings.patch({"voice": {"provider": "grok", "voice": "marin"}})
    assert default_voice(s) == "Ara"  # another provider's voice falls back to Grok's default
    s = settings.patch({"voice": {"voice": "eve"}})
    assert default_voice(s) == "Eve"
