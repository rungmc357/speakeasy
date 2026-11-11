"""home-live-fix (build 74 live feedback, QA 74 lane F): finished work reaches Home's widget stack
whether or not it was read, heard on a call or belongs to the call that just ended (F1); Home's first
words land inside the 2.5 s deadline counted from the request's arrival (F5); an automatic plugin
reload never lands between a call's connect retries (F4). Invented data only."""
from __future__ import annotations

import logging
import threading
import time

import pytest

from speakeasy import home_chat as HC
from speakeasy import home_state as HS
from speakeasy import service as service_module
from test_b73_home_state import NOON, _now, frozen  # noqa: F401
from test_home_ack import post, script
from test_now import task

VIEWS = {"headline": "Three widgets", "spoken": "Done.",
         "views": [{"kind": "weather", "place": "Brooklyn", "temp": 51, "unit": "F"}]}
BASE = {"id": "s:x", "run_id": "run_4767", "title": "Generate home screen widgets",
        "line": "Flight, weather and a countdown", "finished_at": 1}
LANE_F_CASES = {
    "typed Home job, finished, unread": {**BASE, "kind": "new_answer"},
    "call job, finished during the call and told on it": {**BASE, "kind": "seen_answer", "on_call": "heard"},
    "call job, finished after the call ended": {**BASE, "kind": "new_answer", "call_id": "call:vi_05d2"},
    "any job read before the next Home poll": {**BASE, "kind": "seen_answer"},
}


# -- F1: eligibility depends on the result ----------------------------------------------------------

@pytest.mark.parametrize("case", list(LANE_F_CASES))
def test_lane_f_stack_repro_cases_all_enter_when_the_result_has_something_to_show(case):
    got = HS.candidates([LANE_F_CASES[case]], eligible=lambda run: True)
    assert [c["id"] for c in got] == ["w:r:run_4767"], case


def test_a_read_or_heard_answer_with_nothing_to_show_stays_off_the_stack():
    chatter = [{**BASE, "id": f"s:{n}", "run_id": f"run_{n}", "kind": "seen_answer"} for n in range(12)]
    assert HS.candidates(chatter, eligible=lambda run: False) == []
    # ...while an unread answer still enters as before, and a canceled one never does.
    assert len(HS.candidates([{**BASE, "kind": "new_answer"}], eligible=lambda run: False)) == 1
    assert HS.candidates([{**BASE, "kind": "seen_answer", "canceled": True}], eligible=lambda run: True) == []


@pytest.mark.parametrize("result,drafts,worth", [
    (VIEWS, None, True),
    ({"spoken": "Here.", "cards": [{"kind": "image", "path": "/tmp/x.png"}]}, None, True),
    ({"spoken": "Here.", "files": [{"name": "quote.pdf"}]}, None, True),
    ({"spoken": "Which one?", "question": {"kind": "question", "options": [{"text": "A"}]}}, None, True),
    ({"spoken": "Drafted it."}, [{"id": "d1"}], True),
    ({"spoken": "Sure, done.", "headline": "Done"}, None, False),
    (None, None, False),
])
def test_what_counts_as_something_to_show(result, drafts, worth):
    assert HS.substantive(result, drafts) is worth


def _call_job(service, key, *, result, settled, call="vi_call_1"):
    run = task(service.store, key, "completed", session="s_" + key, request="Make widgets for the trip",
               result=result, settled=settled, created=settled - 300, interaction=call)
    service.store.mark_seen(key)   # told on the call: read
    return run


def test_a_call_job_spoken_on_the_call_enters_the_stack(service, frozen):
    run = _call_job(service, "widgets", result=VIEWS, settled=NOON - 60)
    ids = [i["id"] for i in _now(service)["stack"]["items"]]
    assert f"w:r:{run}" in ids


def test_the_stack_does_not_flood_with_view_less_chatter(service, frozen):
    for n in range(10):
        _call_job(service, f"chat{n}", result={"spoken": f"Sure thing {n}.", "headline": "Done"},
                  settled=NOON - 600 + n)
    _call_job(service, "widgets", result=VIEWS, settled=NOON - 30)
    stack = _now(service)["stack"]
    assert [i["id"] for i in stack["items"]] == ["w:r:run_widgets"] and stack["count"] == 1


