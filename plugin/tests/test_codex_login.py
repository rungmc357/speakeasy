"""Sign in to ChatGPT from the app (and `hermes voice setup --sign-in remote`) with Codex's own
device-code login. A fake `codex` binary (tests/fake_codex.py) stands in: no real login ever runs.

Owner's order: (1) a working ChatGPT sign-in in ANY Codex copy on the machine is used silently and
saved; (2) only when none is signed in does the app get a code to show; (3) an API-key-only sign-in
is reported and switched only on request. With no Codex at all, the plugin installs it first."""
from __future__ import annotations

import logging
import time
from argparse import Namespace
from pathlib import Path

import pytest

from fake_codex import DEVICE_URL, USER_CODE, fake_log, make_fake_codex
from speakeasy import codex_login as CL
from speakeasy import codex_transport as ct


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.05)
    return False


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    ct._LOGIN_CACHE.clear(); ct._REJECTED.clear()
    yield home
    ct._LOGIN_CACHE.clear(); ct._REJECTED.clear()


def make_service(home: Path, candidates, installer=None):
    from speakeasy.service import VoiceService
    return VoiceService(home, notifier=None, start_threads=False,
                        codex_candidates=lambda: list(candidates()),
                        codex_install=installer or (lambda: False))


@pytest.fixture
def svc(tmp_path, codex_home):
    made = []

    def build(candidates, installer=None, provider="codex"):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir(exist_ok=True)
        s = make_service(hermes_home, candidates, installer)
        s.settings.patch({"voice": {"provider": provider}})
        made.append(s)
        return s
    yield build
    for s in made:
        s.close()


# -- the flow itself ------------------------------------------------------------------------------

def test_device_flow_returns_url_and_code_then_signs_in(tmp_path, codex_home):
    binary = make_fake_codex(tmp_path / "bin", codex_home)
    signed: list[Path] = []
    flow = CL.CodexLogin(tmp_path / "work", on_signed_in=signed.append)
    out = flow.start(binary)
    assert out["state"] == "pending" and out["verification_url"] == DEVICE_URL and out["user_code"] == USER_CODE
    assert 0 < out["expires_in"] <= 15 * 60 and out["expires_at"] > time.time()
    assert flow.start(binary)["user_code"] == USER_CODE  # one flow at a time: the same code again
    assert wait_for(lambda: flow.status()["state"] == "signed_in")
    assert signed == [binary]
    assert flow.flow.process.poll() is not None  # the child is gone once it's done
    log = fake_log(codex_home)
    assert log.count("account/login/start") == 1 and '"type": "chatgptDeviceCode"' in log
    assert "argv app-server --stdio" in log and "login --device-auth" not in log


def test_declined_sign_in_reports_failed_with_a_plain_reason(tmp_path, codex_home):
    binary = make_fake_codex(tmp_path / "bin", codex_home, mode="fail")
    flow = CL.CodexLogin(tmp_path / "work")
    flow.start(binary)
    assert wait_for(lambda: flow.status()["state"] == "failed")
    assert flow.status()["message"] == "The sign-in was declined in the browser."
    assert "access_denied" not in flow.status()["message"]


def test_code_expiry_kills_the_child(tmp_path, codex_home):
    binary = make_fake_codex(tmp_path / "bin", codex_home, mode="hang")
    flow = CL.CodexLogin(tmp_path / "work", lifetime_s=0.6)
    flow.start(binary)
    process = flow.flow.process
    assert wait_for(lambda: flow.status()["state"] == "expired")
    assert wait_for(lambda: process.poll() is not None)
    assert "expired" in flow.status()["message"] and "user_code" not in flow.status()


def test_cancel_sends_cancel_and_kills_the_child(tmp_path, codex_home):
    binary = make_fake_codex(tmp_path / "bin", codex_home, mode="hang")
    flow = CL.CodexLogin(tmp_path / "work")
    flow.start(binary)
    process = flow.flow.process
    assert flow.cancel()["state"] == "cancelled"
    assert wait_for(lambda: process.poll() is not None)
    assert "account/login/cancel" in fake_log(codex_home)


@pytest.mark.parametrize("mode", ["badurl", "badcode"])
def test_a_url_off_openai_or_a_strange_code_is_never_passed_on(tmp_path, codex_home, mode):
    binary = make_fake_codex(tmp_path / "bin", codex_home, mode=mode)
    flow = CL.CodexLogin(tmp_path / "work")
    with pytest.raises(CL.CodexLoginError):
        flow.start(binary)
    assert flow.status()["state"] == "failed" and "verification_url" not in flow.status()
    assert wait_for(lambda: flow.flow.process.poll() is not None)


@pytest.mark.parametrize("mode", ["error", "exit"])
def test_codex_refusing_or_dying_is_a_clean_error(tmp_path, codex_home, mode):
    binary = make_fake_codex(tmp_path / "bin", codex_home, mode=mode)
    with pytest.raises(CL.CodexLoginError):
        CL.CodexLogin(tmp_path / "work").start(binary)


