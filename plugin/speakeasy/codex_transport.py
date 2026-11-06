"""Voice over the user's own ChatGPT sign-in: the Codex app-server realtime transport (default).

Runs ``codex app-server --stdio --enable realtime_conversation`` per call with the voice model
``gpt-live-1-codex`` over WebRTC. The app-server protocol is experimental: Speakeasy only uses it as
a voice transport. It never gives Codex Hermes credentials and never runs Codex turns or tools for
tasks; every task goes to the user's Hermes.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from .calls import TYPED_ECHO_S, TYPED_NOT_CONNECTED, TYPED_NOT_DELIVERED, TYPED_SENT, SidebandWorker, _words
from .prompt import builder as P
from .text import ID_RE, clean_transcript


def _same_words(typed: str, heard: str) -> bool:
    """A transcript that says what was typed: the same words ignoring case and punctuation, or, for
    four words or more, at least 80% of them in order (an echo that reworded a word or two). Short
    turns ("yes", "turn it off") must match exactly, so a different short answer is never eaten."""
    a, b = _words(typed).replace("'", "").split(), _words(heard).replace("'", "").split()
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.8

REPEAT_HANDOFF_S = 8.0  # the same words handed off again this soon are a repeat, not a new request

CODEX_MODEL = "gpt-live-1-codex"
_ENV_KEEP = ("HOME", "PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "CODEX_HOME")
logger = logging.getLogger(__name__)
_LOGIN_CACHE: dict[str, tuple[float, tuple[bool, str]]] = {}


class CodexStartError(RuntimeError):
    """A voice start failure with a message that is safe and useful to show the user, and a short
    reason code the apps map to an action (``codex_not_signed_in`` → Sign in to ChatGPT,
    ``codex_plan_no_voice`` → Grok / API key)."""

    def __init__(self, message: str, reason: str | None = None):
        super().__init__(message)
        self.reason = reason


PLAN_NO_VOICE = ("Your ChatGPT plan doesn't include voice through Codex (or your workspace turned it off). "
                 "Use Grok or an OpenAI API key for voice instead: Settings › Voice.")
NOT_SIGNED_IN = ("Codex isn't signed in to ChatGPT on the Hermes computer. Tap Sign in to ChatGPT in the app, "
                 "or run codex login there.")


def plan_lacks_voice(message: str) -> bool:
    """Codex's error says the account/plan can't use realtime voice (not a sign-in problem)."""
    text = message.lower()
    plan = any(k in text for k in ("plan", "subscription", "tier", "workspace", "entitle", "upgrade", "access"))
    voice = any(k in text for k in ("realtime", "voice", "gpt-live", "audio"))
    refused = any(k in text for k in ("not available", "not included", "not supported on", "not enabled",
                                      "does not have access", "doesn't have access", "do not have access",
                                      "not allowed", "not entitled", "requires", "upgrade", "disabled",
                                      "403", "forbidden", "unavailable"))
    if "model_not_found" in text and ("gpt-live" in text or "realtime" in text):
        return True
    return plan and voice and refused


def codex_error_reason(message: str) -> str | None:
    if "rate" in message.lower() and "limit" in message.lower():
        return None
    if plan_lacks_voice(message):
        return "codex_plan_no_voice"
    text = message.lower()
    if "didn't provide an api key" in text or "not logged in" in text:
        return "codex_not_signed_in"
    if is_auth_error(message):
        return "codex_not_signed_in"
    return None


def explain_codex_error(message: str) -> str:
    """Map Codex's error text to a plain reason. Only known shapes pass through; nothing else
    from the provider is reflected."""
    text = message.lower()
    if "rate" in text and "limit" in text:
        return "ChatGPT's voice limit was reached. Try again later or switch to an OpenAI API key."
    if plan_lacks_voice(message):
        return PLAN_NO_VOICE
    if "unknown variant" in text and ("v3" in text or "version" in text):
        return ("This Codex is too old for voice. Update it (npm install -g @openai/codex@latest), "
                "or run hermes voice setup to use a current copy.")
    if "voice" in text and "not supported" in text:
        return "That voice isn't available. Pick another voice in Speakeasy Settings."
    if "didn't provide an api key" in text or "not logged in" in text:
        return NOT_SIGNED_IN
    if "login" in text or "auth" in text or "unauthorized" in text or "401" in text:
        return ("OpenAI rejected Codex's saved sign-in (it may have expired or been signed out). "
                "Tap Sign in to ChatGPT in the app, or run codex login again on the Hermes machine.")
    if "rate" in text and "limit" in text:
        return "ChatGPT's voice limit was reached. Try again later or switch to an OpenAI API key."
    return "Codex couldn't start the voice call."


def is_auth_error(message: str) -> bool:
    """A start failure that means the saved sign-in doesn't work, whatever `codex login status` says."""
    text = message.lower()
    return any(k in text for k in ("401", "unauthorized", "api key", "not logged in", "login", "auth"))


_REJECTED: dict[str, tuple[float, str]] = {}
# How long a call's sign-in rejection overrides `codex login status`. Speakeasy never looks at
# Codex's own credential files, so it cannot see a fresh `codex login`; it trusts the status check
# again after this long, or at once when a call succeeds.
REJECTED_HOLD_S = 120.0


