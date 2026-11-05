"""The address drop: how the app finds this Hermes again after a quick tunnel's address changes.

A Cloudflare quick tunnel gets a new ``*.trycloudflare.com`` address whenever its tunnel daemon restarts,
and the app can't ask the old (dead) address for the new one. We run no servers, so the plugin
leaves an encrypted note on a public no-account relay, ntfy.sh (open source; self-hostable:
``address_drop.relay``):

* Made once per Hermes profile (``speakeasy/address_drop.json``, 0600): a random 128-bit topic id
  and a 256-bit key. Both go only to paired devices: in the ``POST /voice/pair`` response and in
  the authenticated ``GET /voice/endpoints``, never in a pairing link (links pass through chats).
* Whenever the advertised tunnel address changes (and every few hours, because the relay forgets
  messages after about 12 h), the plugin publishes ``se1.<base64url(nonce ‖ AES-256-GCM)>``. The
  plaintext is ``{"url", "ts", "seq"}``; the topic is the additional authenticated data. The relay
  sees only ciphertext, the topic and the time.
* The app decrypts it, refuses anything not newer than the last note it accepted (replay of an
  old address), and switches only after its existing device token works at the new address.

Opt out with ``hermes voice config set address_drop.enabled false``; the chat message with an
"Update Speakeasy's address" link (tunnel.py) still goes out either way.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

PREFIX = "se1."
REPUBLISH_S = 6 * 3600


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def drop_path(home: Path) -> Path:
    return Path(home) / "speakeasy" / "address_drop.json"


def load(home: Path) -> dict[str, Any]:
    try:
        data = json.loads(drop_path(home).read_text())
        if isinstance(data, dict) and data.get("topic") and data.get("key"):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _save(home: Path, data: dict[str, Any]) -> None:
    path = drop_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def ensure(home: Path) -> dict[str, Any]:
    """The topic and key, made once (never rotated on their own: paired phones hold them)."""
    data = load(home)
    if not data:
        data = {"topic": "se-" + secrets.token_hex(16), "key": _b64(secrets.token_bytes(32)), "seq": 0,
                "published_url": "", "published_at": 0.0}
        _save(home, data)
    return data


def client_info(home: Path, settings: dict[str, Any]) -> dict[str, Any] | None:
    """What a paired device needs to read the drop, or None when it's turned off."""
    cfg = settings["address_drop"]
    if not cfg["enabled"]:
        return None
    data = ensure(home)
    return {"relay": cfg["relay"], "topic": data["topic"], "key": data["key"]}


def seal(key_b64: str, topic: str, url: str, ts: int, seq: int) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    plain = json.dumps({"url": url, "ts": ts, "seq": seq}, separators=(",", ":")).encode()
    return PREFIX + _b64(nonce + AESGCM(_unb64(key_b64)).encrypt(nonce, plain, topic.encode()))


def open_sealed(key_b64: str, topic: str, message: str) -> dict[str, Any] | None:
    """Decrypt a note (the app's half, here for tests). None when it isn't ours or was altered."""
    if not message.startswith(PREFIX):
        return None
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        raw = _unb64(message[len(PREFIX):])
        plain = AESGCM(_unb64(key_b64)).decrypt(raw[:12], raw[12:], topic.encode())
        data = json.loads(plain)
    except (InvalidTag, ValueError):
        return None
    return data if isinstance(data, dict) else None


def post(url: str, body: str, timeout: float = 10.0) -> bool:
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    req.add_header("Content-Type", "text/plain")
    req.add_header("Title", "Speakeasy")  # nothing about the content: the body is ciphertext
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (configured https relay)
            return 200 <= r.status < 300
    except Exception as exc:
        logger.warning("speakeasy: address drop publish failed (%s)", type(exc).__name__)
        return False


def publish_if_needed(home: Path, settings: dict[str, Any], *, now: float | None = None,
                      send: Callable[[str, str], bool] = post) -> bool:
    """Publish the current tunnel address when it changed or the last note is getting old.
    True when a note went out."""
    cfg, server = settings["address_drop"], settings["server"]
    url = server.get("tunnel_url", "")
    if not cfg["enabled"] or server.get("tunnel") != "cloudflare" or not url:
        return False
    now = time.time() if now is None else now
    data = ensure(home)
    if data.get("published_url") == url and now - float(data.get("published_at") or 0) < REPUBLISH_S:
        return False
    seq = int(data.get("seq") or 0) + 1
    body = seal(data["key"], data["topic"], url, int(now), seq)
    if not send(f"{cfg['relay'].rstrip('/')}/{data['topic']}", body):
        return False
    data.update(seq=seq, published_url=url, published_at=now)
    _save(home, data)
    logger.info("speakeasy: published the encrypted address note (seq %s)", seq)
    return True


_lock = threading.Lock()


def publish_safely(home: Path, settings: dict[str, Any], **kw: Any) -> bool:
    with _lock:
        try:
            return publish_if_needed(home, settings, **kw)
        except Exception as exc:  # never break the server over the drop
            logger.warning("speakeasy: address drop failed (%s)", type(exc).__name__)
            return False
