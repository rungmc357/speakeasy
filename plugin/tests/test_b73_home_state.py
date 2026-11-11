"""b73 Home state (home_state.py): current exchange vs transcript, the 20-minute reset, the widget
stack and its dismiss rules, dealt-with items, recommendations v1, and the old shape for old apps.
Invented data only."""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import http
from speakeasy import home_state as HS
from test_history import state  # noqa: F401
from test_now import NY, _at, _clock, task

NOON = _at(NY, 2026, 11, 6, 12, 0)


def _msg(mid: str, role: str, at: float, run: str | None = None, **extra) -> dict:
    out = {"id": mid, "kind": "message", "role": role, "text": f"{role} says hi", "at": at, **extra}
    if run:
        out["run_id"] = run
    return out


def _card(run: str, at: float) -> dict:
    return {"id": f"c:{run}", "kind": "card", "at": at, "item_id": f"s:{run}", "state": "working"}


def _question_task(service, key, settled, title="Order a desk"):
    return task(service.store, key, "completed", session="s_" + key, request=title,
                result={"spoken": "Two fit.", "question": {"kind": "question", "question": "Which desk?",
                                                           "options": [{"text": "Oakline"}, {"text": "Birch"}]}},
                settled=settled, created=settled - 60)


@pytest.fixture
def frozen(service, monkeypatch):
    """Home, its question sweep and the stack all see one invented clock."""
    from fakes import ModuleClock
    from speakeasy import service as service_module
    box = {"t": NOON}
    monkeypatch.setattr(service_module, "time", ModuleClock(__import__("time").monotonic, wall=lambda: box["t"]))
    service.now_model.clock = lambda: box["t"]
    service.store.set_meta("now_baseline_at", repr(NOON - 30 * 86400))

    def at(t):
        box["t"] = t
    return at


def _now(service):
    return service.now({"tz": NY})


# -- 1. current exchange vs transcript ----------------------------------------------------------------

def test_the_latest_message_its_reply_and_its_status_line_are_the_current_exchange():
    entries = [_msg("m:hu_old", "user", NOON - 300, run="run_a"), _msg("m:ha_old", "assistant", NOON - 299, run="run_a"),
               _card("run_a", NOON - 299),
               _msg("m:hu_new", "user", NOON - 60, run="run_b"), _msg("m:ha_new", "assistant", NOON - 59, run="run_b"),
               _card("run_b", NOON - 59)]
    before = [dict(e) for e in entries]
    cur = HS.current_exchange(entries, NOON)
    assert not cur["clean"]
    assert cur["entry_ids"] == ["m:hu_new", "m:ha_new", "c:run_b"]
    assert cur["message_id"] == "m:hu_new" and cur["reply_ids"] == ["m:ha_new"] and cur["card_ids"] == ["c:run_b"]
    assert [e["id"] for e in entries if e.get("current")] == cur["entry_ids"]
    # The transcript keeps every entry, in order; only the flag is added.
    assert [e["id"] for e in entries] == [e["id"] for e in before]
    assert all({k: v for k, v in e.items() if k != "current"} == b for e, b in zip(entries, before))


def test_sending_a_new_message_moves_the_previous_exchange_to_the_transcript_only():
    entries = [_msg("m:hu_1", "user", NOON - 100, run="run_a"), _msg("m:ha_1", "assistant", NOON - 99, run="run_a"),
               _card("run_a", NOON - 99)]
    assert HS.current_exchange(entries, NOON)["message_id"] == "m:hu_1"
    entries.append(_msg("m:hu_2", "user", NOON - 5))
    cur = HS.current_exchange(entries, NOON)
    assert cur["entry_ids"] == ["m:hu_2"] and cur["reply_ids"] == [] and cur["card_ids"] == []
    assert not any(e.get("current") for e in entries[:3])
    assert len(entries) == 4


def test_an_older_jobs_answer_line_is_not_part_of_the_current_exchange_but_this_jobs_result_is():
    entries = [_msg("m:hu_1", "user", NOON - 100, run="run_b"), _msg("m:ha_1", "assistant", NOON - 99, run="run_b"),
               _card("run_b", NOON - 99),
               _msg("d:run_a", "assistant", NOON - 50, run="run_a", answer=True, item_id="s:run_a"),
               {"id": "x:run_b", "kind": "result", "at": NOON - 40, "run_id": "run_b", "item_id": "s:run_b"}]
    cur = HS.current_exchange(entries, NOON)
    assert "d:run_a" not in cur["entry_ids"] and "x:run_b" in cur["entry_ids"]


