"""Repeat status and recap asks reuse the task that already knows; one subject is never twin tasks.
All subjects here are invented."""
import json
import time

import pytest

from speakeasy import recap, router
from speakeasy.prompt import builder as P
from speakeasy.recap import Earlier

NOW = 1_000_000.0


def done(request, minutes_ago, *, key="k1", result="Three fixes are merged and two are still running.",
         session="sess_old", title="", created_before=2):
    settled = NOW - minutes_ago * 60
    return Earlier(key=key, task_id="d_" + key, request=request, status="completed", title=title, result=result,
                   created=settled - created_before * 60, settled_at=settled, session_id=session)


# -- recognising a repeat ask -------------------------------------------------------------------

@pytest.mark.parametrize("said", [
    "What's the status of build 12?",
    "where are we on build 12",
    "Where'd we leave off on the pull requests?",
    "any update on the garden plan",
    "how's the kitchen quote looking",
    "is the photo export done yet",
    "am I gonna win this week in the bowling league",
    "catch me up on the move",
])
def test_status_and_recap_asks_with_a_subject_are_recognised(said):
    assert recap.recap_ask(said), said


@pytest.mark.parametrize("said", [
    "how's it going?", "where are we?", "what's the status", "book a table for two tonight",
    "find the cheapest flight to Rome next week", "turn the den lights off",
])
def test_subjectless_or_ordinary_asks_are_not(said):
    assert not recap.recap_ask(said), said


def test_same_subject_matches_numbers_and_words():
    assert recap.same_subject("status of build 12?", "Check how build 12 is going: what's merged and what's left")
    assert not recap.same_subject("status of build 12?", "Check how build 13 is going")
    assert recap.same_subject("where'd we leave off on the pull requests", "Check my open pull requests")
    assert not recap.same_subject("how's the kitchen quote looking", "Check my open pull requests")


# -- what happens to a repeat ask ---------------------------------------------------------------

def test_a_repeat_status_ask_five_minutes_later_is_answered_from_the_result():
    earlier = [done("Check how build 12 is going: what's merged and what's left", 5)]
    found = recap.pick("where are we on build 12?", earlier, NOW)
    assert found is earlier[0]
    assert recap.plan("where are we on build 12?", found, NOW, in_call=False, prior_call_end=None,
                      call_opened=NOW - 60) == recap.ANSWER
    said = P.repeat_answer("Build 12 status", found.result, 5, False)
    assert "Same as 5 minutes ago" in said and "Three fixes are merged" in said
    assert "check again fresh" in said and "Don't start new work" in said


def test_asking_for_a_fresh_look_continues_the_session_instead():
    found = done("Check how build 12 is going", 5)
    assert recap.plan("check build 12 again", found, NOW, in_call=False, prior_call_end=None,
                      call_opened=NOW) == recap.CONTINUE


def test_the_same_ask_hours_later_continues_the_old_session():
    for minutes in (90, 180):
        found = done("Check how build 12 is going", minutes)
        assert recap.plan("status of build 12?", found, NOW, in_call=False, prior_call_end=None,
                          call_opened=NOW) == recap.CONTINUE


def test_much_later_it_starts_fresh_unless_the_answer_was_never_heard():
    found = done("Check how build 12 is going", 300)
    assert recap.pick("status of build 12?", [found], NOW) is found  # still found (late lookback)
    assert recap.plan("status of build 12?", found, NOW, in_call=False, prior_call_end=None,
                      call_opened=NOW) is None
    # It finished after the user hung up the call that asked (and before this one): open with it.
    hung_up = found.settled_at - 60
    assert recap.late(found, hung_up, NOW - 30)
    assert recap.plan("status of build 12?", found, NOW, in_call=False, prior_call_end=hung_up,
                      call_opened=NOW - 30) == recap.ANSWER
    said = P.repeat_answer("Bowling league matchup", found.result, 300, True)
    assert "came in after you hung up" in said


def test_a_failed_or_stopped_task_never_answers_and_a_running_one_answers_in_its_call():
    failed = Earlier("k", "d", "Check how build 12 is going", "failed", created=NOW - 300, settled_at=NOW - 240)
    assert recap.plan("status of build 12?", failed, NOW, in_call=False, prior_call_end=None, call_opened=NOW) is None
    running = Earlier("k", "d", "Check how build 12 is going", "running", created=NOW - 120)
    assert recap.plan("status of build 12?", running, NOW, in_call=True, prior_call_end=None,
                      call_opened=NOW) == recap.STATUS


def test_a_yes_to_the_fresh_check_offer_is_recognised():
    for said in ("yes", "yeah, do it", "sure go ahead", "ok check it"):
        assert recap.accepts_offer(said), said
    for said in ("no thanks", "yeah but not now", "book a table for two tonight"):
        assert not recap.accepts_offer(said), said


# -- the routing model never splits one subject ----------------------------------------------------

def parts_from(model_parts, request):
    return router.parse_decision(json.dumps({"follow_up_task_id": None, "parts": model_parts}), request, [], [])


def test_a_two_question_same_subject_sentence_is_one_task():
    said = "Status of build 12? what's still being built?"
    d = parts_from(["What's the status of build 12?", "What's still being built?"], said)
    assert [p.kind for p in d.parts] == ["new"] and d.parts[0].request == said
    said = "check my pull requests? is there anything we have to do there"
    d = parts_from(["Check my open pull requests", "Is there anything we have to do on my pull requests"], said)
    assert len(d.parts) == 1


def test_two_different_subjects_stay_two_tasks():
    d = parts_from(["What's the status of build 12?", "How's my bowling league matchup looking?"],
                   "status of build 12 and how's my bowling matchup")
    assert len(d.parts) == 2
    d = parts_from(["Check build 12", "Check build 13"], "check builds 12 and 13")
    assert len(d.parts) == 2


def test_merging_keeps_the_other_asks_apart():
    groups = recap.merge_parts(["Status of build 12", "Find a dentist near me open Saturday",
                                "What's left in build 12"])
    assert groups == [[0, 2], [1]]
