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
from pathlib import Path
from typing import Any

import pytest

from fakes import SDP, FakeTransport, http, wait_for
from speakeasy import codex_transport as C
from speakeasy.calls import INPUT_TEXT_TYPE, MAX_INPUT_TEXT, Interaction, ServiceError, typed_text
from speakeasy.prompt import builder as P
from speakeasy.service import VoiceService as ServiceVoice


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


# -- the text rule, against the table the apps run too ------------------------------------------------
# mac/Tests/SpeakeasyCoreTests/Contract/typed_text.json holds hand-written answers; SpeakeasyCore's
# TypedTextTests checks the app's normalizer against the same file, so a drift on either side fails.

CONTRACT = Path(__file__).resolve().parents[2] / "mac" / "Tests" / "SpeakeasyCoreTests" / "Contract"


def _vector_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return value["before"] + value["repeat"] * value["times"] + value["after"]


def _typed_text_table() -> dict[str, Any]:
    return json.loads((CONTRACT / "typed_text.json").read_text())


@pytest.mark.parametrize("vector", _typed_text_table()["vectors"], ids=lambda v: v["name"])
def test_typed_text_matches_the_shared_table(service, vector):
    raw, want = _vector_text(vector["input"]), _vector_text(vector["normalized"])
    assert typed_text(raw) == want
    assert len(want) == vector["scalars"]
    # The route's own checks decide the verdict: 400 with a reason, or (for text it would take) on to
    # the call lookup, which this unknown id fails.
    with pytest.raises(ServiceError) as caught:
        service.input_text("vi_contract_none", turn(raw))
    if vector["verdict"] == "ok":
        assert (caught.value.status, caught.value.reason) == (404, "unknown_interaction")
    else:
        assert (caught.value.status, caught.value.reason) == (400, vector["verdict"])


def test_typed_text_separators_are_exactly_the_shared_set():
    table = _typed_text_table()
    assert table["max_scalars"] == MAX_INPUT_TEXT
    separators = {c for lo, hi in table["separators"] for c in range(lo, hi + 1)}
    for code in range(0x110000):
        if 0xD800 <= code <= 0xDFFF:
            continue
        splits = typed_text(f"a{chr(code)}b") == "a b"
        assert splits == (code in separators), hex(code)


def test_the_apps_typed_turn_request_is_taken(server, service):
    """The request SpeakeasyCore builds (Contract/requests/call/input_text.json, written by
    ContractTests.testRecordCallRequests) goes through the real handler as one typed turn."""
    spec = json.loads((CONTRACT / "requests" / "call" / "input_text.json").read_text())
    assert spec["method"] == "POST"
    iid = open_call(server, "req_typed_contract")
    status, body = http(server.base_url, spec["method"], spec["path"].replace("{interaction_id}", iid),
                        spec["body"], server.token)
    assert status == 200 and body == {"interaction_id": iid, "type": INPUT_TEXT_TYPE, "sent": True}
    assert typed_events(service.interaction(iid)) == [
        {"role": "user", "text": typed_text(spec["body"]["text"]), "source": "typed"}]


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
    # Into the live session: one session-wide note quoting the words, no delegation id. Commentary,
    # not thinking: a thinking note is background the voice may hold silently (see TYPED_TURN_KIND).
    assert len(worker.sent) == 1
    kind, delegation_id, content = worker.sent[0]
    assert kind == "session.commentary.append" and delegation_id is None
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
        self.gate: threading.Event | None = None  # set: a user appendText blocks until it's released

    def request(self, method: str, params: dict | None = None, timeout: float = 20) -> dict:
        self.requests.append((method, params or {}))
        user_text = method == "thread/realtime/appendText" and (params or {}).get("role") == "user"
        if self.gate is not None and user_text:
            self.gate.wait(10)
        if self.fail is not None and user_text:
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
    # A developer cue first (treat it as said, never read it back), then the words as the user's turn.
    assert [p["role"] for p in appends] == ["developer", "user"]
    assert appends[1] == {"threadId": "thr_fake", "text": "Turn on the den lights", "role": "user"}
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
    worker.typed_echoes.append(["say it again", time.monotonic() - 60])
    run_on(worker, worker._user_turn("Say it again"))
    assert worker.fragments[-1]["text"] == "Say it again"


def test_codex_rejected_append_records_nothing_and_does_not_break_the_call(server, codex_call):
    interaction, worker, transport = codex_call
    transport.fail = C.CodexStartError("Codex request rejected")
    status, body = typed(server, interaction.interaction_id, turn("hello"))
    # The call is still up: this one send failed. Not a 409 "isn't connected", which would tell the
    # app the call dropped and steer the user away from a retry that works.
    assert status == 502 and body["reason"] == "not_delivered", body
    assert list(worker.fragments) == [] and list(worker.typed_echoes) == []
    assert typed_events(interaction) == []
    # Nothing sticky on the call: no error on its snapshot, and the next send goes through.
    assert interaction.error is None and interaction.snapshot()["error"] is None
    transport.fail = None
    status, body = typed(server, interaction.interaction_id, turn("hello again"))
    assert status == 200, body
    assert worker.turns() == [{"role": "user", "text": "hello again"}]


