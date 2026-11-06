"""Secure input: a key a skill needs, or the Mac password for one sudo command, typed into a secure
card and handed to Hermes. The value must never reach a log, state.sqlite3, an event, a push, a
response, a transcript or the model; answers need the paired device, are single use and expire."""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from fakes import http, wait_for
from speakeasy import secure_input as S

FAKE_KEY = "FAKE-owm-test-key-9f3c2a7e51"          # obviously fake, never a real credential
FAKE_PASSWORD = "fake-mac-password-for-tests-only"
HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"


@pytest.fixture
def logs():
    """Every log record from every logger, at DEBUG, as text."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    old = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    yield buf
    root.removeHandler(handler)
    root.setLevel(old)


def _target(device_id="", key="k1"):
    return S.Target(key=key, run_id="run_abc", session_id="sess_1", title="Weather for the trip", device_id=device_id)


def _ask_in_thread(inputs, kind="secret", target=None, **kw):
    out: dict = {}

    def run():
        buf = inputs.ask(kind, target or _target(), **kw)
        out["value"] = bytes(buf).decode() if buf is not None else None
        if buf is not None:
            S._wipe(buf)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    wait_for(lambda: inputs.visible("dev1") or inputs.visible(""))
    return t, out


def _body(**kw) -> bytearray:
    return bytearray(json.dumps(kw).encode())


# -- the request registry ----------------------------------------------------------------------

def test_answer_reaches_the_waiting_turn_once_and_the_reply_never_echoes_it():
    inputs = S.SecureInputs()
    t, out = _ask_in_thread(inputs, env_var="OPENWEATHER_API_KEY", prompt="OpenWeather API key",
                            metadata={"skill_name": "weather", "help": "Get one at openweathermap.org"})
    [view] = inputs.visible("dev1")
    assert view["kind"] == "secret" and view["env_var"] == "OPENWEATHER_API_KEY"
    assert view["label"] == "OpenWeather API key" and view["skill"] == "weather"
    assert "OPENWEATHER_API_KEY" in view["stored_where"] and "never sees it" in view["stored_where"]
    raw = _body(value=FAKE_KEY)
    reply = inputs.answer(view["id"], "dev1", raw)
    assert reply == {"ok": True, "outcome": "sent", "stored_as": "OPENWEATHER_API_KEY"}
    assert FAKE_KEY not in json.dumps(reply)
    assert not any(raw), "the request body is zeroed once handed over"
    t.join(5)
    assert out["value"] == FAKE_KEY
    with pytest.raises(S.SecureInputError) as again:
        inputs.answer(view["id"], "dev1", _body(value="second-try-value"))
    assert again.value.status == 404  # single use
    assert inputs.visible("dev1") == []


def test_skip_returns_nothing_and_closes():
    inputs = S.SecureInputs()
    t, out = _ask_in_thread(inputs, env_var="OPENWEATHER_API_KEY")
    [view] = inputs.visible("dev1")
    assert inputs.answer(view["id"], "dev1", _body(action="skip")) == {"ok": True, "outcome": "skipped"}
    t.join(5)
    assert out["value"] is None


def test_requests_expire():
    inputs = S.SecureInputs(timeout=0.3)
    t, out = _ask_in_thread(inputs, env_var="OPENWEATHER_API_KEY")
    view = inputs.visible("dev1")[0]
    t.join(5)
    assert out["value"] is None
    with pytest.raises(S.SecureInputError) as late:
        inputs.answer(view["id"], "dev1", _body(value=FAKE_KEY))
    assert late.value.status == 404


def test_only_the_device_the_job_runs_for_may_answer():
    inputs = S.SecureInputs()
    t, out = _ask_in_thread(inputs, target=_target(device_id="dev1"), env_var="OPENWEATHER_API_KEY")
    assert inputs.visible("dev2") == []  # another paired device doesn't even see it
    [view] = inputs.visible("dev1")
    with pytest.raises(S.SecureInputError) as wrong:
        inputs.answer(view["id"], "dev2", _body(value=FAKE_KEY))
    assert wrong.value.status == 403
    inputs.answer(view["id"], "dev1", _body(value=FAKE_KEY))
    t.join(5)
    assert out["value"] == FAKE_KEY


def test_guessing_ids_is_rate_limited():
    inputs = S.SecureInputs()
    for _ in range(S.RATE_MAX):
        with pytest.raises(S.SecureInputError) as miss:
            inputs.answer("sec_" + "x" * 24, "dev1", _body(value="v"))
        assert miss.value.status == 404
    with pytest.raises(S.SecureInputError) as limited:
        inputs.answer("sec_" + "x" * 24, "dev1", _body(value="v"))
    assert limited.value.status == 429


@pytest.mark.parametrize("body", [b"", b"not json", b'{"value": ""}', b'{"value": 5}', b'{"value": "a", "x": 1}',
                                  b'{"value": "line\\nbreak"}'])
def test_bad_bodies_are_refused_and_still_wiped(body):
    inputs = S.SecureInputs()
    t, out = _ask_in_thread(inputs, env_var="OPENWEATHER_API_KEY")
    view = inputs.visible("dev1")[0]
    raw = bytearray(body)
    with pytest.raises(S.SecureInputError) as bad:
        inputs.answer(view["id"], "dev1", raw)
    assert bad.value.status == 400 and not any(raw)
    inputs.answer(view["id"], "dev1", _body(action="skip"))
    t.join(5)


def test_sudo_card_shows_the_whole_command_and_promises_nothing_is_stored():
    inputs = S.SecureInputs()
    command = "sudo launchctl kickstart -k system/com.openssh.sshd && sudo dscacheutil -flushcache"
    t, out = _ask_in_thread(inputs, kind="sudo", command=command)
    [view] = inputs.visible("dev1")
    assert view["kind"] == "sudo" and view["command"] == command
    assert "never stores" in view["stored_where"] and "env_var" not in view
    inputs.answer(view["id"], "dev1", _body(value=FAKE_PASSWORD))
    t.join(5)
    assert out["value"] == FAKE_PASSWORD


def test_friendly_labels():
    assert S.friendly_label("OPENWEATHER_API_KEY", "Enter value for OPENWEATHER_API_KEY") == "Openweather API key"
    assert S.friendly_label("OPENWEATHER_API_KEY", "OpenWeather API key") == "OpenWeather API key"
    assert S.friendly_label("GITHUB_TOKEN", "") == "Github token"


def test_login_card_takes_username_password_and_an_optional_code():
    inputs = S.SecureInputs()
    t, out = _ask_in_thread(inputs, kind="login", origin="https://example.com", site="example.com")
    [view] = inputs.visible("dev1")
    assert view["kind"] == "login" and view["origin"] == "https://example.com"
    with pytest.raises(S.SecureInputError):
        inputs.answer(view["id"], "dev1", _body(value="just-a-value"))
    with pytest.raises(S.SecureInputError):
        inputs.answer(view["id"], "dev1", _body(identifier="me@example.com", password="pw-123456", code="not a code!"))
    reply = inputs.answer(view["id"], "dev1", _body(identifier="me@example.com", password=FAKE_PASSWORD, code="12-34"))
    assert reply == {"ok": True, "outcome": "sent", "site": "example.com"} and FAKE_PASSWORD not in json.dumps(reply)
    t.join(5)
    assert json.loads(out["value"]) == {"identifier": "me@example.com", "password": FAKE_PASSWORD, "code": "1234"}


def test_a_code_typed_with_the_login_answers_the_next_code_prompt_once():
    inputs = S.SecureInputs()
    S._ACTIVE["inputs"] = inputs
    try:
        inputs.hold_code("k1", "example.com", "654321")
        ask = S.code_callback(_target())
        assert ask("example.com", "") == "654321"
        assert inputs.take_code("k1", "example.com") == ""  # single use
    finally:
        S._ACTIVE["inputs"] = None


# -- redaction (defense in depth) ---------------------------------------------------------------

def test_output_that_echoes_an_entered_value_is_redacted():
    inputs = S.SecureInputs()
    inputs.remember(FAKE_KEY)
    inputs.remember('pa"ss\\word-9')
    text = f"curl -H 'x-api-key: {FAKE_KEY}' … again {FAKE_KEY}."
    assert inputs.scrub(text) == "curl -H 'x-api-key: [redacted]' … again [redacted]."
    escaped = json.dumps({"out": 'got pa"ss\\word-9 ok'})
    assert "[redacted]" in inputs.scrub(escaped) and "word-9" not in inputs.scrub(escaped)
    assert inputs.scrub("nothing to see") == "nothing to see"
    inputs.remember("abc")  # too short to redact without mangling words
    assert inputs.scrub("abc def") == "abc def"


def test_values_from_hermes_are_scrubbed_before_the_plugin_reads_them():
    from speakeasy.hermes_api import HermesAPI
    inputs = S.SecureInputs()
    S._ACTIVE["inputs"] = inputs
    try:
        inputs.remember(FAKE_KEY)
        lines = [b'data: {"event":"tool.completed","preview":"key=' + FAKE_KEY.encode() + b'"}\n',
                 b'data: {"event":"run.completed","output":"done"}\n']

        class Resp:
            def __enter__(self):
                return iter(lines)

            def __exit__(self, *a):
                return False
        api = HermesAPI("http://127.0.0.1:1", lambda: "")
        api.opener = lambda req, timeout=None: Resp()
        seen: list = []
        api.events("run_abc", seen.append)
        assert FAKE_KEY not in json.dumps(seen) and "[redacted]" in seen[0]["preview"]
    finally:
        S._ACTIVE["inputs"] = None


# -- over HTTP, through the service -------------------------------------------------------------

def _second_token(server, service):
    code = service.devices.new_pairing_code()
    status, paired = http(server.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Other phone"})
    assert status == 201
    return paired["token"], paired["device_id"]


def _post_raw(base, path, raw: bytes, token=None):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(base + path, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_secure_card_end_to_end_leaks_the_value_nowhere(server, service, home, hermes, logs):
    hermes.hold = True
    pushes: list = []
    service.push.notify = lambda kind, **kw: pushes.append({"kind": kind, **kw}) or []
    status, started = http(server.base_url, "POST", "/voice/work", {"text": "Check the weather in Lisbon"}, server.token)
    assert status == 202, started
    run_id = wait_for(lambda: (http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"] or [{}])[-1].get("run_id"))
    target = service._secure_target(run_id, "")
    assert target is not None and target.run_id == run_id
    assert service._secure_target("run_not_ours", "") is None
    status, body = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert body["capabilities"]["secret_input"] is True

    out: dict = {}
    t = threading.Thread(target=lambda: out.update(v=service.secure.ask(
        "secret", target, env_var="OPENWEATHER_API_KEY", prompt="OpenWeather API key", metadata={"skill_name": "weather"})),
        daemon=True)
    t.start()
    listing = wait_for(lambda: http(server.base_url, "GET", "/voice/secrets", token=server.token)[1]["requests"])
    [card] = listing
    assert card["job"] and card["env_var"] == "OPENWEATHER_API_KEY"

    assert http(server.base_url, "GET", "/voice/secrets")[0] == 401
    status, _ = _post_raw(server.base_url, f"/voice/secrets/{card['id']}", json.dumps({"value": FAKE_KEY}).encode())
    assert status == 401  # no token
    status, reply = _post_raw(server.base_url, f"/voice/secrets/{card['id']}", json.dumps({"value": FAKE_KEY}).encode(),
                              server.token)
    assert status == 200 and reply["outcome"] == "sent" and FAKE_KEY not in json.dumps(reply)
    t.join(5)
    assert bytes(out["v"]).decode() == FAKE_KEY
    S._wipe(out["v"])
    status, _ = _post_raw(server.base_url, f"/voice/secrets/{card['id']}", json.dumps({"value": FAKE_KEY}).encode(),
                          server.token)
    assert status == 404  # single use

    # The push named the need, never the value.
    assert pushes and pushes[0]["kind"] == "needs_you"
    assert "OpenWeather API key" in pushes[0]["body"] and FAKE_KEY not in json.dumps(pushes)
    # Nothing on disk under the Hermes home (state.sqlite3, devices, logs, journals) holds it.
    for path in home.rglob("*"):
        if path.is_file():
            assert FAKE_KEY.encode() not in path.read_bytes(), path
    # No log line, at any level, from any logger.
    assert FAKE_KEY not in logs.getvalue()
    # No board/work view either.
    status, latest = http(server.base_url, "GET", "/voice/work/latest", token=server.token)
    assert FAKE_KEY not in json.dumps(latest)
    hermes.hold = False


def test_a_second_paired_device_cannot_answer_a_calls_card(server, service):
    other_token, other_id = _second_token(server, service)
    device = service.devices.authenticate(server.token)
    target = S.Target(key="k9", run_id="run_x", session_id=None, title="Job", device_id=device)
    t = threading.Thread(target=lambda: service.secure.ask("secret", target, env_var="OPENWEATHER_API_KEY"), daemon=True)
    t.start()
    [card] = wait_for(lambda: http(server.base_url, "GET", "/voice/secrets", token=server.token)[1]["requests"])
    assert http(server.base_url, "GET", "/voice/secrets", token=other_token)[1]["requests"] == []
    status, body = _post_raw(server.base_url, f"/voice/secrets/{card['id']}", json.dumps({"value": FAKE_KEY}).encode(),
                             other_token)
    assert status == 403 and body["reason"] == "wrong_device"
    _post_raw(server.base_url, f"/voice/secrets/{card['id']}", b'{"action":"skip"}', server.token)
    t.join(5)


def test_call_gets_one_line_and_never_a_request_to_say_it(service):
    """The voice line and its rule: say there's a box, never ask for the value aloud."""
    assert "secure box" in S.VOICE_LINE and "say" not in S.VOICE_LINE.lower()
    assert "Never ask them to say" in S.VOICE_RULE and "do not repeat" in S.VOICE_RULE


