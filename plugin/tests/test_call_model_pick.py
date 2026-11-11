"""SPK-361: a model picked on a call (Profile > New work, PATCH /voice/settings jobs) decides every
task a call runs, a continued conversation too, until it is changed. Invented requests and catalog."""
import time

import pytest

from fakes import SDP, http, wait_for
from speakeasy import models as MD

OPUS = "anthropic/claude-opus-5-5"
GPT = "openai-codex/gpt-6.1-sol"


@pytest.fixture
def catalog(service):
    service.catalog.raw()
    service.settings.patch({"jobs": {"model": OPUS, "reasoning": "medium"}})
    return service.catalog


def start_call(server, service, key):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def end_call(server, service, session):
    http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/end", {}, server.token)
    service.interaction(session["interaction_id"]).call_closed = True


def pick(server, model, reasoning=""):
    status, body = http(server.base_url, "PATCH", "/voice/settings",
                        {"jobs": {"model": model, "reasoning": reasoning}}, server.token)
    assert status == 200, body


def run(service, hermes, worker, delegation_id, words, **extra):
    """Say it on the call; the run Hermes was asked for, once it has finished."""
    before = len(hermes.calls)
    worker.delegate(delegation_id, words, **extra)
    wait_for(lambda: len(hermes.calls) > before, timeout=60)
    sent = hermes.calls[-1]
    key = wait_for(lambda: service.store.key_for_run(sent["run_id"]), timeout=60)
    wait_for(lambda: (service.store.work(idem_key=key) or {}).get("status") == "completed", timeout=60)
    return sent, service.store.run_model(key)


def test_a_pick_on_the_call_runs_the_next_new_task_and_the_next_call(server, service, hermes, catalog):
    first, worker = start_call(server, service, "req_pick_1")
    pick(server, GPT, "high")
    sent, record = run(service, hermes, worker, "call_soup", "List three soups")
    assert (sent["provider"], sent["model"]) == ("openai-codex", "gpt-6.1-sol")
    assert sent["model_options"] == {"reasoning": {"enabled": True, "effort": "high"}}
    assert record["source"] == "call"
    end_call(server, service, first)
    _, again = start_call(server, service, "req_pick_2")
    sent, _ = run(service, hermes, again, "call_bread", "Name two breads")
    assert sent["model"] == "gpt-6.1-sol"


def test_a_conversation_continued_on_the_call_runs_on_the_pick_not_its_old_model(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_pick_3")
    sent, record = run(service, hermes, worker, "call_soup", "List three soups")
    assert sent["model"] == "claude-opus-5-5" and record["source"] == "default"
    pick(server, GPT)
    sent, record = run(service, hermes, worker, "call_more", "Add a fourth soup", task_id="call_soup")
    assert sent["session_id"] == hermes.calls[0]["session_id"]  # the same conversation, continued
    assert (sent["provider"], sent["model"]) == ("openai-codex", "gpt-6.1-sol")
    assert "model_options" not in sent  # the pick's reasoning is the default: not borrowed from Opus
    assert record["source"] == "call"


def test_with_no_pick_on_a_call_a_continued_conversation_keeps_its_model(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_pick_4")
    run(service, hermes, worker, "call_soup", "List three soups")
    end_call(server, service, _)
    pick(server, GPT)  # a new default set with no call live: conversations keep theirs (SPK-210)
    assert service.store.model_choice("call") is None
    _, again = start_call(server, service, "req_pick_5")
    sent, record = run(service, hermes, again, "call_more", "Add a fourth soup", task_id="call_soup")
    assert sent["model"] == "claude-opus-5-5" and record["source"] == "existing"
    sent, _ = run(service, hermes, again, "call_bread", "Name two breads")
    assert sent["model"] == "gpt-6.1-sol"  # a new task still takes the new default


def test_picking_hermes_default_on_a_call_moves_a_continued_conversation_to_it(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_pick_6")
    run(service, hermes, worker, "call_soup", "List three soups")
    pick(server, "")  # "Hermes default" in Profile: Speakeasy sends no model, Hermes' own runs
    sent, record = run(service, hermes, worker, "call_more", "Add a fourth soup", task_id="call_soup")
    assert not {"provider", "model", "model_options"} & set(sent)
    assert record["source"] == "hermes"


def test_saying_use_the_fast_model_for_this_still_wins_for_that_request(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_pick_7")
    pick(server, GPT)
    sent, record = run(service, hermes, worker, "call_fast", "Use the fast model for this: what time is it in Lisbon?")
    assert (record["source"], record["preset"]) == ("voice", "fast") and sent["model"] != "gpt-6.1-sol"


def test_a_chat_pick_made_after_the_call_pick_wins_for_that_chat(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_pick_9")
    pick(server, GPT)
    sent, _ = run(service, hermes, worker, "call_soup", "List three soups")
    time.sleep(0.01)
    service.store.set_model_choice("session:" + sent["session_id"], "anthropic/claude-sonnet-5-5", None)
    sent, _ = run(service, hermes, worker, "call_more", "Add a fourth soup", task_id="call_soup")
    assert sent["model"] == "claude-sonnet-5-5"


def test_text_typed_on_a_tasks_page_during_the_call_keeps_that_chats_model(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_pick_10")
    first, _ = run(service, hermes, worker, "call_soup", "List three soups")
    pick(server, GPT)
    before = len(hermes.calls)
    status, body = http(server.base_url, "POST", f"/voice/work/{first['run_id']}/message",
                        {"text": "Add a fourth soup"}, server.token)
    assert status == 200, body
    wait_for(lambda: len(hermes.calls) > before, timeout=60)
    assert hermes.calls[-1]["session_id"] == first["session_id"]
    assert hermes.calls[-1]["model"] == "claude-opus-5-5"  # the page's chip says Opus; typing follows it


def test_the_pick_is_kept_until_changed_and_cleared_by_a_pick_off_a_call(server, service, hermes, catalog):
    session, _ = start_call(server, service, "req_pick_8")
    pick(server, GPT, "high")
    assert service.store.model_choice("call") == {**service.store.model_choice("call"), "model": GPT,
                                                  "reasoning": "high"}
    pick(server, "")
    assert service.store.model_choice("call")["model"] == "default"
    end_call(server, service, session)
    pick(server, OPUS)
    assert service.store.model_choice("call") is None


@pytest.mark.parametrize("call, chat, task, wins", [
    (None, None, None, False),
    ({"updated": 2}, None, None, True),
    ({"updated": 2}, {"updated": 1}, {"updated": 1}, True),
    ({"updated": 2}, {"updated": 3}, None, False),
    ({"updated": 2}, None, {"updated": 3}, False),
])
def test_call_pick_is_the_newest_explicit_pick(call, chat, task, wins):
    assert MD.call_pick(call, chat, task) == (call if wins else None)
