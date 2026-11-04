"""Optional voice provider (beta): Grok realtime voice from xAI.

Unlike the other providers there is no server-side attach to a session the app opened, so the
plugin owns the one xAI socket and relays audio: the app streams mic audio to
``/voice/interactions/<id>/audio`` (relay.py) and gets the assistant's audio back on the same
socket. The xAI credential is the user's xAI sign-in that Hermes holds (``hermes auth add
xai-oauth``), resolved and refreshed by Hermes itself, else ``XAI_API_KEY`` read through Hermes'
secret readers. Speakeasy never opens Hermes' auth store. The credential is fetched per call and
never logged, stored or returned.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .calls import SidebandWorker
from .relay import READ_TIMEOUT_S, RelayClient, RelayProtocolError, RelaySocket
from .settings import XAI_KEY_NAME, hermes_secret, profile_scope
from .text import ID_RE, clean_transcript

logger = logging.getLogger(__name__)

XAI_BASE = "https://api.x.ai/v1"
XAI_MODEL = "grok-voice-latest"
RATE = 24000
AUDIO_FORMAT = {"type": "audio/pcm", "rate": RATE}
RELAY_FORMAT = {"encoding": "pcm16le", "rate": RATE, "channels": 1}
RELAY_LOST_S = 10.0          # the app's relay gone this long ends the call
TRANSCRIPT_WAIT_S = 2.0      # a handoff waits this long for the words it came from
MAX_EARLY_CHUNKS = 400       # mic audio held while the voice socket connects (~8 s of 20 ms chunks)
SIGN_IN_HINT = ("Grok voice needs your xAI sign-in: run `hermes auth add xai-oauth` "
                f"(SuperGrok or X Premium+), or set {XAI_KEY_NAME} in this Hermes profile.")

HANDOFF_TOOL = {
    "type": "function",
    "name": "handoff",
    "description": ("Start or continue backend work: anything that needs looking up, checking, doing, "
                    "sending, reading, writing or remembering. Call it right away with what the user "
                    "wants; the result arrives later as a background note."),
    "parameters": {
        "type": "object",
        "properties": {
            "request": {"type": "string",
                        "description": "What the user wants done, in their words, complete enough to act on."},
            "task_id": {"type": "string",
                        "description": "Only for a follow-up: the id of the open task this adds to, changes or answers."},
        },
        "required": ["request"],
    },
}

GROK_RULES = (
    "\n\n# Your work tool\nWhenever these rules say to delegate, hand off, or start work, call the `handoff` "
    "function with the request (and `task_id` for a follow-up to an open task). Call it in the same turn, "
    "before or while you acknowledge. Background notes arrive as system messages: use them, never read them out."
)


# -- credentials (read per use, never logged or returned) ---------------------------------------

def _oauth_present(hermes_home: Path) -> bool:
    """Offline: does this profile hold an xAI sign-in? Asked of Hermes' credential pool
    (``agent.credential_pool.load_pool``), the same store Hermes' own xAI resolver draws from."""
    try:
        from agent.credential_pool import load_pool  # type: ignore
        with profile_scope(hermes_home):
            return bool(load_pool("xai-oauth").has_credentials())
    except Exception as exc:  # never the message: it could carry account detail
        logger.debug("speakeasy: Hermes credential pool unavailable (%s)", type(exc).__name__)
        return False


def signin_state(hermes_home: Path) -> tuple[bool, bool, str]:
    """(signed_in with xAI, API key set, message). No network."""
    signed_in = _oauth_present(hermes_home)
    key_set = bool(hermes_secret(hermes_home, XAI_KEY_NAME))
    if signed_in:
        return True, key_set, "Signed in to xAI (Grok)."
    if key_set:
        return False, True, f"Using {XAI_KEY_NAME} from this Hermes profile (no xAI sign-in)."
    return False, False, SIGN_IN_HINT


