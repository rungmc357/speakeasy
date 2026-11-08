"""Per-chat model and reasoning (docs/API.md "Models", speakeasy/models.py): Hermes' catalog, the
choice saved per chat / task, and what each run is sent. Invented catalog (fakes.model_options_payload)."""
import json
import sqlite3
import time

import pytest

from fakes import SDP, http, wait_for
from speakeasy import models as MD
from test_history import add_msg, add_session, make_db

NOW = time.time()


def get(server, path):
    return http(server.base_url, "GET", path, token=server.token)


def post(server, path, body):
    return http(server.base_url, "POST", path, body, server.token)


def put(server, path, body):
    return http(server.base_url, "PUT", path, body, server.token)


@pytest.fixture
def state(service):
    path = service.rt.state_db
    make_db(path)
    add_session(path, "d1", "discord", title="Shelf brackets", display="Workshop / #build / Shelf brackets",
                chat_type="thread", at=NOW - 100)
    add_msg(path, "d1", "user", "Can you size the walnut shelf brackets?", at=NOW - 200)
    add_session(path, "api_other", "api_server", title="Recipe scaling", at=NOW - 5)
    add_msg(path, "api_other", "user", "Scale the soup recipe", at=NOW - 6)
    return path


@pytest.fixture
def catalog(service):
    service.catalog.raw()  # read once, the way the background warm-up does
    return service.catalog


# -- the catalog ---------------------------------------------------------------------------------

def test_models_lists_signed_in_providers_with_friendly_names_and_levels(server, hermes, catalog):
    status, body = get(server, "/voice/models")
    assert status == 200, body
    assert body["available"] is True
    ids = [p["id"] for p in body["providers"]]
    assert ids[0] == "anthropic" and "nous" not in ids          # current first; signed-out hidden
    anthropic = body["providers"][0]
    opus = next(m for m in anthropic["models"] if m["model"] == "claude-opus-5-5")
    assert opus["name"] == "Claude Opus 5.5" and opus["default"] is True and opus["hermes_default"] is True
    assert opus["reasoning_levels"][-1] == "max" and "none" in opus["reasoning_levels"]
    codex = next(p for p in body["providers"] if p["id"] == "openai-codex")
    sol = next(m for m in codex["models"] if m["model"] == "gpt-6.1-sol")
    assert sol["name"] == "GPT-6.1 Sol" and sol["id"] == "openai-codex/gpt-6.1-sol"
    qwen = next(p for p in body["providers"] if p["id"] == "studio-qwen")
    assert qwen["local"] is True and qwen["models"][0]["reasoning_levels"] == []   # doesn't reason
    assert body["default"] == {"model": "anthropic/claude-opus-5-5", "reasoning": None, "source": "hermes"}
    assert body["fast"]["model"] == "anthropic/claude-haiku-5-5" and body["fast"]["reasoning"] == "low"
    assert body["smart"]["model"] == "anthropic/claude-opus-5-5" and body["smart"]["reasoning"] == "high"
    assert [lvl["id"] for lvl in body["levels"]] == list(MD.LEVELS)


def test_catalog_is_cached_and_refreshes_on_request(server, hermes, catalog):
    reads = hermes.options_reads
    get(server, "/voice/models")
    get(server, "/voice/models")
    assert hermes.options_reads == reads
    get(server, "/voice/models?refresh=1")
    assert hermes.options_reads == reads + 1


def test_catalog_unavailable_says_so(server, hermes, service):
    hermes.model_options = None
    status, body = get(server, "/voice/models")
    assert status == 200 and body["available"] is False and body["providers"] == []


def test_status_advertises_model_choice(server):
    assert get(server, "/voice/status")[1]["capabilities"]["model_choice"] is True


def test_friendly_names_and_level_clamping():
    assert MD.friendly_name("claude-sonnet-4-5-20250929") == "Claude Sonnet 4.5"
    assert MD.friendly_name("anthropic/claude-haiku-4.5") == "Claude Haiku 4.5"
    assert MD.friendly_name("gpt-5.4-mini") == "GPT-5.4 Mini"
    assert MD.friendly_name("deepseek-v4.1-flash") == "DeepSeek V4.1 Flash"
    assert MD.clamp("max", ["none", "low", "medium", "high"]) == "high"        # never more than asked
    assert MD.clamp("minimal", ["low", "medium", "high"]) == "low"
    assert MD.clamp("none", ["low", "medium"]) is None                         # can't switch it off
    assert MD.clamp("high", []) is None


