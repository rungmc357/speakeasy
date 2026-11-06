"""Settle tasks nobody is following any more.

``sweep_orphans``: every open task row (runstate.OPEN) that nothing in this process follows — all
of them after a restart, later the ones whose follower died — is settled through Hermes' own
record of the run (get_run), or marked unconfirmed for the session sweep below.

Thread tasks:
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
from . import runstate as RS
from .text import ID_RE, split_result

logger = logging.getLogger(__name__)

NO_SESSION_AFTER_S = 30 * 60      # the thread never started a turn
NO_ANSWER_AFTER_S = 3 * 3600      # a turn started but never finished
INTERRUPTED_NOTE = "Lost track of this after a restart; whatever happened is in its thread."
NEVER_STARTED = "The thread never started working on this."


UNCONFIRMED_QUIET_S = 5 * 60    # an unconfirmed run's session has gone quiet: its last reply is final
UNCONFIRMED_NOTE = "Lost track of this while it ran; whatever happened is in its session."

LIVE_WAIT_MAX_S = 35 * 60  # a wait older than its own timeout has been orphaned (the call ended)

ORPHAN_QUIET_S = 10 * 60   # an open row nothing in this process follows, untouched this long, is orphaned
NOT_STARTED_NOTE = "Lost track of this before it started."
ORPHAN_BATCH = 40          # rows one pass may check with Hermes (the rest wait for the next pass)


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
        # Checked moves from the status read above, the result written only when this sweep moved
        # the row: an answer that arrived meanwhile is never overwritten.
        if answer is not None:
            if store.transition(key, "completed", expect={task["status"]}, orphaned=True):
                result = split_result(P.without_voice_header(answer), image_roots())
                store.set_result(key, result)
                store.progress(key, "result", (result or {}).get("full") or "Done")
                settled.append((key, "completed"))
        elif (not session_id and age > NO_SESSION_AFTER_S) or age > NO_ANSWER_AFTER_S:
            # A known thread with no turn never started; an unknown thread (tasks from before the
            # thread was recorded) or a turn that never finished is simply lost track of.
            never = not session_id and bool(task.get("thread_id"))
            if store.transition(key, "interrupted", expect={task["status"]}, orphaned=True):
                store.progress(key, "result", NEVER_STARTED if never else INTERRUPTED_NOTE)
                settled.append((key, "interrupted"))
    settled += _settle_unconfirmed(store, state_db, image_roots, now)
    if settled:
        logger.info("speakeasy: settled %d stuck task(s): %s", len(settled),
                    ", ".join(outcome for _, outcome in settled))
    return settled


def _settle_unconfirmed(store: Any, state_db: Path, image_roots: Callable[[], tuple[Path, ...]],
                        now: float) -> list[tuple[str, str]]:
    """Settle Hermes runs left unconfirmed (the stream dropped before the run's final event) from
    their session: the session's final reply once it has gone quiet, else interrupted after hours."""
    settled = []
    for task in store.unconfirmed_runs():
        key, session_id = task["key"], task.get("session_id")
        last = (T.session_last_at(state_db, session_id) if session_id else None) or task["updated"]
        if now - last < UNCONFIRMED_QUIET_S:
            continue  # the session is still writing: the run may still be working
        # A later task in the same session owns the session's newest reply.
        answer = (T.session_answer(state_db, session_id, since=task.get("created"))
                  if session_id and not task.get("superseded") else None)
        if answer is not None:
            if store.transition(key, "completed", expect={"ambiguous"}, orphaned=True):
                result = split_result(P.without_voice_header(answer), image_roots())
                store.set_result(key, result)
                store.progress(key, "result", (result or {}).get("full") or "Done")
                settled.append((key, "completed"))
        elif task.get("superseded") or now - last > NO_ANSWER_AFTER_S:
            if store.transition(key, "interrupted", expect={"ambiguous"}, orphaned=True):
                store.progress(key, "result", UNCONFIRMED_NOTE)
                settled.append((key, "interrupted"))
    return settled


def sweep_orphans(store: Any, hermes: Any, live: set[str], image_roots: Callable[[], tuple[Path, ...]] = tuple,
                  *, started_at: float | None = None, now: float | None = None,
                  limit: int = ORPHAN_BATCH) -> list[tuple[str, str]]:
    """Settle open runs (runstate.OPEN) that nothing in this process follows (*live*: the keys it
    does). *started_at* (the startup sweep): every such row last touched before this process
    started is from before a restart; otherwise only rows quiet for ORPHAN_QUIET_S (their follower
    died). A row with a Hermes run id is asked how it went (get_run): a final status settles it
    with Hermes' answer; anything else (Hermes forgot the run, it can't be reached) makes it
    unconfirmed, which the session sweep above settles from its session and the user can clear
    meanwhile. A row that never reached Hermes is interrupted. Thread tasks keep their own sweep.
    Every write is a checked transition from the status read here, so a run that settled
    meanwhile is never overwritten. At most *limit* rows a pass. Returns (key, outcome)."""
    now = time.time() if now is None else now
    settled: list[tuple[str, str]] = []
    checked = 0
    for task in store.open_runs():
        key, status, run_id = task["key"], task["status"], task.get("run_id")
        if key in live or status == RS.RunState.AMBIGUOUS.value:
            continue  # followed here, or already waiting on the session sweep
        if started_at is not None:
            if task["updated"] >= started_at:
                continue  # this process's own (its follower is being set up)
        elif now - task["updated"] < ORPHAN_QUIET_S:
            continue
        if not run_id and task.get("thread") and status == RS.RunState.RUNNING.value:
            continue  # a thread task: sweep() settles it from its thread
        if checked >= limit:
            break
        checked += 1
        if not run_id:
            if store.transition(key, "interrupted", expect={status}, orphaned=True):
                store.progress(key, "result", NOT_STARTED_NOTE if status in RS.NOT_STARTED else INTERRUPTED_NOTE)
                settled.append((key, "interrupted"))
            continue
        try:
            reply = hermes.get_run(run_id) if ID_RE.fullmatch(run_id) else {}
        except Exception as exc:
            logger.info("speakeasy: an open task's run could not be checked (%s)", type(exc).__name__)
            reply = {}
        final = reply.get("status") if isinstance(reply, dict) else None
        if final in RS.TERMINAL:
            if store.transition(key, final, expect={status}, orphaned=True):
                output = reply.get("output") if isinstance(reply.get("output"), str) else None
                result = split_result(P.without_voice_header(output), image_roots()) if output else None
                if result:
                    store.set_result(key, result)
                store.progress(key, "result", (result or {}).get("full") or {
                    "completed": "Work complete", "cancelled": "Work stopped",
                    "failed": "Work failed", "interrupted": "Work interrupted"}[final])
                settled.append((key, final))
        elif store.transition(key, "ambiguous", expect={status}):
            settled.append((key, "ambiguous"))
    if settled:
        logger.info("speakeasy: settled %d task(s) nothing was following: %s", len(settled),
                    ", ".join(outcome for _, outcome in settled))
    return settled
