"""`hermes voice setup` end to end with every side effect faked: order of steps, voice sign-in,
Tailscale, never restarting Hermes, and that the pairing link is only handed over once the voice
server answers (or queued for delivery after the user restarts Hermes)."""
from __future__ import annotations

import os
import stat
from argparse import Namespace
from types import SimpleNamespace

import pytest

from speakeasy import cli, setup_flow as F
from speakeasy import settings as S
from fakes import read_profile_env


class Fake:
    """Records commands; answers like a machine with Codex signed in and Hermes running."""

    def __init__(self, *, codex_signed_in=True, codex_installed=True, running=True,
                 brew=True, answers=None, secret="", interactive=True, tailscale_dns="box.tail123.ts.net",
                 tailscale="missing", serve_error=""):
        self.cmds: list[list[str]] = []
        self.lines: list[str] = []
        self.codex_signed_in, self.codex_installed = codex_signed_in, codex_installed
        self.running, self.brew = running, brew
        self.answers = list(answers or [])
        self.secret_value, self.interactive, self.dns = secret, interactive, tailscale_dns
        self.tailscale, self.serve_error = tailscale, serve_error
        self.health_checks = 0
        self.now = 0.0

    def run(self, cmd, **kw):
        cmd = [str(c) for c in cmd]
        self.cmds.append(cmd)
        joined = " ".join(cmd)
        if joined.endswith("login status"):
            ok = self.codex_signed_in
            return SimpleNamespace(returncode=0 if ok else 1, stdout="Logged in using ChatGPT" if ok else "Not logged in", stderr="")
        if cmd[-1] == "login":
            self.codex_signed_in = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["brew", "install"]:
            self.codex_installed = True
            return SimpleNamespace(returncode=0)
        if "status" in cmd and "--json" in cmd:
            state = "Running" if self.tailscale == "running" else "Stopped"
            return SimpleNamespace(returncode=0, stdout='{"BackendState": "%s", "Self": {"DNSName": "%s."}}'
                                   % (state, self.dns if state == "Running" else ""), stderr="")
        if "serve" in cmd:
            return SimpleNamespace(returncode=1 if self.serve_error else 0, stdout="", stderr=self.serve_error)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def ask(self, prompt):
        self.lines.append("? " + prompt)
        return self.answers.pop(0) if self.answers else ""

    def health(self, url):
        self.health_checks += 1
        return self.running

    def which(self, name):
        return {"brew": "/opt/homebrew/bin/brew" if self.brew else None,
                "tailscale": None if self.tailscale == "missing" else "/usr/local/bin/tailscale"}.get(name)

    def env(self):
        return F.Env(run=self.run, ask=self.ask, secret=lambda p: self.secret_value, out=self.lines.append,
                     interactive=self.interactive, health=self.health, sleep=self.sleep, which=self.which,
                     clock=lambda: self.now)

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    monkeypatch.setattr(cli, "_home", lambda: tmp_path)
    monkeypatch.setattr(F, "gateway_running", lambda h: True)
    monkeypatch.setattr(F, "find_codex", lambda configured="": _codex(tmp_path, fake_holder))
    return tmp_path


fake_holder: dict = {}


def _codex(tmp_path, holder):
    fake = holder.get("fake")
    return (tmp_path / "codex") if fake is None or fake.codex_installed else None


def args(**kw):
    base = dict(voice_command="setup", server="", tailscale=False, no_tailscale=False, api_key=False, send="", yes=False,
                no_restart=False, no_open=True, here=False)
    base.update(kw)
    return Namespace(**base)


def run_setup(fake, app_installed=True, **kw):
    fake_holder["fake"] = fake
    opened = []
    orig_open, orig_installed = cli._open, cli._app_installed
    cli._open = lambda link: opened.append(link) or True
    cli._app_installed = lambda: app_installed
    try:
        rc = cli.cmd_setup(args(**kw), cli._home(), env=fake.env())
    finally:
        cli._open, cli._app_installed = orig_open, orig_installed
    return rc, opened


def never_restarts(fake):
    return not any("gateway" in c and ({"restart", "start", "stop"} & set(c)) for c in fake.cmds)