def test_extra_jobs_one_message_started_are_part_of_it():
    entries = [_msg("m:hu_1", "user", NOON - 10, run="run_a"), _card("run_a", NOON - 9), _card("run_c", NOON - 9)]
    assert HS.current_exchange(entries, NOON, extra_runs={"run_c"})["card_ids"] == ["c:run_a", "c:run_c"]


def test_no_message_yet_is_clean():
    cur = HS.current_exchange([_card("run_a", NOON - 10)], NOON)
    assert cur["clean"] and cur["entry_ids"] == []


def test_home_payload_marks_the_current_exchange(service, frozen):
    chat = service.home_chat
    old = chat._add("user", "Find a desk", NOON - 600)
    chat._add("assistant", "On it.", NOON - 599, reply_to=old)
    new = chat._add("user", "And a lamp", NOON - 30)
    chat._add("assistant", "Looking for a lamp.", NOON - 29, reply_to=new)
    stream = _now(service)["stream"]
    assert stream["current"]["message_id"] == "m:" + new
    current = [e["id"] for e in stream["entries"] if e.get("current")]
    assert current[0] == "m:" + new and len(current) == 2
    assert "m:" + old in [e["id"] for e in stream["entries"]]   # still in the transcript


# -- 5. the reset ------------------------------------------------------------------------------------

def test_reset_interval_is_twenty_minutes():
    assert HS.RESET_AFTER_S == 20 * 60


def test_after_twenty_minutes_without_a_message_home_is_clean():
    entries = [_msg("m:hu_1", "user", NOON, run="run_a"), _msg("m:ha_1", "assistant", NOON + 5, run="run_a"),
               _card("run_a", NOON + 5)]
    assert not HS.current_exchange(entries, NOON + 5 + HS.RESET_AFTER_S - 1)["clean"]
    late = HS.current_exchange(entries, NOON + 5 + HS.RESET_AFTER_S)
    assert late["clean"] and late["entry_ids"] == [] and late["last_activity_at"] == NOON + 5
    assert not any(e.get("current") for e in entries)
    assert len(entries) == 3   # the transcript itself is untouched


def test_a_reset_never_removes_stack_items(service, frozen):
    chat = service.home_chat
    chat._add("user", "Summarize the inbox", NOON - 120)
    task(service.store, "inbox", "completed", session="s_inbox", request="Summarize the inbox",
         result={"headline": "Four need a reply"}, settled=NOON - 60, created=NOON - 120)
    body = _now(service)
    assert not body["stream"]["current"]["clean"]
    ids = [i["id"] for i in body["stack"]["items"]]
    assert ids == ["w:r:run_inbox"]
    frozen(NOON + HS.RESET_AFTER_S + 60)
    later = _now(service)
    assert later["stream"]["current"]["clean"]
    assert [i["id"] for i in later["stack"]["items"]] == ids


# -- 2. the widget stack -----------------------------------------------------------------------------

def test_stack_holds_finished_results_and_decisions_never_running_work(service, frozen):
    _question_task(service, "desk", NOON - 300)
    task(service.store, "patio", "completed", session="s_patio", request="Quote the patio",
         result={"headline": "Quote is $4,200"}, settled=NOON - 60, created=NOON - 400)
    task(service.store, "live", "working", session="s_live", request="Compare watering schedules", created=NOON - 30)
    task(service.store, "broke", "failed", session="s_broke", request="Book the plumber", settled=NOON - 200,
         created=NOON - 500)
    stack = _now(service)["stack"]
    by_kind = {i["kind"]: i for i in stack["items"]}
    assert set(by_kind) == {"decision", "stopped", "result"}
    assert not any(i.get("run_id") == "run_live" for i in stack["items"])
    # needs-you first, then results
    assert [i["needs_you"] for i in stack["items"]] == [True, True, False]
    assert by_kind["stopped"]["needs_you"] and by_kind["stopped"]["run_id"] == "run_broke"
    res = by_kind["result"]
    assert res["source"] == {"kind": "job", "run_id": "run_patio", "session_id": "s_patio"}
    assert res["card_id"] == "c:run_patio" and "result_id" in res
    assert by_kind["decision"]["source"]["run_id"] == "run_desk"