def resolve_credential(hermes_home: Path) -> tuple[str, str]:
    """(token, base_url) for the realtime socket, or ("", "") when there is none. Hermes' resolver
    first (it refreshes the sign-in when needed); older Hermes without it falls back to the key."""
    try:
        from tools.xai_http import resolve_xai_http_credentials  # type: ignore
        with profile_scope(hermes_home):
            creds = resolve_xai_http_credentials()
        token = str((creds or {}).get("api_key") or "").strip()
        if token:
            base = str(creds.get("base_url") or XAI_BASE).rstrip("/")
            return token, base if base.startswith("https://") else XAI_BASE
    except Exception as exc:  # never the message: it could carry account detail
        logger.debug("speakeasy: Hermes xAI resolver unavailable (%s)", type(exc).__name__)
    key = hermes_secret(hermes_home, XAI_KEY_NAME)
    return (key, XAI_BASE) if key else ("", "")


def realtime_url(base: str) -> str:
    ws = base.rstrip("/").replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    return f"{ws}/realtime?model={XAI_MODEL}"


def voice_name(voice: str) -> str:
    return voice[:1].upper() + voice[1:].lower() if voice else "Ara"


def session_update(instructions: str, voice: str) -> dict[str, Any]:
    return {"type": "session.update", "session": {
        "instructions": instructions + GROK_RULES,
        "voice": voice_name(voice),
        "turn_detection": {"type": "server_vad"},
        "tools": [HANDOFF_TOOL],
        "audio": {"input": {"format": dict(AUDIO_FORMAT)}, "output": {"format": dict(AUDIO_FORMAT)}},
    }}


