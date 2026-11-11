"""SPK-357: an approval card (a ``confirm`` view still ``pending``) waits on you like a question.

On a call the voice offered "File 5 bugs on Linear", which counted the job as heard and read, so Home
dropped it and the call screen showed nothing while the voice said "open that card". Now it stays on
Home's stack as a decision until Post or Cancel, the voice says where it is when the preview is too long
to approve by voice, and the phone gets "Needs you" instead of "Done". Invented data only."""
from __future__ import annotations

import asyncio
import time
import types

from speakeasy import confirm_voice as V
from speakeasy import confirms as C
from speakeasy.calls import SidebandWorker
from test_confirm_actions import voice_worker
from test_history import get, state  # noqa: F401
from test_now import task
from test_now_call import live_call, on_call

LONG_BODY = "Bug: the approval card is missing from Home on a call. " * 85   # about 4,700 characters


def _approval(title="File 5 bugs on Linear", body=LONG_BODY):
    return C.clean({"type": "post", "title": title, "details": {"account": "Linear", "body": body}})


def _filed(store, key, title, *, interaction="vi_call", settled=None):
    view = _approval(title)
    task(store, key, "completed", session="s_" + key, request=title, interaction=interaction,
         result={"spoken": "The bugs are ready to file.", "headline": "Ready to file on Linear.", "views": [view]},
         settled=settled if settled is not None else time.time() - 5)
    return view


def _voice_offers(call, store, task_id):
    """What calls.py does after presenting the card: the voice replied from this task."""
    worker = SidebandWorker.__new__(SidebandWorker)
    worker.interaction, worker.store = call, store
    worker.rt = types.SimpleNamespace(now_changed=lambda: None)
    worker.replied_task = task_id


def _home(server):
    return get(server, "/voice/now")[1]


def _card(body, run_id):
    return next(i for i in body["items"] if i.get("run_id") == run_id)


def test_an_approval_the_voice_offered_on_a_call_stays_on_homes_stack_as_needs_you(server, service, state):
    store = service.store
    call = live_call(service, started=time.time() - 600)
    _filed(store, "k_five", "File 5 bugs on Linear")
    on_call(call, "d_k_five", "k_five")
    _voice_offers(call, store, "d_k_five")
    assert store.is_seen("k_five")   # heard on the call, as before

    body = _home(server)
    card = _card(body, "run_k_five")
    assert card["kind"] == "new_answer" and card["needs_you"] is True and card["unread"] is True
    assert card.get("on_call") != "heard"
    assert [(s["kind"], s["run_id"], s["needs_you"]) for s in body["stack"]["items"]] == \
        [("decision", "run_k_five", True)]
    assert body["counts"]["needs_you"] == 1 and body["counts"]["finished_unread"] == 0


def test_tapping_post_takes_it_off_home(server, service, state):
    store = service.store
    view = _filed(store, "k_post", "File 5 bugs on Linear", interaction="vi_typed")
    store.mark_seen("k_post")   # opened in the app
    [stacked] = _home(server)["stack"]["items"]
    assert stacked["run_id"] == "run_k_post"

    C.claim(store, "run_k_post", 0, view["sha256"], "approve", source="tap")
    body = _home(server)
    assert body["stack"]["items"] == []
    assert all(not i.get("needs_you") for i in body["items"])
    assert body["counts"]["needs_you"] == 0
    # Posted is done: never offered back as "Answer the question on File 5 bugs on Linear".
    assert service.home_state.row(stacked["id"])["state"] == "done"
    assert "skipped_question" not in [r["source"]["kind"] for r in body["recommendations"]["items"]]


def test_cancel_takes_it_off_home_too(server, service, state):
    store = service.store
    view = _filed(store, "k_cancel", "Post the release notes", interaction="vi_typed")
    assert [s["kind"] for s in _home(server)["stack"]["items"]] == ["decision"]
    C.claim(store, "run_k_cancel", 0, view["sha256"], "cancel", source="tap")
    assert [s for s in _home(server)["stack"]["items"] if s["kind"] == "decision"] == []


def test_two_approvals_from_one_call_each_keep_their_own_card(server, service, state):
    store, now = service.store, time.time()
    call = live_call(service, started=now - 600)
    _filed(store, "k_five", "File 5 bugs on Linear", settled=now - 200)
    _filed(store, "k_six", "File 6 bugs on Linear", settled=now - 20)
    for key in ("k_five", "k_six"):
        on_call(call, "d_" + key, key)
        _voice_offers(call, store, "d_" + key)
    body = _home(server)
    waiting = [i["run_id"] for i in body["items"] if i.get("needs_you")]
    assert sorted(waiting) == ["run_k_five", "run_k_six"]
    assert sorted(s["run_id"] for s in body["stack"]["items"]) == ["run_k_five", "run_k_six"]


def test_after_the_call_ends_the_approval_is_still_on_the_stack(server, service, state):
    store = service.store
    call = live_call(service, started=time.time() - 600)
    _filed(store, "k_five", "File 5 bugs on Linear")
    on_call(call, "d_k_five", "k_five")
    _voice_offers(call, store, "d_k_five")
    call.status, call.call_closed = "closed", True
    with service.lock:
        service.interactions.pop("vi_call", None)
    body = _home(server)
    assert "call_id" not in _card(body, "run_k_five")
    assert [s["run_id"] for s in body["stack"]["items"]] == ["run_k_five"]


def test_an_approval_from_this_morning_is_not_folded_into_earlier_today(server, service, state):
    store = service.store
    _filed(store, "k_old", "Post the standup notes", interaction="vi_typed", settled=time.time() - 5 * 3600)
    store.mark_seen("k_old")
    body = _home(server)
    assert _card(body, "run_k_old")["needs_you"] is True
    assert [s["run_id"] for s in body["stack"]["items"]] == ["run_k_old"]


def test_only_a_confirm_card_still_pending_waits_on_you():
    assert C.pending({"views": [{**_approval(), "state": "approved"}]}) is None
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
    sent = []
    worker = SidebandWorker.__new__(SidebandWorker)
    worker.store = store
    worker.push = lambda kind, key, title, body=None, run_id=None: sent.append((kind, key, body))
    backend = types.SimpleNamespace(run_id="run_k_push")
    worker.push_result(backend, "k_push", "completed", {"spoken": "Ready.", "views": [view]}, None, [], {})
    worker.push_result(backend, "k_push", "completed", {"spoken": "Ready.", "views": [{**view, "state": "approved"}]},
                       None, [], {})
    assert sent == [("needs_you", "confirm:run_k_push", "Waiting for you to tap Post."),
                    ("finished", "run:run_k_push", "Ready.")]
