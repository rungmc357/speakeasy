"""Sign Codex in to ChatGPT from the app: Codex's own device-code login, run on this machine.

Many Hermes machines have no screen, so `codex login` (which opens a local browser) is a hurdle.
Codex's app-server protocol has a device-code login (``account/login/start`` with
``{"type": "chatgptDeviceCode"}``, in every Codex Speakeasy supports, 0.150.0 and later): it returns
a verification URL and a one-time code, the user approves in any browser (their phone), and Codex
finishes on its own, saving the sign-in in Codex's own store and sending ``account/login/completed``.

Rules:
- Only Codex's binary touches Codex's credentials. Speakeasy never reads, copies or logs a token,
  never reads Codex's auth files, and never touches Hermes' own auth.
- One flow at a time. The child process is killed on cancel, on expiry, and when the service stops.
- Only an https URL on an OpenAI/ChatGPT host is passed on to the app; anything else fails the flow.
- The one-time code is shown to the user but never logged.
"""
from __future__ import annotations

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
from urllib.parse import urlsplit

from .codex_transport import child_env

logger = logging.getLogger(__name__)

# Codex gives up on a device code after 15 minutes; stop a little earlier so the app never shows a
# code Codex has already dropped.
CODE_LIFETIME_S = 15 * 60 - 15
START_TIMEOUT_S = 25.0
ALLOWED_HOSTS = ("openai.com", "chatgpt.com")
_CODE_RE = re.compile(r"^[A-Z0-9]{3,10}(?:-[A-Z0-9]{3,10}){0,3}$")

PENDING, SIGNED_IN, EXPIRED, FAILED, CANCELLED, IDLE = (
    "pending", "signed_in", "expired", "failed", "cancelled", "idle")


CHATGPT, API_KEY, NONE = "chatgpt", "api_key", "none"


def sign_in_kind(ok: bool, message: str) -> str:
    """What `login_status` reported: a ChatGPT sign-in, an OpenAI API key, or none."""
    if not ok:
        return NONE
    return API_KEY if "api key" in (message or "").lower() else CHATGPT


def discover(default: Path | None, status_fn: Callable[[Path | None], tuple[bool, str]],
             others: list[Path], too_old_fn: Callable[[Path], bool]) -> dict[str, Any]:
    """Look at every Codex copy on this machine for a working sign-in, before ever asking the user.

    ``default`` is the copy voice uses now (``voice.codex_path``, else the newest one); ``others``
    are every other copy the plugin knows (PATH, Homebrew, npm, the ChatGPT app's bundled Codex,
    the older Codex app, Speakeasy's own copy). Copies too old for voice are skipped. Returns
    ``{binary, kind, installed, too_old, changed}``: the first copy signed in with ChatGPT (the
    default first), else one with an API key, else the default/first usable copy; ``changed`` is
    True when that copy isn't the default (the caller saves it as voice.codex_path)."""
    order = ([default] if default is not None else []) + [p for p in others if p != default]
    usable = [p for p in order if not too_old_fn(p)]
    api_key: Path | None = None
    for path in usable:
        kind = sign_in_kind(*status_fn(path))
        if kind == CHATGPT:
            return {"binary": path, "kind": CHATGPT, "installed": True, "too_old": False,
                    "changed": path != default}
        if kind == API_KEY and api_key is None:
            api_key = path
    if api_key is not None:
        return {"binary": api_key, "kind": API_KEY, "installed": True, "too_old": False, "changed": False}
    return {"binary": usable[0] if usable else None, "kind": NONE, "installed": bool(order),
            "too_old": bool(order) and not usable, "changed": False}


class CodexLoginError(RuntimeError):
    """A start failure with a message that is safe to show the user."""


def safe_verification_url(url: Any) -> str | None:
    """The URL if it is https on openai.com / chatgpt.com (or a subdomain), with no userinfo."""
    if not isinstance(url, str) or len(url) > 512 or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return None
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        return None
    return url


def safe_user_code(code: Any) -> str | None:
    if not isinstance(code, str):
        return None
    code = code.strip().upper()
    return code if _CODE_RE.fullmatch(code) else None


