"""Typed turns from the call screen (``session.input_text``): POST /voice/interactions/{id}/input-text.

The words go to the live voice model as the user's turn (GPT-Live: a session-wide note quoting them;
Codex: ``thread/realtime/appendText`` with role user; Grok: a user message item plus a reply), join the
call's transcript, and go out on the call's SSE stream as ``transcript``. No network, no paid calls."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from fakes import SDP, FakeTransport, http, wait_for
from speakeasy import codex_transport as C
from speakeasy.calls import INPUT_TEXT_TYPE, MAX_INPUT_TEXT, Interaction, typed_text
from speakeasy.prompt import builder as P


def open_call(server, key: str) -> str:
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session["interaction_id"]


def typed(server, iid: str, body: Any, token: Any = "device") -> tuple[int, Any]:
    token = server.token if token == "device" else token
    return http(server.base_url, "POST", f"/voice/interactions/{iid}/input-text", body, token)


def turn(text: str) -> dict[str, str]:
    return {"type": INPUT_TEXT_TYPE, "text": text}


def typed_events(interaction) -> list[Any]:
    return [payload for _, kind, payload in list(interaction.feed.ring) if kind == "transcript"]


# -- the normalizer ----------------------------------------------------------------------------------

def test_typed_text_is_one_clean_line():
    assert typed_text("  turn off\nthe   lights\t\x00now ") == "turn off the lights now"
    assert typed_text("\n\t ") == ""
    assert INPUT_TEXT_TYPE == "session.input_text" and MAX_INPUT_TEXT == 1000


def test_gpt_live_note_quotes_the_words_and_asks_for_a_spoken_reply():
    note = P.typed_turn_note(P.Names("Todd", "Geo"), "What's on my calendar?")
    assert "Geo typed this" in note and "\u201cWhat's on my calendar?\u201d" in note
    assert "answer them out loud" in note


# -- the route (GPT-Live: the fake live worker is the provider) ---------------------------------------

def test_status_advertises_the_text_lane(server):
    status, body = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert status == 200 and body["input_text"] is True


def test_typed_turn_goes_into_the_live_call(server, service):
    iid = open_call(server, "req_typed_1")
    worker = service.workers[-1]
    worker.feed({"type": "session.input_transcript.delta", "delta": "Hi there.", "start_ms": 0, "end_ms": 900})
    worker.feed({"type": "session.output_transcript.delta", "delta": "Hey Geo.", "start_ms": 1000, "end_ms": 1800})
    status, body = typed(server, iid, turn("What's on my\ncalendar  today?"))
    assert status == 200, body
    assert body == {"interaction_id": iid, "type": "session.input_text", "sent": True}
    # Into the live session: one session-wide note quoting the words, no delegation id.
    assert len(worker.sent) == 1
    kind, delegation_id, content = worker.sent[0]
    assert kind == "session.thinking.append" and delegation_id is None
    assert "\u201cWhat's on my calendar today?\u201d" in content and "typed" in content
    # A user turn in the call's transcript, after what was already said ...
    assert worker.turns()[-1] == {"role": "user", "text": "What's on my calendar today?"}
    # ... so the next handoff carries it as the request.
    assert worker.context(10_000).endswith("User: What's on my calendar today?")
    # And on the call's live event stream, once.
    assert typed_events(service.interaction(iid)) == [
        {"role": "user", "text": "What's on my calendar today?", "source": "typed"}]


def test_typed_turn_joins_a_spoken_user_turn_with_a_space(server, service):
    iid = open_call(server, "req_typed_join")
    worker = service.workers[-1]
    worker.feed({"type": "session.input_transcript.delta", "delta": "Remind me", "start_ms": 0, "end_ms": 500})
    assert typed(server, iid, turn("to call Dana"))[0] == 200
    assert worker.turns() == [{"role": "user", "text": "Remind me to call Dana"}]


def test_typed_turn_shows_on_the_sse_stream(server, service):
    iid = open_call(server, "req_typed_sse")
    req = urllib.request.Request(f"{server.base_url}/voice/interactions/{iid}/events")
    req.add_header("Authorization", f"Bearer {server.token}")
    with urllib.request.urlopen(req, timeout=10) as stream:
        seen = b""
        while b"event: snapshot" not in seen:
            seen += stream.readline()
        assert typed(server, iid, turn("Order more coffee"))[0] == 200
        lines: list[bytes] = []
        while True:
            line = stream.readline()
            assert line, "stream ended before the transcript event"
            lines.append(line)
            if line.startswith(b"event: transcript"):
                data = stream.readline()
                break
    assert data.startswith(b"data: ")
    assert json.loads(data[len(b"data: "):]) == {"role": "user", "text": "Order more coffee", "source": "typed"}


def test_requires_the_device_token(server, service):
    iid = open_call(server, "req_typed_auth")
    worker = service.workers[-1]
    assert typed(server, iid, turn("hi"), token=None)[0] == 401
    assert typed(server, iid, turn("hi"), token="not-a-device-token")[0] == 401
    assert worker.sent == [] and list(worker.fragments) == []
    assert typed_events(service.interaction(iid)) == []


def test_rejects_anything_but_one_typed_turn(server, service):
    iid = open_call(server, "req_typed_bad")
    worker = service.workers[-1]
    for body in ({"text": "hi"},
                 {"type": "session.close", "text": "hi"},
                 {"type": INPUT_TEXT_TYPE, "text": "hi", "delegation_id": "d1"},
                 {"type": INPUT_TEXT_TYPE, "text": 5},
                 {"type": INPUT_TEXT_TYPE, "text": None},
                 {"type": INPUT_TEXT_TYPE, "text": "  \n\t "},
                 {"type": INPUT_TEXT_TYPE, "text": "x" * (MAX_INPUT_TEXT + 1)},
                 ["not", "an", "object"]):
        status, payload = typed(server, iid, body)
        assert status == 400, body
        assert payload.get("error"), body
    assert typed(server, iid, turn("  \n "))[1]["reason"] == "empty_text"
    assert typed(server, iid, turn("x" * (MAX_INPUT_TEXT + 1)))[1]["reason"] == "text_too_long"
    assert typed(server, iid, {"text": "hi"})[1]["reason"] == "invalid_body"
    # Not JSON at all.
    req = urllib.request.Request(f"{server.base_url}/voice/interactions/{iid}/input-text", data=b"not json",
                                 method="POST")
    req.add_header("Authorization", f"Bearer {server.token}")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req, timeout=5)
    assert caught.value.code == 400
    # Exactly 1000 characters is fine.
    assert typed(server, iid, turn("y" * MAX_INPUT_TEXT))[0] == 200
    assert len(worker.sent) == 1 and len(typed_events(service.interaction(iid))) == 1


def test_unknown_call(server):
    status, body = typed(server, "vi_nope", turn("hi"))
    assert status == 404 and body["reason"] == "unknown_interaction"


def test_ended_call(server, service):
    iid = open_call(server, "req_typed_ended")
    worker = service.workers[-1]
    worker.call_closed()
    status, body = typed(server, iid, turn("hi"))
    assert (status, body) == (409, {"error": "the call isn't connected", "reason": "call_ended"})
    assert worker.sent == [] and typed_events(service.interaction(iid)) == []


def test_paused_call(server, service):
    iid = open_call(server, "req_typed_paused")
    worker = service.workers[-1]
    assert http(server.base_url, "POST", f"/voice/interactions/{iid}/pause", {}, server.token)[0] == 200
    status, body = typed(server, iid, turn("hi"))
    assert status == 409 and body["reason"] == "call_paused"
    assert worker.sent == []


def test_voice_channel_gone(server, service):
    iid = open_call(server, "req_typed_gone")
    worker = service.workers[-1]
    worker.connected = False  # the provider's event channel dropped; the call isn't closed yet
    status, body = typed(server, iid, turn("hi"))
    assert status == 409 and body["reason"] == "not_connected"
    assert worker.sent == [] and list(worker.fragments) == []


def test_a_note_that_did_not_go_through_is_not_recorded(server, service):
    iid = open_call(server, "req_typed_drop")
    worker = service.workers[-1]

    async def dropped(kind, delegation_id, content):  # the sideband socket died on send
        worker.connected = False
    worker._send = dropped  # type: ignore[method-assign]
    status, body = typed(server, iid, turn("hello"))
    assert status == 409 and body["reason"] == "not_connected"
    assert list(worker.fragments) == [] and typed_events(service.interaction(iid)) == []


def test_after_resume_the_turn_goes_to_the_newest_call(server, service):
    iid = open_call(server, "req_typed_resume")
    first = service.workers[-1]
    assert http(server.base_url, "POST", f"/voice/interactions/{iid}/pause", {}, server.token)[0] == 200
    status, resumed = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, "resume_from": iid},
                           server.token, {"Idempotency-Key": "req_typed_resume_2"})
    assert status == 201
    second = service.workers[-1]
    assert second is not first
    status, body = typed(server, iid, turn("Where were we?"))
    assert status == 200 and body["interaction_id"] == resumed["interaction_id"]
    assert first.sent == [] and len(second.sent) == 1
    assert second.turns()[-1] == {"role": "user", "text": "Where were we?"}


# -- Codex: realtime user text --------------------------------------------------------------------------

class RecordingTransport(FakeTransport):
    def __init__(self, fail: Exception | None = None) -> None:
        super().__init__()
        self.fail = fail

    def request(self, method: str, params: dict | None = None, timeout: float = 20) -> dict:
        self.requests.append((method, params or {}))
        if self.fail is not None and method == "thread/realtime/appendText" and (params or {}).get("role") == "user":
            raise self.fail
        return {}


@pytest.fixture
def codex_call(service):
    """A live Codex call registered with the service, its worker loop running on its own thread."""
    transport = RecordingTransport()
    interaction = Interaction("vi_" + "t" * 32, "thr_fake")
    worker = C.CodexSidebandWorker(service.rt, interaction, transport)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    worker.loop = loop
    with service.lock:
        service.interactions[interaction.interaction_id] = interaction
    yield interaction, worker, transport
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2)


def run_on(worker, coro):
    return asyncio.run_coroutine_threadsafe(coro, worker.loop).result(10)


def test_codex_gets_one_append_text_with_role_user(server, codex_call):
    interaction, worker, transport = codex_call
    status, body = typed(server, interaction.interaction_id, turn("Turn on the den  lights"))
    assert status == 200, body
    appends = [p for m, p in transport.requests if m == "thread/realtime/appendText"]
    assert appends == [{"threadId": "thr_fake", "text": "Turn on the den lights", "role": "user"}]
    assert worker.turns() == [{"role": "user", "text": "Turn on the den lights"}]
    assert typed_events(interaction) == [{"role": "user", "text": "Turn on the den lights", "source": "typed"}]


def test_codex_echo_of_a_typed_turn_is_not_recorded_twice(server, codex_call):
    interaction, worker, transport = codex_call
    assert typed(server, interaction.interaction_id, turn("Turn on the den lights"))[0] == 200
    # Codex transcribes the appended text back as the user's turn: the same turn, once.
    run_on(worker, worker._user_turn("Turn on the den lights."))
    run_on(worker, worker.handle_event({"type": "session.output_transcript.delta", "delta": "Done.",
                                        "start_ms": 9, "end_ms": 9}))
    run_on(worker, worker._user_turn("And the porch"))
    assert worker.turns() == [{"role": "user", "text": "Turn on the den lights"},
                              {"role": "assistant", "text": "Done."},
                              {"role": "user", "text": "And the porch"}]
    assert len(typed_events(interaction)) == 1
    # A real spoken repeat much later is a new turn again.
    worker.typed_echoes.append(("say it again", time.monotonic() - 60))
    run_on(worker, worker._user_turn("Say it again"))
    assert worker.fragments[-1]["text"] == "Say it again"


def test_codex_rejected_append_records_nothing(server, codex_call):
    interaction, worker, transport = codex_call
    transport.fail = C.CodexStartError("Codex request rejected")
    status, body = typed(server, interaction.interaction_id, turn("hello"))
    assert status == 409 and body["reason"] == "not_connected"
    assert list(worker.fragments) == [] and list(worker.typed_echoes) == []
    assert typed_events(interaction) == []


def test_codex_closed_transport_sends_nothing(server, codex_call):
    interaction, worker, transport = codex_call
    transport.closed = True
    status, body = typed(server, interaction.interaction_id, turn("hello"))
    assert status == 409 and body["reason"] == "not_connected"
    assert transport.requests == []


# -- Grok: a user message item, then a reply ---------------------------------------------------------------

def test_grok_takes_a_user_message_and_is_asked_to_reply(home):
    from test_grok_voice import FakeXAI, TOKEN, relay, start_call, worker_of
    from speakeasy.server import SpeakeasyServer
    from speakeasy.service import VoiceService
    xai = FakeXAI()
    service = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                           codex_login=lambda binary: (True, "Signed in to Codex."),
                           grok_credential=lambda: (TOKEN, xai.base),
                           grok_signin=lambda: (True, False, "Signed in to xAI (Grok)."))
    service.settings.patch({"beta": {"grok_voice": True}})
    service.settings.patch({"voice": {"provider": "grok", "voice": "rex"}})
    srv = SpeakeasyServer(service, "127.0.0.1", 0)
    srv.start()
    try:
        code = service.devices.new_pairing_code()
        srv.token = http(srv.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Test"})[1]["token"]
        body = start_call(srv)
        wait_for(lambda: xai.of("session.update"))
        worker = worker_of(service, body)
        with relay(srv, body):
            wait_for(lambda: worker.client is not None)
            status, reply = typed(srv, body["interaction_id"], turn("What's the weather tomorrow?"))
            assert status == 200, reply
            items = wait_for(lambda: [m["item"] for m in xai.of("conversation.item.create")
                                      if m["item"].get("role") == "user"])
            assert items == [{"type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "What's the weather tomorrow?"}]}]
            wait_for(lambda: xai.of("response.create"))
            assert worker.turns()[-1] == {"role": "user", "text": "What's the weather tomorrow?"}
            assert typed_events(service.interaction(body["interaction_id"]))[-1]["source"] == "typed"
    finally:
        srv.httpd.shutdown()
        srv.httpd.server_close()
        service.close()
        xai.close()
