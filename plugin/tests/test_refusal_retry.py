"""A model's safety-filter refusal that goes through when asked again (Anthropic's
"reasoning_extraction" false positive on an ordinary follow-up) is tried once more on the same
session, quietly: the task card never says it failed, nothing is spoken, and the answer that was
refused reaches the task. Hermes' own "your request was not processed" boundary row is never shown
as the assistant's reply. Everything here is invented."""
import logging
import sqlite3

import pytest

from fakes import http, wait_for
from speakeasy import calls as C
from speakeasy import failures
from speakeasy import history as H
from speakeasy.prompt import builder as P
from test_history import add_msg, add_session, make_db
from test_run_failures import REFUSALS, start_call
from test_task_messages import one_task, send, spoken

# An invented refusal shaped like Hermes' verdict for Anthropic's stop_reason=refusal.
REASONING_REFUSAL = ("content_policy_blocked: model declined (content_filter); "
                     "provider refusal category: reasoning_extraction")


@pytest.fixture(autouse=True)
def quick_retry(monkeypatch):
    monkeypatch.setattr(C, "REFUSAL_RETRY_DELAY_S", 0.01)


def latest(server):
    return http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]


def test_a_refusal_is_tried_once_more_and_never_shows_as_failed(server, service, hermes, caplog):
    caplog.set_level(logging.WARNING, logger="speakeasy.calls")
    hermes.fail_with, hermes.fail_left = REASONING_REFUSAL, 1
    seen: list[str] = []
    service.store.settle_listeners.append(lambda key, status: seen.append(status))
    _, worker = start_call(server, service)
    worker.delegate("call_beds", "Compare two soil suppliers for the garden")
    done = wait_for(lambda: [t for t in latest(server) if t["status"] == "completed"])[0]
    assert len(hermes.calls) == 2
    first, second = hermes.calls
    assert second["session_id"] == first["session_id"] and second["input"] == first["input"]
    assert second["idem"] == first["idem"] + ":retry1"     # its own key: Hermes keeps the refused run's
    assert done["run_id"] == second["run_id"] and not done.get("failure")
    assert "failed" not in seen                            # never settled failed, not even for a moment
    assert not any(failures.voice_text("content_refused") in s for s in spoken(worker))
    # The refused run's id still opens the task; the refusal and its category are in errors.log.
    assert service.store.key_for_run(first["run_id"]) == service.store.key_for_run(second["run_id"])
    assert any("reasoning_extraction" in r.getMessage() and "once more" in r.getMessage() for r in caplog.records)


def test_a_refusal_that_repeats_fails_after_one_retry(server, service, hermes):
    hermes.fail_with = REFUSALS[0]
    _, worker = start_call(server, service)
    worker.delegate("call_tidy", "Tidy up the checkout tests")
    task = wait_for(lambda: [t for t in latest(server) if t["status"] == "failed" and t.get("failure")])[0]
    assert task["failure"]["kind"] == "content_refused" and len(hermes.calls) == 2
    wait_for(lambda: P.failed_because(failures.voice_text("content_refused")) in spoken(worker))


def test_other_failures_are_not_retried(server, service, hermes):
    hermes.fail_with = "HTTP 402: Insufficient USD or Diem balance"
    _, worker = start_call(server, service)
    worker.delegate("call_news", "Summarize the cycling news")
    wait_for(lambda: [t for t in latest(server) if t["status"] == "failed" and t.get("failure")])
    assert len(hermes.calls) == 1


