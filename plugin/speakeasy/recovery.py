"""Settle thread tasks nobody is waiting on any more.

A task that runs in a new chat thread is watched by a wait inside the call's worker. A gateway
restart or a plugin reload ends that wait, and nothing else ever marks the task finished, so it
stayed "running" forever: the app showed it spinning and the voice kept treating it as open. This
sweep finds those tasks and settles them from the thread itself.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from . import threads as T
from .prompt import builder as P
from .text import split_result

logger = logging.getLogger(__name__)

NO_SESSION_AFTER_S = 30 * 60      # the thread never started a turn
NO_ANSWER_AFTER_S = 3 * 3600      # a turn started but never finished
INTERRUPTED_NOTE = "Lost track of this after a restart; whatever happened is in its thread."
NEVER_STARTED = "The thread never started working on this."


LIVE_WAIT_MAX_S = 35 * 60  # a wait older than its own timeout has been orphaned (the call ended)


def sweep(store: Any, state_db: Path, image_roots: Callable[[], tuple[Path, ...]], live: dict[str, float],
          now: float | None = None, mono: float | None = None) -> list[tuple[str, str]]:
    """Settle every unsettled thread task not being waited on now. Returns (key, outcome) pairs."""
    now = time.time() if now is None else now
    mono = time.monotonic() if mono is None else mono
    settled = []
    for task in store.unsettled_thread_tasks():
        key = task["key"]
        began = live.get(key)
        if began is not None and mono - began < LIVE_WAIT_MAX_S:
            continue
        session_id = task.get("session_id")
        if not session_id and task.get("platform") and task.get("thread_id"):
            session_id = T.thread_session(state_db, task["platform"], task["thread_id"])
            if session_id:
                store.set_continued(key, session_id, task.get("label") or "a thread")
        # Only a reply written after the task began: one sent into an existing thread (a follow-up)
        # must not settle on that thread's previous reply.
        answer = T.session_answer(state_db, session_id, since=task.get("created")) if session_id else None
        age = now - task["updated"]
        if answer is not None:
            result = split_result(P.without_voice_header(answer), image_roots())
            store.set_result(key, result)
            store.progress(key, "result", (result or {}).get("full") or "Done")
            store.update_run(key, None, "completed")
            settled.append((key, "completed"))
        elif (not session_id and age > NO_SESSION_AFTER_S) or age > NO_ANSWER_AFTER_S:
            # A known thread with no turn never started; an unknown thread (tasks from before the
            # thread was recorded) or a turn that never finished is simply lost track of.
            never = not session_id and bool(task.get("thread_id"))
            store.progress(key, "result", NEVER_STARTED if never else INTERRUPTED_NOTE)
            store.update_run(key, None, "interrupted")
            settled.append((key, "interrupted"))
    if settled:
        logger.info("speakeasy: settled %d stuck thread task(s): %s", len(settled),
                    ", ".join(outcome for _, outcome in settled))
    return settled
