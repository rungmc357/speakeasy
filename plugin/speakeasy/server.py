"""HTTP layer for the Speakeasy Mac app: the /voice/* routes, SSE events, pairing.

Loopback only. Every route except ``GET /health`` and ``POST /voice/pair`` needs
``Authorization: Bearer <device token>`` from ``devices.py``. Shapes: docs/API.md.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .service import VoiceService
from .calls import ServiceError, Interaction, publish_state
from .text import MAX_ROOM_CHARS

logger = logging.getLogger(__name__)

MAX_BODY = 128 * 1024
# POST /voice/sessions can also carry listening mode's room text: up to MAX_ROOM_CHARS code points,
# each at worst 12 bytes of JSON (a character outside the BMP written as two \uXXXX escapes), on top
# of the usual body (whose 128 KiB already fits the largest SDP offer, MAX_SDP, with its escaping).
MAX_SESSION_BODY = MAX_BODY + MAX_ROOM_CHARS * 12
LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_ID = r"([A-Za-z0-9_-]+)"


class Handler(BaseHTTPRequestHandler):
    server_version = f"Speakeasy/{__version__}"
    heartbeat_s = 15.0

    @property
    def service(self) -> VoiceService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # never log tokens or bodies
        logger.debug("speakeasy http: " + fmt, *args)

    # -- plumbing ---------------------------------------------------------------------------
    def _reply(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self, limit: int = MAX_BODY) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ServiceError(400, "invalid content length") from None
        if length <= 0 or length > limit:
            raise ServiceError(413, "request body size rejected")
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            raise ServiceError(400, "invalid JSON") from None
        if not isinstance(body, dict):
            raise ServiceError(400, "JSON object required")
        return body

    @property
    def route(self) -> str:
        return urlsplit(self.path).path

    def _auth(self) -> str:
        return self.service.authenticate(self.headers)

    def _sse(self, seq: int, kind: str, payload: Any) -> None:
        data = json.dumps(payload, separators=(",", ":"))
        self.wfile.write(f"id: {seq}\nevent: {kind}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def _events(self, interaction: Interaction) -> None:
        """Live SSE stream: snapshot (or resume via Last-Event-ID), deduped changes, heartbeat, closed."""
        store, feed = self.service.store, interaction.feed
        name = self.service.settings.get()["assistant_name"]
        try:
            cursor = int(self.headers.get("Last-Event-ID", ""))
        except ValueError:
            cursor = -1
        publish_state(store, interaction, name)
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def snapshot() -> tuple[int, dict[str, Any]]:
            seq = feed.seq - 1 if feed.closed_id is not None else feed.seq
            return seq, {"interaction": feed.last["interaction"] or interaction.snapshot(),
                         "work": feed.last["work"], "approval": feed.last["approval"],
                         "tasks": feed.last["tasks"] or [], "email_drafts": feed.last.get("email_drafts") or [],
                         "away": interaction.away}
        try:
            with feed.cond:
                pending = feed.since(cursor) if cursor >= 0 else None
                first: list[tuple[int, str, Any]] = []
                if pending is None:
                    cursor, body = snapshot()
                    first = [(cursor, "snapshot", body)]
            for seq, kind, payload in first:
                self._sse(seq, kind, payload)
            last_write = time.monotonic()
            while True:
                with feed.cond:
                    if feed.closed_id is not None and cursor >= feed.closed_id:
                        return
                    remaining = self.heartbeat_s - (time.monotonic() - last_write)
                    feed.cond.wait_for(lambda: feed.seq > cursor, timeout=max(0.0, remaining))
                    batch = feed.since(cursor)
                    if batch is None:
                        cursor, body = snapshot()
                        batch = [(cursor, "snapshot", body)]
                for seq, kind, payload in batch:
                    self._sse(seq, kind, payload)
                    cursor = max(cursor, seq)
                    last_write = time.monotonic()
                if time.monotonic() - last_write >= self.heartbeat_s:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_write = time.monotonic()
                    publish_state(store, interaction, name)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            return

    # -- routes -------------------------------------------------------------------------------
    def do_GET(self) -> None:
        path = self.route
        try:
            if path == "/health":
                self._reply(200, {"ok": True, "platform": "voice", "version": __version__,
                                  "provider": self.service.settings.get()["voice"]["provider"]})
                return
            self._auth()
            image = re.fullmatch(rf"/voice/card-image/{_ID}/([1-8])", path)
            live = re.fullmatch(rf"/voice/live-image/{_ID}", path)
            if image or live:
                data, mime = (self.service.card_image(image.group(1), int(image.group(2))) if image
                              else self.service.live_image(live.group(1)))
                self.send_response(200)
                self.send_header("Content-Type", mime)
                # The live image changes during the run; the client asks again when its seq moves.
                self.send_header("Cache-Control", "private, max-age=300" if image else "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            events = re.fullmatch(rf"/voice/interactions/{_ID}/events", path)
            if events:
                self._events(self.service.interaction(events.group(1)))
                return
            match = re.fullmatch(rf"/voice/interactions/{_ID}", path)
            work = re.fullmatch(rf"/voice/work/{_ID}", path)
            routes = {
                "/voice/work/latest": self.service.work_latest,
                "/voice/settings": self.service.get_settings,
                "/voice/brief": self.service.get_brief,
                "/voice/status": self.service.status,
                "/voice/destinations": self.service.destinations,
                "/voice/onboarding": self.service.onboarding,
                "/voice/routing": self.service.routing_choices,
                "/voice/home": self.service.get_home,
                "/voice/brief/tune": self.service.get_tune,
            }
            if path in routes:
                self._reply(200, routes[path]())
            elif path == "/voice/routing/models":
                from urllib.parse import parse_qs
                provider = (parse_qs(urlsplit(self.path).query).get("provider") or [""])[0]
                self._reply(200, self.service.routing_models(provider))
            elif work:
                self._reply(200, self.service.work(work.group(1)))
            elif match:
                self._reply(200, self.service.interaction(match.group(1)).snapshot())
            else:
                raise ServiceError(404, "not found")
        except ServiceError as exc:
            self._reply(exc.status, {"error": exc.message})
        except Exception as exc:
            logger.exception("speakeasy: GET %s failed", path)
            self._reply(500, {"error": f"internal error ({type(exc).__name__})"})

    def do_POST(self) -> None:
        path = self.route
        try:
            if path == "/voice/pair":
                self._reply(201, self.service.pair(self._body()))
                return
            device_id = self._auth()
            if path == "/voice/sessions":
                self._reply(201, self.service.create_session(self._body(MAX_SESSION_BODY),
                                                            self.headers.get("Idempotency-Key", ""),
                                                            device_id))
                return
            if path == "/voice/tasks/dismiss":
                self._reply(200, self.service.dismiss_tasks(self._body()))
                return
            if path == "/voice/destinations/suggest":
                self._reply(200, self.service.suggest_channels())
                return
            if path == "/voice/brief/rewrite":
                self._reply(202, self.service.rewrite_brief())
                return
            if path == "/voice/brief/tune":
                self._reply(202, self.service.start_tune())
                return
            if path == "/voice/brief/tune/apply":
                self._reply(200, self.service.apply_tune(self._body()))
                return
            if path == "/voice/brief/tune/dismiss":
                self._reply(200, self.service.dismiss_tune())
                return
            if path == "/voice/onboarding":
                self._reply(200, self.service.save_onboarding(self._body()))
                return
            if path == "/voice/routing":
                self._reply(200, self.service.choose_routing(self._body()))
                return
            review = re.fullmatch(rf"/voice/reviews/{_ID}/dismiss", path)
            if review:
                self._reply(200, self.service.dismiss_review(review.group(1), self._body()))
                return
            draft = re.fullmatch(rf"/voice/drafts/{_ID}", path)
            if draft:
                self._reply(200, self.service.decide_draft(draft.group(1), self._body()))
                return
            action = re.fullmatch(rf"/voice/interactions/{_ID}/(end|pause|approval|cancel-backend|skip-tour|early-request|mic-check|answer)", path)
            if not action:
                raise ServiceError(404, "not found")
            interaction_id, verb = action.groups()
            if verb == "end":
                self._reply(200, self.service.finish_transport(interaction_id))
            elif verb == "early-request":
                self._reply(200, self.service.early_request(interaction_id, self._body()))
            elif verb == "answer":
                self._reply(200, self.service.answer(interaction_id, self._body()))
            elif verb == "mic-check":
                self._reply(200, self.service.mic_check(interaction_id, self._body()))
            elif verb == "pause":
                self._reply(200, self.service.pause(interaction_id))
            elif verb == "skip-tour":
                self._reply(200, self.service.skip_tour(interaction_id))
            elif verb == "cancel-backend":
                self._reply(202, self.service.cancel_backend(interaction_id, self._body()))
            else:
                self._reply(200, self.service.approve(interaction_id, self._body()))
        except ServiceError as exc:
            self._reply(exc.status, {"error": exc.message})
        except Exception as exc:
            logger.exception("speakeasy: POST %s failed", path)
            self._reply(500, {"error": f"internal error ({type(exc).__name__})"})

    def do_PATCH(self) -> None:
        try:
            self._auth()
            if self.route != "/voice/settings":
                raise ServiceError(404, "not found")
            self._reply(200, self.service.patch_settings(self._body()))
        except ServiceError as exc:
            self._reply(exc.status, {"error": exc.message})

    def do_PUT(self) -> None:
        try:
            self._auth()
            if self.route == "/voice/home":
                self._reply(200, self.service.put_home(self._body()))
                return
            if self.route != "/voice/brief":
                raise ServiceError(404, "not found")
            self._reply(200, self.service.put_brief(self._body()))
        except ServiceError as exc:
            self._reply(exc.status, {"error": exc.message})


class SpeakeasyServer:
    """Owns the ThreadingHTTPServer and the VoiceService. Bound to loopback only."""

    def __init__(self, service: VoiceService, host: str = "127.0.0.1", port: int = 8795):
        if host not in LOOPBACK:
            raise ValueError("Speakeasy binds loopback only; put a TLS proxy (e.g. Tailscale serve) in front")
        self.service = service
        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.service = service  # type: ignore[attr-defined]
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="speakeasy-http", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.service.close()
