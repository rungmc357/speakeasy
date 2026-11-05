"""Secure input: a key a skill needs, the Mac password for one sudo command, or a website login /
one-time code the browser needs, typed into a secure card in the app and handed straight to Hermes,
never to the model, a log or a transcript.

How Hermes asks (see FINDINGS in the repo's docs/SECURE_INPUT.md):

- A skill whose ``required_environment_variables`` are missing calls the turn's secret-capture
  callback (``tools.skills_tool._turn_secret_capture``, bound by ``bind_turn_secret_capture``) with
  ``(env_var, prompt, metadata)``; a non-empty answer is saved with
  ``hermes_cli.config.save_env_value_secure`` and the callback returns Hermes' result dict.
- ``sudo`` in a terminal command calls the thread's sudo callback
  (``tools.terminal_tool._get_sudo_password_callback``) and pipes the answer to ``sudo -S``.
- ``browser_vault action=save_login`` / ``enter_code`` call the thread's vault prompts
  (``agent.vault_backends.unlock.get_save_login_prompt_callback`` / ``get_code_prompt_callback``,
  gated by ``can_prompt_here``); the login goes into Hermes' encrypted vault bound to the page
  origin and is filled model-blind; the code is typed into the page once.

Hermes binds both only for a *local viewer's* turn (Desktop / Mobile over the session socket:
``gateway/session_secret_prompt.py``, ``gateway/session_sudo_prompt.py``,
``gateway/session_vault_prompt.py``). Speakeasy's jobs are
``/v1/runs`` turns (platform ``api_server``), where neither is bound, so a skill gets "secure
secret entry is not supported" and sudo fails. :func:`install` adds a *fallback*: when no callback is
bound and the current turn is a live Speakeasy run (its approval session key is that run's id, or
its session is a Speakeasy task session, on the ``api_server`` platform), the lookup returns this
module's callback. Every other turn sees exactly what it saw before.

What the value touches: the request body of ``POST /voice/secrets/{id}`` (read into a bytearray
that is zeroed after the answer is handed over), the agent thread waiting on that request, and then
Hermes (``.env`` for a key, ``sudo -S`` stdin for a password). Nothing here logs it, stores it in
``state.sqlite3``, publishes it, or returns it. A keyed fingerprint (never the value) stays in
memory so later job output that echoes it is redacted before it leaves the plugin (:meth:`scrub`).
"""
from __future__ import annotations

import contextlib
import dataclasses
import hmac
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

TIMEOUT_S = 120            # Hermes' own deadline for both prompts (session_secret_prompt / session_sudo_prompt)
LOGIN_TIMEOUT_S = 180      # gateway/session_vault_prompt.py SAVE_LOGIN_TIMEOUT_S / CODE_TIMEOUT_S
MAX_VALUE_CHARS = 4096
MAX_BODY_BYTES = 16 * 1024
RATE_WINDOW_S = 60.0
HELD_CODE_S = 180          # a code typed with a login waits this long for the site to ask for it
RATE_MAX = 12              # answers per device per minute (wrong ids included)
MAX_PENDING = 8
REDACTED = "[redacted]"
SKIPPED_MESSAGE = "Secret setup was skipped."
STORED_MESSAGE = "Secret stored securely. The secret value was not exposed to the model."
VOICE_LINE = "I need a key to continue — I've put a secure box on your screen."
VOICE_LINE_LOGIN = "I need a login to continue — I've put a secure box on your screen."
VOICE_LINE_SUDO = "I need your Mac's password to run one command — I've put a secure box on your screen."
VOICE_RULE = ("A secure box is on the user's screen for a key or password the task needs. Never ask them to say "
              "or spell a key, password or code out loud. If they start reading one out, stop them gently, do "
              "not repeat any of it, and point them to the secure box. Don't mention it again unless asked.")
_ENV_RE = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_GENERIC_PROMPT = re.compile(r"^\s*enter (a )?value for\b", re.I)
_WORDS = {"API": "API", "KEY": "key", "TOKEN": "token", "SECRET": "secret", "ID": "ID", "URL": "URL",
          "PASSWORD": "password", "OAUTH": "OAuth", "CLIENT": "client", "ACCESS": "access"}


