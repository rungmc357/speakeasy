"""Prediction and actual request fields must agree; all state is invented."""
import json
import sqlite3

import pytest
from speakeasy import history as H
from speakeasy.service import ServiceError
from test_history import make_db, add_session

GPT = "openai-codex/gpt-6.1-sol"
OPUS = "anthropic/claude-opus-5-5"


@pytest.fixture
def linked(service):
    make_db(service.rt.state_db)
    for sid in ("parent", "child", "other"):
        add_session(service.rt.state_db, sid, "discord" if sid == "parent" else "api_server", title="Garden layout")
    service.catalog.raw()
    service.store.reserve_run("r1", "i1", "t1", 0)
    service.store.set_session("r1", "child")
    service.store.link_history("r1", "parent")
    service.settings.patch({"jobs": {"model": OPUS, "reasoning": "medium"}})
    service._save_chat_choice("other", {"model": None, "reasoning": None})
    return service


def page(s, sid):
    return s._session_models(sid, H.session_meta(s.rt.state_db, sid), [])


def sent(s, sid="child"):
    return s.choose_model("t1", "r1", sid, "")


def test_linked_page_and_dispatch_use_parent_choice(linked):
    s = linked
    s.store.set_model_choice("session:parent", GPT, "high")
    assert page(s, "child")["next_model"]["id"] == GPT
    assert page(s, "child")["next_model"]["reasoning"] == "high"
    assert sent(s) == {"provider": "openai-codex", "model": "gpt-6.1-sol", "model_options": {"reasoning": {"enabled": True, "effort": "high"}}}


def test_latest_explicit_chat_choice_supersedes_older_task_choice(linked):
    s = linked
    s.store.set_model_choice("task:t1", OPUS, "medium")
    s.store.set_model_choice("session:parent", GPT, "high")
    assert sent(s)["model"] == "gpt-6.1-sol"
    assert page(s, "child")["next_model"]["id"] == GPT
    # A genuinely newer temporary task choice remains scoped to that task.
    s.store.set_model_choice("task:t1", OPUS, "none")
    assert sent(s)["model_options"] == {"reasoning": {"enabled": False}}
    assert page(s, "child")["next_model"]["id"] == OPUS
    assert page(s, "other")["next_model"]["id"] == OPUS  # independent default
    assert page(s, "parent")["next_model"]["id"] == OPUS


def test_selection_on_linked_page_updates_canonical_chat_only(linked):
    s = linked
    s.set_session_model("child", {"model": GPT, "reasoning": "high"})
    assert page(s, "parent")["next_model"]["id"] == GPT
    assert page(s, "child")["model_choice"] == page(s, "parent")["model_choice"]
    assert page(s, "other")["model_choice"] is None
    assert s.store.model_choice("session:child") is None


@pytest.mark.parametrize("model,provider", [("gpt-6.1-sol", None), (GPT, None), ("gpt-6.1-sol", "openai-codex")])
def test_existing_hermes_choice_beats_default_without_inventing_effort(linked, model, provider):
    s = linked
    with sqlite3.connect(s.rt.state_db) as db:
        db.execute("ALTER TABLE sessions ADD COLUMN model TEXT")
        db.execute("ALTER TABLE sessions ADD COLUMN model_config TEXT")
        db.execute("UPDATE sessions SET model=?, model_config=? WHERE id='parent'", (model, json.dumps({"provider": provider})))
    assert page(s, "child")["next_model"]["id"] == GPT
    assert page(s, "child")["next_model"]["reasoning"] is None
    assert sent(s) == {"provider": "openai-codex", "model": "gpt-6.1-sol"}


def test_fallback_never_overwrites_requested_preference(linked):
    s = linked
    s.store.set_model_choice("session:parent", GPT, "high")
    s.store.set_run_model("r1", {"model": GPT, "reasoning": "high", "source": "chat", "served": {"provider": "anthropic", "model": "claude-opus-5-5"}})
    assert page(s, "child")["next_model"]["id"] == GPT
    assert sent(s)["model"] == "gpt-6.1-sol"
    assert s.store.model_choice("session:parent")["model"] == GPT


@pytest.mark.parametrize("model", ["unrecognized", "absent/model"])
def test_unavailable_or_unknown_is_not_silently_replaced(linked, model):
    s = linked
    s.store.set_model_choice("session:parent", model, None)
    assert page(s, "child")["next_model"]["id"] == model
    assert page(s, "child")["next_model"]["unavailable"] is True
    with pytest.raises(ServiceError, match="unavailable or unknown"):
        sent(s)


def test_chat_with_no_recorded_model_runs_on_the_default_like_build_69(linked):
    """SPK-228: no recorded model is not an unknown route; it sends on the default with its effort."""
    s = linked
    predicted = page(s, "parent")["next_model"]
    assert (predicted["id"], predicted["reasoning"]) == (OPUS, "medium")
    assert not predicted.get("unavailable")
    assert sent(s, "parent") == {"provider": "anthropic", "model": "claude-opus-5-5",
                                 "model_options": {"reasoning": {"enabled": True, "effort": "medium"}}}
    s.set_session_model("parent", {"model": "default", "reasoning": "default"})
    assert page(s, "parent")["next_model"]["id"] == OPUS
    assert page(s, "parent")["model_choice"] is None


def test_legacy_child_pick_survives_then_parent_explicit_selection_wins(linked):
    s = linked
    s.store.set_model_choice("session:child", GPT, "high")
    assert page(s, "parent")["next_model"]["id"] == GPT
    assert sent(s)["model"] == "gpt-6.1-sol"
    s.set_session_model("parent", {"model": OPUS, "reasoning": "none"})
    assert page(s, "child")["next_model"]["id"] == OPUS
    assert sent(s)["model_options"] == {"reasoning": {"enabled": False}}


def test_compression_segment_keeps_original_preference(linked):
    s = linked
    s.store.set_model_choice("session:parent", GPT, "high")
    add_session(s.rt.state_db, "compressed", "discord", title="Garden layout")
    with sqlite3.connect(s.rt.state_db) as db:
        db.execute("UPDATE sessions SET parent_session_id='parent' WHERE id='compressed'")
    assert page(s, "compressed")["next_model"]["id"] == GPT
    s.set_session_model("compressed", {"model": OPUS, "reasoning": "none"})
    assert page(s, "parent")["next_model"]["id"] == OPUS
    assert sent(s)["model"] == "claude-opus-5-5"


def test_distinct_conversations_and_default_change(linked):
    s = linked
    s.store.set_model_choice("session:parent", GPT, "high")
    s.store.set_model_choice("session:other", OPUS, "none")
    for sid in ("parent", "child", "other", "parent", "child"):
        assert page(s, sid)["next_model"]["id"] == (OPUS if sid == "other" else GPT)
    s.settings.patch({"jobs": {"model": "anthropic/claude-haiku-5-5", "reasoning": "low"}})
    assert page(s, "child")["next_model"]["id"] == GPT
    assert page(s, "other")["next_model"]["reasoning"] == "none"