def test_happy_path_opens_the_app_when_it_is_on_this_mac(home):
    fake = Fake()
    rc, opened = run_setup(fake, no_open=False)
    assert rc == 0 and never_restarts(fake)
    assert len(opened) == 1 and opened[0].startswith("speakeasy://pair?server=http%3A%2F%2F127.0.0.1")
    text = "\n".join(fake.lines)
    assert "✓ Voice: GPT-Live-1 through Codex OAuth" in text and "✓ Voice server is running" in text
    assert "Opened Speakeasy" in text


def test_app_not_on_this_mac_prints_a_web_link_that_offers_the_download(home):
    fake = Fake(interactive=False)
    rc, opened = run_setup(fake, no_open=False, app_installed=False)
    text = "\n".join(fake.lines)
    assert rc == 0 and opened == []
    assert "https://speakeasyvoice.ai/pair#server=http%3A%2F%2F127.0.0.1" in text and "download" in text


def test_needs_restart_says_so_and_never_restarts(home):
    fake = Fake(running=False)
    rc, opened = run_setup(fake, no_open=False)
    text = "\n".join(fake.lines)
    assert rc == 2 and opened == [] and never_restarts(fake)
    assert "needs a restart" in text and "hermes voice pair" in text
    assert "speakeasyvoice.ai" in text  # download link for someone without the app yet
    assert "Pairing code" not in text and "#server=" not in text  # no link that can't work yet


def test_needs_restart_with_send_queues_the_link_for_after_the_restart(home):
    from speakeasy import handoff as H
    fake = Fake(running=False)
    rc, _ = run_setup(fake, send="telegram", yes=True)
    text = "\n".join(fake.lines)
    assert rc == 2 and never_restarts(fake)
    assert "arrives in Telegram by itself" in text
    assert H.pending_target(home) == "telegram"


def test_inside_a_hermes_chat_never_restarts(home, monkeypatch):
    monkeypatch.setenv("_HERMES_GATEWAY", "1")
    fake = Fake(running=False)
    rc, _ = run_setup(fake, yes=True, send="telegram")
    assert rc == 2 and never_restarts(fake)


def test_send_delivers_a_web_link_now_when_already_running(home, monkeypatch):
    from speakeasy import handoff as H
    sent = []
    monkeypatch.setattr(cli, "_send_link", lambda h, target, link, out: sent.append((target, link)) or True)
    fake = Fake(tailscale="running")
    rc, opened = run_setup(fake, yes=True, send="telegram", no_open=False)
    assert rc == 0 and opened == [] and never_restarts(fake)
    assert sent and sent[0][0] == "telegram"
    assert sent[0][1].startswith("https://speakeasyvoice.ai/pair#server=https%3A%2F%2Fbox.tail123.ts.net%3A8795&code=")
    assert H.pending_target(home) == ""


def test_signs_in_to_codex_when_needed(home):
    fake = Fake(codex_signed_in=False)
    rc, _ = run_setup(fake, yes=True)
    assert rc == 0 and any(c[-1] == "login" for c in fake.cmds)
    assert S.Settings(home).get()["voice"]["provider"] == "codex"


def test_installs_codex_with_homebrew(home):
    fake = Fake(codex_installed=False)
    rc, _ = run_setup(fake, yes=True)
    assert rc == 0 and ["brew", "install", "codex"] in fake.cmds


def test_api_key_path_stores_key_privately_and_never_prints_it(home):
    key = "sk-" + "a" * 40
    fake = Fake(secret=key)
    rc, _ = run_setup(fake, api_key=True, yes=True)
    assert rc == 0
    assert read_profile_env(home)[S.OPENAI_KEY_NAME] == key
    assert stat.S_IMODE(os.stat(home / ".env").st_mode) == 0o600
    assert S.Settings(home).get()["voice"]["provider"] == "openai"
    assert key not in "\n".join(fake.lines)
    assert not any("login" in c for c in fake.cmds)


def test_rejects_something_that_is_not_an_api_key(home):
    fake = Fake(secret="hunter2")
    rc, _ = run_setup(fake, api_key=True, yes=True)
    assert rc == 1 and S.OPENAI_KEY_NAME not in read_profile_env(home)


