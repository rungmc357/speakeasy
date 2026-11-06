"""Server routes, pairing/auth, settings, status, brief, onboarding, destinations — over real HTTP."""
from __future__ import annotations

import json
import os
import stat

from fakes import SDP, http, wait_for

BRIEF = """# User
They are a product designer who likes short answers and works mostly from their laptop.

# Assistant persona
Calm, dry and precise. Uses the name the user chose.

# Capability map
Email and calendar through Hermes tools; web search; Telegram delivery; notes in the wiki.

# Answer preferences
Lead with the answer. Say numbers plainly. No filler or restating the question.

# Current context
Planning a trip next month and finishing a portfolio refresh this week.
"""


def test_health_needs_no_auth_and_leaks_nothing(server):
    status, body = http(server.base_url, "GET", "/health")
    assert status == 200 and body["ok"] is True and body["platform"] == "voice"
    assert "token" not in json.dumps(body).lower()


def test_pairing_is_single_use_and_tokens_are_hashed(server, service, home):
    code = service.devices.new_pairing_code()
    status, first = http(server.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Desk"})
    assert status == 201 and first["token"] and first["device_id"]
    status, _ = http(server.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Desk"})
    assert status == 403
    raw = (home / "speakeasy" / "devices.json").read_text()
    assert first["token"] not in raw
    assert stat.S_IMODE(os.stat(home / "speakeasy" / "devices.json").st_mode) == 0o600


def test_wrong_pairing_codes_burn_the_pending_code(home):
    from speakeasy.devices import MAX_WRONG_CODES, DeviceStore
    store = DeviceStore(home)
    code = store.new_pairing_code()
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(MAX_WRONG_CODES - 1):
        assert store.redeem(wrong, "Guess") is None
    assert store.redeem(code, "Mac") is not None  # a few typos never lock the real user out
    code = store.new_pairing_code()
    for _ in range(MAX_WRONG_CODES):
        assert store.redeem(wrong, "Guess") is None
    assert store.redeem(code, "Mac") is None  # guessed at too often: it no longer pairs
    assert store.redeem(store.new_pairing_code(), "Mac") is not None  # a new code works again


def test_auth_does_not_rewrite_the_device_file_on_every_request(home):
    from speakeasy.devices import SEEN_WRITE_S, DeviceStore
    store = DeviceStore(home)
    device_id, token = store.redeem(store.new_pairing_code(), "Mac", now=1000)
    assert store.authenticate(token, now=1000) == device_id
    first = store.path.stat().st_mtime_ns
    code = DeviceStore(home).new_pairing_code()  # the CLI, in another process, makes a new code
    after_cli = store.path.stat().st_mtime_ns
    assert store.authenticate(token, now=1000 + SEEN_WRITE_S / 2) == device_id
    assert store.path.stat().st_mtime_ns == after_cli and after_cli >= first
    assert store.authenticate(token, now=1000 + SEEN_WRITE_S) == device_id
    assert store.devices()[0].last_seen_at == 1000 + SEEN_WRITE_S
    assert store.redeem(code, "Phone") is not None


def test_routes_require_device_token(server, service):
    for method, path in (("GET", "/voice/status"), ("GET", "/voice/work/latest"), ("GET", "/voice/settings"),
                         ("POST", "/voice/sessions"), ("GET", "/voice/brief"), ("GET", "/voice/destinations")):
        assert http(server.base_url, method, path, {} if method == "POST" else None)[0] == 401
        assert http(server.base_url, method, path, {} if method == "POST" else None, "wrong-token")[0] == 401
    device_id = service.devices.devices()[0].id
    service.devices.revoke(device_id)
    assert http(server.base_url, "GET", "/voice/status", token=server.token)[0] == 401


def test_settings_patch_validates_and_never_holds_keys(server, home):
    status, body = http(server.base_url, "PATCH", "/voice/settings",
                        {"assistant_name": "Nova", "user_name": "Sam", "delivery": {"target": "telegram"}},
                        server.token)
    assert status == 200 and body["settings"]["assistant_name"] == "Nova"
    assert http(server.base_url, "PATCH", "/voice/settings", {"voice": {"provider": "other"}}, server.token)[0] == 400
    assert http(server.base_url, "PATCH", "/voice/settings", {"delivery": {"target": "bad target!"}}, server.token)[0] == 400
    assert http(server.base_url, "PATCH", "/voice/settings", {"openai_api_key": "x"}, server.token)[0] == 400
    path = home / "speakeasy" / "settings.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "test-openai-key-not-real" not in path.read_text()


def test_status_reports_readiness_without_secrets(server):
    status, body = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert status == 200
    for key in ("provider", "voice_ready", "codex_found", "codex_signed_in", "api_key_set", "brief_state",
                "hermes_api_ok", "threads_supported", "delivery_target", "continuity_enabled"):
        assert key in body
    assert body["hermes_api_ok"] is True and body["api_key_set"] is True
    dumped = json.dumps(body)
    assert "test-openai-key-not-real" not in dumped and "test-api-server-key-not-real" not in dumped


def test_brief_put_get_and_rejects_secrets(server, service):
    status, body = http(server.base_url, "PUT", "/voice/brief", {"brief": BRIEF}, server.token)
    assert status == 200 and body["state"] == "edited" and body["auto_refresh"] is False
    assert http(server.base_url, "GET", "/voice/brief", token=server.token)[1]["brief"].startswith("# User")
    leaky = BRIEF + "\napi_key: " + "sk" + "-" + "x" * 24 + "\n"  # built at runtime: keeps the repo scan clean
    assert http(server.base_url, "PUT", "/voice/brief", {"brief": leaky}, server.token)[0] == 422
    assert http(server.base_url, "PUT", "/voice/brief", {"brief": "# User\nshort"}, server.token)[0] == 422
    # the brief lands in the voice instructions; the rules come first and work without it
    text = service.instructions(away=[], resume="")
    assert "Calm, dry and precise" in text and text.index("# Delegation policy") < text.index("Calm, dry")


def test_brief_rewrite_runs_through_hermes(server, service, hermes):
    hermes.responder = lambda prompt, sid: BRIEF
    status, body = http(server.base_url, "POST", "/voice/brief/rewrite", {}, server.token)
    assert status == 202
    wait_for(lambda: service.brief.status()["state"] == "ready")
    assert "voice brief" in hermes.calls[-1]["input"].lower()
    assert service.brief.text().startswith("# User")


def test_onboarding_steps(server, service):
    status, body = http(server.base_url, "GET", "/voice/onboarding", token=server.token)
    assert status == 200
    assert set(body["steps"]) >= {"paired", "codex_signed_in", "names_set", "delivery_set", "brief_ready"}
    assert body["steps"]["paired"] is True and body["steps"]["names_set"] is False
    status, body = http(server.base_url, "POST", "/voice/onboarding",
                        {"assistant_name": "Nova", "user_name": "Sam", "delivery_target": "none"}, server.token)
    assert status == 200 and body["steps"]["names_set"] is True and body["steps"]["delivery_set"] is True


def test_destinations_reads_gateway_without_secrets(server, home):
    (home / "gateway_state.json").write_text(json.dumps({"platforms": {
        "telegram": {"state": "connected"}, "api_server": {"state": "connected"}}}))
    (home / "config.yaml").write_text("platforms:\n  telegram:\n    token: not-a-real-token\n"
                                      "    home_channel: {chat_id: '12345', name: Home}\n")
    status, body = http(server.base_url, "GET", "/voice/destinations", token=server.token)
    assert status == 200
    names = [d["platform"] for d in body["destinations"]]
    assert names == ["telegram"]
    assert body["destinations"][0]["home_channel"]["target"] == "telegram:12345"
    assert "not-a-real-token" not in json.dumps(body)
    assert "threads_supported" in body


def test_session_idempotency_and_snapshot_shape(server, service):
    headers = {"Idempotency-Key": "req_same_1"}
    s1, a = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token, headers)
    s2, b = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token, headers)
    assert s1 == 201 and s2 == 201 and a["interaction_id"] == b["interaction_id"]
    assert len(service.workers) == 1
    status, snap = http(server.base_url, "GET", f"/voice/interactions/{a['interaction_id']}", token=server.token)
    assert status == 200
    for key in ("interaction_id", "status", "run_id", "backend_run_id", "approval", "finalization", "paused",
                "resumed_from"):
        assert key in snap
    assert http(server.base_url, "POST", "/voice/sessions", {"sdp": "not sdp"}, server.token,
                {"Idempotency-Key": "req_bad_sdp"})[0] == 400


