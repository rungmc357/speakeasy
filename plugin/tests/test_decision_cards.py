"""Decision cards (SPK-68): a question clears itself when the conversation moves on, the ✕ dismisses it
everywhere (with Undo) without telling the agent anything, "Reply…" answers one question in your own
words, and two open questions stay independent. Invented sessions and tasks only; nothing is routed."""
import json
import time

from fakes import wait_for
from test_history import NOW, add_msg, add_session, get, post, state  # noqa: F401

QUESTION = {"question": "Paint the shed green or grey?", "options": ["Green", "Grey"], "recommended": 0}
OTHER = {"question": "Order the hinges from the hardware shop?", "options": ["Order them", "Wait"],
         "recommended": 0}


def asks(prompt, session_id):
    return "Sanded and primed.\n\n```speakeasy-question\n" + json.dumps(QUESTION) + "\n```"


def plain(prompt, session_id):
    return "Done: " + prompt[-40:]


def _task_with_question(store, key, session_id, question, *, created=None):
    store.reserve_run(key, "vi_x", "d_" + key, 0)
    store.set_session(key, session_id)
    store.update_run(key, "run_" + key, "completed")
    store._db.execute("UPDATE runs SET result_json=?, created=?, settled_at=? WHERE idem_key=?",
                      (json.dumps({"spoken": "Ready.", "question": {"kind": "question", **question}}),
                       created or time.time(), created or time.time(), key))


def _linked_question(server, service, hermes, state):
    """A Discord chat continued from Speakeasy whose linked task ends on a question."""
    hermes.responder = asks
    status, first = post(server, "/voice/history/d1/message", {"text": "Pick a colour for the shed"})
    assert status == 200 and first["via"] == "linked", first
    key = service.store.key_for_run(first["run_id"])
    linked = wait_for(lambda: service.store.session_for(key))
    add_session(state, linked, "api_server", at=NOW - 2)
    wait_for(lambda: service.store.work(first["run_id"])["status"] == "completed")
    _, page = get(server, "/voice/history/d1")
    assert [j.get("question_open") for j in page["session"]["jobs"]] == [True]
    return first["run_id"], key


# -- 1. stale questions clear themselves ----------------------------------------------------------------

def test_a_newer_turn_in_the_chat_moves_the_question_on_everywhere(server, service, hermes, state):
    run_id, key = _linked_question(server, service, hermes, state)
    seq = service.history_events.seq
    # The conversation went on in Discord itself (a newer message from the user there).
    add_msg(state, "d1", "user", "Never mind, what time is the hardware shop open?", at=time.time() + 30)
    _, page = get(server, "/voice/history/d1")
    session = page["session"]
    job = session["jobs"][0]
    assert job["question_open"] is False and job["question_closed"] == "moved_on"
    assert job["set_aside"] is True, "older apps stop showing the card too"
    assert session["state"] != "needs_you"
    assert job["result"]["question"]["question"] == QUESTION["question"], "kept for the quiet history line"
    # The board, Needs you, notifications and the Live Activity read the same record.
    assert service.store.work(run_id).get("question_closed") == "moved_on"
    events = [e for e in service.history_events.since(seq) or [] if e[1] == "decision_resolved"]
    assert {e[2]["session_id"] for e in events} >= {"d1"}
    assert len(hermes.calls) == 1, "moving on tells the agent nothing"


def test_the_agent_replying_later_in_the_chat_also_moves_on(server, service, hermes, state):
    _, key = _linked_question(server, service, hermes, state)
    add_msg(state, "d1", "assistant", "The shop opens at 9 am.", at=time.time() + 30)
    service.sweep_questions(force=True)  # no page open: the board's poll finds it
    assert service.store.question_closed(key) == "moved_on"
    assert service.work_latest()["tasks"] is not None


def test_an_old_message_in_the_chat_does_not_close_a_new_question(server, service, hermes, state):
    _, key = _linked_question(server, service, hermes, state)
    service.sweep_questions(force=True)
    assert service.store.question_closed(key) is None
    _, row = get(server, "/voice/history")
    assert next(r for r in row["sessions"] if r["id"] == "d1")["state"] == "needs_you"


