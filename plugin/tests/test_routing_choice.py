"""Switching the routing model: choices come from Hermes' own provider list (nothing hardcoded),
the config block written, and the HTTP surface."""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from fakes import yaml

from speakeasy import routing_choice, router
from fakes import http

# What Hermes' picker would report: signed-in providers and their models. The plugin must pass
# these through, not invent its own.
FAKE_PROVIDERS = [
    {"slug": "acme", "name": "Acme AI", "models": ["acme-large", "acme-mini"]},
    {"slug": "zeta", "name": "Zeta Cloud", "models": ["zeta-fast-1"]},
]
FAKE_CATALOG = {"acme": ["acme-large", "acme-mini", "acme-mini-2", "acme-nano"], "zeta": ["zeta-fast-1"]}


@pytest.fixture(autouse=True)
def fake_hermes_catalog(monkeypatch):
    """Stand in for Hermes' model picker so tests never touch real accounts."""
    seen: dict = {}

    def list_authenticated_providers(**kwargs):
        seen.update(kwargs)
        return [dict(p, models=list(p["models"])) for p in FAKE_PROVIDERS]

    picker = types.ModuleType("hermes_cli.model_switch_providers")
    picker.list_authenticated_providers = list_authenticated_providers  # type: ignore[attr-defined]
    models = types.ModuleType("hermes_cli.models")
    models.provider_model_ids = lambda provider, force_refresh=False: list(FAKE_CATALOG.get(provider, []))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli.model_switch_providers", picker)
    monkeypatch.setitem(sys.modules, "hermes_cli.models", models)
    return seen


