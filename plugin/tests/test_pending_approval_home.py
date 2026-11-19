"""SPK-357: an approval card (a ``confirm`` view still ``pending``) waits on you like a question.

On a call the voice offered "File 5 bugs on Linear", which counted the job as heard and read, so Home
dropped it and the call screen showed nothing while the voice said "open that card". Now it stays on
Home's stack as a decision until Post or Cancel, the voice says where it is when the preview is too long
to approve by voice, and the phone gets "Needs you" instead of "Done". Invented data only."""
from __future__ import annotations

import asyncio
import time
import types

from fakes import http
from speakeasy import confirm_actions as A
from speakeasy import confirm_voice as V
from speakeasy import confirms as C
from speakeasy.calls import SidebandWorker
from speakeasy.push import RelayClient
from test_b73_home_state import NOON, frozen  # noqa: F401
from test_confirm_actions import voice_worker
from test_history import post, state  # noqa: F401
from test_now import NY, task
from test_now_call import live_call, on_call
from test_push import FakeRelay, register

LONG_BODY = "Bug: the approval card is missing from Home on a call. " * 85   # about 4,700 characters


def _approval(title="File 5 bugs on Linear", body=LONG_BODY):
    return C.clean({"type": "post", "title": title, "details": {"account": "Linear", "body": body}})


def _filed(store, key, title, *, interaction="vi_call", settled=None, session=None, body=LONG_BODY):
    view = _approval(title, body)
    settled = settled if settled is not None else time.time() - 5
    task(store, key, "completed", session=session or "s_" + key, request=title, interaction=interaction,
         result={"spoken": "The bugs are ready to file.", "headline": "Ready to file on Linear.", "views": [view]},
         settled=settled, created=settled - 60)
    return view


def _voice_offers(call, store, task_id):
    """What calls.py does after presenting the card: the voice replied from this task."""
    worker = SidebandWorker.__new__(SidebandWorker)
    worker.interaction, worker.store = call, store
    worker.rt = types.SimpleNamespace(now_changed=lambda: None)
    worker.replied_task = task_id


def _home(service):
    """``GET /voice/now`` as the app reads it (a real read: it feeds the stack)."""
    return service.now({"tz": NY})


def _card(body, run_id):
    return next(i for i in body["items"] if i.get("run_id") == run_id)


def _decisions(body):
    return [s for s in body["stack"]["items"] if s["kind"] == "decision"]


def test_an_approval_the_voice_offered_on_a_call_stays_on_homes_stack_as_needs_you(service, state):
    store = service.store
    call = live_call(service, started=time.time() - 600)
    _filed(store, "k_five", "File 5 bugs on Linear")
    on_call(call, "d_k_five", "k_five")
    _voice_offers(call, store, "d_k_five")
    assert store.is_seen("k_five")   # heard on the call, as before

    body = _home(service)
    card = _card(body, "run_k_five")
    assert card["kind"] == "new_answer" and card["needs_you"] is True and card["unread"] is True
    assert card.get("on_call") != "heard" and "_approval" not in card
    assert [(s["kind"], s["run_id"], s["needs_you"], s["item_id"]) for s in body["stack"]["items"]] == \
        [("decision", "run_k_five", True, card["id"])]
    assert body["counts"]["needs_you"] == 1 and body["counts"]["finished_unread"] == 0


def test_tapping_post_takes_it_off_home(service, state):
    store = service.store
    view = _filed(store, "k_post", "File 5 bugs on Linear", interaction="vi_typed")
    store.mark_seen("k_post")   # opened in the app
    [stacked] = _home(service)["stack"]["items"]
    assert stacked["run_id"] == "run_k_post"

    C.claim(store, "run_k_post", 0, view["sha256"], "approve", source="tap")   # what Post claims
    body = _home(service)
    assert body["stack"]["items"] == []
    assert all(not i.get("needs_you") for i in body["items"])
    assert body["counts"]["needs_you"] == 0
    # Posted is done: never offered back as "Answer the question on File 5 bugs on Linear".
    assert service.home_state.row(stacked["id"])["state"] == "done"
    assert "skipped_question" not in [r["source"]["kind"] for r in body["recommendations"]["items"]]


