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
    assert RESTART_NEEDED == ("Speakeasy was updated while your Hermes was running. "
                              "Send /restart to your Hermes to finish the update.")
    assert "restart Hermes" not in RESTART_NEEDED and "Restart Hermes" not in RESTART_NEEDED


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


# -- SPK-135: `hermes voice pair` applies setup's loopback rule ------------------------------------

def _pair(tmp_path, monkeypatch, *, server="", send="", advertised=""):
    from argparse import Namespace
    from speakeasy import cli
    from speakeasy.settings import Settings
    if advertised:
        Settings(tmp_path).patch({"server": {"advertised_url": advertised}})
    sent = []
    monkeypatch.setattr(cli, "_send_link", lambda h, target, link, out: sent.append((target, link)) or True)
    return cli.cmd_pair(Namespace(server=server, send=send, no_open=False), tmp_path), sent


@pytest.mark.parametrize("server", ["", "http://127.0.0.1:8795", "http://localhost:8795"])
def test_pair_never_sends_a_link_that_only_works_on_this_computer(tmp_path, monkeypatch, capsys, server):
    from speakeasy import cli
    rc, sent = _pair(tmp_path, monkeypatch, server=server, send="telegram")
    out = capsys.readouterr().out
    assert rc == 1 and sent == []
    assert cli.PAIR_LOCAL_ONLY_NOT_SENT in out and "Pairing link:" not in out
    assert "speakeasy-cloudflare.py quick" in out and "tailscale.com/download" in out  # setup's same step
    assert "`hermes voice pair` gives a link that works from anywhere" in out


@pytest.mark.parametrize("person", [True, False])
def test_pair_local_only_help_speaks_to_a_person_or_an_agent(tmp_path, person):
    from speakeasy import cli
    lines: list[str] = []
    cli._local_only_help(tmp_path, lines.append, sent=False, person=person)
    text = "\n".join(lines)
    assert ("Your phone can't reach this computer yet" in text) is person
    assert ("don't ask the user first" in text) is not person
    assert "    " + cli.CLOUDFLARE_DOWNLOAD in lines and "    " + cli.CLOUDFLARE_QUICK in lines


def test_pair_warns_when_its_printed_link_only_works_here(tmp_path, monkeypatch, capsys):
    from speakeasy import cli
    rc, sent = _pair(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    assert rc == 0 and sent == []
    assert "Pairing link: https://speakeasyvoice.ai/pair#" in out  # still usable for Speakeasy on this Mac
    assert cli.PAIR_LOCAL_ONLY in out and "speakeasy-cloudflare.py quick" in out
    assert out.index("Pairing link:") < out.index(cli.PAIR_LOCAL_ONLY)


@pytest.mark.parametrize("advertised", ["https://box.tail123.ts.net:8795",
                                        "https://calm-river-sound-test.trycloudflare.com"])
def test_pair_sends_a_tailnet_or_cloudflare_link_as_before(tmp_path, monkeypatch, capsys, advertised):
    from speakeasy import cli
    rc, sent = _pair(tmp_path, monkeypatch, send="telegram", advertised=advertised)
    out = capsys.readouterr().out
    assert rc == 0 and len(sent) == 1 and sent[0][0] == "telegram"
    assert "speakeasyvoice.ai/pair#" in sent[0][1]
    assert cli.PAIR_LOCAL_ONLY not in out and cli.PAIR_LOCAL_ONLY_NOT_SENT not in out


def test_pair_accepts_no_open_and_help_names_every_device():
    import argparse
    from speakeasy import cli
    parser = argparse.ArgumentParser(prog="hermes voice")
    cli.setup_parser(parser)
    args = parser.parse_args(["pair", "--no-open", "--send", "telegram"])
    assert args.voice_command == "pair" and args.no_open and args.send == "telegram"
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    text = " ".join(_flat(sub.choices[name].format_help()) for name in ("setup", "pair"))
    text += " " + " ".join(c.help or "" for c in sub._choices_actions if c.dest in {"setup", "pair"})
    assert "Mac, iPhone or iPad" in text
    assert "pair a Mac" not in text and "the Mac will use" not in text


def test_qr_text_encodes_the_link():
    pytest.importorskip("qrcode")
    from speakeasy import cli
    art = cli.qr_text("https://speakeasyvoice.ai/pair#server=x&code=123456")
    assert art and len(art.splitlines()) > 10


def test_grok_fix_names_the_real_command():
    # SPK-156: the setup check never sends anyone to a terminal; the Grok fix asks Hermes to sign in
    # to X (GrokSignInCopy), never the old "sign in with X" wording.
    swift = (REPO / "mac" / "Sources" / "SpeakeasyCore" / "ServerSettings.swift").read_text()
    copy = (REPO / "mac" / "Sources" / "SpeakeasyCore" / "CodexLogin.swift").read_text()
    assert "sign in with X\"" not in swift and "GrokSignInCopy.notSignedIn" in swift
    assert "Ask your Hermes to sign in to X." in copy


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
