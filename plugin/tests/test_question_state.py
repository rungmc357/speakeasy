"""One question state (SPK-79): a task's question is open until exactly one close (answered, set aside,
dismissed, moved on, expired, superseded), every close tells every device (decision_resolved) and
withdraws the Needs you notification, the sweep closes stale questions with no page open, and an
answer addressed by run id reaches the task with no call open. Invented tasks only."""
import json
import sqlite3
import time

from fakes import http, wait_for
from test_decision_cards import OTHER, QUESTION, _linked_question, _task_with_question, plain
from test_history import get, post, state  # noqa: F401
from test_push import APNS, FakeRelay, register
from speakeasy.push import RelayClient
from speakeasy.store import QUESTION_STATES, StateStore


def _resolved(service, seq):
    return [e[2] for e in service.history_events.since(seq) or [] if e[1] == "decision_resolved"]


def _push_ready(server, service, home, monkeypatch):
    relay = FakeRelay()
    service.push.relay = RelayClient(home, lambda: "https://relay.example", post=relay)
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    return relay


# -- the state ------------------------------------------------------------------------------------------

def test_a_stored_result_that_asks_opens_its_question(service):
    store = service.store
    store.reserve_run("q1", "vi_x", "d_q1", 0)
    store.update_run("q1", "run_q1", "completed")
    assert store.question_state("q1") is None
    store.set_result("q1", {"spoken": "Ready.", "question": {"kind": "question", **QUESTION}})
    assert store.question_state("q1") == "open"
    assert store.work("run_q1")["question_state"] == "open"
    store.set_result("q1", {"spoken": "Ready, nothing to ask."})
    assert store.question_state("q1") is None


def test_each_close_is_final_and_answered_wins(service):
    store = service.store
    for i, reason in enumerate(sorted(set(QUESTION_STATES) - {"open"})):
        key = f"q_{i}"
        _task_with_question(store, key, f"s_{i}", QUESTION)
        assert store.close_question(key, reason) == [key]
        assert store.question_state(key) == reason
        assert store.close_question(key, "dismissed" if reason != "dismissed" else "moved_on") == []
        if reason != "answered":
            # A tap on a card that had just moved on is still the user's answer.
            assert store.close_question(key, "answered") == [key]
        assert store.close_question(key, "answered") == [], "nothing closes an answered question again"
        assert store.work(idem_key=key)["answered"] is True