def test_cancel_on_the_card_takes_it_off_home_and_nothing_comes_back(server, service, state):
    store = service.store
    view = _filed(store, "k_cancel", "Post the release notes", interaction="vi_typed")
    [decision] = _decisions(_home(service))
    status, reply = post(server, "/voice/work/run_k_cancel/card-action",
                         {"view": 0, "action": "cancel", "value": view["sha256"]})
    assert status == 200 and reply["message"] == "Cancelled."
    body = _home(service)
    assert service.home_state.row(decision["id"])["state"] == "done"
    # The card they just cancelled doesn't come straight back as a "Ready to post" result.
    assert body["stack"]["items"] == [] and body["counts"]["finished_unread"] == 0


def test_cancel_during_its_own_call_leaves_the_job_among_that_calls_answers(server, service, state):
    """On the live call the job belongs to, SPK-376 keeps the call's own answers in the stack, heard or
    not. A cancelled approval is one of them: the decision closes and nothing waits on you, and the job
    stays listed as that call's result (SPK-376's rule, left as it is)."""
    store = service.store
    call = live_call(service, started=time.time() - 600)
    view = _filed(store, "k_oncall", "File 5 bugs on Linear")
    on_call(call, "d_k_oncall", "k_oncall")
    _voice_offers(call, store, "d_k_oncall")
    [decision] = _decisions(_home(service))
    status, reply = post(server, "/voice/work/run_k_oncall/card-action",
                         {"view": 0, "action": "cancel", "value": view["sha256"]})
    assert status == 200 and reply["message"] == "Cancelled."
    body = _home(service)
    assert service.home_state.row(decision["id"])["state"] == "done"
    assert not _card(body, "run_k_oncall").get("needs_you") and body["counts"]["needs_you"] == 0
    assert [(s["kind"], s["needs_you"], s.get("follows")) for s in body["stack"]["items"]] == \
        [("result", False, decision["id"])]


def test_after_post_an_unopened_jobs_outcome_follows_the_decision(service, state):
    store = service.store
    view = _filed(store, "k_posted", "Post the release notes", interaction="vi_typed")
    [decision] = _decisions(_home(service))
    C.claim(store, "run_k_posted", 0, view["sha256"], "approve", source="tap")
    body = _home(service)
    # Never opened, so the job's outcome is a new result that says which decision it came from.
    assert [(s["kind"], s.get("follows")) for s in body["stack"]["items"]] == [("result", decision["id"])]


def test_an_approval_asked_again_with_new_terms_is_a_new_decision(service, state):
    store = service.store
    view = _filed(store, "k_buy", "File 5 bugs on Linear", interaction="vi_typed")
    [first] = _decisions(_home(service))
    C.claim(store, "run_k_buy", 0, view["sha256"], "approve", source="tap")
    # The service asked again (confirm_actions.follow): a new pending card on the same job, before
    # Home was read in between.
    again = _approval("File 5 bugs on Linear", "Bug: the approval card is missing. Two labels changed.")
    A._update(service, "run_k_buy", 0, view["sha256"], again)
    body = _home(service)
    [second] = _decisions(body)
    assert second["id"] != first["id"] and second["run_id"] == "run_k_buy"
    assert service.home_state.row(first["id"])["state"] == "done"
    assert _card(body, "run_k_buy")["needs_you"] is True


def test_two_approvals_from_one_call_each_keep_their_own_card(service, state):
    store, now = service.store, time.time()
    call = live_call(service, started=now - 600)
    _filed(store, "k_five", "File 5 bugs on Linear", settled=now - 200)
    _filed(store, "k_six", "File 6 bugs on Linear", settled=now - 20)
    for key in ("k_five", "k_six"):
        on_call(call, "d_" + key, key)
        _voice_offers(call, store, "d_" + key)
    body = _home(service)
    waiting = [i["run_id"] for i in body["items"] if i.get("needs_you")]
    assert sorted(waiting) == ["run_k_five", "run_k_six"]
    assert sorted(s["run_id"] for s in body["stack"]["items"]) == ["run_k_five", "run_k_six"]