# -- validation ----------------------------------------------------------------------------------

def test_bad_choices_are_refused(server, state, catalog):
    assert post(server, "/voice/work", {"text": "x", "model": "gpt-4o"})[0] == 400           # not an id
    status, body = post(server, "/voice/work", {"text": "x", "model": "anthropic/claude-none"})
    assert status == 400 and "isn't one Hermes offers" in body["error"]
    assert post(server, "/voice/work", {"text": "x", "reasoning": "ultra"})[0] == 400
    status, body = post(server, "/voice/work", {"text": "x", "model": "studio-qwen/qwen3.8-coder",
                                                "reasoning": "high"})
    assert status == 400 and "doesn't take" in body["error"]
    assert put(server, "/voice/history/d1/model", {"model": "nous/hermes-5-405b"})[0] == 400  # signed out
    assert put(server, "/voice/history/d1/model", {"colour": "red"})[0] == 400
    assert put(server, "/voice/history/nope/model", {"reasoning": "low"})[0] == 404
    assert http(server.base_url, "PATCH", "/voice/settings", {"jobs": {"model": "anthropic/x"}},
                server.token)[0] == 400
    assert http(server.base_url, "PATCH", "/voice/settings", {"jobs": {"reasoning": "loud"}}, server.token)[0] == 400


# -- request threading ---------------------------------------------------------------------------

def test_nothing_chosen_sends_nothing_and_shows_hermes_default(server, service, hermes, catalog):
    status, started = post(server, "/voice/work", {"text": "Draft the bake sale flyer"})
    assert status == 202, started
    call = wait_for(lambda: hermes.calls)[-1]
    assert not {"provider", "model", "model_options"} & set(call)
    work = wait_for(lambda: (get(server, f"/voice/work/{started['run_id']}")[1]["work"].get("model") or {})
                    .get("served") and get(server, f"/voice/work/{started['run_id']}")[1]["work"])
    assert work["model"]["id"] == "anthropic/claude-opus-5-5" and work["model"]["name"] == "Claude Opus 5.5"
    assert work["model"]["source"] == "hermes"


def test_new_chat_choice_runs_the_job_and_sticks_to_its_chat(server, service, hermes, state, catalog):
    status, started = post(server, "/voice/work", {"text": "Plan the tomato beds", "model": "openai-codex/gpt-6.1-sol",
                                                   "reasoning": "xhigh"})
    assert status == 202, started
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["provider"] == "openai-codex" and call["model"] == "gpt-6.1-sol"
    assert call["model_options"] == {"reasoning": {"enabled": True, "effort": "xhigh"}}
    key = service.store.key_for_run(started["run_id"])
    sid = service.store.session_for(key)
    assert service.store.model_choice("session:" + sid)["model"] == "openai-codex/gpt-6.1-sol"
    task = wait_for(lambda: (service.store.work(started["run_id"]) or {}).get("model", {}).get("served")
                    and service.store.work(started["run_id"]))
    assert task["model"] == {**task["model"], "id": "openai-codex/gpt-6.1-sol", "name": "GPT-6.1 Sol",
                             "reasoning": "xhigh", "reasoning_label": "Extra high", "source": "task"}
    # Its follow-up from the session page keeps the chat's model.
    add_session(state, sid, "api_server", at=NOW - 1)
    add_msg(state, sid, "user", "User: Plan the tomato beds", at=NOW - 2)
    wait_for(lambda: service.store.work(started["run_id"])["status"] == "completed")
    before = len(hermes.calls)
    status, body = post(server, f"/voice/history/{sid}/message", {"text": "Add a row of basil"})
    assert status == 200 and body["via"] == "task", body
    wait_for(lambda: len(hermes.calls) > before)
    assert hermes.calls[-1]["model"] == "gpt-6.1-sol" and hermes.calls[-1]["session_id"] == sid
    _, page = get(server, f"/voice/history/{sid}")
    s = page["session"]
    assert s["model_choice"] == {"model": "openai-codex/gpt-6.1-sol", "reasoning": "xhigh"}
    assert s["next_model"]["name"] == "GPT-6.1 Sol" and s["model"]["id"] == "openai-codex/gpt-6.1-sol"