def test_codex_closed_transport_sends_nothing(server, codex_call):
    interaction, worker, transport = codex_call
    transport.closed = True
    status, body = typed(server, interaction.interaction_id, turn("hello"))
    assert status == 409 and body["reason"] == "not_connected"
    assert transport.requests == []


# -- Codex echo guard: bounded in time, consumed once, loose about wording ------------------------------

def user_texts(worker) -> list[str]:
    return [f["text"].strip() for f in worker.fragments if f["speaker"] == "user"]


def test_typed_turn_never_echoed_does_not_swallow_the_same_words_said_out_loud_later(server, codex_call):
    interaction, worker, transport = codex_call
    assert typed(server, interaction.interaction_id, turn("Turn on the den lights"))[0] == 200
    # Codex never transcribes the appended text back. The voice answers ...
    run_on(worker, worker.handle_event({"type": "session.output_transcript.delta", "delta": "Done.",
                                        "start_ms": 9, "end_ms": 9}))
    # ... and a while later the user says the very same words out loud: a new turn, recorded.
    worker.typed_echoes[0][1] -= C.TYPED_ECHO_S + 1
    run_on(worker, worker._user_turn("Turn on the den lights."))
    assert worker.turns() == [{"role": "user", "text": "Turn on the den lights"},
                              {"role": "assistant", "text": "Done."},
                              {"role": "user", "text": "Turn on the den lights."}]
    assert list(worker.typed_echoes) == []


def test_a_different_user_turn_ends_the_echo_window(server, codex_call):
    interaction, worker, transport = codex_call
    assert typed(server, interaction.interaction_id, turn("yes please"))[0] == 200
    # Still inside the window, but the user is now talking: whatever they say is theirs.
    run_on(worker, worker._user_turn("No wait"))
    run_on(worker, worker._user_turn("Yes please"))
    assert user_texts(worker) == ["yes please", "No wait", "Yes please"]


def test_an_echo_is_suppressed_once_only(server, codex_call):
    interaction, worker, transport = codex_call
    assert typed(server, interaction.interaction_id, turn("Turn on the den lights"))[0] == 200
    run_on(worker, worker._user_turn("Turn on the den lights."))  # the echo
    run_on(worker, worker._user_turn("Turn on the den lights"))   # the user, saying it again
    assert user_texts(worker) == ["Turn on the den lights", "Turn on the den lights"]


def test_a_reworded_echo_is_not_recorded_twice(server, codex_call):
    interaction, worker, transport = codex_call
    assert typed(server, interaction.interaction_id, turn("Turn on the den lights please"))[0] == 200
    run_on(worker, worker._user_turn("turn on the den light, please."))
    assert user_texts(worker) == ["Turn on the den lights please"]
    assert len(typed_events(interaction)) == 1


def test_echo_matching_is_loose_about_wording_but_exact_for_short_turns():
    assert C._same_words("What's up?", "whats up")
    assert C._same_words("Turn on the den lights please", "Turn on the den light, please.")
    assert not C._same_words("yes", "yeah")
    assert not C._same_words("turn it off", "turn it on")
    assert not C._same_words("Turn on the den lights", "Tell me about the weather tomorrow")
    assert not C._same_words("", "")


# -- client_id: a retry never reaches the call twice ---------------------------------------------------

def turn_with_id(text: str, client_id: str) -> dict[str, str]:
    return {"type": INPUT_TEXT_TYPE, "text": text, "client_id": client_id}


def test_the_same_client_id_twice_sends_once(server, service):
    iid = open_call(server, "req_typed_idem")
    worker = service.workers[-1]
    status, first = typed(server, iid, turn_with_id("Order more coffee", "turn_1"))
    assert status == 200
    assert first == {"interaction_id": iid, "type": INPUT_TEXT_TYPE, "sent": True, "client_id": "turn_1"}
    # The app didn't hear back (a dropped connection) and sends it again with the same id.
    status, again = typed(server, iid, turn_with_id("Order more coffee", "turn_1"))
    assert status == 200 and again == {**first, "duplicate": True}
    assert len(worker.sent) == 1 and worker.turns() == [{"role": "user", "text": "Order more coffee"}]
    # The SSE copy carries the id, so the sending app can tell it from its own local echo.
    assert typed_events(service.interaction(iid)) == [
        {"role": "user", "text": "Order more coffee", "source": "typed", "client_id": "turn_1"}]
    # A new id is a new turn.
    assert typed(server, iid, turn_with_id("Order more coffee", "turn_2"))[1].get("duplicate") is None
    assert len(worker.sent) == 2


