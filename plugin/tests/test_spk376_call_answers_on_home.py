"""SPK-376: on a call, the voice reads an answer out the moment it lands, which marks it heard
(``seen_answer``). In-call Home shows only the widget stack (and at most one Working card), so this
call's answers must join the stack heard or not; running work never does, and an older answer the
call merely brought in doesn't come back. Invented data only."""
from __future__ import annotations

import time

from test_history import get, state  # noqa: F401
from test_now import task
from test_now_call import live_call, on_call


def _stack_runs(body):
    return [i["run_id"] for i in body["stack"]["items"] if i.get("run_id")]


def _two_heard_answers(service, now):
    store = service.store
    call = live_call(service, started=now - 300)
    task(store, "k_tide", "completed", session="s_tide", request="When is low tide at the cove", interaction="vi_call",
         result={"spoken": "Low tide is at 4:10.", "headline": "Low tide 4:10 PM at the cove."}, settled=now - 120,
         created=now - 200)
    task(store, "k_bike", "completed", session="s_bike", request="Is the bike shop open Sunday", interaction="vi_call",
         result={"spoken": "Yes, ten to four.", "headline": "Bike shop: Sun 10 AM-4 PM."}, settled=now - 40,
         created=now - 100)
    task(store, "k_kayak", "working", session="s_kayak", request="Find a kayak rental near the cove",
         interaction="vi_call", created=now - 30)
    on_call(call, "d_k_tide", "k_tide")
    on_call(call, "d_k_bike", "k_bike")
    on_call(call, "d_k_kayak", "k_kayak", "running")
    with call.lock:   # the voice read both out as they landed
        call.spoken_tasks.update({"d_k_tide": now - 115, "d_k_bike": now - 35})
    store.mark_seen("k_tide")
    store.mark_seen("k_bike")
    return call


def test_a_call_that_hears_two_answers_puts_both_on_homes_stack(server, service, state):
    now = time.time()
    _two_heard_answers(service, now)
    body = get(server, "/voice/now")[1]
    heard = {i["run_id"]: i for i in body["items"] if i.get("run_id") in {"run_k_tide", "run_k_bike"}}
    assert {i["kind"] for i in heard.values()} == {"seen_answer"}, "heard on the call (the trigger of the bug)"
    stack = {i["run_id"]: i for i in body["stack"]["items"]}
    assert {"run_k_tide", "run_k_bike"} <= set(stack)
    for run in ("run_k_tide", "run_k_bike"):
        assert stack[run]["kind"] == "result" and stack[run]["needs_you"] is False
        assert stack[run]["card_id"] == "c:" + run
    # Newest result first, like every other result on the stack.
    assert _stack_runs(body).index("run_k_bike") < _stack_runs(body).index("run_k_tide")
    assert stack["run_k_bike"]["line"] == "Bike shop: Sun 10 AM-4 PM."


def test_running_call_work_stays_off_the_stack(server, service, state):
    now = time.time()
    _two_heard_answers(service, now)
    body = get(server, "/voice/now")[1]
    assert "run_k_kayak" not in _stack_runs(body), "the Working card is the only place running work shows"
    working = [i for i in body["items"] if i.get("run_id") == "run_k_kayak"]
    assert working and working[0]["kind"] == "working" and working[0]["on_current_call"] is True


def test_the_calls_answers_stay_on_the_stack_after_hang_up(server, service, state):
    now = time.time()
    call = _two_heard_answers(service, now)
    get(server, "/voice/now")
    with call.lock:
        call.call_closed, call.ended_at, call.status = True, now - 5, "closed"
    body = get(server, "/voice/now")[1]
    assert {"run_k_tide", "run_k_bike"} <= set(_stack_runs(body))


def test_an_older_answer_the_call_brings_in_does_not_come_back(server, service, state):
    now = time.time()
    store = service.store
    task(store, "k_old", "completed", session="s_old", request="Compare two tents", interaction="vi_before",
         result={"spoken": "The green one.", "headline": "Green tent wins."}, settled=now - 900, created=now - 1000)
    store.mark_seen("k_old")   # opened before this call
    call = live_call(service, started=now - 300)
    on_call(call, "d_k_old", "k_old")
    with call.lock:
        call.spoken_tasks["d_k_old"] = now - 60
    body = get(server, "/voice/now")[1]
    assert "run_k_old" not in _stack_runs(body)


def test_off_a_call_a_seen_answer_still_does_not_join_the_stack(server, service, state):
    now = time.time()
    task(service.store, "k_read", "completed", session="s_read", request="Check the library hours",
         result={"spoken": "Till eight.", "headline": "Library open till 8 PM."}, settled=now - 60, created=now - 120)
    service.store.mark_seen("k_read")
    assert "run_k_read" not in _stack_runs(get(server, "/voice/now")[1])