def mark_signed_out(binary: Path | None, reason: str) -> None:
    """A call just proved the saved sign-in doesn't work. `codex login status` only checks that a
    sign-in is saved, so it keeps saying "logged in"; Settings shows "not signed in" for a while."""
    if binary is not None:
        _REJECTED[str(binary)] = (time.monotonic(), reason)
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
        return False, "Codex not found. Install the ChatGPT desktop app (it includes Codex) or the Codex CLI, then run `hermes voice setup`."
    rejected = _REJECTED.get(str(binary))
    if rejected is not None:
        if time.monotonic() - rejected[0] < REJECTED_HOLD_S:
            return False, rejected[1]
        _REJECTED.pop(str(binary), None)  # long enough: trust `codex login status` again
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
            result = (ok, "Signed in to Codex with ChatGPT." if ok else "Codex is not signed in on this computer. Run `codex login` here. Hermes' own ChatGPT/Codex sign-in is separate and doesn't carry over.")
    except (OSError, subprocess.SubprocessError) as exc:
        result = (False, f"Could not run Codex ({type(exc).__name__}).")
    _LOGIN_CACHE[str(binary)] = (time.monotonic(), result)
    return result


class CodexTransport:
    """One `codex app-server` process speaking JSON-RPC lines on stdio."""

    def __init__(self, binary: Path | None, workdir: Path, max_call_s: float = 1800,
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
        reason = codex_error_reason(message)
        if reason == "codex_not_signed_in":
            mark_signed_out(self.binary, explained)
        raise CodexStartError(explained, reason)

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
            # No canned "mm-hm" filler while a handoff starts: the voice's own short acknowledgement
            # is enough, and the filler repeats ("On it. Mm-hm. Mm-hm.").
            "delegationAckFiller": False,
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
    HOLD_QUIET_NOTES = True

    def __init__(self, rt, interaction, transport: CodexTransport):
        super().__init__(rt, interaction)
        self.transport = transport
        self.offset = 0
        # Typed turns sent as realtime user text, as [text, when Codex took it]: if Codex transcribes
        # one back as the user's turn, it is the same turn and is not recorded twice (_typed_echo).
        self.typed_echoes: deque[list[Any]] = deque(maxlen=8)

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
            while True:
                if self.cap_reached(time.monotonic() - self.transport.started_at, self.transport.max_call_s):
                    await self.pause_at_cap()  # resumable, not "ended without a confirmed close"
                    break
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
        if self._typed_echo(text):
            return  # Codex's transcript of a turn already recorded as typed
        self.offset += 1
        await self.handle_event({"type": "session.input_transcript.delta", "delta": text,
                                 "start_ms": self.offset, "end_ms": self.offset})

    def _typed_echo(self, text: str) -> bool:
        """Is this user transcript Codex's echo of a typed turn? Bounded three ways, because a wrong
        yes drops a real spoken turn: only within TYPED_ECHO_S of the voice taking the turn, each
        typed turn suppresses at most one transcript, and the first user transcript that isn't a
        match ends every pending window (an echo comes as the next user turn, so the user is now
        talking and anything they say, the same words included, is theirs). Words are compared
        loosely (case, punctuation, a reworded word or two) so a reworded echo isn't recorded twice."""
        now = time.monotonic()
        live = [entry for entry in self.typed_echoes if now - entry[1] <= TYPED_ECHO_S]
        self.typed_echoes.clear()
        for index, entry in enumerate(live):
            if _same_words(entry[0], text):
                # Consumed; any older typed turn still waiting never echoed, so its window closes too.
                self.typed_echoes.extend(live[index + 1:])
                return True
        return False

    async def forward_typed(self, text: str) -> str:
        """Codex takes a typed turn as realtime user text: `thread/realtime/appendText` with role user
        (the protocol's ConversationTextRole; the server's own notes go in as developer). A failed
        request is this send's failure only (not_delivered): unlike `_send` it never writes
        interaction.error, and the call stays usable for the next turn."""
        if self.transport.closed:
            return TYPED_NOT_CONNECTED
        echo = [text, time.monotonic()]
        self.typed_echoes.append(echo)
        try:
            await asyncio.to_thread(self.transport.request, "thread/realtime/appendText",
                                    {"threadId": self.transport.thread_id, "text": text, "role": "user"}, 10)
        except (RuntimeError, TimeoutError, OSError) as exc:
            self._drop_echo(echo)
            logger.warning("speakeasy: Codex did not take a typed turn (%s)", type(exc).__name__)
            return TYPED_NOT_CONNECTED if self.transport.closed else TYPED_NOT_DELIVERED
        except asyncio.CancelledError:
            # The route gave up waiting (504). Whether Codex got the text is unknown, and this turn
            # won't be recorded as typed, so no guard may outlive it: if Codex does echo it, that
            # echo is the transcript's only copy and must be kept.
            self._drop_echo(echo)
            raise
        echo[1] = time.monotonic()  # the echo window runs from when Codex took it
        return TYPED_SENT

    def _drop_echo(self, echo: list) -> None:
        for index, entry in enumerate(self.typed_echoes):
            if entry is echo:
                del self.typed_echoes[index]
                return

    def typed_offset(self) -> int:
        self.offset += 1
        return self.offset

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
        # The voice sometimes hands off the same words again when nothing came back quickly;
        # that repeat would reach the running task as a duplicate follow-up.
        said, now = clean_transcript(request).strip().lower(), time.monotonic()
        last = getattr(self, "last_handoff", None)
        self.last_handoff = (said, now)
        if last and last[0] == said and now - last[1] < REPEAT_HANDOFF_S:
            logger.info("speakeasy: ignored a repeated Codex handoff")
            self.delegations.add(handoff_id)
            return
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
        # Acknowledge right away: background notes are held on this voice (they'd draw "mm-hmm"),
        # so without this cue the voice waits several seconds and may hand the words off again.
        await self.append("session.thinking.append", None, P.ack_now_note(cleaned_request))
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
