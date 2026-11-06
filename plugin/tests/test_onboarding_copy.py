"""Onboarding copy the user (or their agent, relaying it) reads: the restart step points at Hermes'
own `/restart` chat command first, with the terminal form only as a fallback."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def test_restart_note_leads_with_the_chat_command():
    from speakeasy import setup_flow as F
    note = F.RESTART_NOTE
    assert "/restart" in note and note.index("/restart") < note.index("hermes gateway restart")
    assert "From a terminal instead" in note


def test_app_facing_restart_text_says_send_restart():
    from speakeasy.service import RESTART_NEEDED
    assert "/restart" in RESTART_NEEDED
    assert RESTART_NEEDED.index("/restart") < RESTART_NEEDED.index("hermes gateway restart")


def test_docs_say_send_restart_wherever_they_say_gateway_restart():
    """Every user-aimed `hermes gateway restart` in README and setup.md comes with `/restart` first."""
    for rel in ("README.md", "docs/AGENT_SETUP.md"):
        flat = _flat((REPO / rel).read_text())
        for m in re.finditer(r"hermes gateway restart", flat):
            before = flat[max(0, m.start() - 260):m.start()]
            assert "/restart" in before, f"{rel}: '{flat[m.start() - 120:m.end()]}'"


def test_reload_fallback_mentions_restart_command(tmp_path, capsys):
    from speakeasy import cli
    assert cli.cmd_reload(tmp_path, timeout=0) == 1
    out = capsys.readouterr().out
    assert "/restart" in out and out.index("/restart") < out.index("hermes gateway restart")


# -- wording that doesn't assume a Mac; start vs restart; terminal QR; old Hermes -----------------

def test_setup_strings_do_not_assume_a_mac():
    from speakeasy import cli
    src = (REPO / "plugin" / "speakeasy" / "cli.py").read_text()
    for stale in ("Open it on your Mac", "No Speakeasy on your Mac yet", "Open this link on the Mac you'll"):
        assert stale not in src
    assert "on the iPhone, iPad or Mac you'll talk from" in src


def test_never_started_gateway_says_start_not_restart(tmp_path, monkeypatch):
    from speakeasy import setup_flow as F
    monkeypatch.setattr(F, "gateway_running", lambda h: False)
    assert F.not_up_note(tmp_path) == F.GATEWAY_START_NOTE
    assert "restart" not in F.GATEWAY_START_NOTE.lower() and "hermes gateway install" in F.GATEWAY_START_NOTE
    monkeypatch.setattr(F, "gateway_running", lambda h: True)
    assert F.not_up_note(tmp_path) == F.RESTART_NOTE


def test_setup_with_no_gateway_tells_the_user_to_start_it(tmp_path, monkeypatch):
    from test_setup import Fake, args
    from speakeasy import cli, setup_flow as F
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(F, "gateway_running", lambda h: False)
    monkeypatch.setattr(F, "find_codex", lambda configured="": tmp_path / "codex")
    fake = Fake(running=False, interactive=False)
    rc = cli.cmd_setup(args(yes=True, send="telegram"), tmp_path, env=fake.env())
    text = "\n".join(fake.lines)
    assert rc == 2 and F.GATEWAY_START_NOTE in text and "needs a restart" not in text
    assert "Once Hermes is running, the pairing link arrives in Telegram" in text


def test_pair_prints_a_terminal_qr_for_a_phone_reachable_address(tmp_path, capsys):
    from argparse import Namespace
    from speakeasy import cli
    from speakeasy.settings import Settings
    pytest.importorskip("qrcode")
    Settings(tmp_path).patch({"server": {"advertised_url": "https://box.tail123.ts.net:8795"}})
    assert cli.cmd_pair(Namespace(server="", send=""), tmp_path) == 0
    out = capsys.readouterr().out
    assert "scan this with the iPhone or iPad camera" in out and ("█" in out or "▀" in out or "▄" in out)


def test_pair_skips_the_qr_for_a_loopback_address(tmp_path, capsys):
    from argparse import Namespace
    from speakeasy import cli
    assert cli.cmd_pair(Namespace(server="http://127.0.0.1:8795", send=""), tmp_path) == 0
    assert "scan" not in capsys.readouterr().out


def test_qr_text_encodes_the_link():
    pytest.importorskip("qrcode")
    from speakeasy import cli
    art = cli.qr_text("https://speakeasyvoice.ai/pair#server=x&code=123456")
    assert art and len(art.splitlines()) > 10


def test_grok_fix_names_the_real_command():
    swift = (REPO / "mac" / "Sources" / "SpeakeasyCore" / "ServerSettings.swift").read_text()
    assert "sign in with X\"" not in swift and "hermes auth add xai-oauth" in swift


class _Ctx:
    def __init__(self):
        self.cli, self.platforms = [], []

    def register_cli_command(self, name, **kw):
        self.cli.append((name, kw))

    def register_platform(self, **kw):
        self.platforms.append(kw)


def test_hermes_older_than_the_gate_gets_an_update_message(monkeypatch, capsys):
    import speakeasy
    monkeypatch.setattr(speakeasy, "hermes_too_old", lambda: "0.18.2")
    ctx = _Ctx()
    speakeasy.register(ctx)
    assert ctx.platforms == [] and ctx.cli and ctx.cli[0][0] == "voice"
    assert ctx.cli[0][1]["handler_fn"](None) == 1
    out = capsys.readouterr().out
    assert "needs Hermes 0.22.0 or newer (this is Hermes 0.18.2)" in out and "hermes update" in out
    assert "/restart" in out


def test_current_hermes_is_not_too_old():
    import speakeasy
    assert speakeasy.hermes_too_old() is None


def test_agent_guide_checks_the_hermes_version_first():
    doc = _flat((REPO / "docs" / "AGENT_SETUP.md").read_text())
    assert "Speakeasy needs Hermes **0.22.0 or newer**" in doc and "invalid choice: 'voice'" in doc
