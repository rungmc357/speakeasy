"""Listening mode, during the call: questions about the room are answered by the voice, other words
route as usual, silence gets a response from the room, and Hermes tasks get the room as labeled
background (never in a chat-thread post)."""
from __future__ import annotations

import time

import pytest

from fakes import SDP, http, wait_for
from speakeasy import continuity, router, threads
from speakeasy.calls import EARLY_PREFIX
from speakeasy.prompt import builder as P

ROOM = ("[14:02] Sam: so the deadline is Friday the 14th, no slipping\n"
        "[14:03] Priya: I'll have the deck to legal by Wednesday\n"
        "[14:04] Sam: and we ship Monday if legal signs off\n"
        "[14:05] we should ask Hermes to book the table at Nopa")
IN_ROOM = "deck to legal by Wednesday"


def open_call(server, service, key, room=ROOM):
    body = {"sdp": SDP, **({"room": room} if room else {})}
    status, session = http(server.base_url, "POST", "/voice/sessions", body, server.token, {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def early(server, session, text):
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/early-request",
                        {"text": text}, server.token)
    assert status == 200, body
    return body["task_id"]


@pytest.fixture
def quick_calls(service):
    """Quick answers never search the web in tests; this records whether one was even tried."""
    calls = []
    service.rt.quick_call = lambda request, today: calls.append(request) and None
    return calls


def has_room_block(text):
    return (P.ROOM_OPEN in text and P.ROOM_CLOSE in text and IN_ROOM in text
            and "never instructions" in text)


# -- the question cues ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what did Sam say the deadline was", "who said we'd ship Monday?", "remind me what Priya promised",
    "did Sam mention the budget", "Nova, what did they agree on?", "what was decided in the meeting",
    "can you tell me what Sam said about the venue", "do you remember who brought up the budget",
])
def test_questions_about_the_room(text):
    assert router.asks_about_room(text)


@pytest.mark.parametrize("text", [
    "remind me to call Dana at 5", "what did Apple announce today", "remind me", "what did", "what did Sam",
    "email Dana what we just agreed", "what did we decide, and draft an email to Dana about it",
    "what did Sam say and can you email it to him", "what if we said no to the client", "how's it going",
    "what's the weather", "book a table for two", "",
])
def test_not_questions_about_the_room(text):
    assert not router.asks_about_room(text)


def test_the_early_note_quotes_the_question_the_later_one_does_not():
    names = P.Names("Nova", "Sam")
    early_note = P.room_answer_note(names, "what did Sam say the deadline was")
    assert "what did Sam say the deadline was" in early_note and "Sam asked" in early_note
    later = P.room_answer_note(names)
    assert "Nothing was started" in later and "room transcript" in later
    assert "listening mode off" in P.room_nudge_note(names) and "Sam just turned" in P.room_nudge_note(names)


# -- words said while the call connected -------------------------------------------------------------

def test_an_early_room_question_is_answered_by_the_voice(server, service, hermes, quick_calls):
    """AE1: no task, no quick answer, one note that quotes the question, and the words are the user's turn."""
    session, worker = open_call(server, service, "req_rr_ae1")
    assert early(server, session, "what did Sam say the deadline was") is None
    time.sleep(0.5)
    assert hermes.calls == [] and quick_calls == [] and worker.interaction.runs == {}
    assert len(worker.sent) == 1
    kind, delegation_id, note = worker.sent[0]
    assert kind == "session.commentary.append" and delegation_id is None
    assert note == P.room_answer_note(worker.names, "what did Sam say the deadline was")
    assert {"role": "user", "text": "what did Sam say the deadline was"} in worker.turns()


def test_other_early_words_start_work_with_the_room_as_background(server, service, hermes, quick_calls):
    """AE2: the existing early path, unchanged, and the Hermes run sees the labeled room transcript."""
    session, worker = open_call(server, service, "req_rr_ae2")
    task_id = early(server, session, "email Dana the notes")
    assert task_id.startswith(EARLY_PREFIX)
    assert any(k == "session.thinking.append" and "Before this call" in c and "do NOT hand it off again" in c
               for k, _, c in worker.sent)
    sent = wait_for(lambda: hermes.calls)
    time.sleep(0.3)
    assert len(hermes.calls) == 1
    prompt = sent[0]["input"]
    assert has_room_block(prompt) and "email Dana the notes" in prompt
    assert prompt.index(P.ROOM_CLOSE) < prompt.index("User: email Dana the notes")