# -- Hermes' own code paths (a subprocess with a temp HERMES_HOME; skipped without Hermes) -------

_HERMES_SCRIPT = textwrap.dedent(r'''
    import io, json, logging, os, sys, threading, time
    sys.path.insert(0, os.environ["PLUGIN_DIR"]); sys.path.append(os.environ["HERMES_AGENT"])
    buf = io.StringIO(); h = logging.StreamHandler(buf); logging.getLogger().addHandler(h); logging.getLogger().setLevel(logging.DEBUG)
    from speakeasy import secure_input as S
    from gateway.session_context import set_session_vars
    KEY, PW = os.environ["FAKE_KEY"], os.environ["FAKE_PW"]
    inputs = S.SecureInputs()
    target = S.Target(key="k1", run_id="run_live", session_id="s1", title="Weather", device_id="")
    resolve = lambda session_key, session_id: target if session_key == "run_live" else None
    print("WIRED", json.dumps(S.install(inputs, resolve)))

    def answer(value):
        def go():
            for _ in range(200):
                v = inputs.visible("dev")
                if v:
                    print("VIEW", json.dumps(v[0]))
                    inputs.answer(v[0]["id"], "dev", bytearray(json.dumps({"value": value}).encode())); return
                time.sleep(0.02)
        threading.Thread(target=go, daemon=True).start()

    from tools.skills_tool_setup import _capture_required_environment_variables
    from tools.terminal_tool_sudo import _transform_sudo_command, _get_cached_sudo_password
    from tools import skills_tool

    # Not a Speakeasy run: Hermes behaves exactly as before (the "not supported" hint over api_server).
    set_session_vars(platform="api_server", session_key="run_other", session_id="s9")
    r = _capture_required_environment_variables("weather", [{"name": "OPENWEATHER_API_KEY", "prompt": "OpenWeather API key"}])
    print("OTHER", json.dumps(r))

    # A Speakeasy run: the secure card, then Hermes' own save.
    set_session_vars(platform="api_server", session_key="run_live", session_id="s1")
    answer(KEY)
    r = _capture_required_environment_variables("weather", [{"name": "OPENWEATHER_API_KEY", "prompt": "OpenWeather API key"}])
    print("LIVE", json.dumps(r))
    env = open(os.path.join(os.environ["HERMES_HOME"], ".env")).read()
    print("ENV_HAS_KEY", ("OPENWEATHER_API_KEY=" + KEY) in env)

    # A bound callback (Desktop/Mobile viewer turn) still wins over ours.
    with skills_tool.bind_turn_secret_capture(lambda *a: {"success": True, "skipped": False, "stored_as": a[0]}):
        print("BOUND_WINS", skills_tool._turn_secret_capture.get().__name__ == "<lambda>")

    # sudo: the password goes only to sudo's stdin; allowed once.
    answer(PW)
    cmd, stdin = _transform_sudo_command("sudo true")
    print("SUDO", json.dumps({"cmd": cmd, "stdin_ok": stdin == PW + "\n", "cached": bool(_get_cached_sudo_password())}))
    S.after_tool_call(tool_name="terminal")
    print("SUDO_CACHE_AFTER", bool(_get_cached_sudo_password()))

    # Website login (browser_vault save_login / enter_code): the gate opens only for our runs.
    from agent.vault_backends import unlock
    set_session_vars(platform="api_server", session_key="run_other", session_id="s9")
    print("VAULT_OTHER", json.dumps({"can": unlock.can_prompt_here(), "save": unlock.get_save_login_prompt_callback() is None}))
    set_session_vars(platform="api_server", session_key="run_live", session_id="s1")
    def answer_login():
        def go():
            for _ in range(200):
                v = inputs.visible("dev")
                if v:
                    print("LOGIN_VIEW", json.dumps(v[0]))
                    inputs.answer(v[0]["id"], "dev", bytearray(json.dumps(
                        {"identifier": "fake.user@example.com", "password": PW, "code": "123 456"}).encode())); return
                time.sleep(0.02)
        threading.Thread(target=go, daemon=True).start()
    answer_login()
    can = unlock.can_prompt_here()
    login = unlock.get_save_login_prompt_callback()("https://example.com", "example.com")
    code = unlock.get_code_prompt_callback()("example.com", "")
    print("LOGIN", json.dumps({"can": can, "identifier": login["identifier"], "pw_ok": login["password"] == PW,
                               "code": code, "keys": sorted(login)}))
    from agent.vault_store import get_vault_store
    meta = get_vault_store().add_item("login", "example.com", {"identifier_type": "email", "identifier": login["identifier"],
                                      "password": login["password"]}, origin="https://example.com")
    login.clear()
    listed = [m.__dict__ if hasattr(m, "__dict__") else str(m) for m in get_vault_store().list_items()]
    print("VAULT_LIST_HAS_NO_PW", PW not in json.dumps(listed, default=str))
    vault_files = [os.path.join(dp, f) for dp, _, fs in os.walk(os.environ["HERMES_HOME"]) for f in fs]
    print("VAULT_PLAINTEXT", any(PW.encode() in open(f, "rb").read() for f in vault_files))
    S.uninstall(inputs)
    print("UNINSTALLED", skills_tool._turn_secret_capture.__class__.__name__)
    print("LOG_CLEAN", KEY not in buf.getvalue() and PW not in buf.getvalue())
''')


