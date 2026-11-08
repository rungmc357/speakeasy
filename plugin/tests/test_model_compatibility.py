"""Continuation compatibility uses known routes; unknown history still fails closed.

All catalogs, conversations and requests are invented and use the local fake Hermes HTTP server.
These are request-field checks, never calls to any model provider.
"""
import json
import sqlite3

import pytest

from fakes import wait_for
from test_history import add_session, get, make_db, post


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("source", ["api_server", "discord"])
def test_known_session_route_survives_different_new_work_default(server, service, hermes, warm, source):
    make_db(service.rt.state_db, routed=True)
    add_session(service.rt.state_db, "garden", source, title="Garden layout")
    service.settings.patch({"jobs": {"model": "anthropic/claude-opus-5-5", "reasoning": "medium"}})
    if warm:
        service.catalog.raw()
    status, page = get(server, "/voice/history/garden")
    assert status == 200, page
    predicted = page["session"]["next_model"]
    assert predicted["id"] == "openai-codex/gpt-5.5"
    assert predicted["reasoning"] is None  # new-work effort does not leak into old history
    status, body = post(server, "/voice/history/garden/message", {"text": "Add a shaded bed"})
    assert status == 200, body
    call = wait_for(lambda: hermes.calls)[-1]
    assert (call["provider"], call["model"]) == ("openai-codex", "gpt-5.5")
    assert "model_options" not in call
    assert (call["session_id"] == "garden") is (source == "api_server")


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("route", ["legacy", "missing"])
@pytest.mark.parametrize("source", ["api_server", "discord", "telegram"])
def test_history_with_no_recorded_model_sends_on_the_default_like_build_69(server, service, hermes, route,
                                                                           source, warm):
    """SPK-228 part 2: older chats (no model columns) and chats Hermes never recorded a model for
    run on the configured default with its effort, as build 69 did, instead of a 409."""
    make_db(service.rt.state_db, routed=route != "legacy")
    add_session(service.rt.state_db, "garden", source, title="Garden layout")
    if route == "missing":  # newer schema, but Hermes recorded no model for this chat
        with sqlite3.connect(service.rt.state_db) as db:
            db.execute("UPDATE sessions SET model=NULL, model_config=NULL")
    service.settings.patch({"jobs": {"model": "anthropic/claude-opus-5-5", "reasoning": "medium"}})
    if warm:
        service.catalog.raw()
    status, page = get(server, "/voice/history/garden")
    assert status == 200, page
    predicted = page["session"]["next_model"]
    assert (predicted["id"], predicted["reasoning"]) == ("anthropic/claude-opus-5-5", "medium")
    assert not predicted.get("unavailable")
    status, body = post(server, "/voice/history/garden/message", {"text": "Add a shaded bed"})
    assert status == 200, body
    call = wait_for(lambda: hermes.calls)[-1]
    assert (call["provider"], call["model"]) == ("anthropic", "claude-opus-5-5")
    assert call["model_options"] == {"reasoning": {"enabled": True, "effort": "medium"}}
    assert (call["session_id"] == "garden") is (source == "api_server")


def test_history_with_no_recorded_model_and_no_default_lets_hermes_pick(server, service, hermes):
    make_db(service.rt.state_db)
    add_session(service.rt.state_db, "garden", "discord", title="Garden layout")
    service.catalog.raw()
    status, body = post(server, "/voice/history/garden/message", {"text": "Add a shaded bed"})
    assert status == 200, body
    call = wait_for(lambda: hermes.calls)[-1]
    assert "provider" not in call and "model" not in call and "model_options" not in call


@pytest.mark.parametrize("route", ["unknown", "signed-out", "wrong-provider", "unsupported-effort"])
def test_unresolved_history_never_dispatches_or_borrows_new_work_default(server, service, hermes, route):
    make_db(service.rt.state_db, routed=True)
    add_session(service.rt.state_db, "garden", "api_server", title="Garden layout")
    service.settings.patch({"jobs": {"model": "anthropic/claude-opus-5-5", "reasoning": "medium"}})
    service.catalog.raw()
    model, provider = {
        "unknown": ("absent", "openai-codex"),
        "signed-out": ("hermes-5-405b", "nous"),
        "wrong-provider": ("gpt-5.5", "studio-qwen"),
        "unsupported-effort": ("qwen3.8-coder", "studio-qwen"),
    }[route]
    with sqlite3.connect(service.rt.state_db) as db:
        db.execute("UPDATE sessions SET model=?, model_config=?", (model, json.dumps({"provider": provider})))
    if route == "unsupported-effort":
        service.store.set_model_choice("session:garden", "studio-qwen/qwen3.8-coder", "high")
    status, body = post(server, "/voice/history/garden/message", {"text": "Add a shaded bed"})
    assert status == 409, body
    assert body["reason"] == "model"
    assert hermes.calls == []


def test_new_work_still_uses_selected_model_not_fixture_route(server, service, hermes):
    make_db(service.rt.state_db, routed=True)
    add_session(service.rt.state_db, "garden", "discord", title="Garden layout")
    service.catalog.raw()
    status, body = post(server, "/voice/work", {"text": "Plan a different garden",
                                              "model": "studio-qwen/qwen3.8-coder"})
    assert status == 202, body
    call = wait_for(lambda: hermes.calls)[-1]
    assert (call["provider"], call["model"]) == ("studio-qwen", "qwen3.8-coder")
    assert "model_options" not in call
    assert call.get("session_id") != "garden"
