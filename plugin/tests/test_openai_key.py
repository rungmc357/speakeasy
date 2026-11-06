"""POST /voice/openai-key: the app's Add key button for the API-key voice (Settings › Voice)."""
from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from fakes import http
from speakeasy.calls import ServiceError
from speakeasy.openai_key import OpenAIKeyStore

# Built at runtime so no key-shaped literal sits in the repo; obviously fake.
FAKE_OPENAI_KEY = "sk" + "-" + "proj-fake-test-key-0123456789abcdef"


@pytest.fixture
def logs():
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


def _post_raw(base, path, raw: bytes, token=None):
    req = urllib.request.Request(base + path, data=raw, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_status_says_the_app_can_add_the_key(server):
    status, body = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert status == 200 and body["capabilities"]["openai_key"] is True


def _no_key(home, monkeypatch):
    """The test home ships with a key; this test starts without one (and leaves no env behind)."""
    env = home / ".env"
    env.write_text("".join(l for l in env.read_text().splitlines(True) if "OPENAI_API_KEY" not in l))
    for name in ("SPEAKEASY_OPENAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_add_key_from_the_app_saves_it_to_the_profile_and_leaks_it_nowhere(server, service, home, logs, monkeypatch):
    _no_key(home, monkeypatch)
    status, before = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert before["api_key_set"] is False
    body = json.dumps({"value": "  " + FAKE_OPENAI_KEY + "\n"}).encode()

    assert _post_raw(server.base_url, "/voice/openai-key", body)[0] == 401  # paired devices only
    status, reply = _post_raw(server.base_url, "/voice/openai-key", body, server.token)
    assert status == 200, reply
    assert reply == {"saved": True, "api_key_set": True}

    # Hermes' own reader sees it (what calls use), and status flips.
    assert service.openai_key() == FAKE_OPENAI_KEY
    status, after = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert after["api_key_set"] is True and FAKE_OPENAI_KEY not in json.dumps(after)
    # Stored only in the profile's .env, under the documented name.
    holders = [p for p in home.rglob("*") if p.is_file() and FAKE_OPENAI_KEY.encode() in p.read_bytes()]
    assert [p.name for p in holders] == [".env"]
    assert f"SPEAKEASY_OPENAI_API_KEY={FAKE_OPENAI_KEY}" in (home / ".env").read_text()
    assert FAKE_OPENAI_KEY not in logs.getvalue()


@pytest.mark.parametrize("raw,reason", [
    (b'{"value": "not-a-key"}', "not_a_key"),
    (b'{"value": "sk-short"}', "not_a_key"),
    (b'{"value": "sk-has a space in it 0123456789"}', "not_a_key"),
    (b'{"value": ""}', None),
    (b'{"key": "x"}', None),
    (b'not json', None),
])
def test_bad_values_are_refused_plainly_and_nothing_is_saved(server, service, home, monkeypatch, raw, reason):
    _no_key(home, monkeypatch)
    status, reply = _post_raw(server.base_url, "/voice/openai-key", raw, server.token)
    assert status == 400
    assert reply.get("reason") == reason if reason else "error" in reply
    assert "sk-" in reply["error"] or "OpenAI API key" in reply["error"]
    assert service.openai_key() == ""


def test_body_is_wiped_whatever_happens():
    saved: list = []
    store = OpenAIKeyStore(Path("unused"), save=lambda home, name, value: saved.append((name, value)), is_set=lambda: True)
    raw = bytearray(json.dumps({"value": FAKE_OPENAI_KEY}).encode())
    assert store.save("dev1", raw)["saved"] is True
    assert saved == [("SPEAKEASY_OPENAI_API_KEY", FAKE_OPENAI_KEY)]
    assert set(raw) == {0}
    bad = bytearray(b'{"value": "nope"}')
    with pytest.raises(ServiceError):
        store.save("dev1", bad)
    assert set(bad) == {0}


def test_a_key_hermes_refuses_to_write_says_so_without_paths():
    def refuse(home, name, value):
        raise ValueError("SPEAKEASY_OPENAI_API_KEY is managed by /etc/managed/.env")
    store = OpenAIKeyStore(Path("unused"), save=refuse)
    with pytest.raises(ServiceError) as exc:
        store.save("dev1", bytearray(json.dumps({"value": FAKE_OPENAI_KEY}).encode()))
    assert exc.value.status == 403 and exc.value.reason == "managed" and "/" not in exc.value.message


def test_guessing_is_rate_limited():
    t = [0.0]
    store = OpenAIKeyStore(Path("unused"), save=lambda *a: None, clock=lambda: t[0])
    for _ in range(8):
        with pytest.raises(ServiceError) as exc:
            store.save("dev1", bytearray(b'{"value": "nope"}'))
        assert exc.value.status == 400
    with pytest.raises(ServiceError) as exc:
        store.save("dev1", bytearray(b'{"value": "nope"}'))
    assert exc.value.status == 429
    t[0] = 61.0
    assert store.save("dev1", bytearray(json.dumps({"value": FAKE_OPENAI_KEY}).encode()))["saved"]