def test_real_hermes_secret_and_sudo_paths(tmp_path):
    if not (HERMES_AGENT / "tools" / "skills_tool_setup.py").exists():
        pytest.skip("Hermes not installed here")
    home = tmp_path / "hermes"
    home.mkdir()
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1",
           "PLUGIN_DIR": str(Path(__file__).resolve().parents[1]), "HERMES_AGENT": str(HERMES_AGENT),
           "FAKE_KEY": FAKE_KEY, "FAKE_PW": FAKE_PASSWORD, "TMPDIR": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-c", _HERMES_SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    lines = {ln.split(" ", 1)[0]: ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if " " in ln}
    if "WIRED" not in lines:
        pytest.skip(f"Hermes not importable here: {proc.stderr[-400:]}")
    assert json.loads(lines["WIRED"]) == {"secret": True, "sudo": True, "login": True}
    other = json.loads(lines["OTHER"])
    assert other["missing_names"] == ["OPENWEATHER_API_KEY"] and other["gateway_setup_hint"]
    live = json.loads(lines["LIVE"])
    assert live["missing_names"] == [] and live["setup_skipped"] is False
    assert lines["ENV_HAS_KEY"] == "True"
    assert lines["BOUND_WINS"] == "True"
    sudo = json.loads(lines["SUDO"])
    assert sudo["stdin_ok"] is True and "sudo -S" in sudo["cmd"] and FAKE_PASSWORD not in sudo["cmd"]
    assert lines["SUDO_CACHE_AFTER"] == "False"  # allowed once: the next sudo asks again
    assert json.loads(lines["VAULT_OTHER"]) == {"can": False, "save": True}  # not our run: Hermes as before
    login = json.loads(lines["LOGIN"])
    assert login == {"can": True, "identifier": "fake.user@example.com", "pw_ok": True, "code": "123456",
                     "keys": ["identifier", "password"]}
    view = json.loads(lines["LOGIN_VIEW"])
    assert view["kind"] == "login" and view["site"] == "example.com" and "encrypted vault" in view["stored_where"]
    assert lines["VAULT_LIST_HAS_NO_PW"] == "True" and lines["VAULT_PLAINTEXT"] == "False"
    assert lines["UNINSTALLED"] == "ContextVar"
    assert lines["LOG_CLEAN"] == "True"
    # The card view never carried a value; stdout/stderr never printed one.
    assert FAKE_KEY not in proc.stdout.replace(f"OPENWEATHER_API_KEY={FAKE_KEY}", "") and FAKE_PASSWORD not in proc.stdout
    assert FAKE_KEY not in proc.stderr and FAKE_PASSWORD not in proc.stderr