class SecureInputError(Exception):
    def __init__(self, status: int, message: str, reason: str | None = None):
        super().__init__(message)
        self.status, self.message, self.reason = status, message, reason


@dataclasses.dataclass
class Target:
    """The Speakeasy job a waiting turn belongs to (resolved by the service)."""
    key: str
    run_id: str | None
    session_id: str | None
    title: str
    device_id: str = ""      # the device that owns the job (its call); "" = any paired device


@dataclasses.dataclass
class Pending:
    id: str
    kind: str                # "secret" | "sudo"
    target: Target
    env_var: str = ""
    label: str = ""
    prompt: str = ""
    help: str = ""
    skill: str = ""
    command: str = ""
    origin: str = ""         # login: the page origin the login is saved for
    site: str = ""           # login / code: the host the user recognises
    created: float = 0.0
    expires: float = 0.0     # monotonic
    expires_at: float = 0.0  # wall clock, for the app's countdown
    done: threading.Event = dataclasses.field(default_factory=threading.Event)
    outcome: str = ""        # "", "provided", "skipped", "expired", "cancelled"
    _value: bytearray | None = dataclasses.field(default=None, repr=False)

    def view(self) -> dict[str, Any]:
        """What the app shows. Never carries a value."""
        out: dict[str, Any] = {
            "id": self.id, "kind": self.kind, "job": self.target.title, "run_id": self.target.run_id,
            "session_id": self.target.session_id, "expires_at": round(self.expires_at, 3),
            "seconds_left": max(0, int(self.expires - time.monotonic())),
        }
        if self.kind == "secret":
            out.update({"env_var": self.env_var, "label": self.label, "prompt": self.prompt, "help": self.help,
                        "skill": self.skill,
                        "stored_where": f"Saved to your Hermes's private settings as {self.env_var}. The AI never sees it."})
        elif self.kind == "sudo":
            out.update({"command": self.command, "label": "Your Mac's password",
                        "stored_where": "Used once for this command, then forgotten. Speakeasy never stores it."})
        elif self.kind == "login":
            out.update({"origin": self.origin, "site": self.site, "label": f"Your login for {self.site}",
                        "stored_where": (f"Saved in your Hermes's encrypted vault for {self.site}. The AI can fill it "
                                         "in on that site but never sees the password.")})
        else:  # code
            out.update({"site": self.site, "label": f"The code {self.site} sent you", "help": self.help,
                        "stored_where": "Typed into the page once. Never saved."})
        return out


def friendly_label(env_var: str, prompt: str | None) -> str:
    """"OpenWeather API key" from the skill's prompt when it says so, else from the variable name."""
    text = " ".join((prompt or "").split())
    if text and not _GENERIC_PROMPT.match(text) and len(text) <= 60:
        return text.rstrip(":.")
    words = [w for w in env_var.split("_") if w]
    out = [_WORDS.get(w, w.capitalize()) for w in words]
    return " ".join(out) or "key"


def _wipe(buf: bytearray | None) -> None:
    if buf is not None:
        for i in range(len(buf)):
            buf[i] = 0


class _Fingerprints:
    """Keyed rolling-hash fingerprints of entered values, so output that echoes one can be redacted
    without keeping the value. Rabin-Karp over code points with a random base, plus an HMAC
    confirm on a hit. In memory only."""

    _MOD = (1 << 61) - 1

    def __init__(self) -> None:
        self._key = secrets.token_bytes(32)
        self._base = secrets.randbelow(self._MOD - 1000) + 257
        self._by_len: dict[int, set[tuple[int, bytes]]] = {}
        self._lock = threading.Lock()

    def _roll(self, text: str) -> int:
        h = 0
        for ch in text:
            h = (h * self._base + ord(ch)) % self._MOD
        return h

    def _mac(self, text: str) -> bytes:
        return hmac.new(self._key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).digest()

    def add(self, value: str) -> None:
        if len(value) < 6:  # too short to redact without mangling ordinary words
            return
        with self._lock:
            self._by_len.setdefault(len(value), set()).add((self._roll(value), self._mac(value)))

    def any(self) -> bool:
        return bool(self._by_len)

    def scrub(self, text: str) -> str:
        if not self._by_len or not text:
            return text
        with self._lock:
            groups = {n: set(v) for n, v in self._by_len.items()}
        for n, prints in groups.items():
            if len(text) < n:
                continue
            rolls = {r for r, _ in prints}
            macs = {m for _, m in prints}
            top = pow(self._base, n - 1, self._MOD)
            h = self._roll(text[:n])
            hits: list[int] = []
            i = 0
            while True:
                if h in rolls and self._mac(text[i:i + n]) in macs:
                    hits.append(i)
                if i + n >= len(text):
                    break
                h = ((h - ord(text[i]) * top) * self._base + ord(text[i + n])) % self._MOD
                i += 1
            if hits:
                parts, last = [], 0
                for start in hits:
                    if start < last:
                        continue
                    parts.append(text[last:start])
                    parts.append(REDACTED)
                    last = start + n
                parts.append(text[last:])
                text = "".join(parts)
        return text