def test_tailscale_serves_tailnet_only_and_links_to_it(home):
    fake = Fake(tailscale="running")
    rc, _ = run_setup(fake, tailscale=True, yes=True)
    assert rc == 0
    serve = next(c for c in fake.cmds if "serve" in c)
    assert "funnel" not in " ".join(serve) and "--bg" in serve
    text = "\n".join(fake.lines)
    assert "https%3A%2F%2Fbox.tail123.ts.net%3A8795" in text and never_restarts(fake)


def test_running_tailnet_is_imported_without_a_flag_and_remembered_for_pair(home, capsys):
    fake = Fake(tailscale="running", interactive=False)
    rc, opened = run_setup(fake, yes=True, no_open=False)
    assert rc == 0 and opened == []  # the Mac is elsewhere: never open the link on this host
    text = "\n".join(fake.lines)
    assert "✓ Tailscale detected" in text and "https://box.tail123.ts.net:8795" in text and "┏" in text
    assert not any("funnel" in " ".join(c) for c in fake.cmds)
    stored = S.Settings(home).get()["server"]
    assert {k: stored[k] for k in ("advertised_url", "tailscale_name")} == {
        "advertised_url": "https://box.tail123.ts.net:8795", "tailscale_name": "box.tail123.ts.net"}
    assert stored["tunnel"] == ""  # a tailnet never starts a Cloudflare tunnel
    assert "speakeasyvoice.ai/pair#server=https%3A%2F%2Fbox.tail123.ts.net%3A8795" in text
    assert cli.cmd_pair(Namespace(server="", send=""), home) == 0
    assert "speakeasyvoice.ai/pair#server=https%3A%2F%2Fbox.tail123.ts.net%3A8795" in capsys.readouterr().out


def test_stopped_tailnet_warns_prominently_and_stays_local(home):
    fake = Fake(tailscale="stopped")
    rc, opened = run_setup(fake, yes=True, no_open=False)
    assert rc == 0 and len(opened) == 1 and "127.0.0.1" in opened[0]
    text = "\n".join(fake.lines)
    assert "not connected" in text and "tailscale up" in text and "┏" in text
    assert not any("serve" in c for c in fake.cmds)
    assert S.Settings(home).get()["server"]["advertised_url"] == ""


def test_no_tailscale_is_one_line_then_the_cloudflare_step(home):
    fake = Fake(tailscale="missing")
    rc, _ = run_setup(fake, yes=True)
    text = "\n".join(fake.lines)
    assert rc == 0 and "Tailscale not found" in text and "stays local" not in text
    assert text.count("┏") == 1 and "speakeasy-cloudflare.py quick" in text  # the agent turns Cloudflare on


def test_no_tailscale_flag_skips_detection(home):
    fake = Fake(tailscale="running")
    rc, _ = run_setup(fake, yes=True, no_tailscale=True)
    assert rc == 0 and not any(c and c[0].endswith("/tailscale") for c in fake.cmds)


def test_serve_certificate_failure_prints_the_fix_and_falls_back(home):
    fake = Fake(tailscale="running", serve_error="error: HTTPS certificates are not enabled for this tailnet")
    rc, opened = run_setup(fake, yes=True, no_open=False)
    text = "\n".join(fake.lines)
    assert rc == 0 and "https://login.tailscale.com/admin/dns" in text and "HTTPS Certificates" in text
    assert len(opened) == 1 and "127.0.0.1" in opened[0]
    assert S.Settings(home).get()["server"]["advertised_url"] == ""


def test_remote_pairing_offers_to_send_the_link_to_a_chat(home, monkeypatch):
    import json as _json
    (home / "gateway_state.json").write_text(_json.dumps({"platforms": {"telegram": {"state": "connected"}}}))
    (home / "channel_directory.json").write_text(_json.dumps(
        {"platforms": {"telegram": [{"id": "555000111", "name": "Sam", "type": "dm"}]}}))
    sent = []
    monkeypatch.setattr(cli, "_send_link", lambda h, target, link, out: sent.append((target, link)) or True)
    fake = Fake(tailscale="running", answers=[""])
    rc, opened = run_setup(fake, yes=True, no_open=False)
    assert rc == 0 and opened == []
    assert sent and sent[0][0] == "telegram:555000111" and "box.tail123.ts.net" in sent[0][1]
    assert sent[0][1].startswith("https://speakeasyvoice.ai/pair#")
    assert any("another computer" in line for line in fake.lines)