# -- a skill's key: asked for only when the task needs it (invented "tidepool" skill) ----------

TIDE_VAR = "TIDEPOOL_API_TOKEN"
TIDE_META = {"skill_name": "tidepool", "required_for": "Reading tide charts", "help": "Make one at tidepool.example/keys."}


@pytest.fixture
def wired():
    """The bridge as a live Speakeasy job sees it: our inputs, a resolver that names job k1, and the
    note hook registered (what plugin __init__ does)."""
    inputs = S.SecureInputs()
    target = _target(key="k1")
    old = dict(S._ACTIVE)
    S._ACTIVE.update(inputs=inputs, resolve=lambda session_key, session_id: target, notes=True)
    yield inputs, target
    S._ACTIVE.clear()
    S._ACTIVE.update(old)


def _call_in_thread(cb, *args):
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(r=cb(*args)), daemon=True)
    t.start()
    return t, out


def test_opening_a_skill_does_not_pop_the_box_and_tells_the_agent_to_use_another_way(wired):
    inputs, target = wired
    cb = S.secret_callback(target)
    result = cb(TIDE_VAR, "Tidepool API token", TIDE_META)
    assert result["skipped"] is True and result["reason"] == "deferred"
    assert inputs.visible("dev1") == [] and inputs.visible("") == [], "no box just for loading the skill"
    view = json.dumps({"success": True, "name": "tidepool", "setup_needed": True})
    out = json.loads(S.transform_tool_result(tool_name="skill_view", args={"name": "tidepool"}, result=view))
    note = out["speakeasy_note"]
    assert TIDE_VAR in note and "another way" in note and "skill_view for tidepool again" in note
    assert "Never ask them to say" in note
    assert out["setup_needed"] is True and out["name"] == "tidepool"  # Hermes' own fields kept
    # The note is delivered once.
    assert S.transform_tool_result(tool_name="skill_view", args={"name": "tidepool"}, result=view) is None


