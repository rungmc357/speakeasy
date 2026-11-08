"""Existing conversations shaped like a real Hermes state.db keep working.

Real Hermes records a session's model in ``sessions.model`` and the provider that served it in
``sessions.billing_provider``; its ``model_config`` carries runtime bookkeeping (usage anchors,
delegation markers), never a ``provider``. Model names also repeat across providers (the same model
through its maker and through a router). Build 70's resolver read the provider only from
``model_config``, so every message to every existing conversation answered 409 "Choose a model".

Everything here is invented and runs against the local fake Hermes; nothing reaches a provider.
"""
import json
import sqlite3

import pytest

from fakes import wait_for
from test_history import add_session, get, make_db, post


def real_schema(path, sid, model, billing_provider, *, config=None):
    """Hermes' own columns: model + billing_provider + bookkeeping-only model_config."""
    with sqlite3.connect(path) as db:
        cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
        for name in ("model", "model_config", "billing_provider"):
            if name not in cols:
                db.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT")
        db.execute("UPDATE sessions SET model=?, model_config=?, billing_provider=? WHERE id=?",
                   (model, json.dumps(config if config is not None else {"_usage_anchor": {"turn": 3}}),
                    billing_provider, sid))


def duplicate_models(hermes):
    """The same model names offered by a second provider, as a router or local server would."""
    hermes.model_options["providers"].append(
        {"slug": "openrouter", "name": "OpenRouter", "is_current": False, "is_user_defined": False,
         "authenticated": True, "models": ["gpt-5.5", "claude-opus-5-5"], "capabilities": {}})
    hermes.model_options["providers"].append(
        {"slug": "custom:studio-(local)", "name": "Studio (local)", "is_current": False, "is_user_defined": True,
         "authenticated": True, "models": ["qwen3.8-coder"], "capabilities": {}})


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("source", ["discord", "api_server"])
def test_billing_provider_names_the_route_of_an_existing_conversation(server, service, hermes, warm, source):
    make_db(service.rt.state_db)
    add_session(service.rt.state_db, "garden", source, title="Garden layout")
    real_schema(service.rt.state_db, "garden", "gpt-5.5", "openai-codex")
    duplicate_models(hermes)
    if warm:
        service.catalog.raw()
    status, page = get(server, "/voice/history/garden")
    assert status == 200, page
    assert page["session"]["next_model"]["id"] == "openai-codex/gpt-5.5"
    assert not page["session"]["next_model"].get("unavailable")
    status, body = post(server, "/voice/history/garden/message", {"text": "Add a shaded bed"})
    assert status == 200, body
    call = wait_for(lambda: hermes.calls)[-1]
    assert (call["provider"], call["model"]) == ("openai-codex", "gpt-5.5")


def test_custom_billing_provider_matches_its_one_custom_endpoint(server, service, hermes):
    make_db(service.rt.state_db)
    add_session(service.rt.state_db, "garden", "discord", title="Garden layout")
    real_schema(service.rt.state_db, "garden", "qwen3.8-coder", "custom")
    duplicate_models(hermes)
    service.catalog.raw()
    status, body = post(server, "/voice/history/garden/message", {"text": "Add a shaded bed"})
    assert status == 200, body
    call = wait_for(lambda: hermes.calls)[-1]
    assert (call["provider"], call["model"]) == ("custom:studio-(local)", "qwen3.8-coder")


def test_ambiguous_bare_model_follows_hermes_default_provider(server, service, hermes):
    # No billing provider recorded; the name exists twice; one of them is Hermes' configured default.
    make_db(service.rt.state_db)
    add_session(service.rt.state_db, "garden", "discord", title="Garden layout")
    real_schema(service.rt.state_db, "garden", "claude-opus-5-5", None)
    duplicate_models(hermes)
    service.catalog.raw()
    status, body = post(server, "/voice/history/garden/message", {"text": "Add a shaded bed"})
    assert status == 200, body
    call = wait_for(lambda: hermes.calls)[-1]
    assert (call["provider"], call["model"]) == ("anthropic", "claude-opus-5-5")


def test_genuinely_ambiguous_route_still_fails_closed(server, service, hermes):
    # Two providers, neither recorded nor the default: still asks for a model, never guesses.
    make_db(service.rt.state_db)
    add_session(service.rt.state_db, "garden", "discord", title="Garden layout")
    real_schema(service.rt.state_db, "garden", "gpt-5.5", None)
    duplicate_models(hermes)
    service.catalog.raw()
    status, body = post(server, "/voice/history/garden/message", {"text": "Add a shaded bed"})
    assert status == 409 and body["reason"] == "model", body
    assert hermes.calls == []