def test_a_posted_email_result_is_the_stack_items_result_message(service, frozen):
    from speakeasy.emails import normalize_draft
    task(service.store, "mail", "completed", session="s_mail", request="Draft a note to Pat",
         result={"headline": "Drafted the note to Pat, not sent"}, settled=NOON - 120, created=NOON - 900)
    service.store.add_draft("mail", "s_mail", normalize_draft(
        {"from": "me@example.com", "to": ["Pat Doe <pat@example.org>"], "cc": [], "bcc": [],
         "subject": "Friday", "body": "Hi Pat,\n\nStill on for Friday?"}))
    body = _now(service)
    (item,) = body["stack"]["items"]
    result = next(e for e in body["stream"]["entries"] if e["kind"] == "result")
    assert item["result_id"] == result["id"] == "x:run_mail"


def test_current_exchange_job_in_the_stack_is_flagged(service, frozen):
    chat = service.home_chat
    mid = chat._add("user", "Summarize the inbox", NOON - 120)
    task(service.store, "inbox", "completed", session="s_inbox", request="Summarize the inbox",
         result={"headline": "Four need a reply"}, settled=NOON - 60, created=NOON - 120)
    chat._link(mid, "run_inbox", "start")
    (item,) = _now(service)["stack"]["items"]
    assert item["in_current"] is True


# -- 3. dismiss rules --------------------------------------------------------------------------------

def test_dismissed_on_home_leaves_the_stack_but_stays_on_work_then_work_removes_it_for_good(server, service, frozen):
    task(service.store, "patio", "completed", session="s_patio", request="Quote the patio",
         result={"headline": "Quote is $4,200"}, settled=NOON - 60, created=NOON - 400)
    status, stack = http(server.base_url, "GET", "/voice/home/stack?tz=" + NY, None, server.token)
    assert status == 200 and [i["id"] for i in stack["items"]] == ["w:r:run_patio"]
    path = "/voice/home/stack/w%3Ar%3Arun_patio/dismiss"
    status, got = http(server.base_url, "POST", path, {}, server.token)
    assert status == 200 and got["state"] == "on_work"
    status, again = http(server.base_url, "POST", path, {"from": "home"}, server.token)   # idempotent
    assert status == 200 and again["state"] == "on_work"
    stack = _now(service)["stack"]
    assert stack["items"] == [] and [i["id"] for i in stack["on_work"]] == ["w:r:run_patio"]
    assert stack["on_work"][0]["dismissed_on_home_at"] == NOON
    status, gone = http(server.base_url, "POST", path, {"from": "work"}, server.token)
    assert status == 200 and gone["state"] == "gone"
    status, gone2 = http(server.base_url, "POST", path, {"from": "work"}, server.token)
    assert status == 200 and gone2["state"] == "gone"
    stack = _now(service)["stack"]
    assert stack["items"] == [] and stack["on_work"] == []


def test_dismiss_on_work_without_a_home_dismiss_is_also_for_good(service, frozen):
    task(service.store, "patio", "completed", session="s_patio", request="Quote the patio",
         result={"headline": "Quote is $4,200"}, settled=NOON - 60, created=NOON - 400)
    _now(service)
    service.home_stack_dismiss("w:r:run_patio", {"from": "work"})
    stack = _now(service)["stack"]
    assert stack["items"] == [] and stack["on_work"] == []


def test_dismissals_survive_a_plugin_reload(service, frozen):
    task(service.store, "a", "completed", session="s_a", request="Quote the patio",
         result={"headline": "Quote"}, settled=NOON - 60, created=NOON - 400)
    task(service.store, "b", "completed", session="s_b", request="Find a lamp",
         result={"headline": "Lamp"}, settled=NOON - 50, created=NOON - 400)
    _now(service)
    service.home_stack_dismiss("w:r:run_a", {"from": "home"})
    service.home_stack_dismiss("w:r:run_b", {"from": "work"})
    # A reload builds new objects over the same store.
    service.home_state = HS.HomeState(service, clock=lambda: service.now_model.clock())
    stack = _now(service)["stack"]
    assert stack["items"] == [] and [i["id"] for i in stack["on_work"]] == ["w:r:run_a"]


def test_bad_dismisses_are_refused(server, service, frozen):
    status, _ = http(server.base_url, "POST", "/voice/home/stack/w%3Ar%3Anope/dismiss", {}, server.token)
    assert status == 404
    task(service.store, "a", "completed", session="s_a", request="Quote", result={"headline": "Q"},
         settled=NOON - 60, created=NOON - 400)
    _now(service)
    status, _ = http(server.base_url, "POST", "/voice/home/stack/w%3Ar%3Arun_a/dismiss", {"from": "sky"}, server.token)
    assert status == 400