def test_asking_again_opens_a_box_that_names_the_skill_and_why_and_skip_lets_the_task_carry_on(wired):
    inputs, target = wired
    cb = S.secret_callback(target)
    cb(TIDE_VAR, "Tidepool API token", TIDE_META)
    inputs.take_notes(target.key)
    t, out = _call_in_thread(cb, TIDE_VAR, "Tidepool API token", TIDE_META)
    [card] = wait_for(lambda: inputs.visible("dev1"))
    assert card["skill"] == "tidepool" and card["env_var"] == TIDE_VAR
    assert card["reason"] == ("The Tidepool skill needs this key for reading tide charts. "
                              "Your agent asked for it because this task uses that skill.")
    assert card["skip_label"] == "Skip — the agent has another way"
    assert inputs.answer(card["id"], "dev1", _body(action="skip")) == {"ok": True, "outcome": "skipped"}
    t.join(5)
    assert out["r"]["skipped"] is True
    note = json.loads(S.transform_tool_result(tool_name="skill_view", args={}, result="{}"))["speakeasy_note"]
    assert "skipped" in note and "Carry on with the task" in note and "don't treat the skip as a failure" in note
    # Never asked again in this job: no box, the same carry-on note.
    again = cb(TIDE_VAR, "Tidepool API token", TIDE_META)
    assert again["skipped"] is True and again["reason"] == "skipped_earlier"
    assert inputs.visible("dev1") == []
    assert "Carry on" in json.loads(S.transform_tool_result(tool_name="skill_view", result="{}"))["speakeasy_note"]