def test_a_decision_answer_refused_once_reaches_the_task_without_a_failure_card(server, service, hermes):
    """The reported case: a decision card's answer starts the task's next turn, the provider refuses
    it once, and the card said "Didn't finish" while the work carried on. Now the same turn is asked
    again, the answer reaches the task, and no failure ever shows."""
    from test_question_cards import ASK
    hermes.responder = lambda prompt, sid: (ASK if "Four raised beds" not in prompt
                                            else "Ordered the soil.\nDONE: Soil ordered\nSPOKEN: Ordered.")
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Plan the garden beds", status="completed")
    hermes.fail_with, hermes.fail_left = REASONING_REFUSAL, 1
    status, body = send(server, task["run_id"], "Four raised beds", client_id="c_pick")
    assert status == 200 and body["delivered_as"] == "answer", body
    wait_for(lambda: len(hermes.calls) >= 3)
    refused, retried = hermes.calls[1], hermes.calls[2]
    assert "Four raised beds" in refused["input"] and retried["input"] == refused["input"]
    assert retried["session_id"] == hermes.calls[0]["session_id"]
    done = wait_for(lambda: [t for t in latest(server) if t.get("run_id") == retried["run_id"]
                             and t["status"] == "completed"])[0]
    assert not done.get("failure") and "Ordered the soil" in (done.get("result") or {}).get("full", "")
    assert not any(t["status"] == "failed" for t in latest(server))
    assert not any(failures.voice_text("content_refused") in s for s in spoken(worker))


# -- the session page: Hermes' failed-turn boundary row ----------------------------------------------

NOTICE = "Your request was not processed. Send it again if you still want me to carry it out."


def _mark_failed_turn(path):
    db = sqlite3.connect(path)
    db.execute("UPDATE messages SET display_kind='failed_turn' WHERE content=?", (NOTICE,))
    db.commit()
    db.close()


def test_a_resent_turn_after_a_failure_reads_as_one_message(tmp_path):
    path = tmp_path / "state.db"
    make_db(path)
    add_session(path, "s_soil", "api_server")
    add_msg(path, "s_soil", "user", "Which soil should I buy?", at=100)
    add_msg(path, "s_soil", "assistant", NOTICE, at=101)
    add_msg(path, "s_soil", "user", "Which soil should I buy?", at=110)
    add_msg(path, "s_soil", "assistant", "The loam blend: it drains best.", at=120)
    _mark_failed_turn(path)
    entries = H.session_messages(path, "s_soil")["entries"]
    assert [(e["kind"], e["text"]) for e in entries] == [
        ("user", "Which soil should I buy?"), ("assistant", "The loam blend: it drains best.")]


def test_a_follow_up_of_a_follow_up_shows_only_the_words(tmp_path):
    """Answering a failed task's question asks it as a follow-up of a follow-up: the notes nest and
    are cut short before any ")". The bubble shows the words alone, and a resend folds into one."""
    path = tmp_path / "state.db"
    make_db(path)
    add_session(path, "s_beds", "api_server")
    def brief(said):
        return ("You are Ava, handling one task Sam gave you by voice from Speakeasy on their Mac.\n\n"
                f"Recent voice conversation:\nUser: {said}\n\nDo the task.")

    nested = ("Order the loam blend (following up: Compare two soil suppliers? "
              "(following up: Which beds need new soil this spring, the raised ones or the long")
    add_msg(path, "s_beds", "user", brief("Order the loam blend"), at=100)
    add_msg(path, "s_beds", "assistant", NOTICE, at=101)
    add_msg(path, "s_beds", "user", brief(nested), at=110)
    entries = H.session_messages(path, "s_beds")["entries"]
    assert [(e["kind"], e["text"]) for e in entries] == [("user", "Order the loam blend")]


def test_a_failed_turn_nothing_followed_is_a_quiet_note_not_a_reply(tmp_path):
    path = tmp_path / "state.db"
    make_db(path)
    add_session(path, "s_soil", "api_server")
    add_msg(path, "s_soil", "user", "Which soil should I buy?", at=100)
    add_msg(path, "s_soil", "assistant", NOTICE, at=101)   # older Hermes: the text alone, no display_kind
    entries = H.session_messages(path, "s_soil")["entries"]
    assert entries[-1]["kind"] == "note" and entries[-1]["text"] == H.FAILED_TURN_NOTE
    assert not any(e["kind"] == "assistant" for e in entries)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        preview, _ = H._preview(db, "s_soil")
    finally:
        db.close()
    assert preview == "Which soil should I buy?"           # the list row never previews the notice