def test_an_old_apps_home_swipe_is_a_home_dismiss_of_the_stack_item(service, frozen):
    task(service.store, "a", "completed", session="s_a", request="Quote the patio",
         result={"headline": "Quote"}, settled=NOON - 60, created=NOON - 400)
    body = _now(service)
    (card,) = body["items"]
    service.now_dismiss(card["id"], {})
    stack = _now(service)["stack"]
    assert stack["items"] == [] and [i["id"] for i in stack["on_work"]] == ["w:r:run_a"]


# -- 4. dealt with ------------------------------------------------------------------------------------

def test_an_answered_question_leaves_and_its_jobs_result_comes_back_as_a_new_item(service, frozen):
    _question_task(service, "desk", NOON - 300)
    (item,) = _now(service)["stack"]["items"]
    assert item["kind"] == "decision"
    # The user answers; the job goes on as normal work (Work shows it), not in the stack.
    service.home_state.dealt_with(item_id="q:run_desk")
    service.store.close_question("desk", "answered", "Oakline")
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE runs SET status='running', settled_at=NULL WHERE idem_key='desk'")
    assert _now(service)["stack"]["items"] == []
    # It finishes: its result enters the stack as a new item that says where it came from.
    frozen(NOON + 120)
    service.store.transition("desk", "completed", result={"headline": "Ordered the Oakline"})
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE runs SET settled_at=? WHERE idem_key='desk'", (NOON + 100,))
    stack = _now(service)["stack"]["items"]
    assert [i["kind"] for i in stack] == ["result"]
    assert stack[0]["id"] != item["id"] and stack[0]["follows"] == item["id"]


def test_a_question_that_closed_unanswered_leaves_as_skipped(service, frozen):
    _question_task(service, "desk", NOON - 300)
    (decision,) = _now(service)["stack"]["items"]
    service.store.close_question("desk", "expired")
    stack = _now(service)["stack"]["items"]
    # The decision is gone (skipped); the job's own finished answer is a separate result item.
    assert [i["kind"] for i in stack] == ["result"] and stack[0]["follows"] == decision["id"]
    assert service.home_state.row(decision["id"])["state"] == HS.SKIPPED


def test_replying_to_a_result_deals_with_it(service, frozen):
    task(service.store, "patio", "completed", session="s_patio", request="Quote the patio",
         result={"headline": "Quote is $4,200"}, settled=NOON - 60, created=NOON - 400)
    _now(service)
    service._home_replied({"text": "Book it", "reply_to": "w:r:run_patio"}, {"ok": True, "run_id": "run_next"})
    stack = _now(service)["stack"]
    assert stack["items"] == [] and stack["on_work"] == []


def test_a_running_reply_job_is_not_in_the_stack_and_its_result_follows_the_replied_item(service, frozen):
    task(service.store, "patio", "completed", session="s_patio", request="Quote the patio",
         result={"headline": "Quote is $4,200"}, settled=NOON - 60, created=NOON - 400)
    _now(service)
    task(service.store, "book", "working", session="s_book", request="Book the patio crew", created=NOON - 10)
    service._home_replied({"text": "Book it", "reply_to": "r:run_patio"}, {"ok": True, "run_id": "run_book"})
    assert _now(service)["stack"]["items"] == []
    frozen(NOON + 300)
    service.store.transition("book", "completed", result={"headline": "Crew booked for Friday"})
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE runs SET settled_at=? WHERE idem_key='book'", (NOON + 290,))
    (item,) = _now(service)["stack"]["items"]
    assert item["run_id"] == "run_book" and item["follows"] == "w:r:run_patio"


def test_a_retried_stopped_job_leaves_the_stack(service, frozen):
    task(service.store, "broke", "failed", session="s_broke", request="Book the plumber", settled=NOON - 200,
         created=NOON - 500)
    assert [i["kind"] for i in _now(service)["stack"]["items"]] == ["stopped"]
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE runs SET status='running', settled_at=NULL WHERE idem_key='broke'")
    assert _now(service)["stack"]["items"] == []


# -- 6. recommendations --------------------------------------------------------------------------------

