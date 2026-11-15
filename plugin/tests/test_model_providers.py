"""SPK-508: every model the app is offered says which provider serves it and whether that provider
bills per request, so a picker can group models by provider and never let the user pick the wrong
account by accident. The same model from two providers stays two entries. Invented catalog only."""
from __future__ import annotations

import pytest

from fakes import http
from speakeasy import home_chat as HC
from speakeasy import models as MD


def _payload() -> dict:
    return {
        "provider": "anthropic", "model": "claude-opus-5-5", "base_url": "",
        "providers": [
            {"slug": "anthropic", "name": "Anthropic", "is_current": True, "is_user_defined": False,
             "authenticated": True, "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
             "capabilities": {}},
            {"slug": "openai-codex", "name": "OpenAI Codex", "is_user_defined": False, "authenticated": True,
             "models": ["gpt-6.1-terra", "gpt-6.1-sol"], "capabilities": {}},
            {"slug": "openrouter", "name": "OpenRouter", "is_user_defined": False, "authenticated": True,
             "models": ["anthropic/claude-sonnet-5-5", "acme/big-model-3"], "capabilities": {}},
            {"slug": "nous", "name": "Nous Portal", "is_user_defined": False, "authenticated": True,
             "models": ["hermes-5-405b"], "capabilities": {}},
            {"slug": "venice", "name": "Venice", "is_user_defined": False, "authenticated": True,
             "models": ["venice-uncensored"], "capabilities": {}},
            {"slug": "studio-qwen", "name": "Studio Qwen", "is_user_defined": True, "authenticated": True,
             "models": ["qwen3.8-coder"], "capabilities": {}},
        ]}


def _catalog() -> MD.Catalog:
    return MD.Catalog(_payload, lambda: None)


FIELDS = ("provider", "provider_name", "provider_label", "metered", "provider_kind")


def test_provider_info_names_what_each_provider_is():
    assert MD.provider_info("openai-codex", "OpenAI Codex") == {
        "provider_label": "ChatGPT sign-in", "metered": False, "provider_kind": "subscription"}
    assert MD.provider_info("anthropic", "Anthropic")["provider_label"] == "Claude subscription"
    assert MD.provider_info("openrouter", "OpenRouter") == {
        "provider_label": "OpenRouter — pay per use", "metered": True, "provider_kind": "metered"}
    assert MD.provider_info("nous", "Nous Portal")["provider_label"] == "Nous Portal — pay per use"
    assert MD.provider_info("venice", "Venice") == {"provider_label": "Venice", "metered": False,
                                                    "provider_kind": "other"}
    assert MD.provider_info("studio-qwen", "Studio Qwen", local=True)["provider_kind"] == "local"


def test_every_catalog_entry_carries_provider_id_name_and_metered_flag():
    entries = _catalog().entries()
    for mid, e in entries.items():
        assert all(k in e for k in FIELDS), (mid, e)
        assert isinstance(e["metered"], bool)
        assert e["provider"] == mid.split("/", 1)[0]
    assert entries["openrouter/anthropic/claude-sonnet-5-5"]["metered"] is True
    assert entries["openrouter/anthropic/claude-sonnet-5-5"]["provider_label"] == "OpenRouter — pay per use"
    assert entries["anthropic/claude-sonnet-5-5"]["metered"] is False
    assert entries["nous/hermes-5-405b"]["metered"] is True
    assert entries["studio-qwen/qwen3.8-coder"]["provider_kind"] == "local"


def test_voice_models_rows_and_models_say_their_provider_and_same_model_stays_per_provider():
    body = MD.public(_catalog(), {})
    by_id = {p["id"]: p for p in body["providers"]}
    assert by_id["openrouter"]["metered"] is True and by_id["openrouter"]["label"] == "OpenRouter — pay per use"
    assert by_id["openai-codex"]["label"] == "ChatGPT sign-in" and by_id["openai-codex"]["metered"] is False
    assert by_id["anthropic"]["kind"] == "subscription" and by_id["studio-qwen"]["kind"] == "local"
    for p in body["providers"]:
        for m in p["models"]:
            assert all(k in m for k in FIELDS), m
            assert m["provider"] == p["id"] and m["metered"] == p["metered"]
    # Claude Sonnet 5.5 is offered by Anthropic and by OpenRouter: one entry under each, never merged.
    sonnets = [(p["id"], m["id"]) for p in body["providers"] for m in p["models"] if m["name"] == "Claude Sonnet 5.5"]
    assert sonnets == [("anthropic", "anthropic/claude-sonnet-5-5"),
                       ("openrouter", "openrouter/anthropic/claude-sonnet-5-5")]


def test_describe_badge_carries_the_provider_label_and_metered_flag():
    entries = _catalog().entries()
    badge = MD.describe({"model": "openrouter/anthropic/claude-sonnet-5-5"}, entries)
    assert badge["provider_label"] == "OpenRouter — pay per use" and badge["metered"] is True
    unknown = MD.describe({"model": "nous/hermes-9"}, {})   # not in the list any more: still labelled
    assert unknown["metered"] is True and unknown["provider_label"].endswith("pay per use")


@pytest.fixture
def real_catalog(service, monkeypatch):
    cat = _catalog()
    cat.raw()
    monkeypatch.setattr(service, "catalog", cat)
    monkeypatch.setattr(service.home_chat, "_config",
                        lambda: {"model": {"default": "claude-opus-5-5", "provider": "anthropic"}})
    service.store.set_meta(HC.WORK_META, "")
    service.store.set_meta(HC.CHOICE_META, "")
    return cat


def test_home_model_rows_carry_provider_and_metered(server, service, real_catalog):
    status, body = http(server.base_url, "GET", "/voice/home/model", None, server.token)
    assert status == 200, body
    rows = [body["coordinator"]["auto"], *body["coordinator"]["options"], body["work"]["model"],
            *body["work"]["options"], *body["work"]["more"]]
    for r in rows:
        assert all(k in r for k in ("provider", "provider_name", "provider_label", "metered")), r
    # Auto never picks a metered provider and says whose subscription it uses.
    assert body["coordinator"]["auto"]["provider_label"] == "Claude subscription"
    # The work picker's full list keeps Sonnet from both providers.
    every = [body["work"]["model"], *body["work"]["options"], *body["work"]["more"]]
    sonnet_providers = {r["provider"] for r in every if r["name"] == "Claude Sonnet 5.5"}
    assert sonnet_providers == {"anthropic", "openrouter"}
    metered = {r["id"]: r["metered"] for r in every}
    assert metered["openrouter/anthropic/claude-sonnet-5-5"] is True and metered["anthropic/claude-sonnet-5-5"] is False


def test_choosing_the_openrouter_copy_reads_back_as_openrouter(server, service, real_catalog):
    status, body = http(server.base_url, "POST", "/voice/home/model",
                        {"work_model": "openrouter/anthropic/claude-sonnet-5-5"}, server.token)
    assert status == 200, body
    w = body["work"]["model"]
    assert w["id"] == "openrouter/anthropic/claude-sonnet-5-5"
    assert w["provider_label"] == "OpenRouter — pay per use" and w["metered"] is True