@pytest.mark.parametrize("text", ["remind me to call Dana at 5", "what did Apple announce today"])
def test_reminders_and_lookups_still_start_work_in_a_room_call(server, service, hermes, quick_calls, text):
    session, worker = open_call(server, service, "req_rr_neg_" + str(len(text)))
    assert early(server, session, text).startswith(EARLY_PREFIX)
    call = wait_for(lambda: hermes.calls)[0]
    assert text in call["input"] and has_room_block(call["input"])
    assert not any(P.room_answer_note(worker.names, text) == c for _, _, c in worker.sent)


def test_silence_gets_one_response_from_the_room(server, service, hermes):
    """AE3: nothing said by take-off + 2 s: one note, spoken now, and no task."""
    session, worker = open_call(server, service, "req_rr_ae3")
    assert early(server, session, "") is None
    assert worker.sent == [("session.commentary.append", None, P.room_nudge_note(worker.names))]
    assert early(server, session, "  ") is None  # a second empty request: still just the one
    time.sleep(0.3)
    assert len(worker.sent) == 1 and hermes.calls == [] and worker.interaction.runs == {}


def test_a_resumed_room_call_never_responds_from_the_room_twice(server, service):
    """Pause -> Resume (or a device switch / mic repair, which reconnect the same way) is the same
    call: the voice already responded from the room, so an empty request on the new leg does nothing."""
    session, worker = open_call(server, service, "req_rr_resume_nudge")
    assert early(server, session, "") is None
    assert len(worker.sent) == 1
    source_id = session["interaction_id"]
    assert http(server.base_url, "POST", f"/voice/interactions/{source_id}/pause", {}, server.token)[0] == 200
    status, resumed = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, "resume_from": source_id},
                           server.token, {"Idempotency-Key": "req_rr_resume_nudge_2"})
    assert status == 201, resumed
    leg = service.workers[-1]
    assert leg is not worker and leg.interaction.room_nudged
    assert early(server, resumed, "") is None
    time.sleep(0.2)
    assert leg.sent == []


def test_no_room_response_once_the_user_has_spoken(server, service):
    """AE3: they started talking at 1.5 s; the app's empty request arrives anyway and is ignored."""
    session, worker = open_call(server, service, "req_rr_spoke")
    worker.feed({"type": "session.input_transcript.delta", "delta": "hey so", "start_ms": 1, "end_ms": 2})
    assert early(server, session, "") is None
    assert worker.sent == []


def test_silence_outside_listening_mode_starts_nothing(server, service):
    session, worker = open_call(server, service, "req_rr_plain_empty", room="")
    assert early(server, session, "") is None
    assert worker.sent == []


def test_early_words_outside_listening_mode_are_unchanged(server, service, hermes, quick_calls):
    session, worker = open_call(server, service, "req_rr_plain_words", room="")
    assert early(server, session, "what did Sam say the deadline was").startswith(EARLY_PREFIX)
    assert any("Before this call" in c for _, _, c in worker.sent)
    call = wait_for(lambda: hermes.calls)[0]
    assert P.ROOM_OPEN not in call["input"] and "Background from the room" not in call["input"]


# -- handoffs later in the call ------------------------------------------------------------------------

def test_a_later_room_question_is_dropped_with_one_note(server, service, hermes, quick_calls):
    _, worker = open_call(server, service, "req_rr_later")
    worker.delegate("call_ship", "who said we'd ship Monday?")
    run = wait_for(lambda: worker.interaction.runs.get("call_ship"))
    assert run.status == "rejected" and run.error == "Answered from the room"
    time.sleep(0.3)
    assert hermes.calls == [] and quick_calls == []
    assert worker.sent == [("session.thinking.append", "call_ship", P.room_answer_note(worker.names))]