def _draft(service, key, subject="Friday", *, session=None, reply_to=None, age=60.0):
    """An invented pending draft for task ``key``, written ``age`` seconds ago (wall clock, as the store)."""
    import time

    from speakeasy.emails import normalize_draft
    raw = {"from": "me@example.com", "to": ["pat@example.org"], "cc": [], "bcc": [], "subject": subject, "body": "Hi"}
    if reply_to:
        raw["reply_to_message_id"] = reply_to
    d = service.store.add_draft(key, session or "s_" + key, normalize_draft(raw))
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE email_drafts SET created=?, updated=? WHERE draft_id=?",
                                  (time.time() - age, time.time() - age, d["draft_id"]))
        service.store._db.execute("UPDATE work_events SET created=created-? WHERE idem_key=?", (age + 1, key))
    return d["draft_id"]


def _mail_task(service, key="mail", subject="Friday", **kw):
    task(service.store, key, "completed", session="s_" + key, request=f"Draft a note to Pat about {subject}",
         result={"headline": "Drafted"}, settled=NOON - 600, created=NOON - 900)
    return _draft(service, key, subject, **kw)


def _kinds(service):
    return [r["source"]["kind"] for r in _now(service)["recommendations"]["items"]]


def test_nothing_waiting_gives_an_empty_list_not_filler(service, frozen):
    recs = _now(service)["recommendations"]
    assert recs["items"] == [] and recs["generic"] is False
    assert not hasattr(HS, "GENERIC")


def test_a_finished_job_is_never_a_follow_up_recommendation(service, frozen):
    task(service.store, "patio", "completed", session="s_patio", request="Quote the patio",
         result={"headline": "Quote is $4,200"}, settled=NOON - 60, created=NOON - 400)
    body = _now(service)
    assert body["recommendations"]["items"] == []
    # Its result is the surface: a widget on the stack, not a prompt to send.
    assert [i["id"] for i in body["stack"]["items"]] == ["w:r:run_patio"]
    # Put off on Home, it stays a Work item; still no "Pick back up" or "Follow up" prompt.
    service.home_stack_dismiss("w:r:run_patio", {"from": "home"})
    assert _now(service)["recommendations"]["items"] == []


def test_no_follow_up_items_ever_and_dismissing_pulls_none_in(service, frozen):
    for n in range(5):
        task(service.store, f"f{n}", "completed", session=f"s_f{n}", request=f"Finished job {n}",
             result={"headline": "Done"}, settled=NOON - 600 - n, created=NOON - 900)
    draft_id = _mail_task(service)
    recs = _now(service)["recommendations"]["items"]
    assert [r["id"] for r in recs] == [f"rec:draft:{draft_id}"]
    service.home_state.dismiss_rec(recs[0]["id"])
    assert _now(service)["recommendations"]["items"] == []   # nothing pulled in behind it
    texts = [r["text"] + r["prompt"] for r in recs]
    assert not any("follow up" in t.lower() or "follow-ups" in t.lower() for t in texts)


def test_a_draft_waiting_on_you_says_why(service, frozen):
    draft_id = _mail_task(service)
    (rec,) = _now(service)["recommendations"]["items"]
    assert rec["id"] == f"rec:draft:{draft_id}" and rec["source"]["kind"] == "unsent_draft"
    assert rec["why"] and not rec["generic"]


@pytest.mark.parametrize("status", ["approved", "sent", "saved", "denied", "revising", "failed"])
def test_a_sent_saved_denied_or_revising_draft_is_not_recommended(service, frozen, status):
    draft_id = _mail_task(service)
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE email_drafts SET status=? WHERE draft_id=?", (status, draft_id))
    assert _kinds(service) == []


def test_a_draft_with_a_later_draft_on_its_thread_is_not_recommended(service, frozen):
    old = _mail_task(service, reply_to="<m1@example.org>", age=600)
    task(service.store, "reply", "completed", session="s_reply", request="Answer Pat",
         result={"headline": "Replied"}, settled=NOON - 60, created=NOON - 120)
    newer = _draft(service, "reply", "Re: Friday", reply_to="<m1@example.org>", age=30)
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE email_drafts SET status='sent' WHERE draft_id=?", (newer,))
    ids = [r["id"] for r in _now(service)["recommendations"]["items"]]
    assert f"rec:draft:{old}" not in ids and ids == []   # a reply went out on that thread since


