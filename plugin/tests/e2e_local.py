"""Loads the plugin through Hermes' real plugin manager in a temp HERMES_HOME, starts the voice
platform adapter (the HTTP server inside the gateway), and drives /voice/* end to end against a
fake Hermes API server. No model calls, no network beyond loopback, no paid calls.

Run from the Hermes checkout:
    cd ~/.hermes/hermes-agent && venv/bin/python <plugin>/tests/e2e_local.py
Exits non-zero on any failed check.
"""
import asyncio
import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PLUGIN_SRC = TESTS.parent / "speakeasy"
sys.path.insert(0, str(TESTS))

home = Path(tempfile.mkdtemp(prefix="speakeasy-home-"))
os.environ["HERMES_HOME"] = str(home)
shutil.copytree(PLUGIN_SRC, home / "plugins/speakeasy", ignore=shutil.ignore_patterns("__pycache__"))
(home / "config.yaml").write_text(  # what `hermes plugins enable` writes, plus a Telegram home channel
    "plugins:\n  enabled:\n    - speakeasy\n"
    "platforms:\n  telegram:\n    enabled: true\n    home_channel:\n      platform: telegram\n"
    "      chat_id: '555000111'\n      name: Sam\n"
    "  webhook:\n    enabled: true\n    extra:\n      host: 127.0.0.1\n      port: 8644\n")
# A gateway that has Telegram and Discord connected and has seen a few chats (no real IDs).
(home / "channel_directory.json").write_text(json.dumps({"platforms": {
    "telegram": [{"id": "555000111", "name": "Sam", "type": "dm"}],
    "discord": [{"id": "9000000000001", "name": "general", "guild": "Home", "type": "channel"},
                {"id": "9000000000002", "name": "a thread", "guild": "Home", "type": "thread"}]}}))

from fakes import FAKE_API_KEY, SDP, FakeHermesServer  # noqa: E402

hermes = FakeHermesServer()
(home / ".env").write_text(f"API_SERVER_KEY={FAKE_API_KEY}\nAPI_SERVER_PORT={hermes.httpd.server_address[1]}\n")

failures: list[str] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + (f"  ({detail})" if detail != "" and not ok else ""))
    if not ok:
        failures.append(label)


from hermes_cli import plugins as P  # noqa: E402

P.discover_plugins(force=True)
mgr = P.get_plugin_manager()
check("platform 'voice' registered", "voice" in mgr._plugin_platform_names)
check("cli 'hermes voice' registered", "voice" in mgr._cli_commands)
if "voice" not in mgr._plugin_platform_names:
    sys.exit(1)

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platform_registry import platform_registry  # noqa: E402

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
BASE = f"http://127.0.0.1:{PORT}"
adapter = platform_registry.get("voice").adapter_factory(PlatformConfig(enabled=True, extra={"port": PORT}))