def test_a_key_typed_on_the_second_ask_is_saved_and_the_next_job_starts_fresh(wired, monkeypatch):
    inputs, target = wired
    saved: dict = {}
    import types
    monkeypatch.setitem(sys.modules, "hermes_cli.config", types.SimpleNamespace(
        save_env_value_secure=lambda name, value: saved.update({name: value}) or {"success": True, "stored_as": name}))
    cb = S.secret_callback(target)
    cb(TIDE_VAR, "Tidepool API token", TIDE_META)
    t, out = _call_in_thread(cb, TIDE_VAR, "Tidepool API token", TIDE_META)
    [card] = wait_for(lambda: inputs.visible("dev1"))
    inputs.answer(card["id"], "dev1", _body(value=FAKE_KEY))
    t.join(5)
    assert out["r"]["skipped"] is False and saved == {TIDE_VAR: FAKE_KEY}
    assert FAKE_KEY not in json.dumps(out["r"])
    # Another job opening the same skill is deferred again (the decision is per job).
    other = S.secret_callback(_target(key="k2"))
    assert other(TIDE_VAR, "Tidepool API token", TIDE_META)["reason"] == "deferred"


def test_an_unanswered_box_is_not_reopened_and_the_agent_is_told(wired):
    inputs, target = wired
    inputs.timeout = 0.3
    cb = S.secret_callback(target)
    cb(TIDE_VAR, "Tidepool API token", TIDE_META)
    inputs.take_notes(target.key)
    assert cb(TIDE_VAR, "Tidepool API token", TIDE_META)["skipped"] is True  # expired
    note = json.loads(S.transform_tool_result(tool_name="skill_view", result="{}"))["speakeasy_note"]
    assert "didn't answer" in note
    assert cb(TIDE_VAR, "Tidepool API token", TIDE_META)["reason"] == "skipped_earlier"


