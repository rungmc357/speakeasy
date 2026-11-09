"""Home's model chip names exactly the model Home's jobs run on: one resolver (HomeChat.work_route)
for the chip, the picker's Default tag and the send path. Order: the chip's pick, else Speakeasy's
saved job default (settings jobs.model), else the Hermes default. Invented data only."""
from __future__ import annotations

import json

import pytest

from fakes import http, wait_for
from speakeasy import home_chat as HC
from test_home_chat import _entries, _ReadCatalog, coordinator, post


class _LevelsCatalog(_ReadCatalog):
    def entries(self, raw):
        return {k: {**v, "reasoning_levels": ["none", "low", "medium", "high"]} for k, v in super().entries(raw).items()}


@pytest.fixture
def catalog(service, monkeypatch):
    got = _LevelsCatalog(_entries("acme/big-model-3", "acme/big-model-2", "anthropic/claude-haiku-5-5",
                                  "anthropic/claude-opus-5-5"))
    monkeypatch.setattr(service, "catalog", got)
    monkeypatch.setattr(service.home_chat, "_config", lambda: {"model": {"default": "big-model-3", "provider": "acme"}})
    service.store.set_meta(HC.WORK_META, "")
    return got


def chip(server):
    status, body = http(server.base_url, "GET", "/voice/home/model", None, server.token)
    assert status == 200, body
    return body["work"]


def start(server, service, hermes, words, cid):
    coordinator(service, {"tool": "start_job", "text": "", "args": {"request": words, "reply": "Looking."}})
    body = post(server, {"text": words.lower(), "client_id": cid})[1]
    assert body["action"] == "start", body
    n = len(hermes.calls)
    wait_for(lambda: len(hermes.calls) >= max(n, 1))
    return body, hermes.calls[-1]


def ran_on(call):
    """The model a Hermes run was asked for ("provider/model"), or None = Hermes' default."""
    model, provider = call.get("model"), call.get("provider")
    if not model:
        return None
    return f"{provider}/{model}" if provider else model


def test_nothing_picked_and_no_saved_default_is_the_hermes_default(server, service, hermes, catalog):
    w = chip(server)
    assert w["source"] == "default" and w["model"]["id"] == "acme/big-model-3"
    assert w["default"]["id"] == w["model"]["id"]                                  # the hero's Default tag
    _, call = start(server, service, hermes, "Find a dentist open Saturday", "c_d1")
    assert ran_on(call) is None                       # nothing extra sent: Hermes runs its default (the chip's model)
    assert service.home_chat.work_route()["model"] == "acme/big-model-3"


def test_a_saved_job_default_is_what_the_chip_shows_and_what_runs(server, service, hermes, catalog):
    service.settings.patch({"jobs": {"model": "anthropic/claude-opus-5-5", "reasoning": "high"}})
    w = chip(server)
    assert w["source"] == "jobs" and w["model"]["id"] == "anthropic/claude-opus-5-5" and w["reasoning"] == "high"
    assert w["default"]["id"] == "anthropic/claude-opus-5-5"     # "Default" in the picker = what runs unpicked
    _, call = start(server, service, hermes, "Compare two standing desks", "c_j1")
    assert ran_on(call) == "anthropic/claude-opus-5-5" or "claude-opus-5-5" in json.dumps(call)


def test_the_chips_pick_beats_the_saved_default_for_both(server, service, hermes, catalog):
    service.settings.patch({"jobs": {"model": "anthropic/claude-opus-5-5"}})
    status, body = http(server.base_url, "POST", "/voice/home/model", {"work_model": "acme/big-model-2"}, server.token)
    assert status == 200 and body["work"]["source"] == "chosen" and body["work"]["model"]["id"] == "acme/big-model-2"
    assert body["work"]["default"]["id"] == "anthropic/claude-opus-5-5"
    _, call = start(server, service, hermes, "Find a rain jacket under $100", "c_c1")
    assert ran_on(call) == "acme/big-model-2" or "big-model-2" in json.dumps(call)
    # Back to Default: both follow the saved default again.
    body = http(server.base_url, "POST", "/voice/home/model", {"work_model": None}, server.token)[1]
    assert body["work"]["source"] == "jobs" and body["work"]["model"]["id"] == "anthropic/claude-opus-5-5"


def test_a_saved_default_hermes_no_longer_lists_shows_what_really_runs(server, service, hermes, catalog):
    """The job's start refuses a saved model Hermes doesn't list and runs Hermes' default: the chip too."""
    service.settings.patch({"jobs": {"model": "gone/old-model-1"}})
    w = chip(server)
    assert w["source"] == "default" and w["model"]["id"] == "acme/big-model-3" == w["default"]["id"]
    _, call = start(server, service, hermes, "Find a bike repair shop", "c_g1")
    assert ran_on(call) is None


@pytest.mark.parametrize("settings", [{}, {"jobs": {}}, {"jobs": {"model": ""}}, {"jobs": None},
                                      {"jobs": {"model": "", "reasoning": ""}}])
def test_a_missing_or_empty_saved_default_falls_through_without_crashing(server, service, hermes, catalog,
                                                                         monkeypatch, settings):
    real = service.settings.get()
    monkeypatch.setattr(service.settings, "get", lambda: {**{k: v for k, v in real.items() if k != "jobs"}, **settings})
    assert service._default_layer() == {"model": None, "reasoning": None}
    w = chip(server)
    assert w["source"] == "default" and w["model"]["id"] == "acme/big-model-3"
    _, call = start(server, service, hermes, "Find a quiet cafe near the river", "c_m" + str(len(settings)) +
                    str(abs(hash(json.dumps(settings))) % 1000))
    assert ran_on(call) is None
