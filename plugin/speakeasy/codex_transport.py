"""Voice over the user's own ChatGPT sign-in: the Codex app-server realtime transport (default).

Runs ``codex app-server --stdio --enable realtime_conversation`` per call with the voice model
``gpt-live-1-codex`` over WebRTC. The app-server protocol is experimental: Speakeasy only uses it as
a voice transport. It never gives Codex Hermes credentials and never runs Codex turns or tools for
tasks; every task goes to the user's Hermes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .calls import SidebandWorker
from .text import ID_RE, clean_transcript

CODEX_MODEL = "gpt-live-1-codex"
_ENV_KEEP = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "CODEX_HOME")
logger = logging.getLogger(__name__)
_LOGIN_CACHE: dict[str, tuple[float, tuple[bool, str]]] = {}


class CodexStartError(RuntimeError):
    """A voice start failure with a message that is safe and useful to show the user."""


def explain_codex_error(message: str) -> str:
    """Map Codex's error text to a plain reason. Only known shapes pass through; nothing else
    from the provider is reflected."""
    text = message.lower()
    if "unknown variant" in text and ("v3" in text or "version" in text):
        return ("This Codex is too old for voice. Update it (npm install -g @openai/codex@latest), "
                "or run hermes voice setup to use a current copy.")
    if "voice" in text and "not supported" in text:
        return "That voice isn't available. Pick another voice in Speakeasy Settings."
    if "didn't provide an api key" in text or "not logged in" in text:
        return "Codex isn't signed in to ChatGPT. Run codex login on the Hermes machine."
    if "login" in text or "auth" in text or "unauthorized" in text or "401" in text:
        return ("OpenAI rejected Codex's saved sign-in (it may have expired or been signed out). "
                "Run codex login again on the Hermes machine.")
    if "rate" in text and "limit" in text:
        return "ChatGPT's voice limit was reached. Try again later or switch to an OpenAI API key."
    return "Codex couldn't start the voice call."


def is_auth_error(message: str) -> bool:
    """A start failure that means the saved sign-in doesn't work, whatever `codex login status` says."""
    text = message.lower()
    return any(k in text for k in ("401", "unauthorized", "api key", "not logged in", "login", "auth"))


_REJECTED: dict[str, tuple[float | None, str]] = {}


