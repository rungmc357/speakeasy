"""A Hermes with no AI model yet (a fresh install whose own setup wizard was skipped).

Every turn fails at once there, so setup must say so instead of promising a voice brief, the
Setup check / "You're connected" data must carry it, and the brief is written once a model exists.
"""
from __future__ import annotations

import sys
import time
import types

import pytest

from fakes import http
from speakeasy import agent_model as M
from speakeasy import brief as B
from speakeasy import setup_check

# Hermes' own wording when no provider is configured (hermes_cli.auth.resolve_provider).
NO_PROVIDER = ("Hermes is not connected to any AI provider yet. Run `hermes model` to pick one, type `/login` "
               "in chat, or add a key with `hermes auth add <provider>`.")


# -- Hermes' resolver ---------------------------------------------------------------------------------

class _AuthError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def _fake_auth(monkeypatch, resolve):
    module = types.ModuleType("hermes_cli.auth")
    module.AuthError = _AuthError  # type: ignore[attr-defined]
    module.resolve_provider = resolve  # type: ignore[attr-defined]
    package = types.ModuleType("hermes_cli")
    package.auth = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", module)


def test_resolve_asks_hermes_own_provider_ladder(monkeypatch):
    def none_configured(requested=None):
        raise _AuthError(NO_PROVIDER, code="no_provider_configured")
    _fake_auth(monkeypatch, none_configured)
    assert M.resolve() == M.MISSING
    _fake_auth(monkeypatch, lambda requested=None: "openrouter")
    assert M.resolve() == M.OK


def test_resolve_never_says_missing_when_it_cannot_tell(monkeypatch):
    def other(requested=None):
        raise _AuthError("Unknown provider 'x'.", code="invalid_provider")
    _fake_auth(monkeypatch, other)
    assert M.resolve() == M.UNKNOWN

    def broken(requested=None):
        raise OSError("config unreadable")
    _fake_auth(monkeypatch, broken)
    assert M.resolve() == M.UNKNOWN
    monkeypatch.setitem(sys.modules, "hermes_cli.auth", None)  # an older Hermes without it: ImportError
    assert M.resolve() == M.UNKNOWN


def test_model_check_caches_and_rechecks_a_miss_soon():
    answers, now = [M.MISSING, M.OK], [0.0]
    check = M.ModelCheck(lambda: answers.pop(0), clock=lambda: now[0])
    assert check.state() == M.MISSING
    assert check.state() == M.MISSING  # cached
    now[0] += M.ModelCheck.MISSING_TTL_S + 1
    assert check.state(wait=True) == M.OK
    check.saw(M.MISSING)  # a run just failed for this reason
    assert check.state() == M.MISSING


# -- status + Setup check --------------------------------------------------------------------------------

READY = {"version": "9.9.9", "provider": "codex", "restart_needed": False, "hermes_api_ok": True,
         "hermes_api_key_set": True, "codex_found": True, "codex_signed_in": True, "brief_state": "ready"}


def test_setup_check_shows_a_missing_model_as_a_blocking_row_with_the_command_to_copy():
    out = setup_check.check({**READY, "agent_model": "missing", "brief_state": "failed"})
    rows = {r["id"]: r for r in out["rows"]}
    row = rows["agent_model"]
    assert out["ready"] is False and row["blocking"] and not row["ok"]
    assert row["title"] == "Your Hermes has no AI model yet"
    assert "Ask whoever set up your Hermes" in row["detail"]
    assert row["fix"] == {"kind": "copy", "label": "Copy hermes model", "payload": "hermes model"}
    assert "AI model" in rows["brief"]["detail"]  # the brief waits for it, it isn't broken
    assert [r["id"] for r in out["rows"]] == ["plugin", "restart", "hermes_api", "agent_model", "voice", "brief"]


def test_setup_check_model_row_is_green_when_ok_and_absent_when_unknown():
    ok = {r["id"]: r for r in setup_check.rows({**READY, "agent_model": "ok"})}
    assert ok["agent_model"]["ok"] and setup_check.check({**READY, "agent_model": "ok"})["ready"]
    assert "agent_model" not in {r["id"] for r in setup_check.rows({**READY, "agent_model": "unknown"})}
    assert "agent_model" not in {r["id"] for r in setup_check.rows(READY)}  # an older plugin's status


@pytest.fixture
def no_model_service(home):
    from speakeasy.service import VoiceService
    service = VoiceService(home, notifier=None, start_threads=False, agent_model=lambda: M.MISSING)
    yield service
    service.close()


def test_status_and_setup_check_route_carry_the_missing_model(no_model_service):
    from speakeasy.server import SpeakeasyServer
    srv = SpeakeasyServer(no_model_service, "127.0.0.1", 0)
    srv.start()
    try:
        code = no_model_service.devices.new_pairing_code()
        _, paired = http(srv.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Test Phone"})
        token = paired["token"]
        status, body = http(srv.base_url, "GET", "/voice/status", token=token)
        assert status == 200 and body["agent_model"] == "missing" and body["capabilities"]["agent_model"] is True
        _, check = http(srv.base_url, "GET", "/voice/setup-check", token=token)
        assert check["ready"] is False and "agent_model" in {r["id"] for r in check["rows"]}
    finally:
        srv.stop()


def test_a_test_or_cli_service_says_unknown_without_a_resolver(service):
    assert service.status()["agent_model"] == M.UNKNOWN


# -- the voice brief ----------------------------------------------------------------------------------

def test_brief_failing_for_no_model_says_so_and_writes_itself_once_a_model_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "MODEL_POLL_S", 0.02)
    settings = {"brief": {"auto_refresh": True, "include_recent_voice": True}}
    model = [M.MISSING]
    check = M.ModelCheck(lambda: model[0])
    check.MISSING_TTL_S = 0.0  # type: ignore[misc]
    runs: list[str] = []
    body = "Plenty of detail about the work and the names the voice should know. " * 4
    good = "".join(f"## {name}\n{body}\n" for name in B.REQUIRED_SECTIONS)

    def run(prompt, idem):
        runs.append(idem)
        return ("completed", good) if model[0] == M.OK else ("failed", "")

    m = B.BriefManager(tmp_path, run, lambda: settings, error_fn=lambda: NO_PROVIDER, model_check=check)
    m.rewrite(force=True, background=False)
    st = m.status()
    assert st["state"] == "failed" and st["waits_for_model"] and st["error"] == B.NO_MODEL_ERROR
    assert "not signed in" not in st["error"]  # not mistaken for a sign-in problem
    time.sleep(0.1)
    assert len(runs) == 1 and not m.text()  # no model yet: no extra runs
    model[0] = M.OK
    deadline = time.time() + 3
    while not m.text() and time.time() < deadline:
        time.sleep(0.02)
    m.stop()
    assert m.status()["state"] == "ready" and len(runs) == 2 and not m.status()["waits_for_model"]


def test_brief_other_failures_keep_the_hourly_retry_only(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "MODEL_POLL_S", 0.02)
    settings = {"brief": {"auto_refresh": True, "include_recent_voice": True}}
    runs: list[str] = []
    check = M.ModelCheck(lambda: M.OK)
    m = B.BriefManager(tmp_path, lambda p, i: runs.append(i) or ("failed", ""), lambda: settings,
                       error_fn=lambda: "The provider returned an error.", model_check=check)
    m.rewrite(force=True, background=False)
    time.sleep(0.1)
    m.stop()
    assert len(runs) == 1 and not m.status()["waits_for_model"]