def test_two_approvals_in_one_conversation_each_point_at_their_own_job(service, state):
    store, now = service.store, time.time()
    _filed(store, "k_a", "File 5 bugs on Linear", interaction="vi_typed", settled=now - 200, session="s_linear")
    _filed(store, "k_b", "File 6 bugs on Linear", interaction="vi_typed", settled=now - 20, session="s_linear")
    body = _home(service)
    cards = {i["id"]: i for i in body["items"] if i.get("needs_you")}
    assert len(cards) == 2   # two cards, two ids
    # Each stack decision draws the card (and so the Post button) of its own job.
    assert sorted((s["run_id"], cards[s["item_id"]]["run_id"]) for s in _decisions(body)) == \
        [("run_k_a", "run_k_a"), ("run_k_b", "run_k_b")]


def test_an_earlier_answer_in_the_same_conversation_is_not_folded_into_the_approval(service, state, frozen):
    frozen(NOON)
    store = service.store
    # Home reads newest job first: the earlier answer, then the approval that came after it.
    task(store, "k_found", session="s_linear", request="Find the build 73 bugs", settled=NOON - 300,
         result={"spoken": "I found five bugs.", "headline": "Five bugs found."})
    _filed(store, "k_post", "File 5 bugs on Linear", settled=NOON - 20, session="s_linear", interaction="vi_typed")
    body = _home(service)
    approval = _card(body, "run_k_post")
    assert approval["needs_you"] is True and "folded" not in approval   # no "+1" on a Post / Cancel card
    assert _card(body, "run_k_found")["kind"] in {"new_answer", "seen_answer"}   # its own card, as before
    assert [s["run_id"] for s in _decisions(body)] == ["run_k_post"]


def test_a_dismissed_job_takes_its_approval_off_the_stack(service, state):
    store = service.store
    _filed(store, "k_gone", "Post the release notes", interaction="vi_typed")
    [decision] = _decisions(_home(service))
    assert store.dismiss(["run_k_gone"]) == ["run_k_gone"]
    body = _home(service)
    assert body["stack"]["items"] == [] and service.home_state.row(decision["id"])["state"] == "done"
    assert "skipped_question" not in [r["source"]["kind"] for r in body["recommendations"]["items"]]


def test_after_the_call_ends_the_approval_is_still_on_the_stack(service, state):
    store = service.store
    call = live_call(service, started=time.time() - 600)
    _filed(store, "k_five", "File 5 bugs on Linear")
    on_call(call, "d_k_five", "k_five")
    _voice_offers(call, store, "d_k_five")
    call.status, call.call_closed = "closed", True
    with service.lock:
        service.interactions.pop("vi_call", None)
    body = _home(service)
    assert "call_id" not in _card(body, "run_k_five")
    assert [s["run_id"] for s in body["stack"]["items"]] == ["run_k_five"]


def test_an_approval_from_this_morning_is_not_folded_into_earlier_today(service, state):
    store = service.store
    _filed(store, "k_old", "Post the standup notes", interaction="vi_typed", settled=time.time() - 5 * 3600)
    store.mark_seen("k_old")
    body = _home(service)
    assert _card(body, "run_k_old")["needs_you"] is True
    assert [s["run_id"] for s in body["stack"]["items"]] == ["run_k_old"]


def test_an_approval_nobody_tapped_for_three_days_leaves_the_stack_as_expired(service, state, frozen):
    frozen(NOON)
    _filed(service.store, "k_stale", "Post the standup notes", interaction="vi_typed", settled=NOON - 60)
    [decision] = _decisions(_home(service))
    frozen(NOON + 4 * 86400)
    body = _home(service)
    assert body["stack"]["items"] == []
    assert service.home_state.row(decision["id"])["state"] == "expired"
    assert "skipped_question" not in [r["source"]["kind"] for r in body["recommendations"]["items"]]


def test_only_a_confirm_card_still_pending_waits_on_you():
    assert C.pending({"views": [{**_approval(), "state": "approved"}]}) is None
    assert C.pending({"views": [{**_approval(), "actions": []}]}) is None
    assert C.pending({"spoken": "Done."}) is None
    assert C.pending({"views": [_approval()]})["title"] == "File 5 bugs on Linear"


# -- what the voice says ----------------------------------------------------------------------------------

def test_too_long_to_approve_by_voice_says_where_the_card_is_and_which_button():
    worker, decisions = voice_worker()
    asyncio.run(V.present(worker, "reply", "task", 0, _approval()))
    said = worker.append.call_args.args[2]
    assert said == ("That one is too long to approve by voice. It's waiting on Home in the app: "
                    "tap Post on the card when you've read it.")
    assert not decisions and not worker.confirmation_offers


