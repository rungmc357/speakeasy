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
APPROVAL_EXPIRED_NOTE = "This approval expired when Hermes restarted. Ask again to carry on."
ORPHAN_BATCH = 40          # rows one pass may check with Hermes (the rest wait for the next pass)

# SPK-308: a run this process still follows, but whose answer Hermes already has.
FOLLOWED_CHECK_AFTER_S = 120   # a followed run is asked about once it has run this long
FOLLOWED_DONE_GRACE_S = 90     # Hermes said it finished this long ago and the follower still hasn't recorded it
LOST_RESULT_NOTE = {"completed": "Work complete", "cancelled": "Work stopped",
                    "failed": "Didn't finish", "interrupted": "Didn't finish"}


def _answer(output: str | None, image_roots: Callable[[], tuple[Path, ...]]) -> tuple[dict[str, Any] | None, list]:
    """A recovered run's answer and its email drafts, split as the live path splits them
    (calls.py): the drafts are stored as the job's drafts (its email card), never left inside
    the stored answer."""
    result = split_result(P.without_voice_header(output), image_roots()) if output and output.strip() else None
    return result, (result or {}).pop("_email_drafts", None) or []


def sweep(store: Any, state_db: Path, image_roots: Callable[[], tuple[Path, ...]], live: dict[str, float],
          now: float | None = None, mono: float | None = None, *, hermes: Any = None) -> list[tuple[str, str]]:
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
            result, drafts = _answer(answer, image_roots)
            if store.transition(key, "completed", expect={task["status"]}, orphaned=True, result=result,
                                drafts=drafts):
                store.progress(key, "result", (result or {}).get("full") or "Done")
                settled.append((key, "completed"))
        elif (not session_id and age > NO_SESSION_AFTER_S) or age > NO_ANSWER_AFTER_S:
            # A known thread with no turn never started; an unknown thread (tasks from before the
            # thread was recorded) or a turn that never finished is simply lost track of.
            never = not session_id and bool(task.get("thread_id"))
            if store.transition(key, "interrupted", expect={task["status"]}, orphaned=True):
                store.progress(key, "result", NEVER_STARTED if never else INTERRUPTED_NOTE)
                settled.append((key, "interrupted"))
    settled += _settle_unconfirmed(store, state_db, image_roots, now, hermes)
    if settled:
        logger.info("speakeasy: settled %d stuck task(s): %s", len(settled),
                    ", ".join(outcome for _, outcome in settled))
    return settled


def _hermes_state(hermes: Any, run_id: str | None) -> tuple[str, str | None] | None:
    """Hermes' own word on a run: (known live/final status, output), else None.
    A positive live record also matters: quiet time must not finish a retry still in flight."""
    if hermes is None or not run_id or not ID_RE.fullmatch(run_id):
        return None
    try:
        reply = hermes.get_run(run_id)
    except Exception as exc:
        logger.info("speakeasy: an unconfirmed task's run could not be checked (%s)", type(exc).__name__)
        return None
    final = reply.get("status") if isinstance(reply, dict) else None
    if final not in RS.TERMINAL and final not in RS.LIVE:
        return None
    output = reply.get("output")
    return final, output if isinstance(output, str) and output.strip() else None


def _settle_unconfirmed(store: Any, state_db: Path, image_roots: Callable[[], tuple[Path, ...]],
                        now: float, hermes: Any = None, limit: int = ORPHAN_BATCH) -> list[tuple[str, str]]:
    """Settle Hermes runs left unconfirmed (the stream dropped before the run's final event) from
    Hermes' own record of the run first (a reload drops the follower of a run that keeps working;
    it was only asked once, while it still ran, so its card read Working long after it finished),
    then from their session: the session's final reply once it has gone quiet, else interrupted
    after hours. At most *limit* runs are asked of Hermes a pass."""
    settled = []
    asked = 0
    for task in store.unconfirmed_runs():
        key, session_id = task["key"], task.get("session_id")
        if asked < limit and hermes is not None:
            asked += 1
            known = _hermes_state(hermes, task.get("run_id"))
            if known is not None:
                final, output = known
                if final in RS.LIVE:
                    continue  # positive liveness beats age or an earlier reply during a retry
                result, drafts = _answer(output, image_roots)
                if store.transition(key, final, expect={"ambiguous"}, orphaned=True, drafts=drafts,
                                    **({"result": result} if result else {})):
                    store.progress(key, "result", (result or {}).get("full") or {
                        "completed": "Work complete", "cancelled": "Work stopped",
                        "failed": "Didn't finish", "interrupted": "Didn't finish"}[final])
                    settled.append((key, final))
                continue
        last = (T.session_last_at(state_db, session_id) if session_id else None) or task["updated"]
        if now - last < UNCONFIRMED_QUIET_S:
            continue  # the session is still writing: the run may still be working
        # A later task in the same session owns the session's newest reply.
        answer = (T.session_answer(state_db, session_id, since=task.get("created"))
                  if session_id and not task.get("superseded") else None)
        if answer is not None:
            result, drafts = _answer(answer, image_roots)
            if store.transition(key, "completed", expect={"ambiguous"}, orphaned=True, result=result,
                                drafts=drafts):
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
            output = reply.get("output") if isinstance(reply.get("output"), str) else None
            result, drafts = _answer(output, image_roots)
            if store.transition(key, final, expect={status}, orphaned=True, drafts=drafts,
                                **({"result": result} if result else {})):
                store.progress(key, "result", (result or {}).get("full") or {
                    "completed": "Work complete", "cancelled": "Work stopped",
                    "failed": "Didn't finish", "interrupted": "Didn't finish"}[final])
                settled.append((key, final))
        elif status in RS.ASKING:
            # An approval whose request this process never held (it lived in the process before a
            # restart) and Hermes no longer offers: nothing can answer it, so it says it expired
            # rather than showing buttons that can only fail.
            if store.transition(key, "interrupted", expect={status}, orphaned=True):
                store.progress(key, "result", APPROVAL_EXPIRED_NOTE)
                settled.append((key, "interrupted"))
        elif store.transition(key, "ambiguous", expect={status}):
            settled.append((key, "ambiguous"))
    if settled:
        logger.info("speakeasy: settled %d task(s) nothing was following: %s", len(settled),
                    ", ".join(outcome for _, outcome in settled))
    return settled