def test_rows_from_before_the_state_column_keep_how_they_closed(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    for key, run in (("old_open", "r1"), ("old_answered", "r2"), ("old_dismissed", "r3")):
        _task_with_question(store, key, "s_" + key, QUESTION)
    store._db.execute("UPDATE runs SET answered=? WHERE idem_key='old_answered'", (time.time(),))
    store._db.execute("UPDATE runs SET question_closed='dismissed' WHERE idem_key='old_dismissed'")
    store._db.commit()
    store._db.close()
    raw = sqlite3.connect(path)
    for column in ("question_state", "question_state_at"):
        raw.execute(f"ALTER TABLE runs DROP COLUMN {column}")
    raw.commit()
    raw.close()
    again = StateStore(path)
    assert again.question_state("old_open") == "open"
    assert again.question_state("old_answered") == "answered"
    assert again.question_state("old_dismissed") == "dismissed"


# -- every close reaches every device and the notification -------------------------------------------------

def test_an_expired_question_clears_the_push_and_other_devices(server, service, home, monkeypatch):
    relay = _push_ready(server, service, home, monkeypatch)
    _task_with_question(service.store, "q_day", "s_day", QUESTION, created=time.time() - 25 * 3600)
    service.push.notify("needs_you", key="question:run_q_day", title="Paint the shed", body=QUESTION["question"],
                        run_id="run_q_day")
    event_id = relay.notifies[0][0]["event_id"]
    seq = service.history_events.seq
    assert service.sweep_questions(force=True) == 1, "closed by the sweep, with no page open"
    assert service.store.question_state("q_day") == "expired"
    assert {e["run_id"] for e in _resolved(service, seq)} == {"run_q_day"}
    status, text = http(server.base_url, "GET", f"/voice/push/events/{event_id}", token=server.token)
    assert status == 200 and text["resolved"] is True and "Needs you" not in text["title"]
    # A later wake for the same question never goes out.
    service.push.notify("needs_you", key="question:run_q_day", title="Paint the shed", run_id="run_q_day")
    assert len(relay.notifies) == 1


def test_a_replaced_question_clears_the_push_and_other_devices(server, service, home, monkeypatch):
    relay = _push_ready(server, service, home, monkeypatch)
    store = service.store
    _task_with_question(store, "q_first", "s_shed", QUESTION, created=time.time() - 60)
    service.push.notify("needs_you", key="question:run_q_first", title="Paint the shed", run_id="run_q_first")
    seq = service.history_events.seq
    store.reserve_run("q_next", "vi_x", "d_q_next", 0)
    store.set_session("q_next", "s_shed")
    store.update_run("q_next", "run_q_next", "completed")
    service.sweep_questions(force=True)
    assert store.question_state("q_first") == "superseded"
    assert [e["run_id"] for e in _resolved(service, seq)] == ["run_q_first"]
    assert service.push._withdrawn.get("question:run_q_first")
    assert len(relay.notifies) == 1


def test_a_replaced_row_on_a_call_closes_its_question_at_once(service):
    store = service.store
    _task_with_question(store, "q_row", "s_row", QUESTION)
    seq = service.history_events.seq
    store.dismiss_key("q_row")  # what a follow-up replacing the row does (calls._register) ...
    assert store.close_question("q_row", "superseded") == ["q_row"]  # ... followed by this
    assert [e["run_id"] for e in _resolved(service, seq)] == ["run_q_row"]


def test_answering_the_same_question_on_a_twin_closes_both_once(service):
    store = service.store
    _task_with_question(store, "q_a", "s_twin", QUESTION, created=time.time() - 5)
    _task_with_question(store, "q_b", "s_twin", QUESTION)
    seq = service.history_events.seq
    assert sorted(store.close_question("q_b", "answered")) == ["q_a", "q_b"]
    assert sorted(e["run_id"] for e in _resolved(service, seq)) == ["run_q_a", "run_q_b"]
    assert store.close_question("q_a", "answered") == []


def test_a_dismissed_card_never_comes_back_after_a_reload(server, service, hermes, state):
    run_id, key = _linked_question(server, service, hermes, state)
    assert post(server, f"/voice/tasks/{run_id}/question/dismiss", {})[0] == 200
    # A relaunched app (or another device) reads only the server: nothing local brings it back.
    again = StateStore(service.dir / "state.sqlite3")
    assert again.question_state(key) == "dismissed"
    _, page = get(server, "/voice/history/d1")
    job = page["session"]["jobs"][0]
    assert job["question_state"] == "dismissed" and job["question_open"] is False
    service.sweep_questions(force=True)
    assert service.store.question_state(key) == "dismissed", "the sweep never reopens or re-closes it"


# -- an answer by run id, no call needed ----------------------------------------------------------------

def test_an_answer_from_the_board_with_no_call_reaches_the_task(server, service, hermes, state):
    run_id, key = _linked_question(server, service, hermes, state)
    assert not service.interactions, "no call is open"
    hermes.responder = plain
    before = len(hermes.calls)
    seq = service.history_events.seq
    status, body = post(server, f"/voice/tasks/{run_id}/question/answer", {"text": "Green", "client_id": "pick_1"})
    assert status == 200 and body["question"] == "answered", body
    wait_for(lambda: len(hermes.calls) > before)
    assert "Green" in json.dumps(hermes.calls[-1])
    assert hermes.calls[-1]["session_id"] == service.store.session_for(key), "into the task that asked"
    assert service.store.question_state(key) == "answered"
    assert run_id in {e["run_id"] for e in _resolved(service, seq)}
    # The same pick again (a retry after a timeout) gets the first reply, never a second answer.
    calls = len(hermes.calls)
    status, again = post(server, f"/voice/tasks/{run_id}/question/answer", {"text": "Green", "client_id": "pick_1"})
    assert status == 200 and again["run_id"] == body["run_id"]
    assert len(hermes.calls) == calls
    # A different answer to an answered question is refused.
    status, refused = post(server, f"/voice/tasks/{run_id}/question/answer", {"text": "Grey", "client_id": "pick_2"})
    assert status == 409


def test_answer_refuses_bad_bodies_unknown_tasks_and_tasks_that_ask_nothing(server, service):
    assert post(server, "/voice/tasks/run_nope/question/answer", {"text": "Green"})[0] == 404
    _task_with_question(service.store, "q_bad", "s_bad", OTHER)
    assert post(server, "/voice/tasks/run_q_bad/question/answer", {"text": "x", "extra": 1})[0] == 400
    service.store.reserve_run("plain_t", "vi_x", "d_plain", 0)
    service.store.update_run("plain_t", "run_plain_t", "completed")
    assert post(server, "/voice/tasks/run_plain_t/question/answer", {"text": "Green"})[0] == 409


def test_capability_is_advertised(service):
    assert service.status()["capabilities"]["question_state"] is True


def test_every_device_sees_which_option_was_taken(server, service, hermes, state):
    run_id, key = _linked_question(server, service, hermes, state)
    hermes.responder = plain
    assert post(server, f"/voice/tasks/{run_id}/question/answer", {"text": "Grey"})[0] == 200
    work = service.store.work(run_id)
    assert work["question_state"] == "answered" and work["question_answer"] == "Grey"
