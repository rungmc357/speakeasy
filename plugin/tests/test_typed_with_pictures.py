"""A picture plus a typed note on the call screen is ONE message the work acts on: attaching says
nothing to the voice before Send, Send starts one task carrying the picture and the words, the voice
only acknowledges it (never reads the words back, never says it can't see it), and a typed turn whose
handoff Codex sends without words still becomes that turn's work."""
from __future__ import annotations

import asyncio
import base64
import threading
import time

import pytest

from fakes import SDP, FakeTransport, http, upload_attachment, wait_for
from speakeasy import calls
from speakeasy import codex_transport as C
from speakeasy.calls import INPUT_TEXT_TYPE, Interaction, said_back
from speakeasy import attachments as A
from speakeasy.prompt import builder as P

JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"
PICTURE = JPEG_HEAD + (b"layout-bug " * 800)[:8000 - len(JPEG_HEAD)]
NOTE = "the forecast card pushes the buttons off the screen"


def images_in(run_input) -> list[bytes]:
    if isinstance(run_input, str):
        return []
    return [base64.b64decode(p["image_url"]["url"].split(",", 1)[1])
            for p in run_input[0]["content"] if p["type"] == "image_url"]


def text_of(run_input) -> str:
    return run_input if isinstance(run_input, str) else run_input[0]["content"][0]["text"]


def typed(server, iid: str, text: str) -> tuple[int, dict]:
    return http(server.base_url, "POST", f"/voice/interactions/{iid}/input-text",
                {"type": INPUT_TEXT_TYPE, "text": text}, server.token)


@pytest.fixture
def fast(service, monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)
    service.hermes.image_support = {"images": True, "vision": "native"}


def open_call(server, service, key: str) -> tuple[str, calls.SidebandWorker]:
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, "screen": "ready"},
                           server.token, {"Idempotency-Key": key})
    assert status == 201, session
    return session["interaction_id"], service.workers[-1]


# -- GPT-Live (the fake live worker): one request with the picture and the words ---------------------

def test_picture_and_typed_note_go_as_one_request(server, service, hermes, fast):
    iid, worker = open_call(server, service, "req_typed_pic")
    assert upload_attachment(server.base_url, server.token, iid, PICTURE)[0] == 200
    time.sleep(0.3)
    assert not [s for s in worker.sent if s[0] == "session.commentary.append"], "attaching said something"
    assert not hermes.calls  # nothing goes before Send
    status, body = typed(server, iid, NOTE)
    assert status == 200 and body["sent"] is True
    wait_for(lambda: hermes.calls)
    time.sleep(0.5)
    assert len(hermes.calls) == 1, "one message, one task"
    run_input = hermes.calls[0]["input"]
    assert images_in(run_input) == [PICTURE]
    assert NOTE in text_of(run_input)
    # The voice got one note: it was handed off with the picture; acknowledge, don't repeat, don't look.
    spoken = [c for k, _, c in worker.sent if k == "session.commentary.append" and "typed" in c]
    assert len(spoken) == 1 and "already been handed off" in spoken[0] and "1 picture" in spoken[0]
    assert "can't see what they attached" in spoken[0] and "without repeating their message" in spoken[0]
    snap = http(server.base_url, "GET", f"/voice/interactions/{iid}", token=server.token)[1]
    assert snap["attachments"] == []  # it went with the message
    # The typed words are the user's turn, once; never the assistant's.
    assert [t for t in worker.turns() if t["role"] == "user"] == [{"role": "user", "text": NOTE}]
    assert not [t for t in worker.turns() if t["role"] == "assistant" and said_back(NOTE, t["text"])]


def test_the_voice_handing_the_same_words_off_again_starts_no_twin(server, service, hermes, fast):
    iid, worker = open_call(server, service, "req_typed_twin")
    assert upload_attachment(server.base_url, server.token, iid, PICTURE)[0] == 200
    assert typed(server, iid, NOTE)[0] == 200
    wait_for(lambda: hermes.calls)
    worker.feed({"type": "session.delegation.created", "offset_ms": 1 << 40,
                 "delegation": {"id": "call_again", "target": "client"}})
    time.sleep(1.0)
    assert len(hermes.calls) == 1