def test_without_a_client_id_nothing_changes(server, service):
    iid = open_call(server, "req_typed_noid")
    worker = service.workers[-1]
    for _ in range(2):
        status, body = typed(server, iid, turn("Order more coffee"))
        assert (status, body) == (200, {"interaction_id": iid, "type": INPUT_TEXT_TYPE, "sent": True})
    assert len(worker.sent) == 2
    assert worker.turns() == [{"role": "user", "text": "Order more coffee Order more coffee"}]
    # (The SSE feed collapses identical consecutive payloads of a kind, as it always has, so two
    # identical id-less typed turns publish once; a client_id makes each copy distinct.)
    assert typed_events(service.interaction(iid)) == [
        {"role": "user", "text": "Order more coffee", "source": "typed"}]


def test_client_id_must_be_an_id_and_mean_one_message(server, service):
    iid = open_call(server, "req_typed_idbad")
    worker = service.workers[-1]
    for bad in ("", "has space", "x" * 129, 7, None):
        status, body = typed(server, iid, {"type": INPUT_TEXT_TYPE, "text": "hi", "client_id": bad})
        assert status == 400 and body["reason"] == "invalid_body", bad
    assert typed(server, iid, turn_with_id("hi", "turn_9"))[0] == 200
    status, body = typed(server, iid, turn_with_id("something else", "turn_9"))
    assert status == 409 and body["reason"] == "client_id_reused"
    assert len(worker.sent) == 1


def test_a_client_id_whose_send_failed_can_be_sent_again(server, service):
    iid = open_call(server, "req_typed_idfail")
    worker = service.workers[-1]
    real = worker.send_typed_note
    calls: list[str] = []

    async def flaky(kind, content):
        calls.append(kind)
        return False if len(calls) == 1 else await real(kind, content)
    worker.send_typed_note = flaky  # type: ignore[method-assign]
    status, body = typed(server, iid, turn_with_id("hello", "turn_f"))
    assert status == 502 and body["reason"] == "not_delivered"
    assert list(worker.fragments) == []
    status, body = typed(server, iid, turn_with_id("hello", "turn_f"))
    assert status == 200 and "duplicate" not in body  # the first one never got through
    assert worker.turns() == [{"role": "user", "text": "hello"}]


def test_a_client_id_is_one_turn_across_pause_and_resume(server, service):
    iid = open_call(server, "req_typed_idres")
    first = service.workers[-1]
    assert typed(server, iid, turn_with_id("Where were we?", "turn_r"))[0] == 200
    assert http(server.base_url, "POST", f"/voice/interactions/{iid}/pause", {}, server.token)[0] == 200
    status, resumed = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, "resume_from": iid},
                           server.token, {"Idempotency-Key": "req_typed_idres_2"})
    assert status == 201
    second = service.workers[-1]
    # The app retries on the resumed call's id: the first call already took it.
    status, body = typed(server, resumed["interaction_id"], turn_with_id("Where were we?", "turn_r"))
    assert status == 200 and body["duplicate"] is True and body["interaction_id"] == iid
    assert len(first.sent) == 1 and second.sent == []


# -- a failed send is that send's, not the call's ------------------------------------------------------

def test_a_failed_send_on_a_live_call_is_not_reported_as_disconnected(server, service):
    iid = open_call(server, "req_typed_iso")
    worker = service.workers[-1]
    real = worker.send_typed_note
    failures = [True]  # the first send fails, the socket stays up

    async def once_failing(kind, content):
        if failures:
            failures.pop()
            return False
        return await real(kind, content)
    worker.send_typed_note = once_failing  # type: ignore[method-assign]
    status, body = typed(server, iid, turn("first"))
    assert status == 502 and body["reason"] == "not_delivered"
    interaction = service.interaction(iid)
    assert interaction.error is None and interaction.snapshot()["error"] is None
    status, body = typed(server, iid, turn("second"))
    assert status == 200, body
    assert worker.turns() == [{"role": "user", "text": "second"}]


class FlakySocket:
    def __init__(self) -> None:
        self.fail: BaseException | None = None
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append(json.loads(raw))