def test_a_newer_task_in_the_same_thread_supersedes_the_question(service):
    store = service.store
    _task_with_question(store, "q_old", "s_shed", QUESTION, created=time.time() - 60)
    store.reserve_run("q_new", "vi_x", "d_q_new", 0)
    store.set_session("q_new", "s_shed")
    store.update_run("q_new", "run_q_new", "completed")
    assert service._open_questions(["q_old", "q_new"]) == []
    assert store.question_state("q_old") == "open", "a read never writes"
    service.sweep_questions(force=True)
    assert store.question_closed("q_old") == "superseded"


# -- 2. dismiss, with undo ----------------------------------------------------------------------------

def test_dismiss_clears_it_everywhere_and_undo_brings_it_back(server, service, hermes, state):
    run_id, key = _linked_question(server, service, hermes, state)
    calls = len(hermes.calls)
    status, body = post(server, f"/voice/tasks/{run_id}/question/dismiss", {})
    assert status == 200 and body == {"run_id": run_id, "question": "dismissed"}
    _, page = get(server, "/voice/history/d1")
    assert page["session"]["jobs"][0]["question_closed"] == "dismissed"
    assert page["session"]["state"] != "needs_you"
    assert service.store.work(run_id).get("set_aside") is True
    assert service.store.work(run_id)["status"] == "completed", "dismissing never settles it as not needed"
    status, _ = post(server, f"/voice/tasks/{run_id}/question/dismiss", {})
    assert status == 409
    status, body = post(server, f"/voice/tasks/{run_id}/question/undo", {})
    assert status == 200 and body["question"] == "open"
    _, page = get(server, "/voice/history/d1")
    assert page["session"]["jobs"][0]["question_open"] is True
    assert "question_closed" not in page["session"]["jobs"][0]
    assert len(hermes.calls) == calls, "dismiss and undo tell the agent nothing"


def test_undo_cannot_reopen_a_question_that_moved_on(service):
    _task_with_question(service.store, "q1", "s_a", QUESTION)
    assert service.store.close_question("q1", "moved_on")
    assert not service.store.reopen_question("q1")


def test_dismiss_refuses_bad_bodies_and_unknown_tasks(server):
    assert post(server, "/voice/tasks/run_nope/question/dismiss", {})[0] == 404
    assert post(server, "/voice/tasks/run_nope/question/dismiss", {"x": 1})[0] == 400


def test_capability_is_advertised(service):
    assert service.status()["capabilities"]["question_actions"] is True


# -- 3. answer in your own words -------------------------------------------------------------------------

def test_reply_bound_to_a_question_is_delivered_as_its_answer(server, service, hermes, state):
    run_id, key = _linked_question(server, service, hermes, state)
    hermes.responder = plain
    before = len(hermes.calls)
    status, body = post(server, "/voice/history/d1/message",
                        {"text": "Grey, but a warm one", "answers": run_id})
    assert status == 200 and body["via"] == "answer", body
    wait_for(lambda: len(hermes.calls) > before)
    assert hermes.calls[-1]["session_id"] == service.store.session_for(key), "into the task that asked"
    assert service.store.work(idem_key=key).get("answered") is True
    _, page = get(server, "/voice/history/d1")
    assert not any(j.get("question_open") for j in page["session"]["jobs"])


def test_reply_naming_a_question_from_another_chat_is_refused(server, service, hermes, state):
    _task_with_question(service.store, "q_far", "s_far", QUESTION)
    status, _ = post(server, "/voice/history/d1/message", {"text": "Grey", "answers": "run_q_far"})
    assert status == 404
    assert post(server, "/voice/history/d1/message", {"text": "Grey", "answers": 5})[0] == 400


# -- 4. two open decisions are independent ------------------------------------------------------------------

def test_two_questions_in_two_threads_stay_independent(service):
    store = service.store
    _task_with_question(store, "q_paint", "s_paint", QUESTION, created=time.time() - 30)
    _task_with_question(store, "q_hinge", "s_hinge", OTHER)
    assert service._open_questions(["q_paint", "q_hinge"]) == ["q_hinge", "q_paint"]
    assert service._close_question("q_hinge", "dismissed")
    assert service._open_questions(["q_paint", "q_hinge"]) == ["q_paint"]
    assert store.mark_answered("q_paint")
    assert service._open_questions(["q_paint", "q_hinge"]) == []
    assert store.reopen_question("q_hinge")
    assert service._open_questions(["q_paint", "q_hinge"]) == ["q_hinge"], "undo brings back only its own"
