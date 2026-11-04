"""Catalog rule 11: credentials come through Hermes, never from files.

Speakeasy reads secrets and provider sign-ins only through Hermes' APIs (``agent.secret_scope``,
``hermes_cli.config``, ``agent.credential_pool``) and never opens ``.env``, Hermes' ``auth.json``
or another tool's login file itself.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from fakes import make_home, read_profile_env
from speakeasy import settings as S

PLUGIN = Path(__file__).resolve().parents[1] / "speakeasy"
# Code that builds a path to a credential file: `/ ".env"`, `"auth.json"`, `.codex/...`.
FILE_ACCESS = re.compile(r"""/\s*["']\.env["']|["']auth\.json["']|["']\.codex["']""")


def test_no_plugin_module_builds_a_credential_file_path():
    offenders = []
    for path in sorted(PLUGIN.rglob("*.py")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if FILE_ACCESS.search(code):
                offenders.append(f"{path.relative_to(PLUGIN)}:{n}: {line.strip()}")
    assert offenders == []


def test_secrets_are_read_through_hermes(tmp_path, monkeypatch):
    home = make_home()
    import agent.secret_scope as scope
    seen: list[str] = []
    real = scope.get_secret
    monkeypatch.setattr(scope, "get_secret", lambda name, default=None: seen.append(name) or real(name, default))
    with open(home / ".env", "a") as f:
        f.write("HASS_TOKEN=from-the-profile\n")
    assert S.hermes_secret(home, "HASS_TOKEN") == "from-the-profile"
    monkeypatch.delenv("SPEAKEASY_TEST_ONLY_VAR", raising=False)
    assert S.hermes_secret(home, "SPEAKEASY_TEST_ONLY_VAR") == ""
    assert "SPEAKEASY_TEST_ONLY_VAR" in seen  # a profile miss falls through to Hermes' get_secret


def test_the_profile_scope_is_the_one_hermes_builds(monkeypatch):
    home = make_home()
    import agent.secret_scope as scope
    built: list[Path] = []
    real = scope.build_profile_secret_scope
    monkeypatch.setattr(scope, "build_profile_secret_scope", lambda h: built.append(Path(h)) or real(h))
    S.hermes_secret(home, "API_SERVER_KEY")
    assert built == [home]
    assert scope.current_secret_scope() is None  # nothing left bound afterwards


@pytest.mark.parametrize("name", ["SPEAKEASY_OPENAI_API_KEY", "OPENAI_API_KEY"])
def test_either_openai_key_name_works(name, tmp_path):
    home = tmp_path / "h"; home.mkdir()
    (home / ".env").write_text(f"{name}=sk-{name.lower()}\n")
    assert S.openai_api_key(home) == f"sk-{name.lower()}"


def test_the_documented_name_wins_over_the_fallback(tmp_path):
    home = tmp_path / "h"; home.mkdir()
    (home / ".env").write_text("OPENAI_API_KEY=sk-general\nSPEAKEASY_OPENAI_API_KEY=sk-voice\n")
    assert S.openai_api_key(home) == "sk-voice"


def test_existing_values_are_kept_and_writes_go_through_hermes(tmp_path, monkeypatch):
    """Migration: values users already set are honoured and never rewritten."""
    from speakeasy import cli
    import hermes_cli.config as hc
    home = tmp_path / "h"; home.mkdir()
    (home / ".env").write_text("# mine\nexport API_SERVER_KEY=keep-me\nOTHER=1\n")
    writes: list[str] = []
    real = hc.save_env_value
    monkeypatch.setattr(hc, "save_env_value", lambda k, v: writes.append(k) or real(k, v))
    added = cli.add_env_if_missing(home, {"API_SERVER_KEY": "new", "API_SERVER_HOST": "127.0.0.1"})
    assert added == ["API_SERVER_HOST"] and writes == ["API_SERVER_HOST"]
    values = read_profile_env(home)
    assert values["API_SERVER_KEY"] == "keep-me" and values["OTHER"] == "1" and values["API_SERVER_HOST"] == "127.0.0.1"
    assert "# mine" in (home / ".env").read_text()


def test_xai_sign_in_comes_from_hermes_credential_pool(home, monkeypatch):
    from speakeasy import grok_live
    import agent.credential_pool as pool
    asked: list[str] = []
    real = pool.load_pool
    monkeypatch.setattr(pool, "load_pool", lambda p: asked.append(p) or real(p))
    grok_live.signin_state(home)
    assert asked == ["xai-oauth"]