def explain_failure(error: Any) -> str:
    """Codex's failure text mapped to a plain line; nothing from the provider is reflected."""
    text = str(error or "").lower()
    if "not enabled" in text:
        return "This Codex doesn't allow sign-in with a code. Run codex login on the Hermes computer."
    if "timed out" in text or "expired" in text:
        return "The code expired before it was approved. Start again for a new code."
    if "denied" in text or "declined" in text or "cancel" in text:
        return "The sign-in was declined in the browser."
    return "Codex couldn't finish signing in. Try again, or run codex login on the Hermes computer."


class _Flow:
    def __init__(self, process: Any, started: float):
        self.process = process
        self.started = started
        self.state = PENDING
        self.message = "Waiting for you to approve the code in your browser."
        self.verification_url = ""
        self.user_code = ""
        self.login_id = ""
        self.expires_at = started + CODE_LIFETIME_S
        self.responses: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.done = threading.Event()
        self.lock = threading.Lock()


class CodexLogin:
    """One device-code sign-in at a time through ``codex app-server --stdio``."""

    def __init__(self, workdir: Path, popen: Callable[..., Any] = subprocess.Popen,
                 on_signed_in: Callable[[Path], None] | None = None, clock: Callable[[], float] = time.time,
                 lifetime_s: float | None = None):
        self.workdir = Path(workdir)
        self.popen = popen
        self.on_signed_in = on_signed_in
        self.clock = clock
        self.lifetime_s = CODE_LIFETIME_S if lifetime_s is None else lifetime_s
        self.lock = threading.Lock()
        self.flow: _Flow | None = None
        self.binary: Path | None = None

    # -- public -------------------------------------------------------------------------------
    def start(self, binary: Path) -> dict[str, Any]:
        """Start a flow (or return the one already waiting) and report its URL and code."""
        with self.lock:
            flow = self.flow
            if flow is not None and flow.state == PENDING and self.clock() < flow.expires_at:
                return self._report(flow)
            if flow is not None:
                self._stop(flow)
            self.workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                process = self.popen([str(binary), "app-server", "--stdio"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                     cwd=str(self.workdir), env=child_env())
            except OSError as exc:
                raise CodexLoginError(f"Couldn't run Codex on the Hermes computer ({type(exc).__name__}).") from None
            flow = _Flow(process, self.clock())
            flow.expires_at = flow.started + self.lifetime_s
            self.flow, self.binary = flow, Path(binary)
            threading.Thread(target=self._reader, args=(flow,), daemon=True,
                             name="speakeasy-codex-login").start()
            try:
                self._send(flow, {"id": 1, "method": "initialize", "params": {
                    "clientInfo": {"name": "speakeasy", "version": "0.1"},
                    "capabilities": {"experimentalApi": True}}})
                self._wait(flow, 1)
                self._send(flow, {"method": "initialized", "params": {}})
                self._send(flow, {"id": 2, "method": "account/login/start",
                                  "params": {"type": "chatgptDeviceCode"}})
                result = self._wait(flow, 2)
            except CodexLoginError:
                self._finish(flow, FAILED, "Codex couldn't start a sign-in.")
                raise
            url = safe_verification_url(result.get("verificationUrl"))
            code = safe_user_code(result.get("userCode"))
            if result.get("type") != "chatgptDeviceCode" or not url or not code:
                logger.warning("speakeasy: codex sign-in refused: Codex returned an unexpected sign-in page or code")
                self._finish(flow, FAILED, "Codex returned an unexpected sign-in page.")
                raise CodexLoginError("Codex returned an unexpected sign-in page, so Speakeasy didn't show it.")
            with flow.lock:
                flow.verification_url, flow.user_code = url, code
                flow.login_id = str(result.get("loginId") or "")[:128]
            threading.Thread(target=self._expiry, args=(flow,), daemon=True,
                             name="speakeasy-codex-login-expiry").start()
            logger.info("speakeasy: codex sign-in started (device code)")
            return self._report(flow)

    def status(self) -> dict[str, Any]:
        with self.lock:
            flow = self.flow
        if flow is None:
            return {"state": IDLE}
        if flow.state == PENDING and self.clock() >= flow.expires_at:
            self._finish(flow, EXPIRED, "The code expired. Start again for a new code.")
        return self._report(flow)

    def cancel(self) -> dict[str, Any]:
        with self.lock:
            flow = self.flow
            if flow is None:
                return {"state": IDLE}
            if flow.state == PENDING:
                if flow.login_id:
                    try:
                        self._send(flow, {"id": 3, "method": "account/login/cancel",
                                          "params": {"loginId": flow.login_id}})
                        self._wait(flow, 3, timeout=1.5)  # let Codex drop the code itself first
                    except CodexLoginError:
                        pass
                self._finish(flow, CANCELLED, "Sign-in cancelled.")
                logger.info("speakeasy: codex sign-in cancelled")
            return self._report(flow)

    def close(self) -> None:
        with self.lock:
            flow = self.flow
        if flow is not None and flow.state == PENDING:
            self._finish(flow, CANCELLED, "Sign-in cancelled.")

    # -- internals ----------------------------------------------------------------------------
    def _report(self, flow: _Flow) -> dict[str, Any]:
        with flow.lock:
            out: dict[str, Any] = {"state": flow.state, "message": flow.message}
            if flow.state == PENDING:
                out.update(verification_url=flow.verification_url, user_code=flow.user_code,
                           expires_at=int(flow.expires_at),
                           expires_in=max(0, int(flow.expires_at - self.clock())))
            return out

    def _send(self, flow: _Flow, event: dict[str, Any]) -> None:
        try:
            flow.process.stdin.write(json.dumps(event) + "\n")
            flow.process.stdin.flush()
        except (OSError, ValueError, AttributeError):
            raise CodexLoginError("Codex stopped before the sign-in started.") from None

    def _wait(self, flow: _Flow, number: int, timeout: float = START_TIMEOUT_S) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                event = flow.responses.get(timeout=min(.25, max(.01, deadline - time.monotonic())))
            except queue.Empty:
                if flow.done.is_set():
                    break
                continue
            if event.get("id") != number:
                continue
            if "error" in event:
                # Only the mapped line reaches the app; the raw text is not logged either (it can
                # echo the request).
                raise CodexLoginError(explain_failure((event.get("error") or {}).get("message")))
            result = event.get("result")
            return result if isinstance(result, dict) else {}
        raise CodexLoginError("Codex didn't answer in time. Check that Codex is installed and up to date.")

    def _reader(self, flow: _Flow) -> None:
        try:
            for line in flow.process.stdout:
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if "method" not in event and "id" in event:
                    flow.responses.put(event)
                elif event.get("method") == "account/login/completed":
                    params = event.get("params") or {}
                    login_id = params.get("loginId")
                    if flow.login_id and login_id and login_id != flow.login_id:
                        continue
                    if params.get("success") is True:
                        self._finish(flow, SIGNED_IN, "Signed in to ChatGPT.")
                    else:
                        self._finish(flow, FAILED, explain_failure(params.get("error")))
                    return
        except (OSError, ValueError):
            pass
        finally:
            if flow.state == PENDING and flow.user_code:
                self._finish(flow, FAILED, "Codex stopped before the sign-in finished. Try again.")
            flow.done.set()

    def _expiry(self, flow: _Flow) -> None:
        while not flow.done.wait(timeout=min(1.0, max(.05, flow.expires_at - self.clock()))):
            if flow.state != PENDING:
                return
            if self.clock() >= flow.expires_at:
                self._finish(flow, EXPIRED, "The code expired. Start again for a new code.")
                logger.info("speakeasy: codex sign-in code expired")
                return

    def _finish(self, flow: _Flow, state: str, message: str) -> None:
        with flow.lock:
            if flow.state != PENDING:
                return
            flow.state, flow.message = state, message
        if state == SIGNED_IN:
            logger.info("speakeasy: codex signed in to ChatGPT from the app")
            if self.on_signed_in is not None and self.binary is not None:
                try:
                    self.on_signed_in(self.binary)
                except Exception:  # noqa: BLE001 - a cache refresh must never break the flow
                    logger.exception("speakeasy: codex sign-in refresh failed")
        elif state == FAILED:
            logger.info("speakeasy: codex sign-in failed: %s", message)
        self._stop(flow)

    @staticmethod
    def _stop(flow: _Flow) -> None:
        process = flow.process
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            flow.done.set()
