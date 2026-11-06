"""Setup check (GET /voice/setup-check): the rows the apps show when a call can't start yet.

Every not-ready row carries one machine-readable fix, no row tells anyone to open a terminal, and
the plugin can do a row's fix itself when it says ``action``."""
from __future__ import annotations

import pytest

from fakes import http
from speakeasy import setup_check

READY = {"version": "9.9.9", "provider": "codex", "restart_needed": False, "hermes_api_ok": True,
         "hermes_api_key_set": True, "codex_found": True, "codex_signed_in": True, "brief_state": "ready"}
KINDS = {"codex_sign_in", "action", "copy", "open_settings"}


def by_id(status):
    return {r["id"]: r for r in setup_check.rows(status)}


def test_all_ready():
    out = setup_check.check(READY)
    assert out["ready"] is True and out["version"] == "9.9.9"
    assert all(r["ok"] and r["fix"] is None for r in out["rows"])
    assert [r["id"] for r in out["rows"]] == ["plugin", "restart", "hermes_api", "voice", "brief"]


def test_every_failing_row_has_one_known_fix_and_no_terminal_talk():
    shapes = [
        {**READY, "restart_needed": True},
        {**READY, "hermes_api_ok": False},
        {**READY, "hermes_api_ok": False, "hermes_api_key_set": False},
        {**READY, "codex_signed_in": False},
        {**READY, "codex_signed_in": False, "codex_found": False},
        {**READY, "provider": "openai", "api_key_set": False},
        {**READY, "provider": "grok", "grok_beta": False},
        {**READY, "provider": "grok", "grok_beta": True, "grok_ready": False},
    ]
    for status in shapes:
        out = setup_check.check(status)
        assert out["ready"] is False, status
        for row in out["rows"]:
            if row["ok"] or not row["blocking"]:
                continue
            assert row["fix"]["kind"] in KINDS and row["fix"]["label"], row
            assert row["detail"], row
            text = (row["title"] + " " + row["detail"] + " " + row["fix"]["label"]).lower()
            assert "terminal" not in text and "run " not in text, row


def test_codex_rows_open_the_sign_in_sheet():
    assert by_id({**READY, "codex_signed_in": False})["voice"]["fix"]["kind"] == "codex_sign_in"
    missing = by_id({**READY, "codex_signed_in": False, "codex_found": False})["voice"]
    assert missing["fix"] == {"kind": "codex_sign_in", "label": "Set up ChatGPT voice", "payload": ""}


def test_restart_rows_copy_the_restart_command():
    assert by_id({**READY, "restart_needed": True})["restart"]["fix"]["payload"] == "/restart"
    assert by_id({**READY, "hermes_api_ok": False})["hermes_api"]["fix"]["payload"] == "/restart"
    unset = by_id({**READY, "hermes_api_ok": False, "hermes_api_key_set": False})["hermes_api"]
    assert unset["fix"]["kind"] == "copy" and "hermes voice setup" in unset["fix"]["payload"]


def test_requests_for_hermes_never_ask_for_a_secret_in_chat():
    key = by_id({**READY, "provider": "openai", "api_key_set": False})["voice"]["fix"]
    assert key["kind"] == "copy" and "never in this chat" in key["payload"]


def test_brief_never_blocks():
    out = setup_check.check({**READY, "brief_state": "writing"})
    brief = by_id({**READY, "brief_state": "writing"})["brief"]
    assert out["ready"] is True and brief["ok"] is False and brief["blocking"] is False and brief["fix"] is None


def test_service_answers_and_turns_grok_on(service, monkeypatch):
    # Grok chosen with the beta off only happens in a hand-edited settings file (saves refuse it).
    real = service.status
    monkeypatch.setattr(service, "status", lambda: {**real(), "provider": "grok", "grok_ready": True,
                                                    "grok_beta": service.settings.get()["beta"]["grok_voice"]})
    rows = {r["id"]: r for r in service.setup_check()["rows"]}
    assert rows["voice"]["fix"]["kind"] == "action"
    out = service.setup_check_fix("voice")
    assert service.settings.get()["beta"]["grok_voice"] is True
    assert {r["id"]: r for r in out["rows"]}["voice"]["ok"] is True
    from speakeasy.service import ServiceError
    with pytest.raises(ServiceError) as err:
        service.setup_check_fix("voice")
    assert err.value.status == 409


def test_route_needs_a_paired_device_and_lists_the_capability(server):
    status, _ = http(server.base_url, "GET", "/voice/setup-check")
    assert status == 401
    status, body = http(server.base_url, "GET", "/voice/setup-check", token=server.token)
    assert status == 200 and {"ready", "rows", "version"} <= set(body)
    assert all({"id", "ok", "blocking", "title", "detail", "fix"} <= set(r) for r in body["rows"])
    status, info = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert info["capabilities"]["setup_check"] is True
    status, _ = http(server.base_url, "POST", "/voice/setup-check/restart", {}, token=server.token)
    assert status == 409