def test_use_for_this_chat_persists_and_threads_into_a_session_job(server, service, hermes, state, catalog):
    status, body = put(server, "/voice/history/api_other/model", {"model": "anthropic/claude-sonnet-5-5",
                                                                   "reasoning": "low"})
    assert status == 200, body
    assert body["model_choice"] == {"model": "anthropic/claude-sonnet-5-5", "reasoning": "low"}
    assert body["next_model"]["name"] == "Claude Sonnet 5.5" and body["next_model"]["reasoning"] == "low"
    # Survives a restart: it's in the store, not memory.
    assert service.store.model_choice("session:api_other")["reasoning"] == "low"
    post(server, "/voice/history/api_other/message", {"text": "Now for twelve people"})
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["session_id"] == "api_other" and call["model"] == "claude-sonnet-5-5"
    assert call["model_options"]["reasoning"]["effort"] == "low"
    # Reasoning alone keeps the model; "default" clears a half.
    put(server, "/voice/history/api_other/model", {"reasoning": "none"})
    assert service.store.model_choice("session:api_other") == {
        **service.store.model_choice("session:api_other"), "model": "anthropic/claude-sonnet-5-5", "reasoning": "none"}
    put(server, "/voice/history/api_other/model", {"model": "default", "reasoning": "default"})
    assert service.store.model_choice("session:api_other")["model"] == "default"
    assert get(server, "/voice/history/api_other")[1]["session"]["model_choice"] is None


def test_switching_model_clamps_the_chats_level(server, service, state, catalog):
    put(server, "/voice/history/api_other/model", {"model": "anthropic/claude-opus-5-5", "reasoning": "max"})
    put(server, "/voice/history/api_other/model", {"model": "anthropic/claude-haiku-5-5"})
    assert service.store.model_choice("session:api_other")["reasoning"] == "high"


def test_a_message_carrying_a_choice_sets_it_for_the_chat(server, service, hermes, state, catalog):
    # Existing Hermes model is known, but Speakeasy has never saved an override.
    with sqlite3.connect(state) as db:
        db.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
        db.execute("UPDATE sessions SET model='openai-codex/gpt-6.1-sol' WHERE id='api_other'")
    status, body = post(server, "/voice/history/api_other/message", {"text": "Halve it", "reasoning": "high"})
    assert status == 200, body
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["provider"] == "openai-codex" and call["model"] == "gpt-6.1-sol"
    assert call["model_options"] == {"reasoning": {"enabled": True, "effort": "high"}}
    assert service.store.model_choice("session:api_other") == {
        **service.store.model_choice("session:api_other"), "model": None, "reasoning": "high"}


def test_linked_tasks_use_their_chats_choice(server, service, hermes, state, catalog):
    put(server, "/voice/history/d1/model", {"model": "openai-codex/gpt-5.5", "reasoning": "medium"})
    status, body = post(server, "/voice/history/d1/message", {"text": "Order them please"})
    assert status == 200 and body["via"] == "linked", body
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["session_id"] != "d1" and call["model"] == "gpt-5.5" and call["provider"] == "openai-codex"
    # A later change to the chat reaches the linked task's follow-up too.
    wait_for(lambda: service.store.work(body["run_id"])["status"] == "completed")
    put(server, "/voice/history/d1/model", {"model": "anthropic/claude-haiku-5-5", "reasoning": "low"})
    before = len(hermes.calls)
    post(server, "/voice/history/d1/message", {"text": "And the screws"})
    wait_for(lambda: len(hermes.calls) > before)
    assert hermes.calls[-1]["model"] == "claude-haiku-5-5"
    assert hermes.calls[-1]["model_options"]["reasoning"]["effort"] == "low"


