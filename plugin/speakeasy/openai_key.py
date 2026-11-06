"""The OpenAI API key for the API-key voice path, added from the app (Settings › Voice › Add key).

``POST /voice/openai-key`` takes ``{"value": "sk-..."}``: the body is read into a bytearray that is
zeroed before the reply, and the key is saved through Hermes' env writer as
``SPEAKEASY_OPENAI_API_KEY`` in this profile (``settings.save_hermes_secret``) — the same place
``hermes voice setup --api-key`` puts it. Nothing here logs it, stores it in ``state.sqlite3``,
publishes it, or returns it; the reply says only whether a key is now set. The model never sees it.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .calls import ServiceError

MAX_BODY_BYTES = 16 * 1024
MAX_KEY_CHARS = 512
RATE_WINDOW_S = 60.0
RATE_MAX = 8  # tries per device per minute


def _wipe(buf: bytearray | None) -> None:
    if buf is not None:
        for i in range(len(buf)):
            buf[i] = 0


def _looks_like_key(value: str) -> bool:
    return value.startswith("sk-") and 20 <= len(value) <= MAX_KEY_CHARS \
        and all(c.isascii() and c.isprintable() and not c.isspace() for c in value)


class OpenAIKeyStore:
    def __init__(self, home: Path, *, save: Callable[[Path, str, str], None] | None = None,
                 is_set: Callable[[], bool] | None = None, clock: Callable[[], float] = time.monotonic):
        self.home = home
        self._save = save
        self._is_set = is_set
        self.clock = clock
        self._attempts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _limited(self, device_id: str) -> bool:
        now = self.clock()
        with self._lock:
            hits = [t for t in self._attempts.get(device_id, []) if now - t < RATE_WINDOW_S]
            hits.append(now)
            self._attempts[device_id] = hits
            return len(hits) > RATE_MAX

    def save(self, device_id: str, raw: bytearray) -> dict[str, Any]:
        """Validate and save the key in ``raw`` (zeroed whatever happens)."""
        try:
            if self._limited(device_id or ""):
                raise ServiceError(429, "Too many tries. Wait a minute.", "rate_limited")
            try:
                body = json.loads(bytes(raw).decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                raise ServiceError(400, "Paste your OpenAI API key.") from None
            if not isinstance(body, dict) or set(body) != {"value"} or not isinstance(body.get("value"), str):
                raise ServiceError(400, "Paste your OpenAI API key.")
            value = body.pop("value").strip()
            if not value:
                raise ServiceError(400, "Paste your OpenAI API key.")
            if not _looks_like_key(value):
                del value
                raise ServiceError(400, "That doesn't look like an OpenAI API key. They start with sk-.",
                                   "not_a_key")
            save = self._save
            if save is None:
                from .settings import save_hermes_secret as save
            from .settings import OPENAI_KEY_NAME
            try:
                save(self.home, OPENAI_KEY_NAME, value)
            except ValueError:  # Hermes refused the write (an administrator manages this key)
                raise ServiceError(403, "Your Hermes is set up so this key can't be changed from the app.",
                                   "managed") from None
            except Exception:
                raise ServiceError(500, "Your Hermes couldn't save the key. Try again.") from None
            finally:
                del value
            return {"saved": True, "api_key_set": self._is_set() if self._is_set else True}
        finally:
            _wipe(raw)