def test_setup_prints_the_routing_model(home):
    fake = Fake()
    run_setup(fake, yes=True)
    assert any("Task routing uses" in line and "speakeasy_router" in line for line in fake.lines)


def test_existing_key_and_config_are_kept(home):
    (home / ".env").write_text("API_SERVER_KEY=keep-this-existing-value\nOTHER=1\n")
    fake = Fake()
    rc, _ = run_setup(fake, yes=True)
    assert rc == 0
    env = (home / ".env").read_text()
    assert env.count("API_SERVER_KEY=") == 1 and "keep-this-existing-value" in env and "OTHER=1" in env
    assert "keep-this-existing-value" not in "\n".join(fake.lines)


def test_rerunning_setup_is_harmless(home):
    fake = Fake()
    assert run_setup(fake, yes=True)[0] == 0
    before = (home / "config.yaml").read_text()
    fake2 = Fake()
    assert run_setup(fake2, yes=True)[0] == 0
    assert (home / "config.yaml").read_text() == before
    assert never_restarts(fake2)


def test_setup_turns_voice_on_for_the_real_gateway_loader(home):
    """What setup writes must make Hermes' own plugin-platform check say 'configured'."""
    import yaml
    import speakeasy
    run_setup(Fake(), yes=True)
    cfg = yaml.safe_load((home / "config.yaml").read_text())
    block = cfg["gateway"]["platforms"]["voice"]
    probe = SimpleNamespace(enabled=True, extra=dict(block["extra"]))
    assert speakeasy.is_connected(probe)
    assert not speakeasy.is_connected(SimpleNamespace(enabled=True, extra={}))


def test_here_opens_on_this_machine_even_with_a_tailnet(home, monkeypatch):
    """--here: the agent checked the user talks from this Mac; open the app here over loopback."""
    fake = Fake(tailscale="running")
    fake.restarted = True
    rc, opened = run_setup(fake, yes=True, here=True)
    assert rc == 0 and len(opened) == 1
    assert opened[0].startswith("speakeasy://pair?server=http%3A%2F%2F127.0.0.1")


def test_here_without_the_app_opens_the_download_page(home, monkeypatch):
    fake = Fake()
    fake.restarted = True
    rc, opened = run_setup(fake, app_installed=False, yes=True, here=True)
    assert rc == 0 and opened and opened[0].startswith("https://speakeasyvoice.ai/pair#server=http%3A%2F%2F127.0.0.1")
    assert any("download page" in line for line in fake.lines)


# -- config safety: Speakeasy only ever adds to a user's config.yaml ------------------------------

def _rich_config():
    return {"model": {"default": "some-model", "provider": "some-provider"},
            "display": {"runtime_footer": {"enabled": True, "fields": ["model", "context_pct"]}},
            "plugins": {"enabled": ["other-plugin"], "disabled": ["x"]},
            "gateway": {"strict": True, "platforms": {"api_server": {"enabled": True}}},
            "mcp_servers": {"a": {"command": "a"}}, "custom_section": [1, 2, 3]}


def test_setup_keeps_every_existing_setting(home):
    import yaml
    (home / "config.yaml").write_text(yaml.safe_dump(_rich_config()))
    assert run_setup(Fake(), yes=True)[0] == 0
    saved = yaml.safe_load((home / "config.yaml").read_text())
    from speakeasy.hermes_config import _lost_paths
    assert _lost_paths(_rich_config(), saved) == []
    assert saved["model"] == _rich_config()["model"] and saved["display"] == _rich_config()["display"]
    assert saved["plugins"]["enabled"] == ["other-plugin", "speakeasy"]
    assert saved["gateway"]["platforms"]["voice"]["enabled"] is True


def test_setup_never_overwrites_an_unreadable_config(home):
    broken = "model: {default: x\n  this is: [not yaml\n"
    (home / "config.yaml").write_text(broken)
    fake = Fake()
    rc, _ = run_setup(fake, yes=True)
    assert rc == 1
    assert (home / "config.yaml").read_text() == broken
    assert any("untouched" in line for line in fake.lines)


