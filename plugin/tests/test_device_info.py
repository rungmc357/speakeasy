"""Each paired device's platform, OS and app build: the X-Speakeasy-Client header, kept per device,
listed by `hermes voice devices` and copied into the call log, so a call can be matched to its build."""
from __future__ import annotations

import json

from fakes import SDP, http, wait_for
from speakeasy import cli
from speakeasy.devices import CLIENT_HEADER, SEEN_WRITE_S, DeviceStore, client_label, parse_client
from speakeasy.tune import LOG_FILE

PHONE = "iOS 27.0; app 1.4 (60); iPhone"
PHONE_FIELDS = {"platform": "iOS", "os_version": "27.0", "app_version": "1.4", "build": "60", "model": "iPhone"}


def test_the_header_parses_known_shapes_and_ignores_anything_else():
    assert parse_client(PHONE) == PHONE_FIELDS
    assert parse_client("macOS 27.0.1; app 0.3.15 (52); Mac")["os_version"] == "27.0.1"
    assert parse_client("visionOS 3.0; app 1.4 (60); Apple Vision Pro")["model"] == "Apple Vision Pro"
    assert "model" not in parse_client("iPadOS 27.0; app 1.4 (60)")
    for bad in (None, "", 7, "Android 15; app 1.0 (1); Pixel", "iOS 27.0; app 1.4", "iOS 27.0; app 1.4 (60); x" * 9,
                "iOS 27.0; app 1.4 (60); iPhone; extra", "iOS 27.0; app 1.4 (60); <script>"):
        assert parse_client(bad) is None
    assert client_label(PHONE_FIELDS) == "iOS 27.0 · app 1.4 (60) · iPhone"
    assert client_label({}) == ""


def test_pairing_stores_the_client_and_requests_refresh_it_only_when_it_changes(home):
    store = DeviceStore(home)
    device_id, token = store.redeem(store.new_pairing_code(), "Phone", now=1000, client=parse_client(PHONE))
    assert store.devices()[0].client == PHONE_FIELDS
    assert store.authenticate(token, now=1000, client=parse_client(PHONE)) == device_id
    unchanged = store.path.stat().st_mtime_ns
    assert store.authenticate(token, now=1001, client=parse_client(PHONE)) == device_id
    assert store.path.stat().st_mtime_ns == unchanged  # same build: no rewrite
    updated = parse_client("iOS 27.1; app 1.5 (61); iPhone")
    assert store.authenticate(token, now=1002, client=updated) == device_id  # an app update: written at once
    assert store.devices()[0].client == updated and store.devices()[0].last_seen_at == 1002
    # An older app (no header) keeps working and leaves what was known.
    assert store.authenticate(token, now=1002 + SEEN_WRITE_S) == device_id
    assert store.client(device_id) == updated
    raw = json.loads(store.path.read_text())["devices"][device_id]
    assert set(raw) == {"name", "token_sha256", "created_at", "last_seen_at", "client"}


def test_old_device_files_without_a_client_still_load(home):
    store = DeviceStore(home)
    device_id, token = store.redeem(store.new_pairing_code(), "Mac")
    assert store.devices()[0].client == {} and store.client(device_id) == {}
    assert store.authenticate(token) == device_id


def test_the_server_reads_the_header_on_pair_and_on_status_polls(server, service):
    _, paired = http(server.base_url, "POST", "/voice/pair", {"code": service.devices.new_pairing_code(),
                                                              "device_name": "Phone"}, headers={CLIENT_HEADER: PHONE})
    assert service.devices.client(paired["device_id"]) == PHONE_FIELDS
    newer = "iOS 27.1; app 1.5 (61); iPhone"
    assert http(server.base_url, "GET", "/voice/status", None, paired["token"], {CLIENT_HEADER: newer})[0] == 200
    assert service.devices.client(paired["device_id"])["build"] == "61"
    # The fixture's own device paired without the header: it works and stays unknown.
    assert http(server.base_url, "GET", "/voice/status", None, server.token)[0] == 200
    mac = next(d for d in service.devices.devices() if d.name == "Test Mac")
    assert mac.client == {}
    assert http(server.base_url, "GET", "/voice/status", None, server.token, {CLIENT_HEADER: "garbage"})[0] == 200


def test_devices_cli_lists_the_build(home, monkeypatch, capsys):
    store = DeviceStore(home)
    store.redeem(store.new_pairing_code(), "Phone", client=parse_client(PHONE))
    store.redeem(store.new_pairing_code(), "Old Mac")
    monkeypatch.setattr(cli, "_home", lambda: home)
    assert cli.handle(type("Args", (), {"voice_command": "devices"})()) == 0
    out = capsys.readouterr().out
    assert "Phone  iOS 27.0 · app 1.4 (60) · iPhone  last used never" in out
    assert "Old Mac  app version unknown" in out


def test_the_call_log_names_the_build_that_made_the_call(server, service, home):
    _, paired = http(server.base_url, "POST", "/voice/pair", {"code": service.devices.new_pairing_code(),
                                                              "device_name": "Phone"}, headers={CLIENT_HEADER: PHONE})
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, paired["token"],
                           {"Idempotency-Key": "req_devinfo", CLIENT_HEADER: PHONE})
    assert status == 201
    worker = service.workers[-1]
    assert service.interaction(session["interaction_id"]).client == PHONE_FIELDS
    worker.feed({"type": "session.input_transcript.delta", "delta": "hello there", "start_ms": 1, "end_ms": 2})
    worker.call_closed()
    log = home / "speakeasy" / LOG_FILE
    entry = json.loads(wait_for(lambda: log.exists() and log.read_text().strip()).splitlines()[-1])
    assert entry["device"] == paired["device_id"] and entry["client"] == PHONE_FIELDS