def _auth_stamp() -> float | None:
    """When Codex's saved sign-in last changed (a new `codex login` rewrites it)."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        return (home / "auth.json").stat().st_mtime
    except OSError:
        return None


def mark_signed_out(binary: Path | None, reason: str) -> None:
    """A call just proved the saved sign-in doesn't work. `codex login status` only reads the file,
    so it keeps saying "logged in"; Settings shows "not signed in" until the user signs in again."""
    if binary is not None:
        _REJECTED[str(binary)] = (_auth_stamp(), reason)
        _LOGIN_CACHE.pop(str(binary), None)


def mark_signed_in(binary: Path | None) -> None:
    if binary is not None:
        _REJECTED.pop(str(binary), None)


def _redacted(message: str) -> str:
    return re.sub(r"(sk-[A-Za-z0-9_*-]{2})[A-Za-z0-9_*-]+", r"\1…", message)[:300]


def child_env() -> dict[str, str]:
    """Only what Codex needs to find its own account-local auth. Never Hermes or API-key vars."""
    return {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}


def login_status(binary: Path | None, runner: Callable[..., Any] = subprocess.run,
                 ttl_s: float = 60.0) -> tuple[bool, str]:
    """(signed_in, message) from `codex login status`, cached briefly."""
    if binary is None:
        return False, "Codex CLI not found. Install it and run `codex login`, or set voice.codex_path."
    rejected = _REJECTED.get(str(binary))
    if rejected is not None:
        if rejected[0] == _auth_stamp():
            return False, rejected[1]
        _REJECTED.pop(str(binary), None)  # signed in again since the failed call
    cached = _LOGIN_CACHE.get(str(binary))
    if cached and time.monotonic() - cached[0] < ttl_s:
        return cached[1]
    try:
        done = runner([str(binary), "login", "status"], capture_output=True, text=True, timeout=10, env=child_env())
        text = f"{getattr(done, 'stdout', '') or ''}{getattr(done, 'stderr', '') or ''}".strip()
        lowered = text.lower()
        ok = getattr(done, "returncode", 1) == 0 and "logged in" in lowered and "not logged in" not in lowered
        if ok and "api key" in lowered:
            # Voice through Codex is the ChatGPT-plan path; an API-key login bills the API key instead
            # and fails outright when that key is revoked. Say which one this is.
            result = (True, "Codex is signed in with an OpenAI API key, not a ChatGPT account.")
        else:
            result = (ok, "Signed in to Codex with ChatGPT." if ok else "Codex is not signed in. Run `codex login`.")
    except (OSError, subprocess.SubprocessError) as exc:
        result = (False, f"Could not run Codex ({type(exc).__name__}).")
    _LOGIN_CACHE[str(binary)] = (time.monotonic(), result)
    return result


class CodexTransport:
    """One `codex app-server` process speaking JSON-RPC lines on stdio."""

    def __init__(self, binary: Path | None, workdir: Path, max_call_s: int = 1800,
                 popen: Callable[..., Any] = subprocess.Popen):
        if binary is None or not Path(binary).is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError("Codex app-server executable unavailable")
        workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.max_call_s = max_call_s
        self.binary = Path(binary)
        self.process = popen(
            [str(binary), "app-server", "--stdio", "--enable", "realtime_conversation"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, cwd=str(workdir), env=child_env())
        self.thread_id: str | None = None
        self.notifications: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=512)
        self.pending: dict[int, "queue.Queue[dict[str, Any]]"] = {}
        self.lock = threading.Lock()
        self.sequence = 0
        self.closed = False
        self.started_at = time.monotonic()
        threading.Thread(target=self._reader, daemon=True, name="speakeasy-codex-reader").start()

    def _reader(self) -> None:
        try:
            for line in self.process.stdout:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                with self.lock:
                    waiter = self.pending.get(event.get("id"))  # type: ignore[arg-type]
                if waiter is not None and "method" not in event:
                    waiter.put_nowait(event)
                elif "method" in event:
                    if event["method"] in {"thread/realtime/closed", "thread/realtime/error"}:
                        self.closed = True
                    try:
                        self.notifications.put_nowait(event)
                    except queue.Full:
                        self.closed = True
                        break
        finally:
            self.closed = True
            try:
                self.notifications.put_nowait({"method": "transport/eof"})
            except queue.Full:
                pass

    def _write(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.process.stdin.write(json.dumps(event) + "\n")
            self.process.stdin.flush()

    def _fail(self, message: str) -> None:
        # The raw reason goes only to the Hermes log (keys masked); the user sees the mapped one.
        logger.warning("speakeasy: codex voice start failed: %s", _redacted(message))
        explained = explain_codex_error(message)
        if is_auth_error(message):
            mark_signed_out(self.binary, explained)
        raise CodexStartError(explained)

    def request(self, method: str, params: dict | None = None, timeout: float = 20) -> dict:
        waiter: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=1)
        with self.lock:
            self.sequence += 1
            number = self.sequence
            self.pending[number] = waiter
        try:
            self._write({"id": number, "method": method, "params": params or {}})
            event = waiter.get(timeout=timeout)
            if "error" in event:
                message = str((event.get("error") or {}).get("message") or "")
                self._fail(message)
            return event.get("result") or {}
        except queue.Empty as exc:
            raise TimeoutError("Codex request timed out") from exc
        finally:
            with self.lock:
                self.pending.pop(number, None)

    def start(self, sdp: str, instructions: str, seed: list, voice: str) -> str:
        self.request("initialize", {"clientInfo": {"name": "speakeasy", "version": "0.1"},
                                    "capabilities": {"experimentalApi": True}})
        self._write({"method": "initialized", "params": {}})
        thread = self.request("thread/start", {
            "cwd": str(Path.home()), "sandbox": "read-only", "approvalPolicy": "never", "ephemeral": True,
            "developerInstructions": "Voice transport only. Do not run tools, commands, or Codex tasks. "
                                     "All tasks are delegated by the Speakeasy server to Hermes."})
        self.thread_id = thread["thread"]["id"]
        initial = []
        for item in seed[-20:]:
            if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
                continue
            parts = item.get("content") or []
            text = " ".join(part.get("text", "") for part in parts
                            if isinstance(part, dict) and isinstance(part.get("text"), str))
            if text:
                initial.append({"role": item["role"], "text": text[:2000]})
        params: dict[str, Any] = {
            "threadId": self.thread_id, "version": "v3", "model": CODEX_MODEL, "voice": voice,
            "outputModality": "audio", "transport": {"type": "webrtc", "sdp": sdp},
            "includeStartupContext": False, "clientManagedHandoffs": True,
            "prompt": instructions + "\nOnly the Speakeasy server may execute tasks via Hermes. "
                                     "Do not claim a task was completed until the server supplies its result."}
        if initial:
            params["initialItems"] = initial
        self.request("thread/realtime/start", params, timeout=35)
        deadline = time.monotonic() + 20
        answer, started, deferred = None, False, []
        try:
            while time.monotonic() < deadline and not self.closed:
                try:
                    event = self.notifications.get(timeout=min(.5, max(.01, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                method, p = event.get("method"), event.get("params") or {}
                if method == "thread/realtime/sdp" and p.get("threadId") == self.thread_id:
                    answer = p.get("sdp")
                elif method == "thread/realtime/started" and p.get("threadId") == self.thread_id:
                    started = True
                elif method in {"thread/realtime/error", "thread/realtime/closed", "transport/eof"}:
                    self._fail(str(p.get("message") or ""))
                else:
                    deferred.append(event)
                if answer and started:
                    mark_signed_in(self.binary)
                    logger.info("speakeasy: codex voice started with %s", self.binary)
                    return answer
            raise TimeoutError("Codex realtime startup incomplete")
        finally:
            for event in deferred:
                try:
                    self.notifications.put_nowait(event)
                except queue.Full:
                    self.closed = True

    def stop(self) -> None:
        if self.process.poll() is None:
            if self.thread_id:
                try:
                    self.request("thread/realtime/stop", {"threadId": self.thread_id}, timeout=3)
                except (RuntimeError, TimeoutError, OSError):
                    pass
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.closed = True


class CodexSidebandWorker(SidebandWorker):
    """Reads app-server notifications; only a provider-owned `handoff_request` starts Hermes work."""

    def __init__(self, rt, interaction, transport: CodexTransport):
        super().__init__(rt, interaction)
        self.transport = transport
        self.offset = 0

    def call_connected(self) -> bool:
        return not self.transport.closed and not self.interaction.call_closed

    def stop_transport(self) -> None:
        self.transport.stop()

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.connected_at = time.monotonic()  # the transport started during admission; events flow from here
        with self.interaction.lock:
            self.interaction.status = "listening"
        self.publish()
        try:
            while time.monotonic() - self.transport.started_at < self.transport.max_call_s:
                try:
                    event = await asyncio.to_thread(self.transport.notifications.get, True, .5)
                except queue.Empty:
                    if self.transport.closed:
                        break
                    continue
                method, params = event.get("method"), event.get("params") or {}
                if params.get("threadId") not in (None, self.transport.thread_id):
                    continue
                if method == "thread/realtime/itemAdded":
                    await self._handoff(params.get("item"))
                elif method == "thread/realtime/transcript/done":
                    if params.get("role") == "user":
                        await self._user_turn(params.get("text"))
                    elif params.get("role") == "assistant" and isinstance(params.get("text"), str):
                        await self.handle_event({"type": "session.output_transcript.delta", "delta": params["text"],
                                                 "start_ms": self.offset, "end_ms": self.offset})
                elif method == "thread/realtime/closed":
                    await self.handle_event({"type": "session.closed"})
                    break
                elif method in {"thread/realtime/error", "transport/eof"}:
                    break
        finally:
            self.transport.stop()
            if self.interaction.finalization != "confirmed":
                with self.interaction.lock:
                    if self.interaction.paused:
                        self.interaction.finalization = "confirmed"
                    else:
                        self.interaction.finalization = "incomplete"
                        self.interaction.status = "error"
                        self.interaction.error = "Voice transport ended without a confirmed close"
            self.call_closed()
            if self.dispatch_tasks:
                await asyncio.gather(*self.dispatch_tasks, return_exceptions=True)
            self.publish()

    async def _user_turn(self, text: Any) -> None:
        if not isinstance(text, str):
            return
        text = clean_transcript(text).strip()
        if not text or len(text.encode()) > 8192:
            return
        self.offset += 1
        await self.handle_event({"type": "session.input_transcript.delta", "delta": text,
                                 "start_ms": self.offset, "end_ms": self.offset})

    async def _handoff(self, item: Any) -> None:
        """Only a provider-owned handoff_request can start Hermes work; transcripts never do."""
        if not isinstance(item, dict):
            return
        if item.get("type") != "handoff_request":
            # Logged so a handoff Codex shapes differently shows up instead of vanishing.
            if "handoff" in str(item.get("type") or ""):
                logger.warning("speakeasy: ignored Codex realtime item %r", str(item.get("type"))[:60])
            return
        handoff_id = item.get("handoff_id")
        request = item.get("input_transcript")
        if (not isinstance(handoff_id, str) or not ID_RE.fullmatch(handoff_id)
                or not isinstance(request, str) or not clean_transcript(request).strip()
                or len(request.encode()) > 8192 or handoff_id in self.delegations):
            logger.warning("speakeasy: rejected Codex handoff (id ok: %s, request chars: %s, repeat: %s)",
                           isinstance(handoff_id, str) and bool(ID_RE.fullmatch(handoff_id)),
                           len(request) if isinstance(request, str) else None,
                           handoff_id in self.delegations)
            return
        logger.info("speakeasy: Codex handoff received (%s chars)", len(request))
        history = item.get("active_transcript")
        lines = []
        if isinstance(history, list):
            for turn in history[-12:]:
                if not isinstance(turn, dict) or turn.get("role") not in {"user", "assistant"}:
                    continue
                text = turn.get("text")
                if isinstance(text, str) and len(text.encode()) <= 8192:
                    cleaned = clean_transcript(text).strip()
                    if cleaned:
                        lines.append(f"{turn['role'].title()}: {cleaned}")
        cleaned_request = clean_transcript(request).strip()
        if not lines or not lines[-1].startswith("User: ") or cleaned_request not in lines[-1]:
            lines.append(f"User: {cleaned_request}")
        context = "\n".join(lines)[-10000:]
        self.delegations.add(handoff_id)
        self.touch()
        self.schedule_dispatch(handoff_id, context, item.get("task_id") or item.get("follow_up_task_id"))

    async def _send(self, kind: str, delegation_id: str | None, content: str) -> None:
        if self.transport.closed or len(content) > 2000:
            return
        method = "thread/realtime/appendSpeech" if kind == "session.commentary.append" else "thread/realtime/appendText"
        params: dict[str, Any] = {"threadId": self.transport.thread_id, "text": content}
        if method.endswith("appendText"):
            params["role"] = "developer"
        try:
            await asyncio.to_thread(self.transport.request, method, params, 10)
        except (RuntimeError, TimeoutError, OSError):
            # Fail closed: no paid fallback. The Hermes run stays independently tracked.
            with self.interaction.lock:
                self.interaction.error = "Voice update could not be delivered"
            self.publish()