def test_a_spoken_correction_after_a_typed_picture_note_is_its_own_request(server, service, hermes, fast):
    # "on" typed under a picture, then "off" said out loud: one word apart, never the same request.
    iid, worker = open_call(server, service, "req_typed_correction")
    assert upload_attachment(server.base_url, server.token, iid, PICTURE)[0] == 200
    assert typed(server, iid, "turn the bedroom heater on")[0] == 200
    wait_for(lambda: hermes.calls)
    worker.feed({"type": "session.output_transcript.delta", "delta": "On it.",
                 "start_ms": 1 << 38, "end_ms": (1 << 38) + 1})
    worker.feed({"type": "session.input_transcript.delta", "delta": "Turn the bedroom heater off.",
                 "start_ms": 1 << 39, "end_ms": (1 << 39) + 1})
    worker.feed({"type": "session.delegation.created", "offset_ms": 1 << 40,
                 "delegation": {"id": "call_off", "target": "client"}})
    wait_for(lambda: len(hermes.calls) == 2)
    assert "heater off" in text_of(hermes.calls[1]["input"]).lower()


def test_a_one_word_note_under_a_picture_is_still_work(server, service, hermes, fast):
    iid, worker = open_call(server, service, "req_typed_short")
    assert upload_attachment(server.base_url, server.token, iid, PICTURE)[0] == 200
    assert typed(server, iid, "this")[0] == 200
    wait_for(lambda: hermes.calls)
    assert images_in(hermes.calls[0]["input"]) == [PICTURE]


def test_a_typed_turn_without_pictures_is_left_to_the_voice(server, service, hermes, fast):
    iid, worker = open_call(server, service, "req_typed_plain")
    assert typed(server, iid, "What's the weather tomorrow?")[0] == 200
    time.sleep(0.6)
    assert not hermes.calls  # the voice answers or hands it off, as if it had been said
    assert any("typed this" in c for _, _, c in worker.sent)


# -- the read-back check ------------------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "Also, the forecast card pushes the buttons off the screen.",
    "the forecast card pushes the buttons off the screen",
    "Okay, so the forecast card pushes the buttons off the screen.",
    "You typed: the forecast card pushes the buttons off the screen!",
])
def test_reading_the_typed_words_back_is_caught(said):
    assert said_back(NOTE, said)


@pytest.mark.parametrize("said", [
    "Got it, I'll look at why that card is pushing things off screen.",
    "On it.",
    "Which screen is the forecast card on?",
    # A near read-back with a word dropped stays visible: only exact read-backs are hidden.
    "Okay, so the forecast card pushes buttons off the screen.",
])
def test_a_real_reply_is_not_a_read_back(said):
    assert not said_back(NOTE, said)


@pytest.mark.parametrize("typed_words, said", [
    # an answer that reorders the question's words
    ("is the front door locked", "The front door is locked."),
    ("is the garage door open", "Okay, the garage door is open."),
    # one word changes the meaning
    ("turn the bedroom heater on", "Turn the bedroom heater off."),
    ("the porch light is on", "The porch light is not on."),
    ("move the meeting to friday", "Move the meeting to Thursday."),
])
def test_a_near_match_that_means_something_else_is_never_an_echo(typed_words, said):
    assert not said_back(typed_words, said)


def test_a_reply_to_a_typed_question_is_shown_while_the_read_back_is_hidden(server, codex_call):
    interaction, worker, transport = codex_call
    assert typed(server, interaction.interaction_id, "is the front door locked")[0] == 200
    for text in ("Is the front door locked?", "The front door is locked."):
        transport.notifications.put({"method": "thread/realtime/transcript/done",
                                     "params": {"threadId": "thr_fake", "role": "assistant", "text": text}})
    transport.notifications.put({"method": "thread/realtime/closed", "params": {"threadId": "thr_fake"}})
    run_on(worker, worker.run())
    assert worker.turns() == [{"role": "user", "text": "is the front door locked"},
                              {"role": "assistant", "text": "The front door is locked."}]


def test_short_words_need_an_exact_match():
    assert said_back("turn it off", "Turn it off.")
    assert not said_back("turn it off", "Turned it off.")


# -- Codex: silent attach, developer note on Send, wordless handoffs, no echo -----------------------

class RecordingTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        import queue
        self.notifications: queue.Queue = queue.Queue()

    def request(self, method: str, params: dict | None = None, timeout: float = 20) -> dict:
        self.requests.append((method, params or {}))
        return {}


@pytest.fixture
def codex_call(service):
    transport = RecordingTransport()
    interaction = Interaction("vi_" + "c" * 32, "thr_fake", attachments=A.CallAttachments("ready"))
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


def voice_requests(transport) -> list[tuple[str, dict]]:
    return [(m, p) for m, p in transport.requests if m.startswith("thread/realtime/append")]