def test_opened_in_full_leaves_the_stack_and_one_opened_before_it_entered_never_enters(server, service, frozen):
    run = _call_job(service, "widgets", result=VIEWS, settled=NOON - 60)
    assert [i["id"] for i in _now(service)["stack"]["items"]] == [f"w:r:{run}"]
    service.answer_seen(run, {})   # POST /voice/tasks/{run}/seen: opened in an app
    assert _now(service)["stack"]["items"] == []
    late = _call_job(service, "late", result=VIEWS, settled=NOON - 30)
    service.answer_seen(late, {})
    assert _now(service)["stack"]["items"] == []


def test_a_result_still_leaves_after_result_kept_s(service, frozen):
    run = _call_job(service, "widgets", result=VIEWS, settled=NOON - 60)
    assert _now(service)["stack"]["count"] == 1
    frozen(NOON + HS.RESULT_KEPT_S + 1)
    assert run not in [i.get("run_id") for i in _now(service)["stack"]["items"]]


def test_new_since_you_last_looked_comes_first(service, frozen):
    task(service.store, "desk", "completed", session="s_desk", request="Order a desk",
         result={"spoken": "Two fit.", "question": {"kind": "question", "question": "Which desk?",
                                                    "options": [{"text": "Oakline"}, {"text": "Birch"}]}},
         settled=NOON - 3600, created=NOON - 3660)
    frozen(NOON - 3600)
    assert [i["kind"] for i in _now(service)["stack"]["items"]] == ["decision"]
    frozen(NOON)                                    # away for an hour: a new look
    _now(service)
    _call_job(service, "widgets", result=VIEWS, settled=NOON + 30)
    frozen(NOON + 60)
    kinds = [i["kind"] for i in _now(service)["stack"]["items"]]
    assert kinds == ["result", "decision"]          # the new result ahead of the older decision


def test_the_job_prompt_never_lets_a_result_claim_where_its_cards_show():
    from speakeasy.prompt import builder
    assert "Never say where cards show" in builder.VIEWS_RULE


def test_a_typed_home_flight_job_read_before_the_next_poll_lands_on_clean_home(service, frozen):
    """Live repro 16:05 Nov 11 (the Dino flight run): a typed Home turn whose job finished with a
    flight view and was marked read (seen) before Home's next poll. Build 74 kept it off the stack
    (seen_answer), so clean Home kept the exchange's card as the only trace of it. Within one poll it
    must be finished in items and the exchange, and its widget on the stack, flagged as this exchange's."""
    flight = {"headline": "DL 2195 in the air, arriving LGA 12 minutes early", "spoken": "It's in the air.",
              "views": [{"kind": "flight", "title": "DL 2195", "status": "En route"}]}
    chat = service.home_chat
    chat.clock = service.now_model.clock                 # Home's chat on the same invented clock
    mid = chat._add("user", "what's the flight info", NOON - 270)
    chat._add("assistant", "Checking the flight.", NOON - 267, reply_to=mid, part="ack")
    run = task(service.store, "flight", "completed", session="s_flight", request="what's the flight info",
               result=flight, settled=NOON - 130, created=NOON - 268)
    chat._link(mid, run, "start")
    service.store.mark_seen("flight")
    body = _now(service)
    (item,) = [i for i in body["items"] if i.get("run_id") == run]
    assert item["kind"] == "seen_answer"                 # finished: nothing for a Working card to show
    cur = body["stream"]["current"]
    assert run in cur["run_ids"] and not cur["clean"]
    (card,) = [e for e in body["stream"]["entries"] if e["id"] in cur["card_ids"]]
    assert card["state"] == "seen_answer"
    (widget,) = [i for i in body["stack"]["items"] if i.get("run_id") == run]
    assert widget["kind"] == "result" and widget["in_current"] is True


# -- F5: first words inside the deadline, counted from arrival ---------------------------------------

def test_the_models_ack_goes_up_as_soon_as_it_is_ready_after_the_early_mark(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Here.", "action": "none"}, gate=gate, ack="Looking at the contractor's emails.",
           ack_delay=0.1, deadline=HC.ACK_DEADLINE_S)
    t0 = time.monotonic()
    _, body = post(server, {"text": "summarize what came in from the contractor", "client_id": "c_early"})
    took = time.monotonic() - t0
    gate.set()
    early = HC.ACK_DEADLINE_S * HC.ACK_EARLY_SHARE
    assert early - 0.05 <= took < early + 0.4, took
    assert body["reply"]["part"] == "ack" and body["reply"]["text"] == "Looking at the contractor's emails."