def test_speakeasy_default_from_settings(server, service, hermes, catalog):
    status, saved = http(server.base_url, "PATCH", "/voice/settings",
                         {"jobs": {"model": "openai-codex/gpt-5.5", "reasoning": "high"}}, server.token)
    assert status == 200, saved
    assert get(server, "/voice/models")[1]["default"] == {"model": "openai-codex/gpt-5.5", "reasoning": "high",
                                                          "source": "speakeasy"}
    post(server, "/voice/work", {"text": "Sort the seed packets"})
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["model"] == "gpt-5.5" and call["model_options"]["reasoning"]["effort"] == "high"


def test_catalog_never_blocks_a_task_start(server, service, hermes):
    # Cold catalog: the choice is taken as saved and the task starts without waiting on Hermes' list.
    service.settings.patch({"jobs": {"model": "openai-codex/gpt-5.5"}})
    hermes.model_options = None
    post(server, "/voice/work", {"text": "Water the ferns"})
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["model"] == "gpt-5.5"


# -- voice phrases -------------------------------------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("Use the smart model for this: rewrite the cover letter", "smart"),
    ("Rewrite the cover letter, use the smarter one", "smart"),
    ("Use the fast model for this and check the train times", "fast"),
    ("switch to the quick model and list my errands", "fast"),
    ("What is the fast model called?", None),
    ("Make the shelf smarter", None),
])
def test_voice_phrases(text, kind):
    assert MD.voice_preset(text)[0] == kind


def test_saying_use_the_smart_model_runs_that_task_on_it(server, service, hermes, catalog):
    http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token, {"Idempotency-Key": "req_models_1"})
    worker = service.workers[-1]
    worker.delegate("call_fast", "Use the fast model for this: what time does the bakery open?")
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["model"] == "claude-haiku-5-5" and call["model_options"]["reasoning"]["effort"] == "low"
    # Hermes sees the run a moment before Speakeasy records the id it got back.
    key = wait_for(lambda: service.store.key_for_run(call["run_id"]))
    assert wait_for(lambda: service.store.run_model(key))["preset"] == "fast"
    worker.delegate("call_smart", "Use the smart model for this, plan next week's meals")
    wait_for(lambda: len(hermes.calls) == 2)
    assert hermes.calls[-1]["model"] == "claude-opus-5-5"
    assert hermes.calls[-1]["model_options"]["reasoning"]["effort"] == "high"


def test_a_plain_spoken_request_sends_no_model(server, service, hermes, catalog):
    # Without the phrase: Hermes' own config, nothing sent. Routing rules are untouched.
    http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token, {"Idempotency-Key": "req_models_2"})
    service.workers[-1].delegate("call_plain", "Add oat milk to the list")
    call = wait_for(lambda: hermes.calls)[-1]
    assert not {"provider", "model", "model_options"} & set(call)


def test_fast_and_smart_follow_settings(server, service, hermes, catalog):
    service.settings.patch({"jobs": {"smart_model": "openai-codex/gpt-6.1-sol", "smart_reasoning": "max"}})
    smart = get(server, "/voice/models")[1]["smart"]
    assert smart == {"model": "openai-codex/gpt-6.1-sol", "reasoning": "max"}


def test_store_migrates_and_keeps_choices(home, service):
    from speakeasy.store import StateStore
    service.store.set_model_choice("session:abc", "anthropic/claude-sonnet-5-5", None)
    again = StateStore(service.dir / "state.sqlite3", hermes_home=home)
    assert again.model_choice("session:abc")["model"] == "anthropic/claude-sonnet-5-5"
    again.set_model_choice("session:abc", None, None)
    assert again.model_choice("session:abc") is None
    assert json.dumps(MD.run_fields({"model": "a/b", "reasoning": "none"})) == json.dumps(
        {"provider": "a", "model": "b", "model_options": {"reasoning": {"enabled": False}}})



# -- what really ran (the chip never names a model the work didn't use) ----------------------------