def test_sse_stream_emits_initial_state(server):
    import urllib.request
    _, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                      {"Idempotency-Key": "req_sse_1"})
    req = urllib.request.Request(f"{server.base_url}/voice/interactions/{session['interaction_id']}/events")
    req.add_header("Authorization", f"Bearer {server.token}")
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.headers["Content-Type"].startswith("text/event-stream")
        seen = b""
        while b"\n\n" not in seen:
            seen += r.readline()
    assert b"event: snapshot" in seen and b'"interaction"' in seen and b'"email_drafts"' in seen


def test_destinations_skip_chats_another_profile_answers(server, home):
    # Two #research channels: one answered here, one routed to a different Hermes profile.
    (home / "gateway_state.json").write_text(json.dumps({"platforms": {"discord": {"state": "connected"}}}))
    (home / "config.yaml").write_text(
        "gateway:\n  profile_routes:\n    - {name: other-research, platform: discord, profile: other, chat_id: '222'}\n"
        "    - {name: mine, platform: discord, profile: default, chat_id: '333'}\n")
    (home / "channel_directory.json").write_text(json.dumps({"platforms": {"discord": [
        {"id": "111", "name": "research", "guild": "Server", "type": "group"},
        {"id": "222", "name": "research", "guild": "Server", "type": "group"},
        {"id": "333", "name": "build", "guild": "Server", "type": "group"}]}}))
    status, body = http(server.base_url, "GET", "/voice/destinations", token=server.token)
    assert status == 200
    targets = [c["target"] for d in body["destinations"] for c in d["chats"]]
    assert "discord:111" in targets and "discord:333" in targets
    assert "discord:222" not in targets


def test_a_failing_settings_or_brief_write_answers_with_a_json_500(server, service, monkeypatch):
    """PATCH and PUT had no catch-all: an unexpected error dropped the connection, which the app
    shows as "can't reach Hermes" instead of the server's error."""
    def boom(_body):
        raise RuntimeError("disk full")
    monkeypatch.setattr(service, "patch_settings", boom)
    monkeypatch.setattr(service, "put_brief", boom)
    status, body = http(server.base_url, "PATCH", "/voice/settings", {"assistant_name": "Nova"}, server.token)
    assert status == 500 and body == {"error": "Your Hermes hit a problem with that. Try again."}
    status, body = http(server.base_url, "PUT", "/voice/brief", {"text": "x"}, server.token)
    assert status == 500 and "disk full" not in json.dumps(body)