def _write(home: Path, config: dict) -> None:
    (home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")


def _read(home: Path) -> dict:
    return yaml.safe_load((home / "config.yaml").read_text())


def _block(home: Path) -> dict:
    return _read(home)["auxiliary"][router.AUX_TASK]


BASE = {"model": {"default": "big-model", "provider": "acme"},
        "auxiliary": {"compression": {"provider": "zeta", "model": "zeta-fast-1"}},
        "platforms": {"discord": {"enabled": True}}}


def test_the_choices_are_whatever_hermes_offers(tmp_path, fake_hermes_catalog):
    _write(tmp_path, BASE)
    state = routing_choice.choices(tmp_path)
    assert [p["id"] for p in state["providers"]] == ["acme", "zeta"]
    assert state["providers"][0]["models"] == ["acme-large", "acme-mini"]
    assert state["current"]["is_default"] and state["current"]["thinking"] is True
    assert state["label"] == "Hermes default (your main model)"
    # the GUI read path: cached catalogs only, never a live probe that could stall Settings
    assert fake_hermes_catalog["non_blocking_catalogs"] is True and fake_hermes_catalog["for_picker"] is True
    assert routing_choice.provider_models("acme") == FAKE_CATALOG["acme"]


def test_nothing_in_the_plugin_names_a_provider_or_model():
    source = Path(routing_choice.__file__).read_text().lower()
    for word in ("venice", "anthropic", "deepseek", "claude", "openai", "sonnet", "https://"):
        assert word not in source, word


def test_switching_writes_one_block_and_keeps_everything_else(tmp_path):
    _write(tmp_path, BASE)
    state = routing_choice.choose(tmp_path, "acme", "acme-mini", thinking=False)
    assert state["current"] == {"provider": "acme", "model": "acme-mini", "thinking": False, "is_default": False}
    assert state["label"] == "acme · acme-mini (thinking off)"
    assert _block(tmp_path)["reasoning_effort"] is False
    after = _read(tmp_path)
    assert after["auxiliary"]["compression"] == BASE["auxiliary"]["compression"]
    assert after["platforms"] == BASE["platforms"] and after["model"] == BASE["model"]


def test_switching_provider_blanks_a_hand_set_endpoint(tmp_path):
    _write(tmp_path, dict(BASE, auxiliary={router.AUX_TASK: {
        "provider": "custom", "model": "m", "base_url": "http://old.example/v1", "key_env": "OLD_KEY",
        "api_mode": "chat_completions", "timeout": 10}}))
    routing_choice.choose(tmp_path, "zeta", "zeta-fast-1")
    block = _block(tmp_path)
    assert block["provider"] == "zeta" and block["model"] == "zeta-fast-1"
    assert block["base_url"] == "" and block["key_env"] == "" and block["api_mode"] == ""
    assert block["timeout"] == 10  # unrelated keys are left alone


def test_default_and_thinking_toggle(tmp_path):
    _write(tmp_path, BASE)
    routing_choice.choose(tmp_path, "acme", "acme-mini", thinking=False)
    routing_choice.choose(tmp_path, "acme", "acme-large")  # thinking not mentioned: kept as it was
    assert _block(tmp_path)["reasoning_effort"] is False
    routing_choice.choose(tmp_path, "acme", "acme-large", thinking=True)
    assert _block(tmp_path)["reasoning_effort"] == ""
    routing_choice.choose(tmp_path, "default")
    block = _block(tmp_path)
    assert block["provider"] == "auto" and block["model"] == "" and block["reasoning_effort"] == ""
    assert routing_choice.choices(tmp_path)["current"]["is_default"]


def test_a_provider_hermes_is_not_signed_in_to_is_refused(tmp_path):
    _write(tmp_path, BASE)
    before = (tmp_path / "config.yaml").read_bytes()
    with pytest.raises(routing_choice.RoutingChoiceError, match="isn't set up"):
        routing_choice.choose(tmp_path, "nobody", "x")
    with pytest.raises(routing_choice.RoutingChoiceError, match="model is required"):
        routing_choice.choose(tmp_path, "acme", "")
    assert (tmp_path / "config.yaml").read_bytes() == before


def test_the_model_in_use_is_always_listed(tmp_path):
    _write(tmp_path, dict(BASE, auxiliary={router.AUX_TASK: {"provider": "acme", "model": "acme-nano"}}))
    acme = routing_choice.choices(tmp_path)["providers"][0]
    assert acme["models"][0] == "acme-nano"  # not in the picker's short list, still offered
    _write(tmp_path, dict(BASE, auxiliary={router.AUX_TASK: {"provider": "custom", "model": "local-model"}}))
    first = routing_choice.choices(tmp_path)["providers"][0]
    assert first == {"id": "custom", "name": "custom", "models": ["local-model"]}


def test_the_label_names_the_choice(monkeypatch):
    monkeypatch.undo()  # conftest stubs routing_model for other tests
    assert router.routing_model({"auxiliary": {}}) == "Hermes default (your main model)"
    block = {"provider": "acme", "model": "acme-mini", "reasoning_effort": False}
    assert router.routing_model({"auxiliary": {router.AUX_TASK: block}}) == "acme · acme-mini (thinking off)"
    assert router.routing_model({"auxiliary": {router.AUX_TASK: {"provider": "x", "model": "y"}}}) == "x · y"


def test_the_app_can_list_and_switch_over_http(server, service, home):
    _write(home, BASE)
    base, token = server.base_url, server.token
    status, body = http(base, "GET", "/voice/routing", token=token)
    assert status == 200 and body["current"]["is_default"]
    assert [p["id"] for p in body["providers"]] == ["acme", "zeta"]
    status, body = http(base, "GET", "/voice/routing/models?provider=acme", token=token)
    assert status == 200 and body["models"] == FAKE_CATALOG["acme"]
    status, body = http(base, "POST", "/voice/routing",
                        {"provider": "zeta", "model": "zeta-fast-1", "thinking": False}, token=token)
    assert status == 200 and body["current"]["provider"] == "zeta" and body["current"]["thinking"] is False
    status, body = http(base, "POST", "/voice/routing", {"provider": "nobody", "model": "x"}, token=token)
    assert status == 400 and "isn't set up" in body["error"]
    status, _ = http(base, "POST", "/voice/routing", {"provider": "acme", "model": "m", "thinking": "no"}, token=token)
    assert status == 400
    assert http(base, "GET", "/voice/routing")[0] == 401  # paired devices only
    status, body = http(base, "GET", "/voice/status", token=token)
    assert body["routing_choice"]["current"]["provider"] == "zeta"
