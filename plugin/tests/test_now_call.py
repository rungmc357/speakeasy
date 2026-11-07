"""Home on a call (SPK-124 phase 3, now.py + calls.py): the card the voice is talking about lights up
(call.focus), a question asked on the call goes first (tier 0, "asks · say it or tap"), an answer read
out on the call counts as heard, a risky choice needs a tap, and an ended call leaves one "Your call"
card. Invented data only."""
from __future__ import annotations

import time
import types

from speakeasy.calls import BackendRun, Interaction
from test_history import get, state  # noqa: F401
from test_now import QUESTION, task


def live_call(service, interaction_id="vi_call", started=None):
    call = Interaction(interaction_id=interaction_id, live_session_id="live_" + interaction_id)
    call.status = "active"
    call.worker = types.SimpleNamespace(loop=None, pending_approvals=lambda: [])
    if started is not None:
        call.started_at = started
    with service.lock:
        service.interactions[interaction_id] = call
    return call


def on_call(call, task_id, key, status="completed"):
    with call.lock:
        call.runs[task_id] = BackendRun(task_id, 0, key, status=status)


def test_the_card_the_voice_is_talking_about_is_the_calls_focus(server, service, state):
    store, now = service.store, time.time()
    call = live_call(service, started=now - 300)
    task(store, "k_w", "working", session="s_w", request="Sort the garden photos", interaction="vi_call")
    task(store, "k_a", "completed", session="s_a", request="Compare tomato seeds", interaction="vi_call",
         result={"spoken": "Brandywine.", "headline": "Brandywine wins for your shade."}, settled=now - 20)
    on_call(call, "d_k_w", "k_w", "running")
    on_call(call, "d_k_a", "k_a")
    _, body = get(server, "/voice/now")
    assert body["call"]["focus"] is None
    with call.lock:
        call.talking_about, call.talking_at = "d_k_w", time.time()
    _, body = get(server, "/voice/now")
    assert body["call"]["focus"] == "s:s_w" and body["call"]["focus_label"] == "Talking about this"
    assert body["call_summary"] is None
    # Vision Pro's own focus wins over what the voice last spoke of.
    with call.lock:
        call.focus_task_id = "d_k_a"
    assert get(server, "/voice/now")[1]["call"]["focus"] == "s:s_a"


def test_an_answer_read_out_on_the_call_is_heard_and_counted_seen(server, service, state):
    store, now = service.store, time.time()
    call = live_call(service, started=now - 300)
    task(store, "k_h", "completed", session="s_h", request="Check the tire shop hours", interaction="vi_call",
         result={"spoken": "Open till six.", "headline": "Open till 6 PM today."}, settled=now - 10)
    on_call(call, "d_k_h", "k_h")
    _, body = get(server, "/voice/now")
    assert body["items"][0]["kind"] == "new_answer"
    with call.lock:
        call.spoken_tasks["d_k_h"] = time.time()
    item = get(server, "/voice/now")[1]["items"][0]
    assert item["kind"] == "seen_answer" and item["on_call"] == "heard" and item["unread"] is False
    # Marked read, but the card keeps the answer's own headline: never a stand-in like "Told you on the call."
    assert item["line"] == "Open till 6 PM today."
    assert "call" not in item["line"].lower()


def test_the_worker_marks_a_spoken_answer_seen_and_lights_it(service):
    from speakeasy.calls import SidebandWorker
    store, now = service.store, time.time()
    call = live_call(service, started=now - 60)
    task(store, "k_s", "completed", session="s_s", request="Find a plumber", interaction="vi_call",
         result={"spoken": "Two can come Tuesday."}, settled=now - 5)
    on_call(call, "d_k_s", "k_s")
    calls = []
    worker = SidebandWorker.__new__(SidebandWorker)
    worker.interaction, worker.store = call, store
    worker.rt = types.SimpleNamespace(now_changed=lambda: calls.append(1))
    worker.replied_task = "d_k_s"
    assert worker.replied_task == "d_k_s"
    assert store.is_seen("k_s") and call.talking_about == "d_k_s" and "d_k_s" in call.spoken_tasks
    assert calls == [1]


def test_a_question_asked_on_the_call_goes_first_and_says_how_to_answer(server, service, state):
    store, now = service.store, time.time()
    call = live_call(service, started=now - 300)
    task(store, "k_old", "completed", session="s_old", request="Pick a paint color",
         result={"spoken": "Two fit.", "question": {**QUESTION, "question": "Which paint?"}}, settled=now - 4000,
         created=now - 4100)
    task(store, "k_q", "completed", session="s_q", request="Desk hunt", interaction="vi_call",
         result={"spoken": "Four fit.", "question": QUESTION}, settled=now - 30)
    on_call(call, "d_k_q", "k_q")
    _, body = get(server, "/voice/now")
    first = body["items"][0]
    assert first["id"] == "q:run_k_q" and first["tier"] == 0 and first["on_call"] == "asking"
    assert first["say_ok"] is True and first["asking_label"] == "Asking now" and "lock_line" not in first
    assert body["items"][1]["tier"] == 1 and "on_call" not in body["items"][1]


def test_a_risky_choice_needs_a_tap_not_a_spoken_yes(server, service, state):
    from speakeasy.calls import SidebandWorker
    store, now = service.store, time.time()
    call = live_call(service, started=now - 300)
    risky = {**QUESTION, "question": "Send the order?",
             "options": [{"text": "Send it", "risk": "high"}, {"text": "Not yet"}]}
    task(store, "k_r", "completed", session="s_r", request="Order the desk", interaction="vi_call",
         result={"spoken": "Ready to send.", "question": risky}, settled=now - 30)
    on_call(call, "d_k_r", "k_r")
    item = get(server, "/voice/now")[1]["items"][0]
    assert item["say_ok"] is False and item["lock_line"] == "Can't be undone \u00b7 Needs a tap"
    worker = SidebandWorker.__new__(SidebandWorker)
    worker.interaction, worker.store = call, store
    assert worker.needs_a_tap("d_k_r") is True
    store.mark_answered("k_r", "Not yet")
    assert worker.needs_a_tap("d_k_r") is False


def test_an_ended_call_leaves_one_your_call_card_until_cleared(server, service, state):
    store, now = service.store, time.time()
    call = live_call(service, started=now - 400)
    task(store, "k_c", "completed", session="s_c", request="Compare tomato seeds", interaction="vi_call",
         result={"spoken": "Done.", "question": QUESTION}, settled=now - 100)
    store.mark_answered("k_c", "Oakline 60 in.")
    on_call(call, "d_k_c", "k_c")
    with call.lock:
        call.call_closed, call.ended_at = True, now - 40
    _, body = get(server, "/voice/now")
    assert body["call"] is None
    card = body["call_summary"]
    assert card["title"] == "Your call \u00b7 6 min" and card["minutes"] == 6
    assert card["things"] == ["Compare tomato seeds"] and card["decisions"] == ["Oakline 60 in."]
    assert card["session_id"] == "live_vi_call"
    from test_history import post
    post(server, "/voice/now/clear-seen", {})
    assert get(server, "/voice/now")[1]["call_summary"] is None
