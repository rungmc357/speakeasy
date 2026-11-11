"""Paired devices and one-time pairing codes.

Stored under ``<HERMES_HOME>/speakeasy/devices.json`` (0600). Only SHA-256 hashes of
device tokens are kept, so the file never holds a usable credential. A device that turned on
notifications also has its APNs token (push.py), which addresses the phone, not this Hermes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PAIR_TTL_S = 600
# Wrong codes tried against one pending code before it is burned. Six digits over a 10-30 minute
# window is a million guesses with no limit; this caps them at a handful (hermes voice pair makes a new one).
MAX_WRONG_CODES = 10
# last_seen_at is written at most this often: every request used to rewrite devices.json, and a
# rewrite racing the CLI's ``hermes voice pair`` (another process) could drop its fresh code.
SEEN_WRITE_S = 60
_CODE_ALPHABET = "0123456789"
# ``X-Speakeasy-Client: iOS 27.0; app 1.4 (60); iPhone`` — sent by the apps on every request so the
# build behind a call is known. Platform, OS version, app version + build and a device class only:
# no hardware identifiers, nothing personal. Anything that doesn't fit the shape is ignored.
CLIENT_HEADER = "X-Speakeasy-Client"
PROJECTS_HEADER = "X-Speakeasy-Projects"  # "off": the app holds Projects out (Service.projects_off)
PLATFORMS = ("iOS", "iPadOS", "macOS", "visionOS")
_CLIENT_RE = re.compile(
    r"^\s*(?P<platform>iOS|iPadOS|macOS|visionOS) (?P<os>\d{1,3}(?:\.\d{1,3}){0,2})\s*;"
    r"\s*app (?P<version>[0-9A-Za-z][0-9A-Za-z.+-]{0,23}) \((?P<build>[0-9A-Za-z.]{1,16})\)\s*"
    r"(?:;\s*(?P<model>[A-Za-z][A-Za-z ]{0,23}?))?\s*$")


def parse_client(value: Any) -> dict[str, str] | None:
    """The client fields from an ``X-Speakeasy-Client`` header, or None (missing or malformed)."""
    if not isinstance(value, str) or len(value) > 120:
        return None
    m = _CLIENT_RE.match(value)
    if not m:
        return None
    out = {"platform": m["platform"], "os_version": m["os"], "app_version": m["version"], "build": m["build"]}
    if m["model"]:
        out["model"] = m["model"].strip()
    return out


def client_label(client: dict[str, Any] | None) -> str:
    """``iOS 27.0 · app 1.4 (60) · iPhone`` for listings; "" when the device never said."""
    if not client:
        return ""
    parts = [f"{client.get('platform', '?')} {client.get('os_version', '')}".strip(),
             f"app {client.get('app_version', '?')} ({client.get('build', '?')})"]
    if client.get("model"):
        parts.append(client["model"])
    return " · ".join(parts)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class Device:
    id: str
    name: str
    created_at: float
    last_seen_at: float | None = None
    client: dict[str, str] = field(default_factory=dict)  # parse_client fields; {} = never sent


class DeviceStore:
    def __init__(self, root: Path):
        self.path = root / "speakeasy" / "devices.json"
        self._lock = threading.Lock()

    # -- persistence -------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"devices": {}, "pairing": {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    # -- pairing -----------------------------------------------------------------------
    def new_pairing_code(self, now: float | None = None, ttl: float = PAIR_TTL_S) -> str:
        """A single-use 6-digit code (10 minutes by default). Issuing one expires older ones."""
        now = time.time() if now is None else now
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        with self._lock:
            data = self._load()
            data["pairing"] = {_hash(code): now + ttl}
            data["pairing_misses"] = 0
            self._save(data)
        return code

    def redeem(self, code: str, device_name: str, now: float | None = None,
               client: dict[str, str] | None = None) -> tuple[str, str] | None:
        """Trade a valid code for (device_id, token). The code is consumed either way."""
        now = time.time() if now is None else now
        with self._lock:
            data = self._load()
            expires = data.get("pairing", {}).pop(_hash(code.strip()), None)
            if expires is None or expires < now:
                if expires is None and data.get("pairing"):
                    misses = int(data.get("pairing_misses") or 0) + 1
                    data["pairing_misses"] = misses
                    if misses >= MAX_WRONG_CODES:
                        data["pairing"] = {}  # too many wrong guesses: the pending code is burned
                self._save(data)
                return None
            token = secrets.token_urlsafe(32)
            device_id = secrets.token_hex(4)
            data["devices"][device_id] = {
                "name": (device_name or "Mac").strip()[:64],
                "token_sha256": _hash(token),
                "created_at": now,
                "last_seen_at": None,
                # The key this device's notification text is sealed with (push.seal): handed over
                # once, in the pair reply, over the paired connection; never sent to the relay.
                "seal_key": secrets.token_urlsafe(32),
            }
            if client:
                data["devices"][device_id]["client"] = dict(client)
            self._save(data)
        return device_id, token

    # -- auth --------------------------------------------------------------------------
    def authenticate(self, token: str | None, now: float | None = None,
                     client: dict[str, str] | None = None) -> str | None:
        """The device id for a token. ``client`` (parse_client) is stored when it changed, so an
        app update shows up on its first request; no client (an older app) leaves it as it was."""
        if not token:
            return None
        digest = _hash(token)
        with self._lock:
            data = self._load()
            for device_id, device in data["devices"].items():
                if secrets.compare_digest(device["token_sha256"], digest):
                    seen = time.time() if now is None else now
                    last = device.get("last_seen_at")
                    changed = bool(client) and device.get("client") != client
                    if changed:
                        device["client"] = dict(client or {})
                    if last is None or seen - last >= SEEN_WRITE_S or changed:
                        device["last_seen_at"] = seen
                        self._save(data)
                    return device_id
        return None

    def client(self, device_id: str) -> dict[str, str]:
        """The last client this device reported ({} when unknown)."""
        device = self._load()["devices"].get(device_id) or {}
        return dict(device.get("client") or {})

    def proof(self, device_id: str, nonce: str, host: str) -> str:
        """``POST /voice/prove``: HMAC-SHA256(key = the device token's SHA-256 hex, message =
        "speakeasy-prove-v1|<nonce>|<host>"), hex. The app (which knows its token) checks it before
        sending the token to a new address, so a look-alike server learns nothing. An unknown
        device gets a random answer (no way to tell which ids exist)."""
        import hmac
        device = self._load()["devices"].get(device_id)
        key = device["token_sha256"].encode() if device else secrets.token_bytes(32)
        return hmac.new(key, f"speakeasy-prove-v1|{nonce}|{host}".encode(), hashlib.sha256).hexdigest()

    def devices(self) -> list[Device]:
        data = self._load()
        return [Device(id=k, name=v["name"], created_at=v["created_at"], last_seen_at=v.get("last_seen_at"),
                       client=dict(v.get("client") or {}))
                for k, v in data["devices"].items()]

    # -- push (push.py) -----------------------------------------------------------------
    # ``push``: {apns_token, environment, kinds, updated_at}. The APNs token only lets the relay
    # address a notification to this app on this phone; it is no credential for this Hermes.
    def set_push(self, device_id: str, apns_token: str, environment: str, kinds: list[str],
                 now: float | None = None) -> bool:
        with self._lock:
            data = self._load()
            device = data["devices"].get(device_id)
            if device is None:
                return False
            device["push"] = {"apns_token": apns_token, "environment": environment, "kinds": list(kinds),
                              "updated_at": time.time() if now is None else now}
            # One phone, one push target: a re-paired phone's old record (same app on the same phone,
            # so the same APNs token) stops getting notifications, sealed with a key it threw away.
            for other_id, other in data["devices"].items():
                if other_id != device_id and (other.get("push") or {}).get("apns_token") == apns_token:
                    del other["push"]
            self._save(data)
        return True

    def clear_push(self, device_id: str, only_token: str | None = None) -> bool:
        """Turn notifications off for a device (``only_token``: only while it still has that token,
        so a dead token reported late never removes the new one the phone just sent)."""
        with self._lock:
            data = self._load()
            device = data["devices"].get(device_id) or {}
            push = device.get("push")
            if not push or (only_token and push.get("apns_token") != only_token):
                return False
            del device["push"]
            self._save(data)
        return True

    def push_targets(self) -> dict[str, dict[str, Any]]:
        """device id -> push record (with its ``seal_key``, when it has one), for every device that
        turned notifications on."""
        return {k: {**v["push"], **({"seal_key": v["seal_key"]} if v.get("seal_key") else {})}
                for k, v in self._load()["devices"].items() if v.get("push")}

    def seal_key(self, device_id: str) -> str:
        """This device's notification key, made now for a device paired before there were keys."""
        with self._lock:
            data = self._load()
            device = data["devices"].get(device_id)
            if device is None:
                return ""
            if not device.get("seal_key"):
                device["seal_key"] = secrets.token_urlsafe(32)
                self._save(data)
            return device["seal_key"]

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            data = self._load()
            removed = data["devices"].pop(device_id, None) is not None
            self._save(data)
        return removed
