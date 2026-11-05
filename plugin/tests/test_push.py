"""Phone notifications while the app is closed (push.py): the token a phone registers, when a task
wakes it, what the relay is (and is never) told, signing, dead-token cleanup, and the text the
phone's notification extension reads back from this Hermes."""
from __future__ import annotations

import hashlib
import hmac
import json
import stat

import pytest

from fakes import http, wait_for
from speakeasy import push as push_mod
from speakeasy.push import KINDS, PushNotifier, RelayClient, signature
from speakeasy.settings import SettingsError, validate

APNS = "a1b2c3d4" * 8  # a made-up 64-hex APNs token


class FakeRelay:
    """Stands in for relay/push: answers register and notify, keeps every request as sent."""

    def __init__(self):
        self.requests: list[tuple[str, dict, dict]] = []
        self.notify_reply = (200, {"ok": True, "status": 200})
        self.installs = 0

    def __call__(self, url, body, headers):
        payload = json.loads(body)
        self.requests.append((url, payload, dict(headers)))
        if url.endswith("/v1/register"):
            self.installs += 1
            return 201, {"install_id": f"ins_{self.installs}abcdef", "secret": "s" * 43}
        return self.notify_reply

    @property
    def notifies(self):
        return [(p, h) for u, p, h in self.requests if u.endswith("/v1/notify")]


@pytest.fixture
def relay(service, home):
    fake = FakeRelay()
    service.push.relay = RelayClient(home, lambda: "https://relay.example", post=fake)
    return fake


def register(server, token=APNS, **extra):
    return http(server.base_url, "POST", "/voice/push", {"apns_token": token, "environment": "sandbox", **extra},
                server.token)


def device_id(service):
    return service.devices.devices()[0].id


# -- registration ---------------------------------------------------------------------------------

def test_a_phone_registers_its_token_next_to_its_device(server, service):
    status, body = register(server, kinds=["finished", "needs_you"])
    assert status == 200 and body["enabled"] and body["kinds"] == ["finished", "needs_you"]
    raw = json.loads(service.devices.path.read_text())["devices"][device_id(service)]
    assert raw["push"]["apns_token"] == APNS and raw["push"]["environment"] == "sandbox"
    assert stat.S_IMODE(service.devices.path.stat().st_mode) == 0o600
    # Turning it off forgets the token.
    assert http(server.base_url, "POST", "/voice/push", {"enabled": False}, server.token)[0] == 200
    assert service.devices.push_targets() == {}


def test_registration_needs_the_device_token_and_a_real_apns_token(server):
    assert http(server.base_url, "POST", "/voice/push", {"apns_token": APNS})[0] == 401
    for bad in ({"apns_token": "not-hex"}, {"apns_token": APNS, "environment": "staging"},
                {"apns_token": APNS, "kinds": ["everything"]}, {"apns_token": 7}):
        assert http(server.base_url, "POST", "/voice/push", bad, server.token)[0] == 400


def test_status_lists_the_push_capability(server):
    assert http(server.base_url, "GET", "/voice/status", token=server.token)[1]["capabilities"]["push"] is True


def test_push_settings_validate():
    assert validate({})["push"] == {"enabled": True, "relay_url": "https://push.speakeasyvoice.ai", "presence_s": 45}
    assert validate({"push": {"relay_url": "http://127.0.0.1:8787"}})["push"]["relay_url"] == "http://127.0.0.1:8787"
    for bad in ({"relay_url": "http://relay.example"}, {"presence_s": 1}, {"enabled": "yes"}, {"extra": 1}):
        with pytest.raises(SettingsError):
            validate({"push": bad})


# -- when to notify -------------------------------------------------------------------------------

