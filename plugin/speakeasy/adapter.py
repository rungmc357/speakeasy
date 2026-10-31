"""The ``voice`` gateway platform: runs the Speakeasy HTTP server inside the Hermes gateway.

No separate process. ``connect`` starts the loopback server (``server.py``) and ``disconnect``
stops it. Real work does not flow through gateway messages: each voice task is a Hermes run on the
user's own API server (``/v1/runs``) in its own session, so it runs with the user's model, tools,
memory, skills and approvals. See docs/ARCHITECTURE.md and docs/API.md.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, SendResult

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8795


# The gateway runner, kept here because this module is never swapped by a live reload: liveness.py
# asks it whether a thread task's turn is really still running.
GATEWAY_RUNNER = None


class VoiceAdapter(BasePlatformAdapter):
    def __init__(self, config, **kwargs):
        super().__init__(config=config, platform=Platform("voice"))
        from hermes_constants import get_hermes_home

        extra = getattr(config, "extra", {}) or {}
        self.host = "127.0.0.1"   # a TLS proxy may sit in front; never bind wide
        self.port = int(extra.get("port", DEFAULT_PORT))
        # Resolved at construction (inside the owning profile's scope): HTTP threads don't carry it.
        self.hermes_home = get_hermes_home()
        from . import router
        router.bind_home(self.hermes_home)
        self._reloader: Any = None

    @property
    def _server(self) -> Any:
        return self._reloader.server if self._reloader is not None else None

    @property
    def name(self) -> str:
        return "Speakeasy"

    @property
    def authorization_is_upstream(self) -> bool:
        # Every request is authenticated by a paired device token in server.Handler.
        return True

    @property
    def devices(self):
        from .devices import DeviceStore
        return DeviceStore(self.hermes_home)

    @property
    def service(self):
        """The running VoiceService (None before connect / after disconnect)."""
        return self._server.service if self._server is not None else None

    # -- lifecycle ---------------------------------------------------------------------
    async def connect(self, **_kwargs) -> bool:
        from pathlib import Path
        from .reloader import LiveReloader
        global GATEWAY_RUNNER
        GATEWAY_RUNNER = getattr(self, "gateway_runner", None)
        # The reloader owns the server so a reinstalled plugin loads without a gateway restart.
        self._reloader = LiveReloader(__package__, Path(__file__).parent, self.hermes_home, self.host, self.port)
        try:
            self._reloader.start()
        except OSError as e:
            self._reloader = None
            self._set_fatal_error("bind_failed", f"voice: could not bind {self.host}:{self.port}: {e}", retryable=True)
            return False
        self._mark_connected(listener_base=self._server.base_url)
        self._send_pending_link()
        logger.info("Speakeasy voice platform listening on %s", self._server.base_url)
        return True

    def _send_pending_link(self) -> None:
        """`hermes voice setup` ran before this restart and asked for the pairing link in a chat."""
        try:
            from .handoff import start_pending_delivery
            from .settings import Settings
            server = Settings(self.hermes_home).get()["server"]["advertised_url"] or self._server.base_url
            start_pending_delivery(self.hermes_home, server)
        except Exception as exc:  # never let this stop the voice platform
            logger.warning("speakeasy: pending pairing link not sent: %s", exc)

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._reloader is not None:
            try:
                self._reloader.stop()
            finally:
                self._reloader = None

    # -- gateway messaging (unused: voice results are spoken on the live call) ---------------
    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        return SendResult(success=True, message_id=str(int(time.time() * 1000)))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": f"voice:{chat_id}", "type": "dm"}