def test_a_short_approval_is_still_offered_by_voice():
    worker, _ = voice_worker()
    asyncio.run(V.present(worker, "reply", "task", 0, _approval(body="Bug: the card is missing.")))
    said = worker.append.call_args.args[2]
    assert said.startswith("Before I do that: Post: File 5 bugs on Linear") and said.endswith("Approve this preview?")


# -- the push --------------------------------------------------------------------------------------------

def test_a_finished_job_holding_an_approval_wakes_the_phone_as_needs_you(service):
    store = service.store
    view = _filed(store, "k_push", "File 5 bugs on Linear", interaction="vi_typed")
    sent, as_home = [], []
    worker = SidebandWorker.__new__(SidebandWorker)
    worker.store = store
    worker.rt = types.SimpleNamespace()   # not started from Home
    worker.push = lambda kind, key, title, body=None, run_id=None, home=False: (
        sent.append((kind, key, body)), as_home.append(home))
    backend = types.SimpleNamespace(run_id="run_k_push")
    worker.push_result(backend, "k_push", "completed", {"spoken": "Ready.", "views": [view]}, None, [], {})
    worker.push_result(backend, "k_push", "completed", {"spoken": "Ready.", "views": [{**view, "state": "approved"}]},
                       None, [], {})
    assert sent == [("needs_you", "confirm:run_k_push", "Waiting for you to tap Post."),
                    ("finished", "run:run_k_push", "Ready.")]
    assert as_home == [False, False]
    # A job a Home message started notifies as Home's answer (build 86), approval card or not.
    worker.rt = types.SimpleNamespace(home_origin=lambda run_id: run_id == "run_k_push")
    worker.push_result(backend, "k_push", "completed", {"spoken": "Ready.", "views": [view]}, None, [], {})
    # Its title is the assistant's name, so its words say which job.
    assert sent[-1] == ("needs_you", "confirm:run_k_push", "File 5 bugs on Linear: waiting for you to tap Post.")
    assert as_home[-1] is True


def _resolved(service, seq):
    return [e[2]["run_id"] for e in service.history_events.since(seq) or [] if e[1] == "decision_resolved"]


def test_post_or_cancel_clears_the_cards_needs_you_notification(server, service, state, home, monkeypatch):
    """Like a closed question (QA74 #15): every device hears decision_resolved, the notification's text
    says it was handled, and no later wake goes out for it."""
    relay = FakeRelay()
    service.push.relay = RelayClient(home, lambda: "https://relay.example", post=relay)
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    store = service.store

    def notified(key, title):
        view = _filed(store, key, title, interaction="vi_typed")
        service.push.notify("needs_you", key=f"confirm:run_{key}", title=title, body="Waiting for you to tap Post.",
                            run_id=f"run_{key}")
        return view, relay.notifies[-1][0]["event_id"], service.history_events.seq

    def handled(event_id):
        status, text = http(server.base_url, "GET", f"/voice/push/events/{event_id}", token=server.token)
        return status == 200 and text.get("resolved") is True and text["body"] == "Already handled."

    view, event_id, seq = notified("k_no", "Post the release notes")
    assert len(relay.notifies) == 1 and not handled(event_id)
    status, _ = post(server, "/voice/work/run_k_no/card-action", {"view": 0, "action": "cancel", "value": view["sha256"]})
    assert status == 200
    assert set(_resolved(service, seq)) == {"run_k_no"} and handled(event_id)
    service.push.notify("needs_you", key="confirm:run_k_no", title="Post the release notes", run_id="run_k_no")
    assert len(relay.notifies) == 1   # no second wake for a card already settled

    # Post closes it the same way (the action itself is never started here).
    view, event_id, seq = notified("k_yes", "File 5 bugs on Linear")
    monkeypatch.setattr(service.agent, "start_run", lambda *args, **kwargs: "run_exec")
    monkeypatch.setattr(A, "follow", lambda *args: None)
    assert A.decide(service, "run_k_yes", 0, view["sha256"], "approve")["ok"] is True
    assert set(_resolved(service, seq)) == {"run_k_yes"} and handled(event_id)