def test_a_slow_ack_model_falls_back_at_the_hard_mark(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Here.", "action": "none"}, gate=gate, ack="Too late.", ack_delay=5.0,
           deadline=HC.ACK_DEADLINE_S)
    t0 = time.monotonic()
    _, body = post(server, {"text": "summarize what came in from the contractor", "client_id": "c_slowack"})
    took = time.monotonic() - t0
    gate.set()
    hard = HC.ACK_DEADLINE_S * HC.ACK_FALLBACK_SHARE
    assert hard - 0.05 <= took < HC.ACK_DEADLINE_S, took
    assert body["reply"]["part"] == "ack" and body["reply"]["text"] != "Too late."


def test_a_slow_home_build_never_delays_the_first_words(server, service, hermes, monkeypatch):
    """Home's publish used to run a full build on the request thread before the turn's clock
    started (0.7-2.3 s live); it now runs beside the turn."""
    real = service._now_changed

    def slow():
        time.sleep(1.5)
        real()
    monkeypatch.setattr(service, "_now_changed", slow)
    gate = threading.Event()
    script(service, {"reply": "Here.", "action": "none"}, gate=gate, ack=None, deadline=HC.ACK_DEADLINE_S)
    t0 = time.monotonic()
    _, body = post(server, {"text": "summarize what came in from the contractor", "client_id": "c_slowhome"})
    took = time.monotonic() - t0
    gate.set()
    assert took < HC.ACK_DEADLINE_S, took
    assert body["reply"]["part"] == "ack"


def test_no_fallback_line_when_the_answer_is_moments_away(server, service, hermes):
    """The coordinator has been taking about 1.9 s: at the 1.8 s mark its answer is under 0.5 s away,
    so it comes alone instead of a fallback line it would repeat in the same second."""
    chat = service.home_chat
    script(service, {"reply": "It's 51°F and clear.", "action": "none"}, delay=1.9, ack=None,
           deadline=HC.ACK_DEADLINE_S)
    chat._decide_s = 1.9
    t0 = time.monotonic()
    _, body = post(server, {"text": "what's the weather", "client_id": "c_soon"})
    took = time.monotonic() - t0
    assert body["reply"].get("part") != "ack" and "51" in body["reply"]["text"]
    assert took < HC.ACK_DEADLINE_S, took


def test_home_publishes_off_the_request_thread_and_folds_a_burst(service):
    calls = []
    started = threading.Event()
    release = threading.Event()

    def slow():
        calls.append(threading.current_thread().name)
        started.set()
        release.wait(2)
    service._now_changed = slow
    chat = service.home_chat
    t0 = time.monotonic()
    for _ in range(5):
        chat.publish_soon()
    assert time.monotonic() - t0 < 0.1
    started.wait(1)
    release.set()
    deadline = time.monotonic() + 2
    while chat._pub_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert 1 <= len(calls) <= 2 and all(n == "home-publish" for n in calls)


# -- F4: no reload between connect retries ----------------------------------------------------------

def test_a_call_admitted_abandoned_or_ended_in_the_last_minute_holds_back_a_reload(service, caplog):
    from speakeasy.calls import Interaction
    assert service.busy() is False
    service.note_call_event()                          # a connect was asked for / abandoned
    assert service.busy_reason() == "a call is connecting"
    assert service.busy_reason(now=time.time() + service_module.CALL_SETTLE_S + 1) is None
    service._call_event_at = 0.0
    call = Interaction("int_e", "sess_e", started_at=time.time() - 300)
    call.call_closed = True
    call.ended_at = time.time() - 5                    # ended a few seconds ago
    service.interactions["int_e"] = call
    assert service.busy_reason() == "a call just connected or ended"
    with caplog.at_level(logging.INFO, logger="speakeasy.service"):
        assert service.busy() is True
        call.ended_at = time.time() - service_module.CALL_SETTLE_S - 1
        assert service.busy() is False
    text = caplog.text
    assert "plugin update waiting (a call just connected or ended); it has waited 0s" in text
    assert "plugin update loading after waiting" in text


def test_abandoning_a_connect_marks_a_call_event(service):
    from speakeasy.calls import Interaction
    call = Interaction("int_ab", "sess_ab", started_at=time.time() - 300)
    service.interactions["int_ab"] = call
    service._call_event_at = 0.0
    service.abandon("int_ab")
    assert service.busy_reason() == "a call is connecting"