def call(method: str, path: str, body=None, token=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {token}"} if token else {}), **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = pred()
        if value:
            return value
        time.sleep(0.1)
    return None


async def main() -> None:
    check("adapter connects (HTTP server starts inside the gateway)", await adapter.connect())
    loop = asyncio.get_running_loop()

    async def run(*a, **k):
        return await loop.run_in_executor(None, lambda: call(*a, **k))

    status, health = await run("GET", "/health")
    check("GET /health without auth", status == 200 and health.get("platform") == "voice", health)
    check("POST /voice/sessions without token is 401", (await run("POST", "/voice/sessions", {"sdp": SDP}))[0] == 401)
    check("pair with wrong code is 403",
          (await run("POST", "/voice/pair", {"code": "000000", "device_name": "Test Mac"}))[0] == 403)
    code = adapter.service.devices.new_pairing_code()
    status, paired = await run("POST", "/voice/pair", {"code": code, "device_name": "Test Mac"})
    check("pair", status == 201 and {"device_id", "token"} <= set(paired), paired)
    check("pairing code is single use",
          (await run("POST", "/voice/pair", {"code": code, "device_name": "x"}))[0] == 403)
    token = paired["token"]
    stored = (home / "speakeasy/devices.json").read_text()
    check("device token not stored in plain text", token not in stored)
    check("devices.json is 0600", (home / "speakeasy/devices.json").stat().st_mode & 0o777 == 0o600)

    status, st = await run("GET", "/voice/status", token=token)
    check("GET /voice/status", status == 200 and st["hermes_api_ok"] is True and "threads_supported" in st, st)
    check("status advertises listening mode's room text", st.get("room_listening") is True, st)
    check("status never returns keys", FAKE_API_KEY not in json.dumps(st))
    status, s = await run("PATCH", "/voice/settings", {"assistant_name": "Nova", "user_name": "Sam"}, token)
    check("PATCH /voice/settings", status == 200 and s["settings"]["assistant_name"] == "Nova", s)
    check("settings.json is 0600", (home / "speakeasy/settings.json").stat().st_mode & 0o777 == 0o600)
    # The running gateway rewrites gateway_state.json on connect, so state is set here.
    (home / "gateway_state.json").write_text(json.dumps({"pid": 0, "platforms": {
        "telegram": {"state": "connected"}, "discord": {"state": "connected"}, "api_server": {"state": "connected"}}}))
    code_, dest = await run("GET", "/voice/destinations", token=token)
    names = sorted(d["platform"] for d in dest.get("destinations", []))
    check("GET /voice/destinations lists connected chats and suggests the home channel",
          code_ == 200 and names == ["discord", "telegram"] and dest.get("suggested") == "telegram:555000111", dest)
    status, ob = await run("GET", "/voice/onboarding", token=token)
    check("GET /voice/onboarding", status == 200 and ob["steps"]["paired"] is True, ob)
    check("GET /voice/work/latest (empty)", (await run("GET", "/voice/work/latest", token=token))[0] == 200)
    # Contract fixtures: real server replies, parsed by the Mac app's tests (SPEAKEASY_CONTRACT_DIR).
    if os.environ.get("SPEAKEASY_CONTRACT_DIR"):
        out = Path(os.environ["SPEAKEASY_CONTRACT_DIR"]); out.mkdir(parents=True, exist_ok=True)
        for name, path in [("status", "/voice/status"), ("settings", "/voice/settings"),
                           ("onboarding", "/voice/onboarding"), ("destinations", "/voice/destinations"),
                           ("brief", "/voice/brief")]:
            code_, body_ = await run("GET", path, token=token)
            (out / f"{name}.json").write_text(json.dumps(body_))
        code_, body_ = await run("POST", "/voice/onboarding", {"assistant_name": "Nova", "user_name": "Sam",
                                                               "delivery_target": "none", "write_brief": False}, token)
        check("POST /voice/onboarding with the app's body", code_ == 200, body_)
        (out / "onboarding_post.json").write_text(json.dumps(body_))
    # Home control with a fake Home Assistant (no real one is contacted).
    from test_home_control import FakeHA
    from speakeasy.home_control import HomeControl
    fake_ha = FakeHA()
    svc = adapter.service
    svc.home_control = HomeControl(svc.settings.get, lambda: ("http://ha.test:8123", "fake-token"),
                                   plan_call=lambda m: None, client_factory=lambda url, token: fake_ha)
    svc.rt.home = svc.home_control
    code_, home_found = await run("GET", "/voice/home", token=token)
    check("GET /voice/home finds Home Assistant", code_ == 200 and home_found.get("available") is True, home_found)
    code_, home_on = await run("PUT", "/voice/home", {"enabled": True}, token)
    check("PUT /voice/home turns it on with the default picks", code_ == 200 and home_on.get("enabled") is True, home_on)
    if os.environ.get("SPEAKEASY_CONTRACT_DIR"):
        out = Path(os.environ["SPEAKEASY_CONTRACT_DIR"])
        (out / "home.json").write_text(json.dumps(home_found))
        (out / "home_put.json").write_text(json.dumps(home_on))
    # Requests the Mac app sends, recorded by its tests (mac/Tests/.../Contract/requests/*.json).
    requests_dir = Path(__file__).resolve().parents[2] / "mac/Tests/SpeakeasyCoreTests/Contract/requests"
    for req_file in sorted(requests_dir.glob("*.json")) if requests_dir.is_dir() else []:
        spec = json.loads(req_file.read_text())
        if spec["path"] == "/voice/sessions":
            continue  # starts a call: replayed with the call below (201, needs an Idempotency-Key)
        code_, body_ = await run(spec["method"], spec["path"], spec["body"], token)
        check(f"app request accepted: {req_file.stem}", code_ == 200, body_)

    # A call on the OpenAI-key provider with a fake negotiation: the session is created, and the
    # handoff path is exercised through the service's worker (no provider connection is made).
    adapter.service._openai_negotiate = lambda key, payload: {"session": {"id": "sess_e2e"}, "transport": {"sdp": "v=0\r\n"}}
    from fakes import FakeLiveWorker
    workers = []
    adapter.service._openai_worker = lambda rt, interaction: workers.append(FakeLiveWorker(rt, interaction)) or workers[-1]
    (home / ".env").write_text((home / ".env").read_text() + "SPEAKEASY_OPENAI_API_KEY=e2e-fake-key-not-real\n")
    await run("PATCH", "/voice/settings", {"voice": {"provider": "openai"}}, token)
    status, session = await run("POST", "/voice/sessions", {"sdp": SDP}, token, {"Idempotency-Key": "req_e2e_1"})
    check("POST /voice/sessions", status == 201 and session.get("interaction_id"), session)
    if status == 201:
        draft = {"from": "me@example.com", "to": ["pat@example.org"], "cc": [], "bcc": [],
                 "subject": "Friday", "body": "Still on for Friday?"}
        hermes.responder = lambda prompt, sid: (f"Drafted.\n```email-draft\n{json.dumps(draft)}\n```\n"
                                                "SPOKEN: I drafted it — take a look and approve when ready.")
        workers[-1].delegate("call_e2e_1", "Email Pat about Friday")
        tasks = await loop.run_in_executor(None, lambda: wait(lambda: (lambda t: t if t and t[0].get("email_drafts") else None)(
            call("GET", "/voice/work/latest", token=token)[1].get("tasks"))))
        check("task finished with an email draft card", bool(tasks), tasks)
        if tasks:
            d = tasks[0]["email_drafts"][0]
            check("wrong sha256 is 409", (await run("POST", f"/voice/drafts/{d['draft_id']}",
                                                     {"action": "approve", "sha256": "0" * 64}, token))[0] == 409)
            before = len(hermes.calls)
            hermes.responder = lambda prompt, sid: "Sent.\nSPOKEN: Sent it."
            a1 = await run("POST", f"/voice/drafts/{d['draft_id']}", {"action": "approve", "sha256": d["sha256"]}, token)
            a2 = await run("POST", f"/voice/drafts/{d['draft_id']}", {"action": "approve", "sha256": d["sha256"]}, token)
            check("approve is idempotent", a1[0] == 200 and a2[0] == 200, (a1, a2))
            await asyncio.sleep(1)
            check("approved draft reached Hermes exactly once", len(hermes.calls) - before == 1, len(hermes.calls) - before)
        status, _ = await run("POST", f"/voice/interactions/{session['interaction_id']}/end", {}, token)
        check("POST /voice/interactions/{id}/end", status == 200)
    # Listening mode: the session body the Mac app sends when turning listening off starts a call.
    room_request = requests_dir / "session-with-room.json"
    if room_request.is_file():
        spec = json.loads(room_request.read_text())
        code_, body_ = await run(spec["method"], spec["path"], spec["body"], token, {"Idempotency-Key": "req_e2e_room"})
        check("app request accepted: session-with-room", code_ == 201 and bool(body_.get("interaction_id")), body_)
        if code_ == 201:
            status, _ = await run("POST", f"/voice/interactions/{body_['interaction_id']}/end", {}, token)
            check("POST /voice/interactions/{id}/end (room call)", status == 200)

    adapter.service.devices.revoke(paired["device_id"])
    check("token rejected after revoke", (await run("GET", "/voice/status", token=token))[0] == 401)
    await adapter.disconnect()
    try:
        call("GET", "/health")
        check("server stopped on disconnect", False)
    except (urllib.error.URLError, ConnectionError, OSError):
        check("server stopped on disconnect", True)


try:
    asyncio.run(main())
finally:
    hermes.close()
    shutil.rmtree(home, ignore_errors=True)
print("e2e_local:", "OK" if not failures else f"{len(failures)} FAILED")
sys.exit(1 if failures else 0)