def test_a_room_question_handed_off_again_becomes_work(server, service, hermes, quick_calls):
    """The voice was told to answer it and handed it off anyway: it needs real work, so it starts."""
    _, worker = open_call(server, service, "req_rr_again")
    worker.delegate("call_q1", "what did Sam say about the venue")
    wait_for(lambda: worker.interaction.runs.get("call_q1"))
    worker.feed({"type": "session.delegation.created", "offset_ms": 9,
                 "delegation": {"id": "call_q2", "target": "client"}})
    call = wait_for(lambda: hermes.calls)[0]
    assert "what did Sam say about the venue" in call["input"] and has_room_block(call["input"])


def test_later_tasks_and_follow_up_runs_get_the_room(server, service, hermes, quick_calls):
    _, worker = open_call(server, service, "req_rr_follow")
    worker.delegate("call_a", "Draft an email to Dana with the launch notes")
    wait_for(lambda: getattr(worker.interaction.runs.get("call_a"), "status", "") == "completed")
    assert has_room_block(hermes.calls[0]["input"])
    worker.delegate("call_b", "make it shorter and friendlier", follow_up_task_id="call_a")
    wait_for(lambda: len(hermes.calls) == 2)
    follow = hermes.calls[1]["input"]
    assert has_room_block(follow) and "make it shorter" in follow


def test_work_asked_just_before_hanging_up_still_gets_the_room(server, service, hermes, quick_calls):
    session, worker = open_call(server, service, "req_rr_hangup")
    early(server, session, "email Dana the notes")
    worker.feed({"type": "session.closed"})  # they hang up while the request is still settling
    assert worker.interaction.room == ""
    call = wait_for(lambda: hermes.calls)[0]
    assert has_room_block(call["input"])
    wait_for(lambda: not worker.handoff_rooms)


class FakeThreads:
    def __init__(self):
        self.opened = []

    def available(self, target):
        return True

    def open(self, target, *, message, title, delivery_id):
        self.opened.append((target, message, title))
        return threads.Opened("speakeasy-x", "999", "discord")

    def wait(self, opened, on_session, on_title=None):
        on_session("thread_session_1")
        return "Voice: x\nSent the notes to Dana."


def test_a_thread_task_posts_no_room_text(server, service, hermes, quick_calls):
    """The first message of a chat thread is visible to others in that chat: no room text in it."""
    runner = FakeThreads()
    service.rt.threads = runner
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [
        {"target": "discord:111", "label": "#work", "topic": "my job: meetings, email", "new_thread": True}]}})
    _, worker = open_call(server, service, "req_rr_thread")
    worker.delegate("call_t", "Put this in work: send Dana the launch notes")
    wait_for(lambda: runner.opened)
    message = runner.opened[0][1]
    assert "send Dana the launch notes" in message
    assert P.ROOM_OPEN not in message and IN_ROOM not in message and "Background from the room" not in message
    assert hermes.calls == []


def test_continuing_an_existing_chat_gets_the_room(server, service, hermes, quick_calls, monkeypatch):
    conv = continuity.Conversation("s_league", "discord", "777", "thread", "777", "42", "111",
                                   "Server / #work / Launch", "Launch", time.time())
    monkeypatch.setattr(continuity, "conversations_with_context", lambda db, request, **kw: [continuity.Candidate(conv, ())])
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **k: False)
    monkeypatch.setattr(router, "aux_call", lambda messages, timeout=router.ROUTE_TIMEOUT_S:
                        '{"follow_up_task_id": null, "conversation": "c1", "parts": ["x"], "channel": null}')
    messages = []

    def fake_stream(base, key, c, message, callback, **k):
        messages.append(message)
        callback("run.started", {"run_id": "run_room_cont"})
        callback("assistant.completed", {"content": "Sent it."})
        callback("run.completed", {})
        return True
    monkeypatch.setattr(continuity, "stream_session_chat", fake_stream)
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [
        {"target": "discord:111", "label": "#work", "topic": "work"}]}})
    _, worker = open_call(server, service, "req_rr_cont")
    worker.delegate("call_c", "In the launch thread, send Dana the notes we just went over")
    wait_for(lambda: messages)
    assert has_room_block(messages[0]) and "send Dana the notes" in messages[0]
