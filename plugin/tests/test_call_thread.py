"""SPK-266: one thread for Home and the call. What the user says on a call and what the voice says
back both land in Home's thread as messages (one per spoken turn, kept after the call); a message
typed during a call goes to Home's coordinator, never to the voice, and the voice gets a quiet note
of it; a job started by text mid-call is on the call's own list. Invented data only."""
from __future__ import annotations

import time

from fakes import http, wait_for
from test_home_chat import coordinator, post, stream
from test_input_text import open_call


def say(worker, role: str, words: str, ms: int = 0) -> None:
    kind = "session.input_transcript.delta" if role == "user" else "session.output_transcript.delta"
    worker.feed({"type": kind, "delta": words, "start_ms": ms, "end_ms": ms + 500})


def messages(server) -> list[dict]:
    return [e for e in stream(server) if e["kind"] == "message"]


def test_capability_is_advertised(server):
    status, body = http(server.base_url, "GET", "/voice/status", None, server.token)
    assert status == 200 and body["capabilities"]["call_thread"] is True


def test_a_voice_turn_adds_both_bubbles_to_home(server, service):
    open_call(server, "req_thread_1")
    worker = service.workers[-1]
    say(worker, "user", "How is the patio quote coming along?")
    say(worker, "assistant", "It's nearly done, ")
    say(worker, "assistant", "I'm checking the last two prices.")
    worker.mirror_turns(final=True)
    got = messages(server)
    assert [(m["role"], m["text"]) for m in got] == [
        ("user", "How is the patio quote coming along?"),
        ("assistant", "It's nearly done, I'm checking the last two prices.")]
    assert all(m["call"]["id"] == worker.interaction.interaction_id and m["call"]["typed"] is False for m in got)
    assert all("model" not in m for m in got)   # N13: no model label on Home


def test_a_turn_written_early_is_rewritten_in_place_never_twice(server, service):
    open_call(server, "req_thread_grow")
    worker = service.workers[-1]
    say(worker, "assistant", "Two of the three")
    worker.mirror_turns(final=True)
    say(worker, "assistant", " prices are in.")
    say(worker, "user", "Great, thanks.")   # the change of speaker writes the voice's finished turn
    worker.mirror_turns(final=True)
    got = messages(server)
    assert [(m["role"], m["text"]) for m in got] == [("assistant", "Two of the three prices are in."),
                                                    ("user", "Great, thanks.")]


def test_the_quiet_timer_writes_the_open_turn(server, service):
    open_call(server, "req_thread_quiet")
    worker = service.workers[-1]
    worker.MIRROR_QUIET_S = 0.05
    say(worker, "assistant", "The forecast says light rain after six.")
    assert wait_for(lambda: messages(server), timeout=3)[0]["text"] == "The forecast says light rain after six."


def test_the_call_turns_stay_after_the_call(server, service):
    iid = open_call(server, "req_thread_end")
    worker = service.workers[-1]
    say(worker, "user", "What time does the hardware store close?")
    say(worker, "assistant", "It closes at nine tonight.")
    status, _ = http(server.base_url, "POST", f"/voice/interactions/{iid}/end", {}, server.token)
    assert status in (200, 202)
    got = wait_for(lambda: len(messages(server)) == 2 and messages(server))
    assert got[1]["text"] == "It closes at nine tonight."


def test_typed_mid_call_goes_to_the_coordinator_not_the_voice(server, service, hermes):
    open_call(server, "req_thread_typed")
    worker = service.workers[-1]
    seen = coordinator(service, {"reply": "Hey! Want me to look into anything?", "action": "none"})
    status, body = post(server, {"text": "yo", "client_id": "c_mid_yo"})
    assert status == 200 and body["reply"]["text"] == "Hey! Want me to look into anything?"
    assert seen, "the coordinator never saw it"
    # The voice got only a quiet note (never commentary it must answer out loud).
    notes = wait_for(lambda: [s for s in worker.sent if "typed in the app" in s[2]])
    kind, delegation_id, content = notes[0]
    assert kind == "session.thinking.append" and delegation_id is None
    assert "\u201cyo\u201d" in content and "Home replied on screen" in content and "Don't answer it" in content
    assert not any(s[0] == "session.commentary.append" for s in worker.sent)
    assert not hermes.calls   # small talk started no job
    assert [(m["role"], m["text"]) for m in messages(server)][-2:] == [
        ("user", "yo"), ("assistant", "Hey! Want me to look into anything?")]


def test_a_job_started_by_text_mid_call_is_on_the_calls_list(server, service, hermes):
    iid = open_call(server, "req_thread_job")
    worker = service.workers[-1]
    coordinator(service, {"reply": "Looking for a late-night pharmacy near you.", "action": "start",
                          "request": "Find a pharmacy open after 10 pm near the office"})
    status, body = post(server, {"text": "find a pharmacy open late near the office", "client_id": "c_mid_job"})
    assert status == 200 and body["action"] == "start" and body["task_id"]
    interaction = service.interaction(iid)
    with interaction.lock:
        assert body["task_id"] in interaction.runs   # the voice sees it as open work
    note = wait_for(lambda: [s for s in worker.sent if "That started a job" in s[2]])[0][2]
    assert "pharmacy" in note.lower()


def test_no_message_is_lost_when_the_coordinator_fails(server, service, monkeypatch):
    open_call(server, "req_thread_fail")
    coordinator(service, {"reply": "Checking the gutter quote.", "action": "start", "request": "Check the gutter quote"})

    def boom(*_a, **_k):
        raise RuntimeError("down")
    monkeypatch.setattr(service, "start_job", boom)
    status, _ = post(server, {"text": "check the gutter quote", "client_id": "c_mid_fail"})
    assert status >= 500
    got = messages(server)
    # The typed words stay as a bubble, and the thread says it didn't go through.
    assert ("user", "check the gutter quote") in [(m["role"], m["text"]) for m in got]
    assert got[-1]["role"] == "assistant" and "didn't go through" in got[-1]["text"]
    mine = next(m for m in got if m["role"] == "user")
    assert mine["failed"] is True and mine["client_id"] == "c_mid_fail"   # the app offers Retry on it


def test_a_calls_job_needs_no_extra_ask_bubble_when_its_turns_show(server, service, hermes):
    open_call(server, "req_thread_ask")
    worker = service.workers[-1]
    worker.delegate("d_thread_1", "Find a quiet cafe near the river")
    say(worker, "assistant", "Sure, looking now.")
    worker.mirror_turns(final=True)
    wait_for(lambda: [e for e in stream(server) if e["kind"] == "card"])
    entries = stream(server)
    assert not [e for e in entries if e["kind"] == "ask"]
    assert ("user", "Find a quiet cafe near the river") in [(e.get("role"), e.get("text")) for e in entries]


def test_wall_clock_times_follow_when_it_was_said(server, service):
    open_call(server, "req_thread_time")
    worker = service.workers[-1]
    before = time.time()
    say(worker, "user", "Is the garage door closed?")
    worker.mirror_turns(final=True)
    at = messages(server)[0]["at"]
    assert before - 1 <= at <= time.time() + 1