def test_default_chip_names_what_hermes_really_served(server, service, hermes, catalog):
    """config.yaml names one model, but Hermes serves Speakeasy's runs on another (a profile or
    fallback): after a run, the default is the served one, with the configured one beside it."""
    hermes.served_default = {"provider": "openai-codex", "model": "gpt-6.1-sol"}
    status, before = get(server, "/voice/models")
    assert before["default"]["model"] == "anthropic/claude-opus-5-5" and "served" not in before["default"]
    status, started = post(server, "/voice/work", {"text": "Sort the seed packets by month"})
    assert status == 202, started
    work = wait_for(lambda: (get(server, f"/voice/work/{started['run_id']}")[1]["work"].get("model") or {})
                    .get("served") and get(server, f"/voice/work/{started['run_id']}")[1]["work"])
    assert work["model"]["id"] == "openai-codex/gpt-6.1-sol" and work["model"]["served"] is True
    status, after = get(server, "/voice/models")
    assert after["default"]["model"] == "openai-codex/gpt-6.1-sol"
    assert after["default"]["served"] is True and after["default"]["configured"] == "anthropic/claude-opus-5-5"
    defaults = [m["id"] for p in after["providers"] for m in p["models"] if m.get("default")]
    assert defaults == ["openai-codex/gpt-6.1-sol"]
    assert after["hermes_default"]["model"] == "anthropic/claude-opus-5-5"


def test_a_speakeasy_default_is_never_overridden_by_what_served(server, service, hermes, catalog):
    service.settings.patch({"jobs": {"model": "anthropic/claude-opus-5-5"}})
    service.store.reserve_run("idem_served_x", "int_x", "t_x", 1)
    service.store.set_run_model("idem_served_x", {"model": None, "reasoning": None, "source": "hermes"})
    service.store.served_model("idem_served_x", {"provider": "openai-codex", "model": "gpt-6.1-sol"})
    status, body = get(server, "/voice/models")
    assert body["default"]["model"] == "anthropic/claude-opus-5-5" and body["default"]["source"] == "speakeasy"


def test_last_default_served_skips_chosen_and_continued_runs(service):
    store = service.store
    store.reserve_run("idem_a", "int_x", "t_a", 1)
    store.set_run_model("idem_a", {"model": None, "source": "hermes"})
    store.served_model("idem_a", {"provider": "studio-qwen", "model": "qwen3.8-coder"})
    time.sleep(0.01)
    store.reserve_run("idem_b", "int_x", "t_b", 1)  # chosen on purpose: not the default
    store.set_run_model("idem_b", {"model": "openai-codex/gpt-6.1-sol", "source": "chat"})
    store.served_model("idem_b", {"provider": "openai-codex", "model": "gpt-6.1-sol"})
    time.sleep(0.01)
    store.reserve_run("idem_c", "int_x", "t_c", 1)  # continued an outside chat: that chat's model
    store.set_continued("idem_c", "tg_chat_1", "your Telegram chat")
    store.set_run_model("idem_c", {"model": None, "source": "hermes"})
    store.served_model("idem_c", {"provider": "xai", "model": "grok-5"})
    assert store.last_default_served() == "studio-qwen/qwen3.8-coder"


def test_a_session_recorded_model_only_fills_in_never_replaces_a_runtime(service):
    store = service.store
    store.reserve_run("idem_t", "int_x", "t_t", 1)
    assert store.served_model("idem_t", {"provider": "", "model": "grok-5"}, bare=True)
    assert MD.describe(store.run_model("idem_t"), {})["id"] == "/grok-5"
    assert store.served_model("idem_t", {"provider": "xai", "model": "grok-5"})
    assert not store.served_model("idem_t", {"provider": "", "model": "other"}, bare=True)
    assert store.run_model("idem_t")["served"] == {"provider": "xai", "model": "grok-5"}


@pytest.mark.parametrize("written, shown", [("high", "high"), ("false", "none"), ("bogus", None)])
def test_hermes_default_reasoning_reads_config_without_pyyaml(service, monkeypatch, written, shown):
    """Hermes 0.23+ installs ship no PyYAML; the "Default" level still comes from Hermes' config."""
    import sys
    cfg = service.home / "config.yaml"
    old = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    cfg.write_text(old + f"agent:\n  reasoning_effort: {written}\n", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "yaml", None)  # `import yaml` now raises ImportError
    assert service._hermes_reasoning() == shown