def test_codex_attaching_says_nothing_until_send(server, service, hermes, fast, codex_call):
    interaction, worker, transport = codex_call
    assert upload_attachment(server.base_url, server.token, interaction.interaction_id, PICTURE)[0] == 200
    time.sleep(0.4)
    assert voice_requests(transport) == []  # held: Codex would answer it out loud
    assert any("1 picture" in n for n in worker.quiet_notes.values())
    status, _ = typed(server, interaction.interaction_id, NOTE)
    assert status == 200
    wait_for(lambda: hermes.calls)
    sent = voice_requests(transport)
    # One developer note for the message; never the user's words as text to say or answer.
    first = sent[0]
    assert first[0] == "thread/realtime/appendText" and first[1]["role"] == "developer"
    assert "already been handed off" in first[1]["text"] and NOTE in first[1]["text"]
    assert "waiting on the panel" not in first[1]["text"]  # the stale "picture waiting" note is gone
    assert not [p for m, p in sent if m == "thread/realtime/appendText" and p.get("role") == "user"]
    assert not [p for m, p in sent if m == "thread/realtime/appendSpeech" and NOTE in p["text"]]
    assert images_in(hermes.calls[0]["input"]) == [PICTURE] and NOTE in text_of(hermes.calls[0]["input"])
    time.sleep(0.4)
    assert len(hermes.calls) == 1


def test_codex_typed_turn_goes_with_a_cue_to_treat_it_as_speech(server, codex_call):
    interaction, worker, transport = codex_call
    assert typed(server, interaction.interaction_id, "Find why the checkout test fails")[0] == 200
    appends = [p for m, p in transport.requests if m == "thread/realtime/appendText"]
    assert [p["role"] for p in appends] == ["developer", "user"]
    assert "instead of saying it" in appends[0]["text"] and "Never repeat their words" in appends[0]["text"]
    assert appends[1]["text"] == "Find why the checkout test fails"


def test_codex_wordless_handoff_after_a_typed_turn_is_that_turn(server, service, hermes, fast, codex_call):
    interaction, worker, transport = codex_call
    assert typed(server, interaction.interaction_id, "Find why the checkout test fails")[0] == 200
    run_on(worker, worker._handoff({"type": "handoff_request", "handoff_id": "item_typed1",
                                    "input_transcript": "", "active_transcript": []}))
    wait_for(lambda: hermes.calls)
    assert "Find why the checkout test fails" in text_of(hermes.calls[0]["input"])
    # Used once: another empty handoff is still rejected.
    run_on(worker, worker._handoff({"type": "handoff_request", "handoff_id": "item_typed2",
                                    "input_transcript": "", "active_transcript": []}))
    time.sleep(0.6)
    assert len(hermes.calls) == 1


def test_codex_wordless_handoff_long_after_typing_is_still_rejected(server, codex_call):
    interaction, worker, transport = codex_call
    assert typed(server, interaction.interaction_id, "Find why the checkout test fails")[0] == 200
    worker.recent_typed[-1]["at"] -= calls.TYPED_HANDOFF_S + 1
    run_on(worker, worker._handoff({"type": "handoff_request", "handoff_id": "item_late1",
                                    "input_transcript": "", "active_transcript": []}))
    assert "item_late1" not in worker.delegations


def test_codex_reading_the_typed_words_back_is_never_its_line(server, codex_call):
    interaction, worker, transport = codex_call
    assert typed(server, interaction.interaction_id, NOTE)[0] == 200
    transport.notifications.put({"method": "thread/realtime/transcript/done",
                                 "params": {"threadId": "thr_fake", "role": "assistant",
                                            "text": "Also, the forecast card pushes the buttons off the screen."}})
    transport.notifications.put({"method": "thread/realtime/transcript/done",
                                 "params": {"threadId": "thr_fake", "role": "assistant",
                                            "text": "Which page is that on?"}})
    transport.notifications.put({"method": "thread/realtime/closed", "params": {"threadId": "thr_fake"}})
    run_on(worker, worker.run())
    assert worker.turns() == [{"role": "user", "text": NOTE},
                              {"role": "assistant", "text": "Which page is that on?"}]


def test_typed_work_note_wording():
    note = P.typed_work_note(P.Names("Todd", "Geo"), NOTE, 2, 1)
    assert "Geo just sent a typed message with 2 pictures and 1 file attached" in note
    assert "\u201c" + NOTE + "\u201d" in note
    assert "do NOT hand it off again" in note and "say you can't see it" in note