def test_without_the_note_hook_the_box_opens_as_before(wired):
    inputs, target = wired
    S._ACTIVE["notes"] = False  # an older Hermes with no transform_tool_result: never hide the need
    t, out = _call_in_thread(S.secret_callback(target), TIDE_VAR, "Tidepool API token", TIDE_META)
    [card] = wait_for(lambda: inputs.visible("dev1"))
    inputs.answer(card["id"], "dev1", _body(action="skip"))
    t.join(5)
    assert out["r"]["skipped"] is True


def test_the_note_hook_leaves_other_tools_and_other_turns_alone(wired):
    inputs, target = wired
    S.secret_callback(target)(TIDE_VAR, "Tidepool API token", TIDE_META)
    assert S.transform_tool_result(tool_name="terminal", args={}, result='{"output": "ok"}') is None
    S._ACTIVE["resolve"] = lambda session_key, session_id: None   # not a Speakeasy job
    assert S.transform_tool_result(tool_name="skill_view", args={}, result="{}") is None
    S._ACTIVE["resolve"] = lambda session_key, session_id: target
    plain = S.transform_tool_result(tool_name="skill_view", args={}, result="not json")
    assert plain.startswith("not json") and "[Speakeasy]" in plain


def test_reason_reads_well_without_details():
    assert S.secret_reason("") == ("A skill your agent is using needs this key. "
                                   "Your agent asked for it because this task uses that skill.")
    assert S.secret_reason("tide-pool_charts").startswith("The Tide pool charts skill needs this key.")