def test_update_refuses_a_change_that_removes_settings(tmp_path):
    import yaml
    from speakeasy import hermes_config
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_rich_config()))
    before = path.read_text()
    with pytest.raises(hermes_config.ConfigWriteRefused):
        hermes_config.update(path, lambda cfg: cfg.pop("model") is not None)
    with pytest.raises(hermes_config.ConfigWriteRefused):
        hermes_config.update(path, lambda cfg: cfg["display"]["runtime_footer"].pop("fields") is not None)
    assert path.read_text() == before


def test_update_without_a_change_does_not_write(tmp_path):
    import yaml
    from speakeasy import hermes_config
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_rich_config()))
    mtime = path.stat().st_mtime_ns
    assert hermes_config.update(path, lambda cfg: False) is False
    assert path.stat().st_mtime_ns == mtime


def _fake_app(home, rel):
    path = home / "Applications" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_codex_is_found_inside_a_chatgpt_only_install(tmp_path, monkeypatch):
    from speakeasy.settings import codex_candidates
    monkeypatch.setenv("PATH", "")
    bundled = _fake_app(tmp_path, "ChatGPT.app/Contents/Resources/codex")
    assert bundled in codex_candidates(tmp_path)


def test_codex_is_found_inside_a_leftover_codex_app(tmp_path, monkeypatch):
    from speakeasy.settings import codex_candidates, find_codex
    monkeypatch.setenv("PATH", "")
    old = _fake_app(tmp_path, "Codex.app/Contents/Resources/codex")
    new = _fake_app(tmp_path, "ChatGPT.app/Contents/Resources/codex-cli/bin/codex")
    found = codex_candidates(tmp_path)
    assert old in found and new in found
    versions = {old: (0, 150, 0), new: (0, 160, 0)}
    assert find_codex(version_fn=lambda p: versions.get(p), candidates=found) == new


# -- automatic Cloudflare: no Tailscale → the agent turns Cloudflare mode on, no menu ---------------

def _setup_text(home, monkeypatch, *, tailscale="missing", tunnel=False, **kw):
    from speakeasy import tunnel as T
    from speakeasy.settings import Settings
    if tunnel:
        Settings(home).patch({"server": {"tunnel": "cloudflare"}})
        monkeypatch.setattr(T, "wait_for_url", lambda *a, **k: "https://calm-river-sound-test.trycloudflare.com")
    fake = Fake(tailscale=tailscale)
    rc, _ = run_setup(fake, yes=True, **kw)
    return rc, "\n".join(fake.lines)


def test_no_tailscale_and_no_tunnel_prints_the_cloudflare_step_not_a_menu(home, monkeypatch):
    rc, text = _setup_text(home, monkeypatch)
    assert rc == 0 and "speakeasy-cloudflare.py quick" in text and "┏" in text
    assert "No Tailscale? Your agent can" not in text and "Connected through Cloudflare\"" in text
    assert "✓ Connected through Cloudflare" not in text


def test_same_mac_needs_no_cloudflare(home, monkeypatch):
    rc, text = _setup_text(home, monkeypatch, here=True)
    assert "speakeasy-cloudflare.py quick" not in text


def test_tunnel_on_without_tailscale_says_connected_through_cloudflare(home, monkeypatch):
    from speakeasy import cli
    rc, text = _setup_text(home, monkeypatch, tunnel=True, send="telegram")
    assert cli.CLOUDFLARE_CONNECTED in text and cli.CLOUDFLARE_USER_MESSAGE in text
    assert "speakeasy-cloudflare.py quick" not in text
    assert "calm-river-sound-test.trycloudflare.com" in text


def test_tunnel_with_tailscale_is_only_a_backup(home, monkeypatch):
    from speakeasy import cli
    rc, text = _setup_text(home, monkeypatch, tunnel=True, tailscale="running")
    assert cli.CLOUDFLARE_BACKUP in text and cli.CLOUDFLARE_CONNECTED not in text
    assert cli.CLOUDFLARE_USER_MESSAGE not in text and "speakeasy-cloudflare.py quick" not in text