class SecureInputs:
    """Pending secure requests: created on the agent thread (which then waits), answered once over
    the paired channel. ``notify(pending)`` tells the user (board, push, voice); ``settled(pending)``
    after it closes."""

    def __init__(self, notify: Callable[[Pending], None] | None = None,
                 settled: Callable[[Pending], None] | None = None,
                 clock: Callable[[], float] = time.monotonic, timeout: float = TIMEOUT_S):
        self.notify = notify or (lambda p: None)
        self.settled = settled or (lambda p: None)
        self.clock = clock
        self.timeout = timeout
        self._lock = threading.Lock()
        self._pending: dict[str, Pending] = {}
        self._attempts: dict[str, list[float]] = {}
        self._sudo_granted: set[str] = set()   # Hermes session keys whose cached admin password must go
        # A one-time code typed on an Add-a-login card, held (in memory, briefly) for that job's next
        # code prompt on the same site: (job key, site) -> (bytes, expires).
        self._held_codes: dict[tuple[str, str], tuple[bytearray, float]] = {}
        self.prints = _Fingerprints()

    # -- the agent side ----------------------------------------------------------------------
    def ask(self, kind: str, target: Target, *, env_var: str = "", prompt: str = "", metadata: dict | None = None,
            command: str = "", origin: str = "", site: str = "", hint: str = "", timeout: float | None = None,
            cancelled: Callable[[], bool] = lambda: False) -> bytearray | None:
        """Open one request and block until it is answered (the value, as a bytearray the caller
        must wipe), skipped, cancelled or expired (``None``)."""
        meta = metadata if isinstance(metadata, dict) else {}
        now = self.clock()
        wait = min(self.timeout, timeout) if timeout is not None else self.timeout
        pending = Pending(id="sec_" + secrets.token_urlsafe(18), kind=kind, target=target, env_var=env_var,
                          label=friendly_label(env_var, prompt) if kind == "secret" else "",
                          prompt=_clip(prompt, 300) if kind == "secret" else "",
                          help=_clip(meta.get("help") if kind == "secret" else hint, 300),
                          skill=_clip(meta.get("skill_name"), 80), command=_clip(command, 4000),
                          origin=_clip(origin, 300), site=_clip(site, 120),
                          created=now, expires=now + wait, expires_at=time.time() + wait)
        with self._lock:
            if len(self._pending) >= MAX_PENDING:
                return None
            self._pending[pending.id] = pending
        try:
            self.notify(pending)
        except Exception as exc:  # telling the user must never break the turn
            logger.warning("speakeasy: secure input notice failed: %s", type(exc).__name__)
        try:
            while not pending.done.is_set():
                left = pending.expires - self.clock()
                if left <= 0:
                    pending.outcome = pending.outcome or "expired"
                    break
                if cancelled():
                    pending.outcome = pending.outcome or "cancelled"
                    break
                pending.done.wait(min(0.25, left))
        finally:
            with self._lock:
                self._pending.pop(pending.id, None)
                value, pending._value = pending._value, None
            try:
                self.settled(pending)
            except Exception as exc:
                logger.warning("speakeasy: secure input settle failed: %s", type(exc).__name__)
        if pending.outcome != "provided" or not value:
            _wipe(value)
            return None
        return value

    # -- the app side ------------------------------------------------------------------------
    def visible(self, device_id: str, *, session_id: str | None = None, key: str | None = None) -> list[dict[str, Any]]:
        now = self.clock()
        with self._lock:
            live = [p for p in self._pending.values() if p.expires > now and not p.done.is_set()]
        out = []
        for p in sorted(live, key=lambda p: p.created):
            if p.target.device_id and p.target.device_id != device_id:
                continue
            if session_id is not None and p.target.session_id != session_id:
                continue
            if key is not None and p.target.key != key:
                continue
            out.append(p.view())
        return out

    def pending_for(self, *, key: str | None = None, session_id: str | None = None) -> bool:
        with self._lock:
            return any((key is None or p.target.key == key) and (session_id is None or p.target.session_id == session_id)
                       for p in self._pending.values())

    def _find(self, request_id: str) -> Pending | None:
        """Constant-time over every open id (no early exit, no dict lookup on the guess)."""
        found = None
        supplied = request_id.encode()
        with self._lock:
            for pid, p in self._pending.items():
                if hmac.compare_digest(pid.encode(), supplied):
                    found = p
        return found

    def _limited(self, device_id: str) -> bool:
        now = self.clock()
        with self._lock:
            hits = [t for t in self._attempts.get(device_id, []) if now - t < RATE_WINDOW_S]
            hits.append(now)
            self._attempts[device_id] = hits
            return len(hits) > RATE_MAX

    def answer(self, request_id: str, device_id: str, raw: bytearray) -> dict[str, Any]:
        """``POST /voice/secrets/{id}``: ``{"value": "..."}`` or ``{"action": "skip"}``. ``raw`` is
        zeroed before this returns, whatever happens. The reply never carries the value."""
        try:
            if self._limited(device_id):
                raise SecureInputError(429, "Too many tries. Wait a minute.", "rate_limited")
            pending = self._find(request_id) if isinstance(request_id, str) else None
            if pending is None or pending.done.is_set() or pending.expires <= self.clock():
                raise SecureInputError(404, "That request has closed.", "closed")
            if pending.target.device_id and not hmac.compare_digest(pending.target.device_id.encode(), device_id.encode()):
                raise SecureInputError(403, "Answer this on the device the task is running for.", "wrong_device")
            try:
                body = json.loads(bytes(raw).decode("utf-8")) if raw else None
            except (ValueError, UnicodeDecodeError):
                raise SecureInputError(400, "Send a value or skip.") from None
            login_keys = {"identifier", "password"}
            if pending.kind == "login" and isinstance(body, dict) and login_keys <= set(body) <= login_keys | {"code"}:
                return self._close(pending, "provided", self._login_value(body))
            if not isinstance(body, dict) or not (set(body) == {"value"} or body == {"action": "skip"}):
                raise SecureInputError(400, "Send a value or skip.")
            if body.get("action") == "skip":
                return self._close(pending, "skipped", None)
            if pending.kind == "login":
                raise SecureInputError(400, "Send a username and password, or skip.")
            value = body.get("value")
            body.clear()
            if not isinstance(value, str) or not value.strip():
                raise SecureInputError(400, "The value is empty.")
            if len(value) > MAX_VALUE_CHARS or any(c in value for c in "\r\n\x00"):
                raise SecureInputError(400, "That value can't be saved (too long, or it has a line break).")
            if pending.kind == "secret":
                value = value.strip()
            buf = bytearray(value.encode("utf-8"))
            del value
            return self._close(pending, "provided", buf)
        finally:
            _wipe(raw)

    @staticmethod
    def _login_value(body: dict[str, Any]) -> bytearray:
        """``{identifier, password, code?}`` → the bytes the save-login callback reads (JSON)."""
        identifier, password, code = body.get("identifier"), body.get("password"), body.get("code")
        body.clear()
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 320:
            raise SecureInputError(400, "Enter the username or email.")
        if not isinstance(password, str) or not password or len(password) > MAX_VALUE_CHARS \
                or any(c in password for c in "\r\n\x00"):
            raise SecureInputError(400, "Enter the password.")
        out: dict[str, str] = {"identifier": identifier.strip(), "password": password}
        if isinstance(code, str) and code.strip():
            clean = code.strip().replace(" ", "").replace("-", "")
            if not clean.isalnum() or len(clean) > 12:
                raise SecureInputError(400, "That code doesn't look right.")
            out["code"] = clean
        buf = bytearray(json.dumps(out).encode("utf-8"))
        out.clear()
        del password
        return buf

    def _close(self, pending: Pending, outcome: str, value: bytearray | None) -> dict[str, Any]:
        with self._lock:
            if pending.done.is_set() or self._pending.get(pending.id) is not pending:
                _wipe(value)
                raise SecureInputError(404, "That request has closed.", "closed")
            pending.outcome, pending._value = outcome, value
            pending.done.set()   # single use: nothing can answer it again
        if outcome == "skipped":
            return {"ok": True, "outcome": "skipped"}
        if pending.kind == "secret":
            return {"ok": True, "outcome": "sent", "stored_as": pending.env_var}
        if pending.kind == "login":
            return {"ok": True, "outcome": "sent", "site": pending.site}
        return {"ok": True, "outcome": "sent"}

    def cancel_key(self, key: str) -> None:
        """A job stopped: close its requests (the agent side returns Hermes' skipped result)."""
        with self._lock:
            for p in self._pending.values():
                if p.target.key == key and not p.done.is_set():
                    p.outcome = "cancelled"
                    p.done.set()

    def hold_code(self, key: str, site: str, code: str) -> None:
        with self._lock:
            old = self._held_codes.pop((key, site), None)
            if old:
                _wipe(old[0])
            self._held_codes[(key, site)] = (bytearray(code.encode()), self.clock() + HELD_CODE_S)

    def take_code(self, key: str, site: str) -> str:
        with self._lock:
            held = self._held_codes.pop((key, site), None)
            for k in [k for k, (_, exp) in self._held_codes.items() if exp <= self.clock()]:
                _wipe(self._held_codes.pop(k)[0])
        if held is None:
            return ""
        buf, expires = held
        try:
            return bytes(buf).decode() if expires > self.clock() else ""
        finally:
            _wipe(buf)

    # -- redaction ---------------------------------------------------------------------------
    def remember(self, value: str) -> None:
        """Fingerprint the value as typed and as JSON would escape it (both json.dumps modes)."""
        self.prints.add(value)
        for ascii_only in (True, False):
            escaped = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
            if escaped != value:
                self.prints.add(escaped)

    def scrub(self, text: str) -> str:
        return self.prints.scrub(text)

    def scrub_json(self, raw: str) -> str:
        """Redact both the plain and the JSON-escaped form of an entered value."""
        if not self.prints.any():
            return raw
        return self.prints.scrub(raw)


