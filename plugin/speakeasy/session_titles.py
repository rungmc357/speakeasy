"""Name the Hermes session behind each voice task after the task.

Left alone, Hermes' auto-titler names a task's session from the start of its prompt, which is the
same Speakeasy preamble every time ("Handle voice task from Speakeasy #3"), so the session list,
dashboard and /resume show dozens of identical names. Speakeasy already names every task (the
instant stand-in, then the titler's name), so it sets that name on the session through Hermes'
public session API (``PATCH /api/sessions/{id}``). A title set that way is the user's own name to
Hermes, so its auto-titler never overwrites it.

Only the session minted for a task is ever renamed, never a chat the task continues (a Discord
thread, a conversation the user started), and never once someone else has renamed it.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .hermes_api import HermesError

logger = logging.getLogger(__name__)

# The session row appears once Hermes starts the run, a moment after /v1/runs returns, and a
# task's name can be ready before that: wait for it this long before giving up.
ROW_WAIT_S = 20.0
RETRY_S = 1.0
MAX_TITLE = 100  # Hermes' SessionDB.MAX_TITLE_LENGTH

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_ours: dict[str, str] = {}  # session id -> the title Speakeasy last set on it
PENDING: set[Any] = set()  # running pushes (asyncio tasks), held so they aren't collected mid-flight


def _lock(session_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(session_id, threading.Lock())


def candidates(title: str, now: float | None = None) -> list[str]:
    """The name, then the name with its day, then with day and time. Hermes keeps session titles
    unique and a repeated ask ("Check Mac mini storage usage") is common; a date tells two apart
    where a bare "#2" says nothing."""
    title = " ".join(title.split())[:MAX_TITLE].rstrip()
    if not title:
        return []
    at = time.localtime(now)
    day = f"{time.strftime('%b', at)} {at.tm_mday}"
    clock = time.strftime("%I:%M%p", at).lstrip("0").lower()
    out = [title]
    for suffix in (f" ({day})", f" ({day}, {clock})"):
        room = MAX_TITLE - len(suffix)
        out.append(title[:room].rstrip() + suffix)
    return out


def push(hermes: Any, session_id: str, title_now: Callable[[], str | None], *,
         wait_s: float = ROW_WAIT_S, retry_s: float = RETRY_S) -> str | None:
    """Set the session's title to the task's current name; returns the title set, or None.

    *title_now* is read under the session's lock, so concurrent pushes (the stand-in after the run
    starts, the titler's name a moment later) settle on the newest name. Blocking: call it off the
    event loop. Never raises: a session left with Hermes' own name is only cosmetic."""
    setter = getattr(hermes, "set_session_title", None)
    getter = getattr(hermes, "session_title", None)
    if setter is None or getter is None:
        return None  # not a Hermes backend
    deadline = time.monotonic() + wait_s
    with _lock(session_id):
        while True:
            title = title_now()
            if not title:
                return None
            try:
                current = getter(session_id)
                break
            except HermesError as exc:
                if exc.status != 404 or time.monotonic() >= deadline:
                    logger.debug("speakeasy: session %s not titled (%s)", session_id, exc.message)
                    return None
            except Exception:
                logger.debug("speakeasy: session %s not titled", session_id, exc_info=True)
                return None
            time.sleep(retry_s)
        ours = _ours.get(session_id)
        if ours is not None and current not in (None, "", ours):
            return None  # renamed by someone else since: theirs stays
        names = candidates(title)
        if current and current in names:
            _ours[session_id] = current
            return current
        for name in names:
            try:
                setter(session_id, name)
            except HermesError as exc:
                if exc.status == 400:
                    continue  # taken by another session: try it with its day
                logger.debug("speakeasy: session %s not titled (%s)", session_id, exc.message)
                return None
            except Exception:
                logger.debug("speakeasy: session %s not titled", session_id, exc_info=True)
                return None
            _ours[session_id] = name
            return name
        return None