def test_a_draft_a_later_job_worked_on_is_not_recommended(service, frozen):
    _mail_task(service, subject="Sam <> Ahmad", age=600)
    assert _kinds(service) == ["unsent_draft"]
    # A separate job naming the draft's thread, after the draft was written: it was handled there.
    task(service.store, "finish", "completed", session="s_finish",
         request="Finish the email draft \u201cRe: Sam <> Ahmad\u201d, say I'm away until the 22nd",
         result={"headline": "Sent"}, settled=NOON - 30, created=NOON - 60)
    assert _kinds(service) == []


def test_a_draft_its_own_session_went_on_from_is_not_recommended(service, frozen):
    _mail_task(service, age=600)
    task(service.store, "more", "completed", session="s_mail", request="Make it shorter and send it",
         result={"headline": "Done"}, settled=NOON - 30, created=NOON - 60)
    assert _kinds(service) == []


def test_a_draft_its_own_job_did_more_on_is_not_recommended(service, frozen):
    _mail_task(service, age=600)
    service.store.progress("mail", "milestone", "Sending the email")
    assert _kinds(service) == []


def test_a_draft_whose_result_was_replied_to_or_cleared_is_not_recommended(service, frozen):
    _mail_task(service, age=600)
    _now(service)
    service.home_stack_dismiss("w:r:run_mail", {"from": "work"})
    assert _kinds(service) == []


def test_an_old_draft_is_not_recommended(service, frozen):
    import time
    frozen(time.time())   # the draft's age is on the store's wall clock
    _mail_task(service, age=HS.DRAFT_FRESH_S + 60)
    assert _kinds(service) == []


def test_unfinished_business_comes_first_and_at_most_three(service, frozen):
    draft_id = _mail_task(service)
    task(service.store, "broke", "failed", session="s_broke", request="Book the plumber", settled=NOON - 200,
         created=NOON - 500)
    _now(service)
    service.home_stack_dismiss("w:s:run_broke", {"from": "home"})   # acknowledged, still open on Work
    recs = _now(service)["recommendations"]["items"]
    assert len(recs) <= HS.RECS_MAX
    assert [r["source"]["kind"] for r in recs] == ["unsent_draft", "stopped_job"]
    assert recs[0]["source"]["draft_id"] == draft_id and recs[1]["source"]["stack_id"] == "w:s:run_broke"
    assert all(r["why"] for r in recs)


def test_skipped_questions_and_questions_put_off_on_home_are_recommended(service, frozen):
    _question_task(service, "desk", NOON - 300)
    _question_task(service, "chair", NOON - 200, title="Order a chair")
    _now(service)
    (chair,) = [i["id"] for i in _now(service)["stack"]["items"] if "run_chair" in i["id"]]
    service.home_stack_dismiss(chair, {"from": "home"})
    service.store.close_question("desk", "expired")
    kinds = _kinds(service)
    assert "skipped_question" in kinds and "dismissed_on_home" in kinds


def test_a_dismissed_recommendation_never_returns(server, service, frozen):
    _mail_task(service)
    (rec,) = _now(service)["recommendations"]["items"]
    status, got = http(server.base_url, "POST", "/voice/home/recommendations/" + rec["id"].replace(":", "%3A")
                       + "/dismiss", {}, server.token)
    assert status == 200 and got["dismissed"]
    again = _now(service)["recommendations"]["items"]
    assert rec["id"] not in [r["id"] for r in again]
    # Something else waiting recomputes, and it still stays away.
    _mail_task(service, key="mail2", subject="Saturday")
    later = _now(service)["recommendations"]["items"]
    assert rec["id"] not in [r["id"] for r in later] and later
    status, _ = http(server.base_url, "GET", "/voice/home/recommendations?tz=" + NY, None, server.token)
    assert status == 200


def test_recommendations_are_served_from_the_saved_list_until_something_changes(service, frozen, monkeypatch):
    calls = []
    real = service.home_state.compute
    monkeypatch.setattr(service.home_state, "compute", lambda inputs: calls.append(1) or real(inputs))
    _mail_task(service)
    first = _now(service)["recommendations"]
    _now(service)
    service.home_state.recommendations()
    assert len(calls) == 1
    _mail_task(service, key="mail2", subject="Saturday")
    assert _now(service)["recommendations"] != first and len(calls) == 2
    service.home_state.dismiss_rec(first["items"][0]["id"])
    _now(service)
    assert len(calls) == 3


# -- old clients ------------------------------------------------------------------------------------