def test_plugin_registers_the_note_hook():
    import speakeasy
    hooks: dict = {}

    class Ctx:
        def register_hook(self, name, fn):
            hooks[name] = fn
    old = S._ACTIVE.get("notes")
    try:
        S._ACTIVE["notes"] = False
        speakeasy._register_secure_input_hook(Ctx())
        assert hooks["transform_tool_result"] is S.transform_tool_result and S._ACTIVE["notes"] is True
        assert hooks["post_tool_call"] is S.after_tool_call
    finally:
        S._ACTIVE["notes"] = old


_SKILL_SCRIPT = textwrap.dedent(r'''
    import json, os, sys, threading, time
    sys.path.insert(0, os.environ["PLUGIN_DIR"]); sys.path.append(os.environ["HERMES_AGENT"])
    from speakeasy import secure_input as S
    from gateway.session_context import set_session_vars
    inputs = S.SecureInputs()
    target = S.Target(key="k1", run_id="run_live", session_id="s1", title="Tide times", device_id="")
    S.install(inputs, lambda session_key, session_id: target if session_key == "run_live" else None)
    S._ACTIVE["notes"] = True
    set_session_vars(platform="api_server", session_key="run_live", session_id="s1")
    from tools.skills_tool import skill_view

    def view():
        raw = skill_view("tidepool")
        return json.loads(S.transform_tool_result(tool_name="skill_view", args={"name": "tidepool"}, result=raw) or raw)

    first = view()
    print("FIRST", json.dumps({"boxes": len(inputs.visible("")), "note": first.get("speakeasy_note", ""),
                               "setup_needed": first.get("setup_needed"), "has_content": bool(first.get("content"))}))
    seen = {}
    def skip():
        for _ in range(300):
            v = inputs.visible("")
            if v:
                seen.update(v[0]); inputs.answer(v[0]["id"], "", bytearray(b'{"action":"skip"}')); return
            time.sleep(0.02)
    threading.Thread(target=skip, daemon=True).start()
    second = view()
    print("SECOND", json.dumps({"card": seen, "note": second.get("speakeasy_note", "")}))
    third = view()
    print("THIRD", json.dumps({"boxes": len(inputs.visible("")), "note": third.get("speakeasy_note", "")}))
    S.uninstall(inputs)
''')


def test_real_hermes_skill_view_asks_only_when_needed_and_skip_carries_on(tmp_path):
    if not (HERMES_AGENT / "tools" / "skills_tool_setup.py").exists():
        pytest.skip("Hermes not installed here")
    home = tmp_path / "hermes"
    skill = home / "skills" / "tidepool"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(textwrap.dedent("""\
        ---
        name: tidepool
        description: Read tide charts from an invented tide service.
        required_environment_variables:
          - name: TIDEPOOL_API_TOKEN
            prompt: Tidepool API token
            required_for: reading tide charts
        ---
        # Tidepool
        Call the tide service with the token.
        """))
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1",
           "PLUGIN_DIR": str(Path(__file__).resolve().parents[1]), "HERMES_AGENT": str(HERMES_AGENT), "TMPDIR": str(tmp_path)}
    proc = subprocess.run([sys.executable, "-c", _SKILL_SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    lines = {ln.split(" ", 1)[0]: ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if " " in ln}
    if "FIRST" not in lines:
        pytest.skip(f"Hermes not importable here: {proc.stderr[-400:]}")
    first = json.loads(lines["FIRST"])
    assert first["boxes"] == 0 and first["has_content"] is True, "the skill still loads, with no box"
    assert "TIDEPOOL_API_TOKEN" in first["note"] and "another way" in first["note"]
    second = json.loads(lines["SECOND"])
    assert second["card"]["skill"] == "tidepool" and "reading tide charts" in second["card"]["reason"]
    assert second["card"]["skip_label"] == "Skip — the agent has another way"
    assert "Carry on with the task" in second["note"]
    third = json.loads(lines["THIRD"])
    assert third["boxes"] == 0 and "Carry on" in third["note"]