def seed_items(seed: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Resume history (prompt.resume_input's shape) as xAI conversation items."""
    out = []
    for item in seed:
        role = item.get("role")
        text = "".join(str(c.get("text") or "") for c in item.get("content") or [] if isinstance(c, dict))
        if role in {"user", "assistant"} and text.strip():
            out.append({"type": "conversation.item.create", "item": {
                "type": "message", "role": role,
                "content": [{"type": "input_text" if role == "user" else "text", "text": text}]}})
    return out


class GrokRelayWorker(SidebandWorker):
    """Owns the call's xAI socket; relays audio to and from the app's relay socket."""

    def __init__(self, rt, interaction, *, token: str, url: str, instructions: str, voice: str,
                 seed: list[dict[str, Any]] | None = None, max_call_s: float = 1800,
                 relay_lost_s: float | None = None, connector: Callable[..., Any] | None = None):
        super().__init__(rt, interaction)
        self._token = token  # never logged; dropped once connected
        self.url, self.instructions, self.voice = url, instructions, voice
        self.seed = seed or []
        self.max_call_s = max_call_s
        self.relay_lost_s = RELAY_LOST_S if relay_lost_s is None else relay_lost_s
        self.connector = connector
        self.ws: Any = None
        self.started_at = time.monotonic()
        self._clock = 0
        self.relay_lock = threading.Lock()
        self.client: RelayClient | None = None
        self.detached_at: float | None = self.started_at + self.relay_lost_s  # the app gets twice as long to first connect
        self.muted = False
        self.audio_q: asyncio.Queue[bytes] | None = None
        self.early_audio: deque[bytes] = deque(maxlen=MAX_EARLY_CHUNKS)
        self.end_reason: str | None = None
        self.response_active = False
        self.response_id: str | None = None
        self.dropped_responses: set[str] = set()
        self.respond_after = False      # a response.create is owed once the current one is done
        self.spoken_queue: deque[dict[str, Any]] = deque(maxlen=8)  # spoken lines waiting for a reply to end
        self.user_pending = False       # speech ended, its transcript not in yet
        self.user_heard = asyncio.Event()
        self.handoffs: set[str] = set()
        self.last_handoff_ms = 0
        self.spoken_responses: deque[str] = deque(maxlen=32)  # replies that already said something
        self.skip_transcripts = 0
        self.stats: dict[str, Any] = {"mic_chunks": 0, "audio_out_chunks": 0, "connect_ms": None,
                                      "first_audio_ms": None}

    # -- clock: transcript offsets for context() ------------------------------------------------
    def now_ms(self) -> int:
        """Strictly increasing, so the transcript's order survives context()'s offset cut."""
        self._clock = max(self._clock + 1, int((time.monotonic() - self.started_at) * 1000) + 1)
        return self._clock

    # -- SidebandWorker hooks --------------------------------------------------------------
    def call_connected(self) -> bool:
        return self.ws is not None and not self.interaction.call_closed

    def stop_transport(self) -> None:
        self.end_call("paused" if self.interaction.paused else "ended")

    def end_call(self, reason: str) -> None:
        """End from any thread: close the voice socket; run()'s cleanup tells the app why."""
        if self.end_reason is None:
            self.end_reason = reason
        ws, loop = self.ws, self.loop
        if loop is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(self._close_ws(ws), loop)
            except RuntimeError:
                pass

    async def _close_ws(self, ws: Any) -> None:
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        elif self.ws is None and self.audio_q is not None:
            self.audio_q.put_nowait(b"")  # wake a run() still connecting

    async def _send_json(self, payload: dict[str, Any]) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(payload))
            return True
        except Exception:
            return False

    async def _send(self, kind: str, delegation_id: str | None, content: str) -> None:
        if self.ws is None or len(content) > 2000 or not content.strip():
            return
        if kind == "session.commentary.append":
            # Spoken verbatim, like the other providers' spoken lines: xAI's force_message synthesizes
            # the text as a whole assistant turn (no response.create). One at a time, after any reply.
            item = {"type": "conversation.item.create", "item": {
                "type": "force_message", "role": "assistant",
                "content": [{"type": "output_text", "text": content}]}}
            if self.response_active:
                self.spoken_queue.append(item)
                return
            self.response_active = True
            await self._send_json(item)
        else:  # thinking: silent context, no reply
            await self._send_json({"type": "conversation.item.create", "item": {
                "type": "message", "role": "system", "content": [{"type": "input_text", "text": content}]}})

    async def _respond(self) -> None:
        """Ask for a reply now, or right after the one in progress (xAI takes one at a time)."""
        if self.response_active or self.spoken_queue:
            self.respond_after = True
            return
        self.respond_after = False
        self.response_active = True  # until response.created/done says otherwise
        await self._send_json({"type": "response.create"})

    # -- the app's relay (handler threads) ---------------------------------------------------------
    def serve_client(self, ws: RelaySocket) -> None:
        """Runs on the HTTP handler thread for as long as the app's relay is open. A second
        connection replaces the first."""
        try:
            ws.sock.settimeout(READ_TIMEOUT_S)
        except OSError:
            pass
        client = RelayClient(ws)
        with self.relay_lock:
            old, self.client = self.client, client
            self.detached_at = None
        if old is not None:
            old.close("replaced")
        if self.interaction.call_closed or self.end_reason:
            client.close(self.end_reason or "ended")
            return
        logger.info("speakeasy: Grok relay attached")
        try:
            while True:
                message = ws.recv()
                if message is None:
                    break
                kind, data = message
                if kind == "binary":
                    self.mic(data)
                    continue
                try:
                    control = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(control, dict):
                    continue
                if control.get("type") == "mute":
                    self.muted = bool(control.get("muted"))
                elif control.get("type") == "end":
                    self.end_call("ended")
                    break
        except (OSError, RelayProtocolError) as exc:
            logger.info("speakeasy: Grok relay dropped (%s)", type(exc).__name__)
        finally:
            with self.relay_lock:
                if self.client is client:
                    self.client = None
                    self.detached_at = time.monotonic()
            client.close(self.end_reason)
            # The handler closes the socket when this returns: let queued frames (and "closed") out first.
            client.writer.join(2)

    def mic(self, data: bytes) -> None:
        if self.muted or not data or len(data) > 256 * 1024 or len(data) % 2:
            return
        self.stats["mic_chunks"] += 1
        loop, q = self.loop, self.audio_q
        if loop is None or q is None:
            self.early_audio.append(data)
            return
        try:
            loop.call_soon_threadsafe(q.put_nowait, data)
        except RuntimeError:
            pass

    def to_app(self, method: str, *args: Any) -> None:
        client = self.client
        if client is not None:
            getattr(client, method)(*args)

    # -- the voice socket ------------------------------------------------------------------------
    async def _connect(self) -> Any:
        if self.connector is not None:
            return await self.connector(self.url, self._token)
        import websockets  # lazy: only the socket-owning providers need it
        return await websockets.connect(self.url, additional_headers={"Authorization": f"Bearer {self._token}"},
                                        max_size=4 * 1024 * 1024, ping_interval=20, open_timeout=15,
                                        close_timeout=2)

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.audio_q = asyncio.Queue()
        self.user_heard = asyncio.Event()
        while self.early_audio:
            self.audio_q.put_nowait(self.early_audio.popleft())
        failure: str | None = None
        pump = watch = None
        try:
            ws = await self._connect()
            self._token = ""
            try:
                self.ws = ws
                self.connected = True
                self.connected_at = time.monotonic()
                self.stats["connect_ms"] = int((self.connected_at - self.started_at) * 1000)
                await ws.send(json.dumps(session_update(self.instructions, self.voice)))
                for item in seed_items(self.seed):
                    await ws.send(json.dumps(item))
                with self.interaction.lock:
                    self.interaction.status = "listening"
                self.publish()
                pump = asyncio.create_task(self._pump_mic(ws))
                watch = asyncio.create_task(self._watchdog())
                if self.end_reason:
                    await ws.close()
                async for raw in ws:
                    if isinstance(raw, (bytes, bytearray)):
                        self._audio_out(bytes(raw))  # binary output transport, if ever enabled
                        continue
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        await self.on_xai(event)
            finally:
                self.ws = None
                await ws.close()
        except Exception as exc:
            failure = type(exc).__name__
        finally:
            self._token = ""
            for task in (pump, watch):
                if task is not None:
                    task.cancel()
            self.connected = False
            reason = self.end_reason
            if reason is not None or self.interaction.paused:
                await self.handle_event({"type": "session.closed"})  # a deliberate end: confirmed
            else:
                with self.interaction.lock:
                    if self.interaction.finalization != "confirmed":
                        self.interaction.status = "error"
                        self.interaction.error = f"Voice connection lost{': ' + failure if failure else ''}"
                        self.interaction.finalization = "incomplete"
                reason = "voice connection lost"
            self.call_closed()
            with self.relay_lock:
                client, self.client = self.client, None
            if client is not None:
                client.close(reason or "ended")
            if self.dispatch_tasks:
                await asyncio.gather(*self.dispatch_tasks, return_exceptions=True)
            self.publish()

    async def _pump_mic(self, ws: Any) -> None:
        assert self.audio_q is not None
        while True:
            data = await self.audio_q.get()
            if not data:
                continue
            if self.muted:
                continue
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(data).decode()}))

    async def _watchdog(self) -> None:
        while self.end_reason is None:
            await asyncio.sleep(.25)
            now = time.monotonic()
            if now - self.started_at >= self.max_call_s:
                self.end_call("max call length")
            with self.relay_lock:
                gone = self.detached_at
            if gone is not None and now - gone > self.relay_lost_s:
                self.end_call("relay lost")

    def _audio_out(self, data: bytes, response_id: Any = None) -> None:
        if not data:
            return
        if isinstance(response_id, str) and response_id not in self.spoken_responses:
            self.spoken_responses.append(response_id)
        if self.stats["first_audio_ms"] is None:
            self.stats["first_audio_ms"] = int((time.monotonic() - self.started_at) * 1000)
        self.stats["audio_out_chunks"] += 1
        self.to_app("audio", data)

    # -- xAI events --------------------------------------------------------------------------------
    async def on_xai(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "response.output_audio.delta" or kind == "response.audio.delta":
            if event.get("response_id") in self.dropped_responses:
                return
            try:
                data = base64.b64decode(event.get("delta") or "", validate=False)
            except ValueError:
                return
            self._audio_out(data, event.get("response_id"))
        elif kind in {"response.output_audio_transcript.delta", "response.audio_transcript.delta"}:
            if event.get("response_id") in self.dropped_responses:
                return
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                now = self.now_ms()
                await self.handle_event({"type": "session.output_transcript.delta", "delta": delta,
                                         "start_ms": now, "end_ms": now})
        elif kind == "conversation.item.input_audio_transcription.completed":
            await self._user_words(event.get("transcript"))
        elif kind == "input_audio_buffer.speech_started":
            self.user_pending = True
            self.user_heard.clear()
            self.touch()
            # Barge-in: the app drops what it hasn't played yet; the reply in progress is cancelled.
            if self.response_active and self.response_id:
                self.dropped_responses.add(self.response_id)
                await self._send_json({"type": "response.cancel"})
            client = self.client
            if client is not None:
                client.flush()
                client.event({"type": "speech_started"})
        elif kind == "input_audio_buffer.speech_stopped":
            self.user_pending = True
        elif kind == "response.created":
            self.response_active = True
            self.response_id = (event.get("response") or {}).get("id") or event.get("response_id")
        elif kind == "response.done":
            self.response_active = False
            self.response_id = None
            self.to_app("event", {"type": "response_done"})
            if self.spoken_queue:
                self.response_active = True
                await self._send_json(self.spoken_queue.popleft())
            elif self.respond_after:
                await self._respond()
        elif kind == "response.function_call_arguments.done":
            await self._function_call(event)
        elif kind == "error":
            err = event.get("error") if isinstance(event.get("error"), dict) else {}
            logger.warning("speakeasy: Grok voice error %s/%s", str(err.get("type") or "")[:40],
                           str(err.get("code") or "")[:60])
            if self.response_active and not self.response_id:
                self.response_active = False  # a refused response.create: never wait on it
        elif kind == "session.updated":
            logger.info("speakeasy: Grok voice session configured")

    async def _user_words(self, text: Any) -> None:
        self.user_pending = False
        self.user_heard.set()
        if self.skip_transcripts:  # already stood in for by a handoff's request
            self.skip_transcripts -= 1
            return
        if not isinstance(text, str):
            return
        text = clean_transcript(text).strip()
        if not text or len(text.encode()) > 8192:
            return
        if self.fragments and self.fragments[-1]["speaker"] == "user":
            text = " " + text
        now = self.now_ms()
        await self.handle_event({"type": "session.input_transcript.delta", "delta": text,
                                 "start_ms": now, "end_ms": now})

    async def _function_call(self, event: dict[str, Any]) -> None:
        call_id, name = event.get("call_id"), event.get("name")
        if not isinstance(call_id, str) or not call_id or call_id in self.handoffs:
            return
        self.handoffs.add(call_id)
        try:
            args = json.loads(event.get("arguments") or "{}")
        except ValueError:
            args = {}
        args = args if isinstance(args, dict) else {}
        request = args.get("request")
        if name != "handoff" or not isinstance(request, str) or not clean_transcript(request).strip():
            await self._tool_output(call_id, {"status": "error", "message": "unknown tool or empty request"})
            return
        request = clean_transcript(request).strip()[:4000]
        task_id = args.get("task_id")
        marked = task_id if isinstance(task_id, str) and ID_RE.fullmatch(task_id) else None
        # Answer at once. A reply is asked for only when the turn that called the tool said nothing
        # yet: otherwise the voice acknowledges twice ("On it. On it.").
        spoke = event.get("response_id") in self.spoken_responses
        await self._tool_output(call_id, {"status": "started",
                                          "note": "Work has started; the result comes later as a background note."},
                                respond=not spoke)
        if self.user_pending:  # the words this came from are still being transcribed
            try:
                await asyncio.wait_for(self.user_heard.wait(), TRANSCRIPT_WAIT_S)
            except asyncio.TimeoutError:
                pass
        if not self._heard_since_last_handoff():
            # No words of theirs arrived for this handoff: the voice's summary stands in for them.
            self.skip_transcripts += 1 if self.user_pending else 0
            now = self.now_ms()
            await self.handle_event({"type": "session.input_transcript.delta", "delta": request,
                                     "start_ms": now, "end_ms": now})
        # The boundary is the end of their words (as with the other providers), not now: the voice
        # often says "on it" before calling the tool, and that line must not hide the request.
        boundary = max((f["end_ms"] for f in self.fragments if f["speaker"] == "user"), default=self.now_ms())
        self.last_handoff_ms = self.now_ms()
        delegation: dict[str, Any] = {"id": "grok_" + secrets.token_hex(12), "target": "client"}
        if marked:
            delegation["task_id"] = marked
        logger.info("speakeasy: Grok handoff received (%s chars)", len(request))
        # The same boundary event the other providers send: the base class freezes the transcript here.
        await self.handle_event({"type": "session.delegation.created", "offset_ms": boundary,
                                 "delegation": delegation})

    def _heard_since_last_handoff(self) -> bool:
        return any(f["speaker"] == "user" and f["end_ms"] > self.last_handoff_ms for f in self.fragments)

    async def _tool_output(self, call_id: str, output: dict[str, Any], respond: bool = True) -> None:
        await self._send_json({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": json.dumps(output)}})
        if respond:
            await self._respond()
