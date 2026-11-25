"""SPK-361: a model picked on a call (Profile > New work, PATCH /voice/settings jobs) decides every
task that call runs, a conversation the call continues too, until it is changed. Invented requests
and catalog. The three retest cases come first: a pick made before the call, a pick made during the
call followed by a new task, and a call task that continues an existing conversation."""
from fakes import SDP, http, wait_for
from speakeasy import models as MD

import pytest

OPUS = "anthropic/claude-opus-5-5"
GPT = "openai-codex/gpt-6.1-sol"


@pytest.fixture
def catalog(service):
    service.catalog.raw()
    service.settings.patch({"jobs": {"model": OPUS, "reasoning": "medium"}})
    return service.catalog


def start_call(server, service, key, **body):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, **body}, server.token,
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


# -- the three retest cases ------------------------------------------------------------------------

def test_case1_a_pick_made_before_the_call_runs_the_calls_new_task(server, service, hermes, catalog):
    pick(server, GPT, "high")
    _, worker = start_call(server, service, "req_case_1")
    sent, record = run(service, hermes, worker, "call_soup", "List three soups")
    assert (sent["provider"], sent["model"]) == ("openai-codex", "gpt-6.1-sol")
    assert sent["model_options"] == {"reasoning": {"enabled": True, "effort": "high"}}


def test_case2_a_pick_made_during_the_call_runs_the_next_new_task(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_case_2")
    sent, _ = run(service, hermes, worker, "call_soup", "List three soups")
    assert sent["model"] == "claude-opus-5-5"
    pick(server, GPT, "high")
    sent, _ = run(service, hermes, worker, "call_bread", "Name two breads")
    assert (sent["provider"], sent["model"]) == ("openai-codex", "gpt-6.1-sol")
    assert sent["model_options"] == {"reasoning": {"enabled": True, "effort": "high"}}


def test_case3_a_call_task_continuing_a_conversation_runs_on_the_calls_pick(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_case_3")
    first, record = run(service, hermes, worker, "call_soup", "List three soups")
    assert first["model"] == "claude-opus-5-5" and record["source"] == "default"
    pick(server, GPT)
    sent, record = run(service, hermes, worker, "call_more", "Add a fourth soup", task_id="call_soup")
    assert sent["session_id"] == first["session_id"]  # the same conversation, continued
    assert (sent["provider"], sent["model"]) == ("openai-codex", "gpt-6.1-sol")
    assert "model_options" not in sent  # the pick's reasoning is the default: not borrowed from Opus
    assert record["source"] == "call"


# -- the pick's scope: that call only, until changed ------------------------------------------------

def test_the_pick_belongs_to_its_call_a_later_calls_continued_conversation_keeps_its_model(
        server, service, hermes, catalog):
    first, worker = start_call(server, service, "req_scope_1")
    run(service, hermes, worker, "call_soup", "List three soups")
    pick(server, GPT)
    end_call(server, service, first)
    _, again = start_call(server, service, "req_scope_2")
    sent, record = run(service, hermes, again, "call_more", "Add a fourth soup", task_id="call_soup")
    assert sent["model"] == "claude-opus-5-5" and record["source"] == "existing"  # SPK-210
    sent, _ = run(service, hermes, again, "call_bread", "Name two breads")
    assert sent["model"] == "gpt-6.1-sol"  # a new task still takes New work


def test_a_pick_made_with_no_call_on_tags_no_call(server, service, hermes, catalog):
    first, worker = start_call(server, service, "req_scope_3")
    end_call(server, service, first)
    pick(server, GPT)
    assert service.store.model_choice("interaction:" + first["interaction_id"]) is None


def test_the_pick_is_kept_until_changed_on_that_call(server, service, hermes, catalog):
    session, _ = start_call(server, service, "req_scope_4")
    scope = "interaction:" + session["interaction_id"]
    pick(server, GPT, "high")
    assert (service.store.model_choice(scope)["model"], service.store.model_choice(scope)["reasoning"]) == (GPT, "high")
    pick(server, GPT, "high")  # the same values again: nothing changes
    pick(server, "")
    assert service.store.model_choice(scope)["model"] == "default"


def test_a_pick_made_while_the_call_is_paused_holds_after_resume(server, service, hermes, catalog):
    session, worker = start_call(server, service, "req_scope_5")
    run(service, hermes, worker, "call_soup", "List three soups")
    iid = session["interaction_id"]
    assert http(server.base_url, "POST", f"/voice/interactions/{iid}/pause", {}, server.token)[0] == 200
    worker.call_closed()  # the app closes its voice session on pause; the call is still on, paused
    pick(server, GPT)
    resumed, again = start_call(server, service, "req_scope_5_resume", resume_from=iid)
    assert resumed["interaction_id"] != iid
    sent, record = run(service, hermes, again, "call_more", "Add a fourth soup", task_id="call_soup")
    assert sent["model"] == "gpt-6.1-sol" and record["source"] == "call"


def test_picking_hermes_default_on_a_call_moves_a_continued_conversation_to_it(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_scope_6")
    run(service, hermes, worker, "call_soup", "List three soups")
    pick(server, "")  # "Hermes default" in Profile: Speakeasy sends no model, Hermes' own runs
    sent, record = run(service, hermes, worker, "call_more", "Add a fourth soup", task_id="call_soup")
    assert not {"provider", "model", "model_options"} & set(sent)
    assert record["source"] == "hermes"


def test_saying_use_the_fast_model_for_this_still_wins_for_that_request(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_scope_7")
    pick(server, GPT)
    sent, record = run(service, hermes, worker, "call_fast", "Use the fast model for this: what time is it in Lisbon?")
    assert (record["source"], record["preset"]) == ("voice", "fast") and sent["model"] != "gpt-6.1-sol"


def test_a_chat_pick_made_after_the_call_pick_wins_for_that_chat(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_scope_8")
    pick(server, GPT)
    sent, _ = run(service, hermes, worker, "call_soup", "List three soups")
    service.store.set_model_choice("session:" + sent["session_id"], "anthropic/claude-sonnet-5-5", None)
    sent, _ = run(service, hermes, worker, "call_more", "Add a fourth soup", task_id="call_soup")
    assert sent["model"] == "claude-sonnet-5-5"


def test_text_typed_on_a_tasks_page_during_the_call_keeps_that_chats_model(server, service, hermes, catalog):
    _, worker = start_call(server, service, "req_scope_9")
    first, _ = run(service, hermes, worker, "call_soup", "List three soups")
    pick(server, GPT)
    before = len(hermes.calls)
    status, body = http(server.base_url, "POST", f"/voice/work/{first['run_id']}/message",
                        {"text": "Add a fourth soup"}, server.token)
    assert status == 200, body
    wait_for(lambda: len(hermes.calls) > before, timeout=60)
    assert hermes.calls[-1]["session_id"] == first["session_id"]
    assert hermes.calls[-1]["model"] == "claude-opus-5-5"  # the page's chip says Opus; typing follows it


@pytest.mark.parametrize("call, chat, task, wins", [
    (None, None, None, False),
    ({"updated": 2}, None, None, True),
    ({"updated": 2}, {"updated": 1}, {"updated": 1}, True),
    ({"updated": 2}, {"updated": 3}, None, False),
    ({"updated": 2}, None, {"updated": 3}, False),
])
def test_call_pick_is_the_newest_explicit_pick(call, chat, task, wins):
    assert MD.call_pick(call, chat, task) == (call if wins else None)