def _clip(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    text = "".join(c for c in text if c == "\n" or c >= " ").strip()
    return text[:limit]


# -- the Hermes bridge ----------------------------------------------------------------------------

_ACTIVE: dict[str, Any] = {"inputs": None, "resolve": None}
_SECRET_PROXY_ATTR = "_speakeasy_secure_input"


def _current_turn() -> tuple[str, str, str]:
    """(approval session key, session id, platform) of the turn on this thread: ContextVars only, so
    another session's process-wide env never leaks in."""
    session_key = session_id = platform = ""
    with contextlib.suppress(Exception):
        from tools import approval_context
        session_key = approval_context._approval_session_key.get() or ""
    with contextlib.suppress(Exception):
        from gateway import session_context as sc
        unset = getattr(sc, "_UNSET", object())
        for name in ("HERMES_SESSION_ID", "HERMES_SESSION_PLATFORM"):
            var = sc._VAR_MAP.get(name)
            value = var.get() if var is not None else unset
            if value is not unset and isinstance(value, str):
                if name == "HERMES_SESSION_ID":
                    session_id = value
                else:
                    platform = value
        if not session_key:
            var = sc._VAR_MAP.get("HERMES_SESSION_KEY")
            value = var.get() if var is not None else unset
            if value is not unset and isinstance(value, str):
                session_key = value
    return session_key, session_id, platform


def current_target() -> Target | None:
    """The live Speakeasy job this thread's turn belongs to, or None (then Hermes behaves as before)."""
    resolve = _ACTIVE.get("resolve")
    if resolve is None or _ACTIVE.get("inputs") is None:
        return None
    session_key, session_id, platform = _current_turn()
    if platform and platform != "api_server":
        return None  # Speakeasy's jobs are /v1/runs turns; a Telegram or Desktop turn is never ours
    try:
        return resolve(session_key, session_id)
    except Exception as exc:
        logger.warning("speakeasy: secure input target lookup failed: %s", type(exc).__name__)
        return None


def _cancelled_for(target: Target) -> Callable[[], bool]:
    def cancelled() -> bool:
        resolve = _ACTIVE.get("resolve")
        if resolve is None:
            return True
        with contextlib.suppress(Exception):
            session_key, session_id, _ = _current_turn()
            return resolve(session_key, session_id) is None
        return False
    return cancelled


def secret_callback(target: Target) -> Callable[..., dict[str, Any]]:
    """Hermes' secret-capture callback shape ``(var_name, prompt, metadata) -> result``."""

    def callback(var_name: str, prompt: str, metadata: dict | None = None) -> dict[str, Any]:
        inputs: SecureInputs | None = _ACTIVE.get("inputs")
        skipped = {"success": True, "reason": "cancelled", "stored_as": var_name, "validated": False,
                   "skipped": True, "message": SKIPPED_MESSAGE}
        if inputs is None or not isinstance(var_name, str) or not _ENV_RE.fullmatch(var_name):
            return skipped
        buf = inputs.ask("secret", target, env_var=var_name, prompt=prompt or "", metadata=metadata,
                         cancelled=_cancelled_for(target))
        if buf is None:
            return skipped
        try:
            value = buf.decode("utf-8")
            from hermes_cli.config import save_env_value_secure
            result = save_env_value_secure(var_name, value)
            inputs.remember(value)
            del value
        except Exception as exc:
            logger.warning("speakeasy: saving %s failed: %s", var_name, type(exc).__name__)
            return {**skipped, "reason": "save_failed", "message": "Secret could not be saved."}
        finally:
            _wipe(buf)
        return {**(result if isinstance(result, dict) else {"success": True, "stored_as": var_name}),
                "skipped": False, "message": STORED_MESSAGE}

    return callback


def sudo_callback(target: Target) -> Callable[[], str]:
    """Hermes' sudo callback shape ``() -> password`` ("" = skip/deny)."""

    def callback() -> str:
        inputs: SecureInputs | None = _ACTIVE.get("inputs")
        if inputs is None:
            return ""
        command = ""
        with contextlib.suppress(Exception):
            from tools.terminal_tool_sudo import get_sudo_prompt_command
            command = get_sudo_prompt_command() or ""
        with contextlib.suppress(Exception):
            from gateway.run import _redact_approval_command  # what Desktop/Mobile show (credentials masked)
            command = _redact_approval_command(command)
        buf = inputs.ask("sudo", target, command=command or "(command not shown)", cancelled=_cancelled_for(target))
        if buf is None:
            return ""
        try:
            value = buf.decode("utf-8")
        finally:
            _wipe(buf)
        session_key, _, _ = _current_turn()
        if session_key:
            with inputs._lock:
                inputs._sudo_granted.add(session_key)
        inputs.remember(value)
        return value

    return callback


def save_login_callback(target: Target) -> Callable[[str, str], dict[str, str] | None]:
    """Hermes' vault save-login prompt ``(origin, site) -> {identifier, password} | None``: the
    browser_vault tool stores the answer in Hermes' encrypted vault for that origin and fills it."""

    def callback(origin: str, site: str) -> dict[str, str] | None:
        inputs: SecureInputs | None = _ACTIVE.get("inputs")
        if inputs is None:
            return None
        buf = inputs.ask("login", target, origin=str(origin or ""), site=str(site or origin or ""),
                         timeout=LOGIN_TIMEOUT_S, cancelled=_cancelled_for(target))
        if buf is None:
            return None
        try:
            data = json.loads(bytes(buf).decode("utf-8"))
        except ValueError:
            return None
        finally:
            _wipe(buf)
        code = data.pop("code", "")
        if code:
            inputs.hold_code(target.key, str(site or origin or ""), code)
        inputs.remember(data.get("password", ""))
        return data  # the vault tool clears it after saving (browser_vault_save_login: answer.clear())

    return callback


def code_callback(target: Target) -> Callable[[str, str], str]:
    """Hermes' vault one-time-code prompt ``(site, hint) -> code`` ("" = declined)."""

    def callback(site: str, hint: str = "") -> str:
        inputs: SecureInputs | None = _ACTIVE.get("inputs")
        if inputs is None:
            return ""
        held = inputs.take_code(target.key, str(site or ""))
        if held:
            return held
        buf = inputs.ask("code", target, site=str(site or ""), hint=str(hint or ""), timeout=LOGIN_TIMEOUT_S,
                         cancelled=_cancelled_for(target))
        if buf is None:
            return ""
        try:
            return bytes(buf).decode("utf-8").strip()
        finally:
            _wipe(buf)

    return callback


def _fallback_getter(original: Callable[[], Any], make: Callable[[Target], Any]) -> Callable[[], Any]:
    """A thread's bound prompt callback when there is one, else (only for a live Speakeasy run) ours."""
    def getter():
        bound = original()
        if bound is not None:
            return bound
        target = current_target()
        return make(target) if target is not None else None
    getter.__wrapped__ = original  # type: ignore[attr-defined]
    getter.__name__ = getattr(original, "__name__", "getter")
    setattr(getter, _SECRET_PROXY_ATTR, True)
    return getter


def _can_prompt_wrapper(original: Callable[[], bool]) -> Callable[[], bool]:
    """``can_prompt_here`` is False on api_server (nobody can answer an unattended API turn); a live
    Speakeasy run has someone: the paired app's secure box."""
    def can_prompt_here() -> bool:
        return bool(original()) or current_target() is not None
    can_prompt_here.__wrapped__ = original  # type: ignore[attr-defined]
    setattr(can_prompt_here, _SECRET_PROXY_ATTR, True)
    return can_prompt_here


def _unwrap(fn: Any) -> Any:
    while getattr(fn, _SECRET_PROXY_ATTR, False) and hasattr(fn, "__wrapped__"):
        fn = fn.__wrapped__
    return fn


class _TurnCaptureProxy:
    """Stands in for ``tools.skills_tool._turn_secret_capture`` (a ContextVar): ``set``/``reset`` go to
    the real one, ``get`` returns the bound callback, else (only for a live Speakeasy run) ours."""

    def __init__(self, real: Any):
        self._real = real
        setattr(self, _SECRET_PROXY_ATTR, True)

    @property
    def name(self) -> str:
        return self._real.name

    def get(self, *default: Any) -> Any:
        value = self._real.get(*default)
        if value is not None:
            return value
        target = current_target()
        return secret_callback(target) if target is not None else value

    def set(self, value: Any) -> Any:
        return self._real.set(value)

    def reset(self, token: Any) -> None:
        self._real.reset(token)


def _sudo_wrapper(original: Callable[[], Any]) -> Callable[[], Any]:
    def _get_sudo_password_callback():
        bound = original()
        if bound is not None:
            return bound
        target = current_target()
        return sudo_callback(target) if target is not None else None
    _get_sudo_password_callback.__wrapped__ = original  # type: ignore[attr-defined]
    setattr(_get_sudo_password_callback, _SECRET_PROXY_ATTR, True)
    return _get_sudo_password_callback


def install(inputs: SecureInputs, resolve: Callable[[str, str], Target | None]) -> dict[str, bool]:
    """Point Hermes' secret and sudo lookups at this service's requests (idempotent; a reload just
    swaps the service). Returns which of the two are wired; a Hermes without them gets neither."""
    _ACTIVE["inputs"], _ACTIVE["resolve"] = inputs, resolve
    wired = {"secret": False, "sudo": False, "login": False}
    try:
        from tools import skills_tool
        current = getattr(skills_tool, "_turn_secret_capture", None)
        if getattr(current, _SECRET_PROXY_ATTR, False):
            current = current._real  # an earlier (maybe reloaded) copy of this module: rewrap with this one
        if current is not None and callable(getattr(current, "get", None)) and callable(getattr(current, "set", None)):
            skills_tool._turn_secret_capture = _TurnCaptureProxy(current)
            wired["secret"] = True
    except Exception as exc:
        logger.warning("speakeasy: secure key entry unavailable on this Hermes: %s", type(exc).__name__)
    try:
        from tools import terminal_tool
        current = getattr(terminal_tool, "_get_sudo_password_callback", None)
        while getattr(current, _SECRET_PROXY_ATTR, False):
            current = current.__wrapped__  # type: ignore[union-attr]
        if callable(current) and callable(getattr(terminal_tool, "set_sudo_password_callback", None)):
            terminal_tool._get_sudo_password_callback = _sudo_wrapper(current)
            wired["sudo"] = True
    except Exception as exc:
        logger.warning("speakeasy: secure admin-password entry unavailable on this Hermes: %s", type(exc).__name__)
    try:
        from agent.vault_backends import unlock as vault_unlock
        ok = True
        for name, make in (("get_save_login_prompt_callback", save_login_callback),
                           ("get_code_prompt_callback", code_callback)):
            current = _unwrap(getattr(vault_unlock, name, None))
            if not callable(current):
                ok = False
                continue
            setattr(vault_unlock, name, _fallback_getter(current, make))
        current = _unwrap(getattr(vault_unlock, "can_prompt_here", None))
        if callable(current) and ok:
            vault_unlock.can_prompt_here = _can_prompt_wrapper(current)
            wired["login"] = True
    except Exception as exc:
        logger.warning("speakeasy: secure login entry unavailable on this Hermes: %s", type(exc).__name__)
    return wired


def uninstall(inputs: SecureInputs | None = None) -> None:
    """Detach this service (the lookups then fall straight through) and restore Hermes' originals."""
    if inputs is not None and _ACTIVE.get("inputs") is not inputs:
        return
    _ACTIVE["inputs"] = _ACTIVE["resolve"] = None
    with contextlib.suppress(Exception):
        from tools import skills_tool
        current = getattr(skills_tool, "_turn_secret_capture", None)
        if getattr(current, _SECRET_PROXY_ATTR, False):
            skills_tool._turn_secret_capture = current._real
    with contextlib.suppress(Exception):
        from tools import terminal_tool
        current = getattr(terminal_tool, "_get_sudo_password_callback", None)
        if getattr(current, _SECRET_PROXY_ATTR, False):
            terminal_tool._get_sudo_password_callback = current.__wrapped__  # type: ignore[union-attr]
    with contextlib.suppress(Exception):
        from agent.vault_backends import unlock as vault_unlock
        for name in ("get_save_login_prompt_callback", "get_code_prompt_callback", "can_prompt_here"):
            setattr(vault_unlock, name, _unwrap(getattr(vault_unlock, name)))


def after_tool_call(**kwargs: Any) -> None:
    """``post_tool_call`` hook: sudo is allowed once. Hermes caches a prompted sudo password for the
    session; after the command a Speakeasy card allowed, that cache entry is dropped so the next
    sudo asks again."""
    inputs: SecureInputs | None = _ACTIVE.get("inputs")
    if inputs is None or not inputs._sudo_granted:
        return
    session_key, _, _ = _current_turn()
    with inputs._lock:
        if session_key not in inputs._sudo_granted:
            return
        inputs._sudo_granted.discard(session_key)
    with contextlib.suppress(Exception):
        from tools.terminal_tool_sudo import _set_cached_sudo_password
        _set_cached_sudo_password("")


def scrub_bytes(raw: bytes) -> bytes:
    """Defense in depth for everything read back from Hermes (run events, session messages): a value
    entered in a secure box is replaced with ``[redacted]`` if any output echoes it."""
    inputs: SecureInputs | None = _ACTIVE.get("inputs")
    if inputs is None or not inputs.prints.any() or not raw:
        return raw
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw
    clean = inputs.scrub(text)
    return raw if clean is text else clean.encode("utf-8")


def scrub_text(text: str) -> str:
    inputs: SecureInputs | None = _ACTIVE.get("inputs")
    return inputs.scrub(text) if inputs is not None and text else text