def test_url_check():
    ok = CL.safe_verification_url
    assert ok("https://auth.openai.com/codex/device") and ok("https://chatgpt.com/device")
    for bad in ("http://auth.openai.com/codex/device", "https://openai.com.evil.com/x", "https://evilopenai.com/",
                "https://user@auth.openai.com/", "https://auth.openai.com:8443/", "javascript:alert(1)", "", None,
                "https://auth.openai.com/a b"):
        assert ok(bad) is None, bad
    assert CL.safe_user_code("abcd-1234") == "ABCD-1234" and CL.safe_user_code("<x>") is None


def test_the_code_is_never_logged(tmp_path, codex_home, caplog):
    caplog.set_level(logging.DEBUG)
    binary = make_fake_codex(tmp_path / "bin", codex_home)
    flow = CL.CodexLogin(tmp_path / "work")
    flow.start(binary)
    assert wait_for(lambda: flow.status()["state"] == "signed_in")
    assert USER_CODE not in caplog.text and "login-1" not in caplog.text


def test_the_child_gets_only_codex_env(tmp_path, codex_home, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    seen = {}

    def popen(cmd, **kw):
        seen.update(kw["env"])
        raise OSError("stop here")
    with pytest.raises(CL.CodexLoginError):
        CL.CodexLogin(tmp_path / "work", popen=popen).start(tmp_path / "codex")
    assert "OPENAI_API_KEY" not in seen and seen.get("CODEX_HOME") == str(codex_home)


# -- the owner's order: find a working sign-in first ----------------------------------------------

def test_signed_in_chatgpt_app_copy_is_used_with_no_prompt(svc, tmp_path, codex_home):
    """PATH copy signed out, the ChatGPT app's bundled copy signed in: use it, save it, no prompt."""
    path_copy = make_fake_codex(tmp_path / "path", codex_home, own_state="none")
    app_copy = make_fake_codex(tmp_path / "ChatGPT.app/Contents/Resources/codex-cli/bin", codex_home,
                               own_state="chatgpt", version="0.150.0")
    s = svc(lambda: [path_copy, app_copy])
    status = s.status()
    assert status["codex_signed_in"] is True and status["codex_sign_in"] == "chatgpt"
    assert status["capabilities"]["codex_login"] is True
    assert s.settings.get()["voice"]["codex_path"] == str(app_copy)
    out = s.codex_login_start({})
    assert out["state"] == "signed_in" and "user_code" not in out
    assert s.codex_login_status()["state"] == "signed_in"
    assert "app-server" not in fake_log(codex_home)  # no device flow was started


def test_none_signed_in_gets_a_code_to_show(svc, tmp_path, codex_home):
    a = make_fake_codex(tmp_path / "a", codex_home, own_state="none")
    b = make_fake_codex(tmp_path / "b", codex_home, own_state="none", version="0.151.0")
    s = svc(lambda: [a, b])
    assert s.status()["codex_sign_in"] == "none" and s.status()["codex_signed_in"] is False
    out = s.codex_login_start({})
    assert out["state"] == "pending" and out["user_code"] == USER_CODE and out["verification_url"] == DEVICE_URL
    assert s.codex_login_status()["user_code"] == USER_CODE  # polling sees the same code
    assert wait_for(lambda: s.codex_login_status()["state"] == "signed_in")
    assert s.status()["codex_signed_in"] is True  # the cached "not signed in" was dropped


def test_too_old_copies_are_skipped(svc, tmp_path, codex_home):
    old = make_fake_codex(tmp_path / "old", codex_home, own_state="chatgpt", version="0.140.0")
    s = svc(lambda: [old])
    assert s.status()["codex_sign_in"] == "none"


def test_api_key_only_says_so_and_switches_only_when_asked(svc, tmp_path, codex_home):
    binary = make_fake_codex(tmp_path / "bin", codex_home, own_state="api_key")
    s = svc(lambda: [binary])
    assert s.status()["codex_sign_in"] == "api_key"
    from speakeasy.calls import ServiceError
    with pytest.raises(ServiceError) as err:
        s.codex_login_start({})
    assert err.value.status == 409 and err.value.reason == "api_key_signed_in" and "API key" in err.value.message
    out = s.codex_login_start({"switch_from_api_key": True})
    assert out["state"] == "pending"
    assert wait_for(lambda: s.codex_login_status()["state"] == "signed_in")


def test_not_installed_installs_codex_then_shows_a_code(svc, tmp_path, codex_home):
    copies: list[Path] = []

    def install():
        copies.append(make_fake_codex(tmp_path / "speakeasy-codex", codex_home, own_state="none"))
        return True
    s = svc(lambda: copies, installer=install)
    first = s.codex_login_start({})
    assert first["state"] in {"installing", "pending"}
    assert wait_for(lambda: s.codex_login_status()["state"] == "pending")
    assert s.codex_login_status()["user_code"] == USER_CODE
    s.codex_login_cancel()


def test_install_failure_is_reported(svc, tmp_path, codex_home):
    s = svc(lambda: [], installer=lambda: False)
    s.codex_login_start({})
    assert wait_for(lambda: s.codex_login_status()["state"] == "failed")
    assert "install Codex" in s.codex_login_status()["message"]
    assert s.codex_login_cancel()["state"] == "idle" and s.codex_login_status()["state"] == "idle"


def test_bad_body_is_refused(svc, tmp_path, codex_home):
    s = svc(lambda: [])
    from speakeasy.calls import ServiceError
    for body in ({"token": "x"}, {"switch_from_api_key": "yes"}):
        with pytest.raises(ServiceError):
            s.codex_login_start(body)


def test_routes(svc, tmp_path, codex_home):
    from fakes import http
    from speakeasy.server import SpeakeasyServer
    binary = make_fake_codex(tmp_path / "bin", codex_home, mode="hang")
    s = svc(lambda: [binary])
    srv = SpeakeasyServer(s, "127.0.0.1", 0)
    srv.start()
    try:
        code = s.devices.new_pairing_code()
        _, paired = http(srv.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Test"})
        token = paired["token"]
        status, body = http(srv.base_url, "POST", "/voice/codex/login", {}, token=token)
        assert status == 200 and body["state"] == "pending" and body["user_code"] == USER_CODE
        status, body = http(srv.base_url, "GET", "/voice/codex/login", token=token)
        assert status == 200 and body["state"] == "pending"
        status, body = http(srv.base_url, "DELETE", "/voice/codex/login", token=token)
        assert status == 200 and body["state"] == "cancelled"
        assert http(srv.base_url, "GET", "/voice/codex/login")[0] == 401  # paired devices only
    finally:
        srv.stop()


# -- `hermes voice setup --yes --sign-in remote` ---------------------------------------------------

def _setup_args(**kw):
    base = dict(voice_command="setup", server="", tailscale=False, no_tailscale=True, api_key=False, send="",
                yes=True, no_restart=False, no_open=True, here=False, sign_in="remote")
    base.update(kw)
    return Namespace(**base)


@pytest.fixture
def setup_env(tmp_path, codex_home, monkeypatch):
    from speakeasy import cli, setup_flow as F
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setattr(cli, "_home", lambda: hermes_home)
    monkeypatch.setattr(F, "gateway_running", lambda h: True)
    lines: list[str] = []
    env = F.Env(out=lines.append, interactive=False, health=lambda url: False, which=lambda n: None,
                sleep=lambda s: time.sleep(.05))
    return hermes_home, env, lines


def _run(hermes_home, env, binary, monkeypatch, **kw):
    from speakeasy import cli, setup_flow as F
    monkeypatch.setattr(F, "find_codex", lambda configured="": binary)
    monkeypatch.setattr(F, "codex_candidates", lambda: [binary] if binary else [])
    return cli.cmd_setup(_setup_args(**kw), hermes_home, env=env)


def test_remote_sign_in_prints_one_line_and_waits(setup_env, tmp_path, codex_home, monkeypatch):
    hermes_home, env, lines = setup_env
    binary = make_fake_codex(tmp_path / "bin", codex_home)
    rc = _run(hermes_home, env, binary, monkeypatch)
    sign = [l for l in lines if l.startswith("SIGN_IN ")]
    assert sign == [f"SIGN_IN url={DEVICE_URL} code={USER_CODE} expires_in={sign[0].rsplit('=', 1)[1]}"]
    assert 0 < int(sign[0].rsplit("=", 1)[1]) <= 900
    assert "SIGNED_IN" in lines and rc in (0, 2)  # 2: the gateway still needs its first restart
    assert "open" not in fake_log(codex_home)


def test_remote_sign_in_is_skipped_when_a_copy_is_signed_in(setup_env, tmp_path, codex_home, monkeypatch):
    hermes_home, env, lines = setup_env
    binary = make_fake_codex(tmp_path / "bin", codex_home, own_state="chatgpt")
    _run(hermes_home, env, binary, monkeypatch)
    assert not any(l.startswith("SIGN_IN") for l in lines) and "app-server" not in fake_log(codex_home)


@pytest.mark.parametrize("mode", ["fail", "hang"])
def test_remote_sign_in_fails_non_zero_with_a_retry_hint(setup_env, tmp_path, codex_home, monkeypatch, mode):
    hermes_home, env, lines = setup_env
    binary = make_fake_codex(tmp_path / "bin", codex_home, mode=mode)
    monkeypatch.setattr(CL, "CODE_LIFETIME_S", 0.8)
    rc = _run(hermes_home, env, binary, monkeypatch)
    assert rc == 3
    text = "\n".join(lines)
    assert "SIGN_IN_FAILED" in text and "--sign-in remote` again" in text