def test_an_open_app_gets_no_notification_and_a_closed_one_does(server, service, relay):
    register(server)
    did = device_id(service)
    assert service.push.present(did)  # it just made a request
    assert service.push.notify("finished", key="run_a", title="Speed up the dashboard") == []
    now = [1000.0]
    service.push.clock = lambda: now[0]
    service.push._seen.clear()
    service.push.seen(did)
    now[0] += 44
    assert service.push.present(did)
    now[0] += 2
    assert not service.push.present(did)
    service.push.stream_opened(did)  # a live event stream counts however old the last request is
    now[0] += 600
    assert service.push.present(did)
    service.push.stream_closed(did)
    now[0] += 46
    assert service.push.notify("finished", key="run_a", title="Speed up the dashboard") == [did]
    assert len(relay.notifies) == 1


def test_only_the_kinds_the_user_picked_and_each_event_once(server, service, relay, monkeypatch):
    register(server, kinds=["needs_you", "failed"])
    monkeypatch.setattr(service.push, "present", lambda d: False)
    assert service.push.notify("finished", key="run_b", title="Draft the release notes") == []
    assert service.push.notify("reply", key="run_b", title="Draft the release notes") == []
    assert service.push.notify("needs_you", key="approval:apr_1", title="Clean up old builds") != []
    assert service.push.notify("needs_you", key="approval:apr_1", title="Clean up old builds") == []  # deduped
    assert service.push.notify("failed", key="run:run_c", title="Clean up old builds") != []
    assert [p["kind"] for p, _ in relay.notifies] == ["needs_you", "failed"]
    service.settings.patch({"push": {"enabled": False}})
    assert service.push.notify("failed", key="run:run_d", title="x") == []


def test_finished_and_failed_jobs_wake_the_phone_with_the_right_kind(server, service, hermes, relay, monkeypatch):
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    assert http(server.base_url, "POST", "/voice/work", {"text": "Find why the checkout test fails"}, server.token)[0] == 202
    wait_for(lambda: relay.notifies)
    payload, _ = relay.notifies[0]
    assert payload["kind"] == "finished"
    hermes.fail_with = "Provider authentication failed: invalid api key"
    http(server.base_url, "POST", "/voice/work", {"text": "Compare the two pricing pages"}, server.token)
    wait_for(lambda: len(relay.notifies) == 2)
    assert relay.notifies[1][0]["kind"] == "failed"