OLD_NOW_KEYS = {"sentence", "counts", "tiers", "call", "call_summary", "items", "background", "earlier", "stream",
                "pill", "rev", "as_of", "notice"}


def test_an_old_clients_requests_still_get_the_old_shape(server, service, frozen):
    task(service.store, "patio", "completed", session="s_patio", request="Quote the patio",
         result={"headline": "Quote"}, settled=NOON - 60, created=NOON - 400)
    status, body = http(server.base_url, "GET", "/voice/now", None, server.token)   # no tz, as old apps may
    assert status == 200
    assert OLD_NOW_KEYS <= set(body)
    assert set(body) - OLD_NOW_KEYS <= {"stack", "recommendations"}   # only additive keys
    assert {"entries", "needs_you", "folded"} <= set(body["stream"])
    (item,) = body["items"]
    assert item["kind"] == "new_answer" and item["id"].startswith(("s:", "t:"))
    # The old swipe route answers exactly as before.
    status, got = http(server.base_url, "POST", "/voice/now/" + item["id"].replace(":", "%3A") + "/dismiss", {},
                       server.token)
    assert status == 200 and got == {"ok": True, "id": item["id"], "dismissed": True}
    status, undo = http(server.base_url, "DELETE", "/voice/now/" + item["id"].replace(":", "%3A") + "/dismiss", None,
                        server.token)
    assert status == 200 and undo == {"ok": True, "id": item["id"], "dismissed": False}


# -- the contract the apps decode -------------------------------------------------------------------

CONTRACT_B73 = __import__("pathlib").Path(__file__).resolve().parents[2] / "mac/Tests/SpeakeasyCoreTests/Contract/now_b73.json"


def test_the_b73_now_reply_matches_the_contract_the_apps_decode(service, frozen):
    """mac/.../Contract/now_b73.json is a real /voice/now reply with the b73 keys (invented data):
    SpeakeasyCore's HomeStateTests decode it. Regenerate with SPEAKEASY_WRITE_CONTRACT=1."""
    import json
    import os

    from speakeasy.emails import normalize_draft
    from test_now import _shape
    chat = service.home_chat
    old = chat._add("user", "Find a desk", NOON - 900)
    chat._add("assistant", "On it.", NOON - 899, reply_to=old)
    mid = chat._add("user", "Summarize the inbox", NOON - 120)
    chat._add("assistant", "Reading it now.", NOON - 119, reply_to=mid, part="ack")   # home-ack: an acknowledgement part
    task(service.store, "inbox", "completed", session="s_inbox", request="Summarize the inbox",
         result={"headline": "Four need a reply"}, settled=NOON - 60, created=NOON - 120)
    chat._link(mid, "run_inbox", "start")
    _question_task(service, "desk", NOON - 300)
    task(service.store, "broke", "failed", session="s_broke", request="Book the plumber", settled=NOON - 200,
         created=NOON - 500)
    task(service.store, "mail", "completed", session="s_mail", request="Draft a note to Pat",
         result={"headline": "Drafted the note to Pat, not sent"}, settled=NOON - 240, created=NOON - 900)
    service.store.add_draft("mail", "s_mail", normalize_draft(
        {"from": "me@example.com", "to": ["Pat Doe <pat@example.org>"], "cc": [], "bcc": [],
         "subject": "Friday", "body": "Hi Pat,\n\nStill on for Friday?"}))
    task(service.store, "patio", "completed", session="s_patio", request="Quote the patio",
         result={"headline": "Quote is $4,200"}, settled=NOON - 400, created=NOON - 800)
    _now(service)
    service.home_stack_dismiss("w:r:run_patio", {"from": "home"})
    body = json.loads(json.dumps(_now(service), default=str))
    if os.environ.get("SPEAKEASY_WRITE_CONTRACT"):
        CONTRACT_B73.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
    saved = json.loads(CONTRACT_B73.read_text())
    assert _shape(body["stream"]["current"]) == _shape(saved["stream"]["current"])
    assert body["stack"]["count"] == saved["stack"]["count"] == 4
    keys = lambda items: set().union(*(set(i) for i in items))  # noqa: E731
    assert keys(body["stack"]["items"] + body["stack"]["on_work"]) == keys(saved["stack"]["items"] + saved["stack"]["on_work"])
    assert [r["source"]["kind"] for r in body["recommendations"]["items"]] == \
        [r["source"]["kind"] for r in saved["recommendations"]["items"]]
