"""Client for the user's Hermes API server on loopback (/v1/runs, events, stop, steer, approval)."""
from __future__ import annotations

import contextlib
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Sequence

from . import attachments as A
from .secure_input import scrub_bytes
from .plain_errors import HERMES_STREAM_FAILED, HERMES_UNAVAILABLE
from .text import ID_RE, TERMINAL

logger = logging.getLogger(__name__)

MAX_BODY = 512 * 1024
START_IMAGES_TIMEOUT_S = 120.0  # POST /v1/runs with pictures: Hermes reads them in before it answers
RUN_EVENTS_TIMEOUT_S = 1800
# What Speakeasy may ever answer an approval with. "always" (permanent) is never sent, from any
# surface: a spoken or tapped answer grants at most the rest of one task.
APPROVAL_CHOICES = frozenset({"once", "session", "deny"})
# The image formats every vision provider Hermes talks to accepts as they are.
IMAGE_DATA_URL_RE = re.compile(r"data:image/(?:jpeg|png|gif|webp);base64,[A-Za-z0-9+/]+={0,2}")


class HermesError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _loopback(base: str) -> str:
    """Only this machine's own addresses: loopback, or an interface address such as a Tailscale IP
    that the API server is bound to (traffic to it stays on this machine)."""
    from .settings import is_this_machine
    parsed = urllib.parse.urlsplit(base)
    host = parsed.hostname or ""
    if parsed.scheme != "http" or not (host in {"127.0.0.1", "::1", "localhost"} or is_this_machine(host)):
        raise ValueError("Hermes API must be an http origin on this machine")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Hermes API origin must not contain credentials/query/fragment")
    return base.rstrip("/")


