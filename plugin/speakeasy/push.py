"""Notifications for a paired iPhone while the app is closed, without the words leaving this Mac.

How it works (docs/API.md, "Push notifications"):

1. The app sends its APNs device token to ``POST /voice/push`` (device-authenticated). It is kept
   in devices.json next to that device, with the kinds the user wants and the APNs environment.
2. When a task finishes, needs the user (approval, question, email draft), fails, or a Hermes
   conversation gets a reply, and that device has not been heard from for ``presence_s`` (no open
   event stream, no request), ``PushNotifier.notify`` keeps the real text here as a short-lived
   *event* and asks the relay to wake the phone.
3. The relay (relay/push in this repo, a Cloudflare Worker) receives only
   ``{device_token, environment, kind, nonce, collapse_id, event_id}``: never a title, a task, an
   answer or anything else about the work. It sends APNs a generic alert ("Your Hermes has an
   update") with ``mutable-content`` and the opaque event id.
4. The app's Notification Service Extension fetches ``GET /voice/push/events/{id}`` from this
   Hermes over the paired connection and rewrites the alert ("Done: Speed up the dashboard"). If
   it can't reach Hermes in time, the generic text stays.

Each Hermes install registers with the relay once (``POST /v1/register``) for an install id and a
secret (``speakeasy/push_install.json``, 0600); every notify is HMAC-signed with it. The secret and
device tokens are never logged.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

KINDS = ("finished", "needs_you", "failed", "reply")
ENVIRONMENTS = ("sandbox", "production")
DEFAULT_RELAY = "https://push.speakeasyvoice.ai"
APNS_TOKEN_RE = re.compile(r"^[0-9a-f]{64,200}$")
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
EVENT_TTL_S = 6 * 3600          # the extension asks within seconds; a late tap still finds the text
MAX_EVENTS = 500
RELAY_TIMEOUT_S = 10.0
SIGNATURE_VERSION = "v1"
# What the relay is allowed to see. Anything else in a notify body is a bug (and the relay refuses it).
RELAY_FIELDS = ("device_token", "environment", "kind", "nonce", "collapse_id", "event_id")
PREFIX = {"finished": "Done", "needs_you": "Needs you", "failed": "Failed", "reply": "New reply"}


def signature(secret: str, install_id: str, timestamp: str, body: bytes) -> str:
    """HMAC-SHA256(secret, "v1\\n<install>\\n<timestamp>\\n<sha256(body) hex>"), hex. The nonce rides
    inside the body, so it is covered too (the relay refuses a nonce it has seen)."""
    digest = hashlib.sha256(body).hexdigest()
    message = f"{SIGNATURE_VERSION}\n{install_id}\n{timestamp}\n{digest}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def token_hash(token: str) -> str:
    """A short, non-reversible label for logs (never the token itself)."""
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def clean_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class RelayClient:
    """Talks to the notification relay. ``post`` is swappable for tests (url, body, headers) ->
    (status, json)."""

    def __init__(self, hermes_home: Path, base_url: Callable[[], str],
                 post: Callable[[str, bytes, dict[str, str]], tuple[int, dict[str, Any]]] | None = None):
        self.path = Path(hermes_home) / "speakeasy" / "push_install.json"
        self.base_url = base_url
        self._post = post or self._urllib_post
        self._lock = threading.Lock()

    @staticmethod
    def _urllib_post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(request, timeout=RELAY_TIMEOUT_S) as response:
                raw, status = response.read(65536), response.status
        except urllib.error.HTTPError as exc:
            raw, status = exc.read(65536), exc.code
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return status, data if isinstance(data, dict) else {}

    # -- install identity --------------------------------------------------------------------
    def install(self) -> dict[str, str] | None:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("relay") != self.base_url():
            return None  # registered with another relay: register again
        if isinstance(data.get("install_id"), str) and isinstance(data.get("secret"), str):
            return data
        return None

    def register(self) -> dict[str, str] | None:
        status, data = self._post(self.base_url() + "/v1/register", b"{}", {})
        install_id, secret = data.get("install_id"), data.get("secret")
        if status != 201 or not isinstance(install_id, str) or not isinstance(secret, str):
            logger.warning("speakeasy: notification relay registration failed (%s)", status)
            return None
        record = {"relay": self.base_url(), "install_id": install_id, "secret": secret, "registered_at": time.time()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.replace(tmp, self.path)
        return record

    def forget_install(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # -- notify ------------------------------------------------------------------------------
    def notify(self, payload: dict[str, str]) -> dict[str, Any]:
        """Send one wake-up. Returns ``{ok, status, reason, invalid_token}``. Registers on first use,
        and once more when the relay no longer knows this install."""
        assert set(payload) <= set(RELAY_FIELDS), "only the opaque fields may go to the relay"
        with self._lock:
            for attempt in range(2):
                record = self.install() or self.register()
                if record is None:
                    return {"ok": False, "status": 0, "reason": "not_registered", "invalid_token": False}
                body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
                stamp = str(int(time.time()))
                headers = {"X-Speakeasy-Install": record["install_id"], "X-Speakeasy-Timestamp": stamp,
                           "X-Speakeasy-Signature": signature(record["secret"], record["install_id"], stamp, body)}
                try:
                    status, data = self._post(self.base_url() + "/v1/notify", body, headers)
                except (OSError, urllib.error.URLError) as exc:
                    return {"ok": False, "status": 0, "reason": type(exc).__name__, "invalid_token": False}
                if status == 401 and data.get("reason") == "unknown_install" and attempt == 0:
                    self.forget_install()
                    continue
                return {"ok": status == 200 and bool(data.get("ok")), "status": status,
                        "reason": str(data.get("reason") or "")[:60], "invalid_token": bool(data.get("invalid_token"))}
        return {"ok": False, "status": 401, "reason": "unknown_install", "invalid_token": False}


class PushNotifier:
    """When to wake which phone, what text its extension gets back, and the relay calls (one
    background thread, so a slow relay never holds up a task)."""

    def __init__(self, devices: Any, settings: Callable[[], dict[str, Any]], relay: RelayClient | None = None,
                 hermes_home: Path | None = None, claim: Callable[[str], bool] | None = None,
                 clock: Callable[[], float] = time.monotonic, background: bool = True):
        self.devices = devices
        self.settings = settings
        self.relay = relay or RelayClient(Path(hermes_home or "."), lambda: self.config()["relay_url"])
        self.claim = claim or (lambda key: True)
        self.clock = clock
        self._seen: dict[str, float] = {}
        self._streams: dict[str, int] = {}
        self._events: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.queue: "queue.Queue[tuple[str, dict[str, str]]] | None" = None
        if background:
            self.queue = queue.Queue()
            threading.Thread(target=self._worker, daemon=True, name="speakeasy-push").start()

    def config(self) -> dict[str, Any]:
        return self.settings().get("push") or {"enabled": True, "relay_url": DEFAULT_RELAY, "presence_s": 45}

    # -- presence ----------------------------------------------------------------------------
    def seen(self, device_id: str) -> None:
        with self._lock:
            self._seen[device_id] = self.clock()

    def stream_opened(self, device_id: str) -> None:
        with self._lock:
            self._streams[device_id] = self._streams.get(device_id, 0) + 1
            self._seen[device_id] = self.clock()

    def stream_closed(self, device_id: str) -> None:
        with self._lock:
            left = self._streams.get(device_id, 0) - 1
            if left > 0:
                self._streams[device_id] = left
            else:
                self._streams.pop(device_id, None)
            self._seen[device_id] = self.clock()

    def present(self, device_id: str) -> bool:
        """The app is on this device right now: an open event stream, or a request in the last
        ``presence_s`` seconds. A present app shows the change itself; no notification."""
        with self._lock:
            if self._streams.get(device_id):
                return True
            last = self._seen.get(device_id)
        return last is not None and self.clock() - last < float(self.config()["presence_s"])

    # -- registration (POST /voice/push) -----------------------------------------------------
    def register(self, device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        from .calls import ServiceError
        if body.get("enabled") is False:
            self.devices.clear_push(device_id)
            return {"ok": True, "enabled": False}
        token = body.get("apns_token")
        environment = body.get("environment", "production")
        kinds = body.get("kinds", list(KINDS))
        if not isinstance(token, str) or not APNS_TOKEN_RE.fullmatch(token.lower()):
            raise ServiceError(400, "apns_token must be the device token in hex")
        if environment not in ENVIRONMENTS:
            raise ServiceError(400, "environment must be sandbox or production")
        if not isinstance(kinds, list) or not all(k in KINDS for k in kinds):
            raise ServiceError(400, f"kinds must be a list of: {', '.join(KINDS)}")
        if not self.devices.set_push(device_id, token.lower(), environment, sorted(set(kinds), key=KINDS.index)):
            raise ServiceError(404, "device not found")
        return {"ok": True, "enabled": True, "kinds": sorted(set(kinds), key=KINDS.index),
                "relay": self.config()["relay_url"], "available": bool(self.config()["enabled"])}

    # -- events the extension reads (GET /voice/push/events/{id}) ----------------------------
    def event(self, device_id: str, event_id: str) -> dict[str, Any]:
        from .calls import ServiceError
        now = time.time()
        with self._lock:
            found = self._events.get(event_id) if EVENT_ID_RE.fullmatch(event_id or "") else None
        if not found or found["device_id"] != device_id or now - found["created_at"] > EVENT_TTL_S:
            raise ServiceError(404, "not found")
        return {k: found[k] for k in ("kind", "title", "body", "run_id", "thread_id")}

    def _keep(self, event: dict[str, Any]) -> str:
        event_id = secrets.token_urlsafe(18)
        now = time.time()
        with self._lock:
            for key in [k for k, v in self._events.items() if now - v["created_at"] > EVENT_TTL_S]:
                del self._events[key]
            while len(self._events) >= MAX_EVENTS:
                del self._events[next(iter(self._events))]
            self._events[event_id] = {**event, "created_at": now}
        return event_id

    # -- the rules ---------------------------------------------------------------------------
    def notify(self, kind: str, *, key: str, title: str | None, body: str | None = None,
               run_id: str | None = None) -> list[str]:
        """Wake every registered phone that wants ``kind`` and isn't open right now. ``key`` dedupes
        (one notification per device per task event). Returns the device ids queued."""
        if kind not in KINDS or not self.config()["enabled"]:
            return []
        sent: list[str] = []
        try:
            targets = self.devices.push_targets()
        except Exception as exc:  # never break a task over a notification
            logger.warning("speakeasy: push targets unreadable: %s", type(exc).__name__)
            return []
        task = clean_text(title, 80) or "Your task"
        text = {"title": f"{PREFIX[kind]}: {task}", "body": clean_text(body, 180),
                "kind": kind, "run_id": run_id or "", "thread_id": hashlib.sha256((run_id or key).encode()).hexdigest()[:16]}
        for device_id, push in targets.items():
            if kind not in push.get("kinds", KINDS) or self.present(device_id):
                continue
            if not self.claim(f"push:{device_id}:{kind}:{key}"):
                continue
            event_id = self._keep({"device_id": device_id, **text})
            payload = {"device_token": push["apns_token"], "environment": push.get("environment", "production"),
                       "kind": kind, "nonce": secrets.token_urlsafe(18), "collapse_id": text["thread_id"],
                       "event_id": event_id}
            if self.queue is not None:
                self.queue.put((device_id, payload))
            else:
                self._send(device_id, payload)
            sent.append(device_id)
        return sent

    def _send(self, device_id: str, payload: dict[str, str]) -> dict[str, Any]:
        try:
            result = self.relay.notify(payload)
        except Exception as exc:
            result = {"ok": False, "status": 0, "reason": type(exc).__name__, "invalid_token": False}
        if result.get("invalid_token"):
            # APNs says this token is dead (app deleted, token rotated): forget it so we stop asking.
            self.devices.clear_push(device_id, only_token=payload["device_token"])
            logger.info("speakeasy: dropped a push token APNs no longer accepts (%s)", token_hash(payload["device_token"]))
        elif not result.get("ok"):
            logger.info("speakeasy: notification not sent (%s %s)", result.get("status"), result.get("reason"))
        return result

    def _worker(self) -> None:
        assert self.queue is not None
        while True:
            device_id, payload = self.queue.get()
            self._send(device_id, payload)
            self.queue.task_done()
