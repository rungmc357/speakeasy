"""A fake `codex` binary for sign-in tests: never talks to OpenAI, never touches a real login.

`make_fake_codex(dir, codex_home)` writes an executable `codex` that answers:
- `--version`: codex-cli <version> (from <dir>/version, default 0.163.0)
- `login status`: from $CODEX_HOME/fake_state (chatgpt | api_key | none)
- `app-server --stdio`: initialize, then `account/login/start {"type": "chatgptDeviceCode"}` returns a
  device URL + code; after a delay it writes fake_state=chatgpt and sends `account/login/completed`.
  $CODEX_HOME/fake_mode picks the behaviour: ok | fail | hang | badurl | badcode | exit | error.
$CODEX_HOME/fake_log records every app-server request method and argv (for assertions).
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

DEVICE_URL = "https://auth.openai.com/codex/device"
USER_CODE = "WXYZ-12345"

SCRIPT = r'''
import json, os, sys, time
from pathlib import Path
home = Path(os.environ.get("CODEX_HOME") or "/nonexistent")
here = Path(__file__).resolve().parent
def read(p, d):
    try:
        return p.read_text().strip() or d
    except OSError:
        return d
def log(line):
    with open(home / "fake_log", "a") as f:
        f.write(line + "\n")
# A copy with its own fake_state file next to it has its own sign-in (two copies, two states).
state_file = here / "fake_state" if (here / "fake_state").exists() else home / "fake_state"
args = sys.argv[1:]
log("argv " + " ".join(args))
if args[:1] == ["--version"]:
    print("codex-cli " + read(here / "version", "0.163.0")); sys.exit(0)
if args[:2] == ["login", "status"]:
    state = read(state_file, "none")
    if state == "chatgpt":
        print("Logged in using ChatGPT"); sys.exit(0)
    if state == "api_key":
        print("Logged in using an API key - sk-ab***xy"); sys.exit(0)
    print("Not logged in"); sys.exit(1)
if args[:1] != ["app-server"]:
    sys.exit(2)
mode = read(home / "fake_mode", "ok")
delay = float(read(home / "fake_delay", "0.4"))
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    log("method " + str(method) + " " + json.dumps(msg.get("params")))
    if method == "initialize":
        send({"id": msg["id"], "result": {"userAgent": "fake"}})
    elif method == "account/login/start":
        if mode == "error":
            send({"id": msg["id"], "error": {"code": -1, "message": "device code login is not enabled for this Codex server"}}); continue
        if mode == "exit":
            sys.exit(1)
        url = "https://evil.example.com/device" if mode == "badurl" else "''' + DEVICE_URL + r'''"
        code = "<script>" if mode == "badcode" else "''' + USER_CODE + r'''"
        send({"id": msg["id"], "result": {"type": "chatgptDeviceCode", "loginId": "login-1",
                                           "verificationUrl": url, "userCode": code}})
        if mode in ("hang", "badurl", "badcode"):
            continue
        time.sleep(delay)
        if mode == "fail":
            send({"method": "account/login/completed", "params": {"loginId": "login-1", "success": False,
                                                                  "error": "access_denied by user"}})
        else:
            state_file.write_text("chatgpt")
            send({"method": "account/login/completed", "params": {"loginId": "login-1", "success": True}})
    elif method == "account/login/cancel":
        send({"id": msg["id"], "result": {"status": "canceled"}})
'''


def make_fake_codex(directory: Path, codex_home: Path, *, state: str = "none", mode: str = "ok",
                    version: str = "0.163.0", delay: float = 0.4, own_state: str | None = None) -> Path:
    """``own_state`` gives this copy a sign-in of its own (else it reads $CODEX_HOME/fake_state)."""
    directory.mkdir(parents=True, exist_ok=True)
    codex_home.mkdir(parents=True, exist_ok=True)
    path = directory / "codex"
    path.write_text(f"#!{sys.executable}\n" + SCRIPT)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (directory / "version").write_text(version)
    if own_state is not None:
        (directory / "fake_state").write_text(own_state)
    (codex_home / "fake_state").write_text(state)
    (codex_home / "fake_mode").write_text(mode)
    (codex_home / "fake_delay").write_text(str(delay))
    return path


def fake_log(codex_home: Path) -> str:
    try:
        return (codex_home / "fake_log").read_text()
    except OSError:
        return ""


assert os  # keep the import for callers that patch the environment
