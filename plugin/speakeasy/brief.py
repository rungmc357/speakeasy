"""The voice brief: written by the user's own Hermes, validated, stored, refreshed in the background.

- `<HERMES_HOME>/speakeasy/voice-brief.md` holds the brief; `brief-state.json` its metadata.
- A write runs `prompt/brief_request.md` through `/v1/runs` (the user's model, memory, permissions).
- Validation: size cap, the five required sections, a secret scan. A failed write never replaces a
  good brief.
- Refresh: at most once a day, only when the input hash (memory, user profile, persona, config tool
  and platform choices, skill names) changed, only while auto-refresh is on and the brief was not
  edited by hand. Never at call start.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .plain_errors import BRIEF_FAILED
from .prompt.builder import PROMPT_DIR

logger = logging.getLogger(__name__)

MAX_BRIEF_CHARS = 12_000
MIN_BRIEF_CHARS = 200
REFRESH_INTERVAL_S = 24 * 3600
CHECK_INTERVAL_S = 3600
RETRY_INTERVAL_S = 3600
FIRST_WRITE_DELAY_S = 10.0
FIRST_WRITE_GIVE_UP_S = 600.0
REQUIRED_SECTIONS = ("User", "Assistant persona", "Capability map", "Answer preferences", "Current context")
_SECRET_PATTERNS = [
    re.compile(p) for p in (
        r"sk-[A-Za-z0-9_-]{20,}", r"sk-ant-[A-Za-z0-9_-]{20,}", r"gh[pousr]_[A-Za-z0-9]{30,}",
        r"xox[bpas]-[A-Za-z0-9-]{10,}", r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bop:" + "//",  # password-manager reference
        r"(?i)\b(?:password|passwd|api[_ -]?key|secret|token)\s*[:=]\s*\S{6,}",
        r"\b(?:\d[ -]?){13,19}\b",                     # card-like numbers
        r"[A-Za-z0-9_+=/-]{40,}",                      # long opaque strings
        r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b",               # email addresses
        r"\b[a-z0-9-]+\.tail[0-9a-f]{6}\.ts\.net\b",   # tailnet hosts
    )]


class BriefInvalid(ValueError):
    pass


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def strip_status_lines(output: str) -> str:
    text = re.sub(r"(?im)^[ \t]*(?:DONE|SPOKEN|STATUS|DETAIL):.*$", "", output or "")
    text = re.sub(r"^\s*```(?:markdown|md)?\s*\n(.*)\n```\s*$", r"\1", text.strip(), flags=re.S)
    return text.strip()


# Heading keywords per section, so "## 1. User", "## **Capabilities**" or "## What I can do" count.
_SECTION_KEYWORDS = {
    "User": ("user", "about you", "about me", "who you are"),
    "Assistant persona": ("persona", "personality", "assistant", "about me as"),
    "Capability map": ("capabilit", "can do", "abilities", "what i can", "tools"),
    "Answer preferences": ("answer", "preference", "style", "how you like"),
    "Current context": ("context", "current", "projects", "working on"),
}


def sections_found(text: str) -> set[str]:
    """Which of the five sections have a heading, matched loosely."""
    found: set[str] = set()
    for raw in re.findall(r"(?m)^#{1,4}\s+(.+?)\s*$", text):
        heading = re.sub(r"[^a-z ]+", " ", raw.lower())
        heading = " ".join(heading.split())
        for section, words in _SECTION_KEYWORDS.items():
            if section not in found and any(w in heading for w in words):
                found.add(section)
                break
    return found


def validate(text: str) -> str:
    """Return the cleaned brief or raise BriefInvalid with a short reason."""
    text = strip_status_lines(text)
    if len(text) < MIN_BRIEF_CHARS:
        raise BriefInvalid("brief is too short")
    if len(text) > MAX_BRIEF_CHARS:
        raise BriefInvalid("brief is too long")
    found = sections_found(text)
    if "Capability map" not in found or len(found) < 3:
        missing = [s for s in REQUIRED_SECTIONS if s not in found]
        raise BriefInvalid(f"brief is missing sections: {', '.join(missing)}")
    for pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            raise BriefInvalid("brief looks like it contains secrets or sensitive data")
    return text + "\n"


def input_hash(hermes_home: Path) -> str:
    """Hash of what the brief is written from; changes trigger a (daily) refresh."""
    h = hashlib.sha256()
    home = Path(hermes_home)
    for rel in ("memories/MEMORY.md", "memories/USER.md", "MEMORY.md", "USER.md", "SOUL.md"):
        try:
            h.update(rel.encode() + b"\0" + (home / rel).read_bytes() + b"\0")
        except OSError:
            pass
    from .settings import hermes_config
    cfg = hermes_config(home)
    if isinstance(cfg, dict):
        picked = {k: cfg.get(k) for k in ("toolsets", "agent", "personality", "display")}
        platforms = cfg.get("platforms")
        picked["platforms"] = sorted(platforms) if isinstance(platforms, dict) else []
        h.update(json.dumps(picked, sort_keys=True, default=str).encode())
    skills = home / "skills"
    if skills.is_dir():
        names = sorted(str(p.parent.relative_to(skills)) for p in skills.glob("**/SKILL.md"))[:2000]
        h.update("\n".join(names).encode())
    return h.hexdigest()


def explain_run_failure(status: str, error: str) -> str:
    """A short reason the user can act on. Hermes' run errors are written for people and hold no
    secrets; they are trimmed, and anything that looks like a credential is cut."""
    lowered = error.lower()
    if "credential" in lowered or "authentication" in lowered or "not logged in" in lowered or "auth" in lowered:
        return ("Your Hermes isn't signed in to its model provider, so it couldn't write the brief. "
                "Fix that with `hermes model`, then press Rewrite.")
    if error:
        text = re.sub(r"\s+", " ", error).strip()
        text = re.sub(r"(sk-|ghp_|xox[bp]-)[A-Za-z0-9_-]+", "[hidden]", text)
        return f"Hermes couldn't write the brief: {text[:160]}"
    return f"Hermes couldn't write the brief (run {status})."


class BriefManager:
    def __init__(self, hermes_home: Path, run_fn: Callable[[str, str], tuple[str, str]] | None,
                 settings_fn: Callable[[], dict[str, Any]], clock: Callable[[], float] = time.time,
                 error_fn: Callable[[], str] | None = None):
        self.home = Path(hermes_home)
        self.dir = self.home / "speakeasy"
        self.path = self.dir / "voice-brief.md"
        self.state_path = self.dir / "brief-state.json"
        self.run_fn, self.settings_fn, self.clock = run_fn, settings_fn, clock
        self.error_fn = error_fn
        self._lock = threading.Lock()
        self._writing = False
        self._stop = threading.Event()

    # -- state -------------------------------------------------------------------------
    def _state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_state(self, **changes: Any) -> dict[str, Any]:
        state = {**self._state(), **changes}
        _write_private(self.state_path, json.dumps(state, indent=2) + "\n")
        return state

    def text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def status(self) -> dict[str, Any]:
        state = self._state()
        text = self.text()
        if self._writing:
            label = "writing"
        elif text:
            label = "edited" if state.get("edited") else "ready"
        elif state.get("error"):
            label = "failed"
        else:
            label = "none"
        return {"state": label, "updated_at": state.get("updated_at"), "edited": bool(state.get("edited")),
                "auto_refresh": bool(self.settings_fn()["brief"]["auto_refresh"]) and not state.get("edited"),
                "error": state.get("error"), "words": len(text.split()) if text else 0}

    def get(self) -> dict[str, Any]:
        return {"brief": self.text(), **self.status()}

    # -- edits -------------------------------------------------------------------------
    def put(self, text: Any) -> dict[str, Any]:
        """A manual edit. Kept as written (after validation); stops auto-refresh."""
        if not isinstance(text, str):
            raise BriefInvalid("brief must be text")
        if not text.strip():
            with self._lock:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                self._save_state(edited=False, updated_at=self.clock(), error=None)
            return self.get()
        clean = validate(text)
        with self._lock:
            _write_private(self.path, clean)
            self._save_state(edited=True, updated_at=self.clock(), error=None)
        return self.get()

    # -- writes by Hermes ------------------------------------------------------------------
    def request_prompt(self) -> str:
        return (PROMPT_DIR / "brief_request.md").read_text(encoding="utf-8")

    def rewrite(self, *, force: bool = True, background: bool = True) -> dict[str, Any]:
        """Ask Hermes to (re)write the brief. With force=False, an edited brief is left alone."""
        if self.run_fn is None:
            raise BriefInvalid("Hermes API is not configured")
        with self._lock:
            if self._writing:
                return self.status()
            if not force and self._state().get("edited"):
                return self.status()
            self._writing = True
        if background:
            threading.Thread(target=self._write, daemon=True, name="speakeasy-brief").start()
            return self.status()
        self._write()
        return self.status()

    def _write(self) -> None:
        digest = input_hash(self.home)
        now = self.clock()
        try:
            if self.run_fn is None:
                raise BriefInvalid("Hermes API is not configured")
            status, output = self.run_fn(self.request_prompt(), "speakeasy_brief_" + hashlib.sha256(
                f"{digest}:{now}".encode()).hexdigest()[:24])
            if status != "completed":
                raise BriefInvalid(explain_run_failure(status, self.last_run_error()))
            clean = validate(output)
            with self._lock:
                _write_private(self.path, clean)
                self._save_state(edited=False, updated_at=now, input_hash=digest, last_attempt=now, error=None)
        except Exception as exc:
            message = str(exc) if isinstance(exc, BriefInvalid) else BRIEF_FAILED
            logger.warning("speakeasy: %s (%s)", message, type(exc).__name__)
            with self._lock:
                self._save_state(last_attempt=now, error=message[:200])
        finally:
            with self._lock:
                self._writing = False

    def last_run_error(self) -> str:
        try:
            return (self.error_fn() if self.error_fn else "") or ""
        except Exception:
            return ""

    def refresh_due(self) -> bool:
        if not self.settings_fn()["brief"]["auto_refresh"]:
            return False
        state = self._state()
        if state.get("edited"):
            return False
        if not self.text():
            # No brief yet: the first write failed (Hermes not signed in, gateway was restarting...).
            # Try again on the hourly check, never more often.
            return bool(state.get("error")) and self.clock() - float(state.get("last_attempt") or 0) >= RETRY_INTERVAL_S
        last = float(state.get("last_attempt") or state.get("updated_at") or 0)
        if self.clock() - last < REFRESH_INTERVAL_S:
            return False
        return state.get("input_hash") != input_hash(self.home)

    def maybe_refresh(self) -> bool:
        if self.run_fn is None or not self.refresh_due():
            return False
        self.rewrite(force=False, background=True)
        return True

    def first_write_due(self) -> bool:
        """No brief yet, never attempted (or the last attempt failed over an hour ago)."""
        if self.text():
            return False
        state = self._state()
        last = float(state.get("last_attempt") or 0)
        return not last or (state.get("error") is not None and self.clock() - last >= CHECK_INTERVAL_S)

    def start_scheduler(self, first_write_delay_s: float = FIRST_WRITE_DELAY_S,
                        ready: Callable[[], bool] | None = None) -> None:
        """Hourly refresh check. Also writes the first brief shortly after start, once `ready()`
        (the Hermes API answers), so a fresh install gets one without the user asking."""
        def first() -> None:
            waited = 0.0
            while not self._stop.wait(first_write_delay_s):
                waited += first_write_delay_s
                try:
                    if not self.first_write_due() or self.run_fn is None:
                        return
                    if ready is None or ready():
                        self.rewrite(force=False, background=False)
                        return
                except Exception as exc:
                    logger.warning("speakeasy: first brief write failed: %s", type(exc).__name__)
                    return
                if waited >= FIRST_WRITE_GIVE_UP_S:
                    return

        threading.Thread(target=first, daemon=True, name="speakeasy-brief-first").start()

        def loop() -> None:
            while not self._stop.wait(CHECK_INTERVAL_S):
                try:
                    self.maybe_refresh()
                except Exception as exc:  # never kill the gateway thread
                    logger.warning("speakeasy: brief refresh check failed: %s", type(exc).__name__)
        threading.Thread(target=loop, daemon=True, name="speakeasy-brief-refresh").start()

    def stop(self) -> None:
        self._stop.set()