def user_content(text: str, images: Sequence[str] | None = None) -> str | list[dict[str, Any]]:
    """One user turn as Hermes takes it: the plain text, or with images the canonical OpenAI vision
    parts (a text part, then one ``image_url`` part per data URL). ``/v1/runs`` hands its input to
    the agent without normalizing it, so this is the one spelling sent (never ``input_image``)."""
    if not images:
        return text
    sizes = []
    for url in images:
        if not isinstance(url, str) or not IMAGE_DATA_URL_RE.fullmatch(url):
            raise HermesError(400, "images must be base64 JPEG, PNG, GIF or WebP data URLs")
        sizes.append((len(url) - url.index(",") - 1) * 3 // 4)  # decoded bytes, give or take padding
    if len(sizes) > A.MAX_REQUEST_IMAGES or max(sizes) > A.MAX_IMAGE_BYTES or sum(sizes) > A.MAX_REQUEST_IMAGE_BYTES:
        raise HermesError(413, "images are over the request limit")
    return [{"type": "text", "text": text}, *({"type": "image_url", "image_url": {"url": url}} for url in images)]


# -- can Hermes read images? ---------------------------------------------------------------------
#
# The plugin runs inside Hermes, so this asks Hermes' own code. Image parts in a run's input reach
# ``agent.vision_message_prep``: a main model with vision sees them as they are; otherwise each one
# is replaced by an auxiliary vision model's description (Hermes' ``vision_analyze``).

def image_input_available() -> bool:
    """Whether this Hermes has the image input path at all. Cheap and offline (the gateway has
    imported these already); False outside Hermes and on versions that predate it."""
    try:
        from agent.image_routing import _lookup_supports_vision  # type: ignore  # noqa: F401
        from agent.vision_message_prep import VisionMessagePrepMixin  # type: ignore  # noqa: F401
    except Exception:
        return False
    return True


def detect_image_support() -> dict[str, Any]:
    """``{images, vision}``: can a run carry images, and how the agent reads them. ``vision`` is
    ``native`` (the main model sees them), ``described`` (an auxiliary vision model describes them
    to a main model without vision), ``none`` (neither) or ``unknown`` (the check itself failed).

    Can wait on the network (the models.dev catalog, provider probes): run it off the request path
    and cache the result (``HermesAPI.refresh_image_support``)."""
    if not image_input_available():
        return {"images": False, "vision": "none"}
    try:
        from agent.auxiliary_client import _read_main_model, _read_main_provider  # type: ignore
        from agent.image_routing import _lookup_supports_vision  # type: ignore
        from hermes_cli.config import load_config  # type: ignore
        from .router import _profile_scope
        with _profile_scope():
            # The same test the agent makes per turn (VisionMessagePrepMixin._model_supports_vision).
            if _lookup_supports_vision(_read_main_provider(), _read_main_model(), load_config()) is True:
                return {"images": True, "vision": "native"}
            return {"images": True, "vision": "described" if _auxiliary_vision() else "none"}
    except Exception as exc:
        logger.info("speakeasy: could not tell whether Hermes reads images (%s)", type(exc).__name__)
        return {"images": True, "vision": "unknown"}


def _auxiliary_vision() -> bool:
    """Whether Hermes can resolve an auxiliary vision model, as its own vision tool gate does
    (``tools.vision_tools.check_video_requirements``): the configured ``auxiliary.vision``
    backend, then auto. Probe mode resolves providers without building real clients."""
    from agent.auxiliary_client import resolve_vision_provider_client  # type: ignore
    try:
        from agent.auxiliary_client import aux_probe_mode  # type: ignore
    except ImportError:
        aux_probe_mode = contextlib.nullcontext
    with aux_probe_mode():
        return any(resolve_vision_provider_client(**kw)[1] is not None for kw in ({}, {"provider": "auto"}))


class HermesAPI:
    """`key_fn` is called per request so a key written by `hermes voice setup` is picked up
    without a restart; the key is never logged or returned."""

    def __init__(self, base: str, key_fn: Callable[[], str], profile: str = "",
                 opener: Callable[..., Any] = urllib.request.urlopen):
        root = _loopback(base)
        self.base = f"{root}/p/{profile}" if profile else root
        self.key_fn, self.opener = key_fn, opener
        self.last_error = ""  # Hermes' own message for the last failed background run
        # Whether Hermes reads images: "unknown" until the first check (refresh_image_support).
        self.image_support: dict[str, Any] = {"images": image_input_available(), "vision": "unknown"}

    def _request(self, path: str, body: dict[str, Any] | None = None, method: str | None = None,
                 headers: dict[str, str] | None = None) -> urllib.request.Request:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data else "GET"))
        key = self.key_fn()
        if key:
            req.add_header("Authorization", f"Bearer {key}")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        return req

    def _json(self, method: str, path: str, body: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None, timeout: float = 30) -> dict[str, Any]:
        req = self._request(path, body, method, headers)
        try:
            with self.opener(req, timeout=timeout) as response:
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise HermesError(502, "Hermes response too large")
                data = json.loads(scrub_bytes(raw) or b"{}")
                return data if isinstance(data, dict) else {}
        except urllib.error.HTTPError as exc:
            raise HermesError(502 if exc.code >= 500 else exc.code, f"Hermes HTTP {exc.code}") from None
        except TimeoutError:
            # The request reached Hermes and the answer was slow (a run with pictures can take over
            # half a minute to admit): not "unreachable", and never advice to restart it.
            raise HermesError(504, "Hermes timed out answering") from None
        except (urllib.error.URLError, OSError) as exc:
            logger.info("speakeasy: Hermes API unavailable (%s)", type(exc).__name__)
            raise HermesError(502, HERMES_UNAVAILABLE) from None
        except ValueError:
            raise HermesError(502, "Hermes returned invalid JSON") from None

    def capabilities(self):
        from .backends.base import Capabilities
        support = self.image_support
        return Capabilities(kind="hermes", display_name="Hermes", chat_delivery=True, threads=True,
                            conversation_continuity=True, email_drafts=True, daily_brief=True,
                            images=bool(support["images"]) and support["vision"] != "none")

    def refresh_image_support(self) -> dict[str, Any]:
        """Check again whether Hermes reads images and cache it. Slow at times: never call it on a
        request (the service runs it at startup and then hourly)."""
        self.image_support = detect_image_support()
        return self.image_support

    def health(self, timeout: float = 2) -> bool:
        try:
            self._json("GET", "/health", timeout=timeout)
            return True
        except HermesError:
            return False

    def model_options(self, timeout: float = 20) -> dict[str, Any]:
        """Hermes' model-picker inventory (``GET /api/model/options``): providers, their models and
        capabilities, and the configured ``provider``/``model``. Can be slow: cached by models.Catalog."""
        return self._json("GET", "/api/model/options", timeout=timeout)

    def start_run(self, prompt: str, idem_key: str, session_id: str | None = None,
                  session_key: str | None = None, images: Sequence[str] | None = None,
                  choice: dict[str, Any] | None = None) -> str:
        """*choice*: Hermes' per-request overrides for this run (models.run_fields): ``provider`` +
        ``model`` and/or ``model_options``. None = Hermes' own configuration."""
        # Text alone stays a plain string; with images, one user message of vision parts.
        body: dict[str, Any] = {"input": [{"role": "user", "content": user_content(prompt, images)}]
                                if images else prompt}
        if session_id:
            # A task's own Hermes session: tasks in separate sessions run at the same time.
            body["session_id"] = session_id
        for field in ("provider", "model", "model_options"):
            if choice and choice.get(field):
                body[field] = choice[field]
        headers = {"Idempotency-Key": idem_key}
        if session_key:
            headers["X-Hermes-Session-Key"] = session_key
        # Pictures make admission slow (Hermes reads them in before it answers). A slow answer is
        # asked again with the same Idempotency-Key, so it is the same run, never a second one.
        timeout = START_IMAGES_TIMEOUT_S if images else 30
        try:
            result = self._json("POST", "/v1/runs", body, headers, timeout=timeout)
        except HermesError as exc:
            if exc.status != 504:
                raise
            result = self._json("POST", "/v1/runs", body, headers, timeout=timeout)
        run_id = result.get("run_id")
        if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id):
            raise HermesError(502, "Hermes returned an invalid run ID")
        return run_id

    def events(self, run_id: str, callback: Callable[[dict[str, Any]], None]) -> bool:
        """Stream one run's events; True when a terminal event arrived."""
        if not ID_RE.fullmatch(run_id):
            raise HermesError(400, "invalid run ID")
        req = self._request(f"/v1/runs/{run_id}/events")
        try:
            with self.opener(req, timeout=RUN_EVENTS_TIMEOUT_S) as response:
                total, terminal = 0, False
                for raw in response:
                    total += len(raw)
                    if total > 4 * 1024 * 1024:
                        raise HermesError(502, "Hermes event stream too large")
                    if raw.startswith(b"data: "):
                        try:
                            event = json.loads(scrub_bytes(raw[6:]))
                        except ValueError:
                            continue
                        if isinstance(event, dict):
                            callback(event)
                            terminal = terminal or event.get("event") in {f"run.{s}" for s in TERMINAL}
                return terminal
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            logger.info("speakeasy: Hermes event stream failed (%s)", type(exc).__name__)
            raise HermesError(502, HERMES_STREAM_FAILED) from None

    def get_run(self, run_id: str) -> dict[str, Any]:
        if not ID_RE.fullmatch(run_id):
            raise HermesError(400, "invalid run ID")
        return self._json("GET", f"/v1/runs/{run_id}")

    def stop(self, run_id: str) -> dict[str, Any]:
        if not ID_RE.fullmatch(run_id):
            raise HermesError(400, "invalid run ID")
        return self._json("POST", f"/v1/runs/{run_id}/stop", {})

    def steer(self, run_id: str, text: str) -> bool:
        """Add guidance to a run that is still working; False if it no longer accepts it."""
        if not ID_RE.fullmatch(run_id):
            raise HermesError(400, "invalid run ID")
        try:
            return bool(self._json("POST", f"/v1/runs/{run_id}/steer", {"input": text}).get("accepted"))
        except HermesError:
            return False

    def approve(self, run_id: str, request_id: str, choice: str,
                offered: Sequence[str] | None = None) -> dict[str, Any]:
        """Answer one approval request. Speakeasy never grants a permanent approval ("always"), from
        any surface; "session" (the rest of this task) only when Hermes offered it for this request
        (the ``choices`` on its ``approval.request`` event, passed as ``offered``)."""
        if choice not in APPROVAL_CHOICES:
            raise HermesError(400, "approval choice must be once, session or deny")
        if choice == "session" and "session" not in (offered or ()):
            raise HermesError(400, "this approval can only be allowed once or denied")
        return self._json("POST", f"/v1/runs/{run_id}/approval", {"request_id": request_id, "choice": choice})

    def session_messages(self, session_id: str, limit: int = 60, timeout: float = 1.5) -> list[dict[str, Any]]:
        data = self._json("GET", f"/api/sessions/{urllib.parse.quote(session_id, safe='')}/messages"
                                 f"?limit={limit}&order=latest", timeout=timeout)
        messages = data.get("data") or []
        return [m for m in messages if isinstance(m, dict)] if isinstance(messages, list) else []

    def session_title(self, session_id: str, timeout: float = 5) -> str | None:
        """A session's current title (None when untitled); HermesError 404 until the row exists."""
        data = self._json("GET", f"/api/sessions/{urllib.parse.quote(session_id, safe='')}", timeout=timeout)
        session = data.get("session")
        title = session.get("title") if isinstance(session, dict) else None
        return title if isinstance(title, str) and title.strip() else None

    def set_session_title(self, session_id: str, title: str, timeout: float = 5) -> None:
        """Rename a session (Hermes' public ``PATCH /api/sessions/{id}``). A title set this way has
        the user's authority in Hermes, so its auto-titler leaves it alone. HermesError 400 when
        another session already has the title."""
        self._json("PATCH", f"/api/sessions/{urllib.parse.quote(session_id, safe='')}", {"title": title},
                   timeout=timeout)

    def patch_session(self, session_id: str, fields: dict[str, Any], timeout: float = 5) -> None:
        """Hermes' own ``PATCH /api/sessions/{id}`` (title, archived, pinned): the change every Hermes app
        shows (the desktop sidebar, the CLI's session list). HermesError on refusal."""
        self._json("PATCH", f"/api/sessions/{urllib.parse.quote(session_id, safe='')}", dict(fields), timeout=timeout)

    def run_to_completion(self, prompt: str, idem_key: str, session_id: str | None = None) -> tuple[str, str]:
        """Start a run and wait for its terminal event: (status, output). Used for background jobs."""
        run_id = self.start_run(prompt, idem_key, session_id)
        final: dict[str, Any] = {}

        def on_event(event: dict[str, Any]) -> None:
            kind = event.get("event") or ""
            if kind.startswith("run.") and kind[4:] in TERMINAL:
                final["status"] = kind[4:]
                if isinstance(event.get("output"), str):
                    final["output"] = event["output"]

        if not self.events(run_id, on_event) or "output" not in final or final.get("status") != "completed":
            result = self.get_run(run_id)
            final.setdefault("status", result.get("status") or "unknown")
            if isinstance(result.get("output"), str):
                final.setdefault("output", result["output"])
            if final.get("status") != "completed" and isinstance(result.get("error"), str):
                self.last_error = result["error"]
        return final.get("status", "unknown"), final.get("output", "")
