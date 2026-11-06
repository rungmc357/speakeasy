"""Thread continuity (generic state.db sessions), delivery targets, cards, CLI setup."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from argparse import Namespace
from pathlib import Path

import pytest

from speakeasy import continuity, delivery, settings as S
from speakeasy.cards import ImageRejected, default_image_roots, read_local_image, vetted_image_url
from speakeasy.text import media_images, product_cards
from fakes import read_profile_env

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64


def make_state_db(path: Path, rows: list[tuple]) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_type TEXT, thread_id TEXT, "
               "origin_json TEXT, title TEXT, started_at REAL, ended_at REAL)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
               "content TEXT, tool_calls TEXT, timestamp REAL)")
    now = time.time()
    for sid, source, title, chat_name, ended in rows:
        origin = {"platform": source, "chat_id": "1001", "chat_name": chat_name, "chat_type": "group"}
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                   (sid, source, "group", None, json.dumps(origin), title, now - 3600, ended))
        db.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)",
                   (sid, "assistant", "ok", None, now - 60))
    db.commit()
    db.close()


# -- continuity ---------------------------------------------------------------------------------

def test_continuity_matches_recent_platform_sessions(tmp_path):
    db = tmp_path / "state.db"
    make_state_db(db, [("s_trip", "telegram", "Lisbon trip planning", "Travel", None),
                       ("s_garden", "discord", "Garden redesign", "Home / #garden", None),
                       ("s_cron", "cron", "Lisbon trip planning", "", None),
                       ("s_old", "telegram", "Lisbon trip hotels", "Travel", time.time() - 10)])
    convs = continuity.recent_conversations(db)
    assert {c.session_id for c in convs} == {"s_trip", "s_garden"}  # no cron, no ended sessions
    hit = continuity.match("In the Lisbon trip planning chat, also book a museum", convs)
    assert hit and hit.session_id == "s_trip" and hit.platform == "telegram"
    assert continuity.match("What's the weather like?", convs) is None


def test_continuity_is_read_only_and_tolerates_missing_db(tmp_path):
    assert continuity.recent_conversations(tmp_path / "missing.db") == []


# -- delivery ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("target,ok", [("none", True), ("telegram", True), ("discord:123456", True),
                                       ("telegram:-100123:45", True), ("slack:#general", True),
                                       ("rm -rf /", False), ("telegram:$(id)", False), ("", False)])
def test_delivery_target_validation(target, ok):
    assert S.valid_delivery_target(target) is ok


def test_notifier_uses_stdin_not_argv(tmp_path):
    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        return Namespace(returncode=0)
    n = delivery.HermesSendNotifier(tmp_path, runner=runner)
    assert n.send("telegram", "secret-ish body text") is True
    cmd, kw = calls[0]
    assert "secret-ish body text" not in " ".join(cmd) and kw["input"] == "secret-ish body text"
    assert cmd[cmd.index("send"):cmd.index("send") + 5] == ["send", "--to", "telegram", "--file", "-"]
    assert n.send("none", "x") is False and n.send("bad target!", "x") is False


def test_threads_supported_is_detected_not_assumed():
    assert isinstance(delivery.threads_supported(), bool)


# -- cards ------------------------------------------------------------------------------------------

def test_media_image_cards_only_under_roots(tmp_path):
    root = tmp_path / "home"
    (root / "cache").mkdir(parents=True)
    good = root / "cache" / "chart.png"
    good.write_bytes(PNG)
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG)
    roots = default_image_roots(root)
    text, cards, tags = media_images(f"Here you go.\nMEDIA:{good}\nMEDIA:{outside}\n", roots)
    assert "MEDIA:" not in text and len(cards) == 1 and cards[0]["path"] == str(good.resolve())
    assert len(tags) == 2  # every tag still goes to chat delivery; only vetted ones become app cards
    data, mime = read_local_image(str(good), roots)
    assert mime == "image/png" and data == PNG
    with pytest.raises(ImageRejected):
        read_local_image(str(outside), roots)


def test_product_cards_parse_and_vet_urls():
    raw = ('Options:\n```product-cards\n[{"name": "Desk lamp", "price": "$40", '
           '"url": "https://shop.example.com/lamp", "image_url": "https://img.example.com/l.png"}]\n```\n')
    text, cards = product_cards(raw)
    assert "product-cards" not in text and cards[0]["name"] == "Desk lamp"
    for bad in ("http://img.example.com/x.png", "https://127.0.0.1/x.png", "https://user:pw@example.com/x.png",
                "https://example.com:8443/x.png"):
        with pytest.raises(ImageRejected):
            vetted_image_url(bad)


# -- settings / CLI setup -----------------------------------------------------------------------------

def test_settings_file_is_private_and_rejects_unknown(tmp_path):
    store = S.Settings(tmp_path)
    store.patch({"assistant_name": "Nova"})
    path = tmp_path / "speakeasy" / "settings.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with pytest.raises(S.SettingsError):
        store.patch({"assistant_name": "{bad}"})
    with pytest.raises(S.SettingsError):
        store.patch({"openai_api_key": "x"})


def test_cli_setup_before_restart_prepares_profile(tmp_path, monkeypatch):
    """Voice server not running yet: profile is prepared, the user is told to restart Hermes, and no
    link is offered while the server is down (setup never restarts Hermes itself)."""
    from speakeasy import cli, setup_flow as F
    lines: list[str] = []
    env = F.Env(run=lambda *a, **k: type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),
                out=lines.append, interactive=False, health=lambda url: False, which=lambda n: None)
    monkeypatch.setattr(F, "find_codex", lambda configured="": None)
    monkeypatch.setattr(F, "gateway_running", lambda h: True)   # running, but without the voice platform yet
    rc = cli.cmd_setup(Namespace(server="", tailscale=False, api_key=False, send="", yes=False,
                                 no_restart=True, no_open=True), tmp_path, env=env)
    assert rc == 1
    values = read_profile_env(tmp_path)
    assert len(values["API_SERVER_KEY"]) >= 32 and values["API_SERVER_HOST"] == "127.0.0.1"
    assert stat.S_IMODE(os.stat(tmp_path / ".env").st_mode) == 0o600
    import yaml
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert config["gateway"]["platforms"]["voice"]["enabled"] is True and "speakeasy" in config["plugins"]["enabled"]
    assert (tmp_path / "speakeasy" / "settings.json").exists()
    text = "\n".join(lines)
    assert "needs a restart" in text and "hermes gateway restart" in text and "#server=" not in text
    assert values["API_SERVER_KEY"] not in text


def test_cli_config_get_set(tmp_path, monkeypatch, capsys):
    from speakeasy import cli
    monkeypatch.setattr(cli, "_home", lambda: tmp_path)
    assert cli.handle(Namespace(voice_command="config", config_command="set", key="assistant_name", value="Nova")) == 0
    assert cli.handle(Namespace(voice_command="config", config_command="get", key="assistant_name")) == 0
    assert "Nova" in capsys.readouterr().out
    assert cli.handle(Namespace(voice_command="config", config_command="set", key="voice.provider", value="x")) != 0


def test_brief_headings_are_matched_loosely():
    from speakeasy import brief as B
    body = "Some sentence about the person that is long enough to count as real content here. " * 3
    loose = (f"## 1. User\n{body}\n## **Capabilities**\n{body}\n## How you like answers\n{body}\n"
             f"## Current projects\n{body}\n")
    assert B.sections_found(loose) == {"User", "Capability map", "Answer preferences", "Current context"}
    assert B.validate(loose)
    no_caps = f"## User\n{body}\n## Answer preferences\n{body}\n## Current context\n{body}\n"
    with pytest.raises(B.BriefInvalid):
        B.validate(no_caps)


def test_brief_failure_says_why_and_retries_hourly(tmp_path):
    from speakeasy import brief as B
    now = [1_000_000.0]
    settings = {"brief": {"auto_refresh": True, "include_recent_voice": True}}
    err = ["Provider authentication failed: No Codex credentials stored."]
    m = B.BriefManager(tmp_path, lambda prompt, idem: ("failed", ""), lambda: settings,
                       clock=lambda: now[0], error_fn=lambda: err[0])
    m.rewrite(force=True, background=False)
    st = m.status()
    assert st["state"] == "failed" and "hermes model" in st["error"]
    assert not m.refresh_due()
    now[0] += B.RETRY_INTERVAL_S
    assert m.refresh_due()
    err[0] = "something odd happened with " + "sk-" + "x" * 24
    m.rewrite(force=True, background=False)
    assert "[hidden]" in m.status()["error"] and "sk-x" not in m.status()["error"]


def test_hermes_api_base_follows_the_api_server_host(tmp_path, monkeypatch):
    from speakeasy import settings as S2
    assert S2.is_this_machine("127.0.0.1") and not S2.is_this_machine("203.0.113.9")
    (tmp_path / ".env").write_text("API_SERVER_PORT=8650\nAPI_SERVER_HOST=0.0.0.0\n")
    assert S2.hermes_api_base(tmp_path) == "http://127.0.0.1:8650"
    # A host that is not this machine never gets the key: fall back to loopback.
    (tmp_path / ".env").write_text("API_SERVER_PORT=8650\nAPI_SERVER_HOST=203.0.113.9\n")
    assert S2.hermes_api_base(tmp_path) == "http://127.0.0.1:8650"
    # One of this machine's own addresses (bound to a specific interface) is used as is.
    monkeypatch.setattr(S2, "is_this_machine", lambda host: host == "100.100.1.2")
    (tmp_path / ".env").write_text("API_SERVER_PORT=8650\nAPI_SERVER_HOST=100.100.1.2\n")
    assert S2.hermes_api_base(tmp_path) == "http://100.100.1.2:8650"
    # The nested gateway.platforms block counts too.
    (tmp_path / ".env").write_text("")
    (tmp_path / "config.yaml").write_text("gateway:\n  platforms:\n    api_server:\n      extra:\n        port: 8777\n")
    assert S2.hermes_api_base(tmp_path) == "http://127.0.0.1:8777"


def test_find_codex_picks_the_newest_and_flags_old_ones(tmp_path):
    from speakeasy.settings import codex_too_old, find_codex
    old, new = tmp_path / "old", tmp_path / "new"
    versions = {old: (0, 144, 4), new: (0, 155, 0)}
    for path in versions:
        path.write_text("#!/bin/sh\n"); path.chmod(0o755)
    pick = find_codex("", version_fn=versions.get, candidates=[old, new])
    assert pick == new
    assert codex_too_old(old, version_fn=versions.get) and not codex_too_old(new, version_fn=versions.get)
    assert find_codex(str(old)) == old  # an explicit choice is respected


def test_codex_errors_become_plain_reasons_without_echoing_provider_text():
    from speakeasy.codex_transport import explain_codex_error
    assert "too old" in explain_codex_error("Invalid request: unknown variant `v3`, expected `v1` or `v2`")
    assert "voice" in explain_codex_error("realtime voice `marin` is not supported for v3")
    secret = "weird internal detail 12345"
    assert secret not in explain_codex_error(secret)


def test_saved_voice_falls_back_when_provider_does_not_accept_it():
    def pick(provider, voice):
        return S.default_voice({"voice": {"provider": provider, "voice": voice}})
    assert pick("codex", "spruce") == "spruce"
    assert pick("codex", "bubbie") == "bubbie"  # GPT-Live 1.1 voice
    assert pick("codex", "porto") == S.DEFAULT_VOICES["codex"]  # listed by Codex, refused by the server
    assert pick("codex", "marin") == S.DEFAULT_VOICES["codex"]  # API voice saved, then switched to Codex
    assert pick("openai", "spruce") == S.DEFAULT_VOICES["openai"]
    assert pick("openai", "") == S.DEFAULT_VOICES["openai"]
    for provider, default in S.DEFAULT_VOICES.items():
        assert default in S.PROVIDER_VOICES[provider]
    assert not set(S.PROVIDER_VOICES["codex"]) & set(S.PROVIDER_VOICES["openai"])


def test_reported_version_matches_plugin_manifest():
    import re
    from pathlib import Path
    import speakeasy
    manifest = (Path(speakeasy.__file__).with_name("plugin.yaml")).read_text()
    assert speakeasy.__version__ == re.search(r"^version:\s*(\S+)", manifest, re.M).group(1)


def test_calls_never_route_to_a_channel_another_profile_answers(tmp_path):
    from speakeasy.calls import Runtime
    (tmp_path / "config.yaml").write_text(
        "gateway:\n  profile_routes:\n    - {name: other, platform: discord, profile: other, chat_id: '222'}\n")
    settings = {"delivery": {"target": "discord:999", "new_thread": False, "channels": [
        {"target": "discord:111", "label": "#research", "topic": "Research", "new_thread": True},
        {"target": "discord:222", "label": "#research", "topic": "Research", "new_thread": True}]}}
    rt = Runtime(store=None, hermes=None, settings=lambda: settings, hermes_home=tmp_path,
                 image_roots=lambda: (), notices=None)
    assert [c["target"] for c in rt.routable()["delivery"]["channels"]] == ["discord:111"]
    assert len(settings["delivery"]["channels"]) == 2  # saved settings untouched
