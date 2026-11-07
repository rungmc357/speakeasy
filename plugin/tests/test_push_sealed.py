"""A question's words in the notification itself (SPK-124 Q4): sealed on this Mac with the phone's own
key from pairing, so the relay and APNs carry only ciphertext; an older relay still gets a plain wake-up.
Invented data only."""
from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

from fakes import http
from speakeasy import push as push_mod
from test_push import FakeRelay, register, relay  # noqa: F401


def _open(key: str, sealed: str) -> dict:
    raw = base64.urlsafe_b64decode(sealed + "=" * (-len(sealed) % 4))
    k = base64.urlsafe_b64decode(key + "=" * (-len(key) % 4))
    return json.loads(ChaCha20Poly1305(k).decrypt(raw[:12], raw[12:], b"speakeasy-push-v1"))


def test_pairing_hands_the_phone_its_own_key(server, service):
    _, paired = http(server.base_url, "POST", "/voice/pair", {"code": service.devices.new_pairing_code(), "device_name": "iPhone"})
    key = paired["seal_key"]
    assert len(base64.urlsafe_b64decode(key + "=")) == 32
    assert service.devices.seal_key(paired["device_id"]) == key
    # A phone paired before keys existed gets one when it turns notifications on.
    status, reply = register(server)
    assert status == 200 and reply["seal_key"]


def test_the_question_rides_sealed_and_only_the_phone_can_read_it(server, service, relay, monkeypatch):
    _, reply = register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    service.push.notify("needs_you", key="q1", title="Desk hunt", body="Which of the three desks should I order?",
                        run_id="run_q1")
    (payload, headers), = relay.notifies
    assert set(payload) == set(push_mod.RELAY_FIELDS)
    sent = json.dumps(payload) + json.dumps(headers)
    for word in ("Desk", "desks", "order", "run_q1"):
        assert word not in sent
    opened = _open(reply["seal_key"], payload["sealed"])
    assert opened == {"title": "Needs you: Desk hunt", "body": "Which of the three desks should I order?",
                      "kind": "needs_you", "run_id": "run_q1", "level": "personal"}


def test_an_older_relay_gets_the_plain_wake_up(server, service, relay, monkeypatch):
    register(server)
    monkeypatch.setattr(service.push, "present", lambda d: False)
    replies = iter([(400, {"ok": False, "reason": "exactly device_token, environment, kind, nonce, collapse_id, event_id"}),
                    (200, {"ok": True})])

    def answer(url, body, headers):
        relay.requests.append((url, json.loads(body), dict(headers)))
        if url.endswith("/v1/register"):
            return 201, {"install_id": "ins_1abcdef", "secret": "s" * 43}
        return next(replies)
    service.push.relay._post = answer
    service.push.notify("needs_you", key="q2", title="Desk hunt", body="Which desk?", run_id="run_q2")
    sends = relay.notifies
    assert "sealed" in sends[0][0] and "sealed" not in sends[1][0]
    assert service.push.relay.sealed_refused is True


def test_seal_refuses_a_bad_key():
    assert push_mod.seal("short", {"title": "x"}) is None
