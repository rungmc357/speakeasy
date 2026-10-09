"""Optional voice provider: an OpenAI API key (Platform ``gpt-live-1`` over WebRTC).

The key is read only from the Hermes profile secret scope / ``.env`` as ``SPEAKEASY_OPENAI_API_KEY``;
it never goes into settings, logs, or any route response.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from .calls import ServiceError, SidebandWorker, new_event_id

OPENAI_BASE = "https://api.openai.com"
OPENAI_MODEL = "gpt-live-1"
MAX_BODY = 128 * 1024


def negotiate(key: str, payload: dict[str, Any], base: str = OPENAI_BASE,
              opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any]:
    """Create one GPT-Live session (SDP offer in, answer out)."""
    req = urllib.request.Request(base + "/v1/live/sessions", data=json.dumps(payload).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with opener(req, timeout=30) as response:
            raw = response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        raise ServiceError(exc.code if 400 <= exc.code < 500 else 502,
                          f"GPT-Live session creation failed ({exc.code})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ServiceError(502, f"GPT-Live unreachable ({type(exc).__name__})") from None
    if len(raw) > MAX_BODY:
        raise ServiceError(502, "GPT-Live response too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise ServiceError(502, "GPT-Live returned invalid JSON") from None
    return data if isinstance(data, dict) else {}


def session_payload(instructions: str, voice: str, sdp: str, seed: list[dict[str, Any]]) -> dict[str, Any]:
    session: dict[str, Any] = {
        "model": OPENAI_MODEL, "audio": {"output": {"voice": voice}}, "instructions": instructions,
        "delegation": {"type": "client"}, "store": False,
    }
    if seed:
        session["input"] = seed
    return {"session": session, "transport": {"type": "webrtc", "sdp": sdp}}


class OpenAISidebandWorker(SidebandWorker):
    """Attaches to the live session's server-side event channel over a websocket."""

    def __init__(self, rt, interaction, key_fn: Callable[[], str], base: str = OPENAI_BASE):
        super().__init__(rt, interaction)
        self.key_fn, self.base = key_fn, base
        self.ws: Any = None

    def call_connected(self) -> bool:
        return self.ws is not None and not self.interaction.call_closed

    def stop_transport(self) -> None:
        ws, loop = self.ws, self.loop
        if ws is not None and loop is not None:
            asyncio.run_coroutine_threadsafe(ws.close(), loop)

    async def run(self) -> None:
        import websockets  # lazy: only the OpenAI provider needs it

        self.loop = asyncio.get_running_loop()
        ws_base = self.base.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{ws_base}/v1/live/sessions/{urllib.parse.quote(self.interaction.live_session_id, safe='')}/attach"
        try:
            async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {self.key_fn()}"},
                                          max_size=2 * 1024 * 1024, ping_interval=20) as ws:
                self.ws = ws
                self.connected = True
                self.connected_at = time.monotonic()
                with self.interaction.lock:
                    self.interaction.status = "listening"
                self.publish()
                async for raw in ws:
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        await self.handle_event(event)
        except Exception as exc:
            with self.interaction.lock:
                if self.interaction.finalization != "confirmed" and not self.interaction.paused:
                    self.interaction.status = "error"
                    self.interaction.error = f"Voice connection lost: {type(exc).__name__}"
                    self.interaction.finalization = "incomplete"
            self.publish()
        finally:
            self.ws = None
            self.connected = False
            self.call_closed()
            if self.dispatch_tasks:
                await asyncio.gather(*self.dispatch_tasks, return_exceptions=True)
            self.publish()

    async def _send(self, kind: str, delegation_id: str | None, content: str) -> None:
        if not self.ws or len(content) > 2000:
            return
        try:
            await self.ws.send(json.dumps({"type": kind, "event_id": new_event_id(),
                                           "delegation_id": delegation_id, "content": content}))
        except Exception:
            self.ws = None