def test_an_approval_wakes_the_phone_as_needs_you(server, service, hermes, relay, monkeypatch):
    from test_routing import start_call
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    hermes.approval_first = True
    _, worker = start_call(server, service)
    worker.feed({"type": "session.output_transcript.delta", "delta": "On it.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_risky", "Delete the old build logs")
    wait_for(lambda: [p for p, _ in relay.notifies if p["kind"] == "needs_you"])


# -- the content rule -----------------------------------------------------------------------------

def test_the_relay_never_receives_any_words(server, service, relay, monkeypatch):
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    title, body = "Speed up the dashboard", "The slow query now uses the date index and returns in 40 ms."
    service.push.notify("finished", key="run_e", title=title, body=body, run_id="run_e")
    (payload, headers), = relay.notifies
    assert set(payload) == set(push_mod.RELAY_FIELDS)
    sent = json.dumps(payload) + json.dumps(headers)
    for word in ("Speed", "dashboard", "query", "index", "Done", "run_e"):
        assert word not in sent
    # The register call carries nothing at all.
    assert relay.requests[0][1] == {}


def test_the_extension_reads_the_real_text_back_only_for_its_own_device(server, service, relay, monkeypatch):
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    service.push.notify("needs_you", key="q1", title="Pick a venue", body="Which of the two halls?", run_id="run_f")
    event_id = relay.notifies[0][0]["event_id"]
    status, text = http(server.base_url, "GET", f"/voice/push/events/{event_id}", token=server.token)
    assert status == 200
    assert text["title"] == "Needs you: Pick a venue" and text["body"] == "Which of the two halls?"
    assert text["kind"] == "needs_you" and text["run_id"] == "run_f"
    assert http(server.base_url, "GET", f"/voice/push/events/{event_id}")[0] == 401
    assert http(server.base_url, "GET", "/voice/push/events/" + "x" * 24, token=server.token)[0] == 404
    # Another paired device can't read it.
    _, other = http(server.base_url, "POST", "/voice/pair", {"code": service.devices.new_pairing_code(), "device_name": "iPad"})
    assert http(server.base_url, "GET", f"/voice/push/events/{event_id}", token=other["token"])[0] == 404


def test_the_extensions_fetch_does_not_count_as_the_app_being_open(server, service, relay):
    register(server)
    did = device_id(service)
    now = [5000.0]
    service.push.clock = lambda: now[0]
    service.push._seen.clear()  # (seen under the real clock)
    service.push.notify("finished", key="g1", title="Job one")
    event_id = relay.notifies[0][0]["event_id"]
    http(server.base_url, "GET", f"/voice/push/events/{event_id}", token=server.token)
    assert not service.push.present(did)


# -- signing and the install ----------------------------------------------------------------------

def test_requests_are_signed_with_the_install_secret(server, service, relay, home, monkeypatch):
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    service.push.notify("finished", key="s1", title="Job")
    service.push.notify("finished", key="s2", title="Job")
    assert relay.installs == 1  # registered once, then reused
    record = json.loads((home / "speakeasy" / "push_install.json").read_text())
    assert stat.S_IMODE((home / "speakeasy" / "push_install.json").stat().st_mode) == 0o600
    for url, payload, headers in relay.requests[1:]:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        assert headers["X-Speakeasy-Install"] == record["install_id"]
        expected = signature(record["secret"], record["install_id"], headers["X-Speakeasy-Timestamp"], body)
        assert hmac.compare_digest(headers["X-Speakeasy-Signature"], expected)
    nonces = [p["nonce"] for p, _ in relay.notifies]
    assert len(set(nonces)) == 2


def test_signature_matches_the_relays_definition():
    body = b'{"a":1}'
    digest = hashlib.sha256(body).hexdigest()
    want = hmac.new(b"k", f"v1\nins\n100\n{digest}".encode(), hashlib.sha256).hexdigest()
    assert signature("k", "ins", "100", body) == want


def test_an_unknown_install_registers_again_once(server, service, relay, monkeypatch):
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    replies = iter([(401, {"ok": False, "reason": "unknown_install"}), (200, {"ok": True})])
    real = relay.__call__

    def post(url, body, headers):
        if url.endswith("/v1/notify"):
            relay.requests.append((url, json.loads(body), dict(headers)))
            return next(replies)
        return real(url, body, headers)
    service.push.relay._post = post
    service.push.notify("finished", key="u1", title="Job")
    assert relay.installs == 2 and len(relay.notifies) == 2


def test_a_token_apns_rejects_is_forgotten(server, service, relay, monkeypatch):
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    relay.notify_reply = (200, {"ok": False, "status": 410, "reason": "Unregistered", "invalid_token": True})
    service.push.notify("finished", key="d1", title="Job")
    assert service.devices.push_targets() == {}
    # A late report for an old token never removes the new one.
    register(server, token="f0" * 32)
    assert not service.devices.clear_push(device_id(service), only_token=APNS)
    assert service.devices.push_targets()[device_id(service)]["apns_token"] == "f0" * 32


def test_a_relay_that_is_down_never_breaks_a_task(server, service, home, monkeypatch):
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)

    def down(url, body, headers):
        raise OSError("connection refused")
    service.push.relay = RelayClient(home, lambda: "https://relay.example", post=down)
    assert service.push.notify("finished", key="x1", title="Job") != []  # queued/sent, no exception


def test_every_kind_has_a_prefix():
    assert set(push_mod.PREFIX) == set(KINDS)
    notifier = PushNotifier.__new__(PushNotifier)
    assert notifier is not None
