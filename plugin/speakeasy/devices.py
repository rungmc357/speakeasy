"""Paired devices and one-time pairing codes.

Stored under ``<HERMES_HOME>/speakeasy/devices.json`` (0600). Only SHA-256 hashes of
device tokens are kept, so the file never holds a usable credential.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PAIR_TTL_S = 600
# Wrong codes tried against one pending code before it is burned. Six digits over a 10-30 minute
# window is a million guesses with no limit; this caps them at a handful (hermes voice pair makes a new one).
MAX_WRONG_CODES = 10
# last_seen_at is written at most this often: every request used to rewrite devices.json, and a
# rewrite racing the CLI's ``hermes voice pair`` (another process) could drop its fresh code.
SEEN_WRITE_S = 60
_CODE_ALPHABET = "0123456789"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class Device:
    id: str
    name: str
    created_at: float
    last_seen_at: float | None = None


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

    def redeem(self, code: str, device_name: str, now: float | None = None) -> tuple[str, str] | None:
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
            }
            self._save(data)
        return device_id, token

    # -- auth --------------------------------------------------------------------------
    def authenticate(self, token: str | None, now: float | None = None) -> str | None:
        if not token:
            return None
        digest = _hash(token)
        with self._lock:
            data = self._load()
            for device_id, device in data["devices"].items():
                if secrets.compare_digest(device["token_sha256"], digest):
                    seen = time.time() if now is None else now
                    last = device.get("last_seen_at")
                    if last is None or seen - last >= SEEN_WRITE_S:
                        device["last_seen_at"] = seen
                        self._save(data)
                    return device_id
        return None

    def devices(self) -> list[Device]:
        data = self._load()
        return [Device(id=k, name=v["name"], created_at=v["created_at"], last_seen_at=v.get("last_seen_at"))
                for k, v in data["devices"].items()]

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            data = self._load()
            removed = data["devices"].pop(device_id, None) is not None
            self._save(data)
        return removed