def finished_followed(store: Any, hermes: Any, followed: dict[str, str], seen_done: dict[str, float], *,
                      now: float | None = None, mono: float | None = None,
                      limit: int = ORPHAN_BATCH) -> tuple[list[tuple[str, str, dict[str, Any]]], list[str]]:
    """Runs this process still follows (*followed*: task key -> Hermes run id) that Hermes says ended
    a while ago, while their row is still open: the follower never recorded the answer (SPK-308: a
    432 s turn finished, its answer sat in Hermes, and the job read "working" for the rest of the
    call). Nothing settles here; the caller hands each one back to the normal settle path.

    A run is asked about once it has run FOLLOWED_CHECK_AFTER_S. A final status seen for the first
    time is remembered in *seen_done* (key -> *mono*); it is returned only once FOLLOWED_DONE_GRACE_S
    has passed with the row still open, so a follower that is merely a moment behind always settles
    it itself. A run Hermes no longer knows (it restarted, or forgot a long-finished run) whose row
    has been quiet for ORPHAN_QUIET_S is returned in the second list: it should be made unconfirmed,
    which the unconfirmed sweep settles from the task's session. Every write the caller makes is a
    checked transition, so a follower that does catch up never collides with it.

    Returns ([(key, Hermes' final status, Hermes' reply)], [keys Hermes forgot])."""
    now = time.time() if now is None else now
    mono = time.monotonic() if mono is None else mono
    rows = {r["key"]: r for r in store.open_runs()}
    for key in list(seen_done):
        if key not in followed or key not in rows:
            seen_done.pop(key, None)  # settled (or no longer followed): nothing to remember
    done: list[tuple[str, str, dict[str, Any]]] = []
    forgotten: list[str] = []
    asked = 0
    for key, run_id in followed.items():
        row = rows.get(key)
        if row is None or row["status"] in (RS.NOT_STARTED | {RS.RunState.AMBIGUOUS.value}):
            continue  # final already, not started yet, or the unconfirmed sweep's
        if not run_id or not ID_RE.fullmatch(str(run_id)) or str(run_id).startswith("lr_"):
            continue  # a thread task: sweep() settles it from its thread
        if now - float(row.get("created") or row["updated"]) < FOLLOWED_CHECK_AFTER_S:
            continue
        if asked >= limit:
            break
        asked += 1
        try:
            reply = hermes.get_run(run_id)
        except Exception as exc:
            if getattr(exc, "status", None) == 404 and now - row["updated"] >= ORPHAN_QUIET_S:
                forgotten.append(key)
            seen_done.pop(key, None)
            continue
        final = reply.get("status") if isinstance(reply, dict) else None
        if final not in RS.TERMINAL:
            seen_done.pop(key, None)
            continue
        first = seen_done.setdefault(key, mono)
        if mono - first >= FOLLOWED_DONE_GRACE_S:
            done.append((key, final, reply))
    return done, forgotten


def settle_from_hermes(store: Any, key: str, expect: str, final: str, reply: dict[str, Any],
                       image_roots: Callable[[], tuple[Path, ...]] = tuple, mirror: Any = None) -> bool:
    """Settle *key* (checked: only from *expect*) with Hermes' own record of how its run ended: the
    answer for a completed run, the failure for a failed one. True when this write settled it."""
    output = reply.get("output") if isinstance(reply.get("output"), str) else None
    result, drafts = _answer(output, image_roots)
    extra: dict[str, Any] = {"result": result, "drafts": drafts} if result else {}
    if final == "failed":
        from . import failures
        error = reply.get("error") if isinstance(reply.get("error"), str) else None
        kind = failures.failure_kind(error) or (None if result else failures.UNKNOWN)
        extra["failure"] = (kind, None if kind in failures.REASONS else failures.failure_hint(error))
    if not store.transition(key, final, expect={expect}, orphaned=True, mirror=mirror, **extra):
        return False
    store.progress(key, "result", (result or {}).get("full") or LOST_RESULT_NOTE[final])
    return True