def test_gpt_live_failed_typed_send_leaves_the_socket_alone(service):
    from websockets.exceptions import ConnectionClosedError
    from speakeasy.calls import TYPED_NOT_CONNECTED, TYPED_NOT_DELIVERED, TYPED_SENT
    from speakeasy.openai_live import OpenAISidebandWorker
    interaction = Interaction("vi_" + "g" * 32, "sess_fake")
    worker = OpenAISidebandWorker(service.rt, interaction, key_fn=lambda: "k")
    ws = worker.ws = FlakySocket()
    ws.fail = RuntimeError("one bad send")
    assert asyncio.run(worker.forward_typed("hello")) == TYPED_NOT_DELIVERED
    assert worker.ws is ws and worker.call_connected() and interaction.error is None
    ws.fail = None
    assert asyncio.run(worker.forward_typed("hello")) == TYPED_SENT
    assert ws.sent[-1]["type"] == "session.commentary.append" and ws.sent[-1]["delegation_id"] is None
    assert "\u201chello\u201d" in ws.sent[-1]["content"]
    # A socket that really closed is the call, not the send.
    ws.fail = ConnectionClosedError(None, None)
    assert asyncio.run(worker.forward_typed("hello")) == TYPED_NOT_CONNECTED
    assert worker.ws is None


# -- 504: the outcome isn't known, and the answer says so ------------------------------------------------

def test_timeout_without_client_id_is_honest_and_records_nothing(server, service, monkeypatch):
    from speakeasy import service as service_module
    monkeypatch.setattr(service_module, "INPUT_TEXT_WAIT_S", 0.3)
    iid = open_call(server, "req_typed_504")
    worker = service.workers[-1]

    async def stalled(kind, delegation_id, content):
        await asyncio.sleep(3)
    worker._send = stalled  # type: ignore[method-assign]
    status, body = typed(server, iid, turn("hello"))
    assert status == 504 and body["reason"] == "timeout"
    assert "may already have it" in body["error"]  # not "nothing was sent"
    time.sleep(0.5)  # the send was cancelled: nothing turns up in the transcript later
    assert list(worker.fragments) == [] and typed_events(service.interaction(iid)) == []


def test_timeout_with_client_id_lets_a_retry_learn_the_outcome(server, service, monkeypatch):
    from speakeasy import service as service_module
    monkeypatch.setattr(service_module, "INPUT_TEXT_WAIT_S", 0.3)
    iid = open_call(server, "req_typed_504id")
    worker = service.workers[-1]
    gate, sends = threading.Event(), []

    async def slow(kind, delegation_id, content):
        while not gate.is_set():
            await asyncio.sleep(0.02)
        sends.append(kind)
    worker._send = slow  # type: ignore[method-assign]
    status, body = typed(server, iid, turn_with_id("hello", "turn_t"))
    assert status == 504 and body["reason"] == "timeout" and "same client_id" in body["error"]
    # Still pending: a retry waits on the same send, it doesn't start a second one.
    assert typed(server, iid, turn_with_id("hello", "turn_t"))[0] == 504
    gate.set()
    wait_for(lambda: worker.fragments)
    status, body = typed(server, iid, turn_with_id("hello", "turn_t"))
    assert status == 200 and body["duplicate"] is True
    assert sends == ["session.commentary.append"] and worker.turns() == [{"role": "user", "text": "hello"}]
    assert len(typed_events(service.interaction(iid))) == 1


def test_codex_timeout_leaves_no_echo_guard_behind(server, codex_call, monkeypatch):
    from speakeasy import service as service_module
    monkeypatch.setattr(service_module, "INPUT_TEXT_WAIT_S", 0.3)
    interaction, worker, transport = codex_call
    transport.gate = threading.Event()
    try:
        status, body = typed(server, interaction.interaction_id, turn("Turn on the den lights"))
        assert status == 504 and body["reason"] == "timeout"
        wait_for(lambda: not worker.typed_echoes)  # dropped with the cancelled send
    finally:
        transport.gate.set()
    # Codex did get it after all and transcribes it back: that is the transcript's only copy, kept.
    run_on(worker, worker._user_turn("Turn on the den lights"))
    assert user_texts(worker) == ["Turn on the den lights"]
    assert typed_events(interaction) == []


class LandingAsTheWaitExpires:
    """A send that finishes in the same instant the 15 s wait gives up: `result(timeout)` raises
    TimeoutError, and by the time the route asks, the future is done and it was `sent`."""

    def __init__(self) -> None:
        self.cancelled = False

    def result(self, timeout: float | None = None) -> tuple[str, str]:
        if timeout is not None:
            raise TimeoutError()
        return ("sent", "vi_landed")

    def done(self) -> bool:
        return True

    def cancel(self) -> None:
        self.cancelled = True


def test_a_send_that_lands_as_the_wait_expires_is_reported_as_sent():
    # CWE-362: the wait and the send race. The turn is in the call, so the route must say `sent`;
    # a 502 here would tell the app nothing was recorded and invite a duplicate.
    future = LandingAsTheWaitExpires()
    assert ServiceVoice._typed_wait(future, retry_safe=False) == ("sent", "vi_landed")
    assert future.cancelled is False


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
