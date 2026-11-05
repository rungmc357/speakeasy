"""Helpers a task started: the subagents its Hermes run spawned with ``delegate_task`` (docs/API.md
"Helpers", capability ``job_subagents``).

Where it comes from (nothing is guessed):

- ``subagent.start`` / ``subagent.complete`` on the run's event stream. Hermes' API server forwards
  these two lifecycle events (never the children's own tool events) with ``subagent_id``, ``goal``,
  ``task_index``, ``child_session_id``, ``parent_id``/``depth`` (nested helpers), and on completion
  ``status``, ``summary`` (≤ 500 characters), ``duration_seconds`` and ``tool_count``.
- Each child's own session transcript in Hermes' state.db (read-only; Hermes writes a child's tool
  calls before they run). ``SubagentWatch`` follows every running child's transcript at most once a
  second into that child's own steps, and finds children by their parent session when no event names
  them (a task Hermes runs in a chat thread has no event stream at all).
- A child that never reports (a background ``delegate_task``, a lost event): settled from its
  transcript once its session has closed (its last answer, or "stopped" without one), or "stopped"
  once the task has ended and the child has been quiet for ``IDLE_SETTLE_S``.

State lives in the task's activity state (``state["subagents"]``), so it is saved, throttled and
restored with the rest of the task's live activity. None of this changes routing or a task's status.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import activity as A

logger = logging.getLogger(__name__)

MAX_SUBAGENTS = 12      # kept per task (a delegate_task batch is at most a handful)
MAX_CHILD_STEPS = 30    # each helper's own steps, newest last
GOAL_CHARS = 300
SUMMARY_CHARS = 500
TITLE_CHARS = 60
MAX_TITLE_WORDS = 8
NOW_CHARS = 80
IDLE_SETTLE_S = 900.0   # after the task ends, a helper quiet this long with no word back is "stopped"
POLL_S = 1.0
WATCH_MAX_S = 4 * 3600.0
STATUSES = ("running", "done", "failed", "stopped")
_DONE = {"completed": "done", "complete": "done", "done": "done", "success": "done",
         "failed": "failed", "error": "failed", "timeout": "failed",
         "interrupted": "stopped", "cancelled": "stopped", "stopped": "stopped"}


# -- titles ------------------------------------------------------------------------------------

def instant_title(goal: Any) -> str | None:
    """A to-do style name from the helper's goal, the same word-cut rules as a task's stand-in name
    (``text.short_title``): no classifier, never mid-phrase. The titler's name replaces it later."""
    from .text import _trim_tail, short_title
    first = " ".join(str(goal or "").split("\n\n", 1)[0].split())
    title = short_title(first) or short_title(str(goal or ""))
    if title and len(title.split()) > MAX_TITLE_WORDS:  # a helper's row is narrower than a task's
        title = " ".join(_trim_tail(title.split()[:MAX_TITLE_WORDS], True)).strip(" ,.;:!?-")
    return A.plain(title, TITLE_CHARS) if title else None


# -- the model ---------------------------------------------------------------------------------

def _subs(state: dict[str, Any]) -> list[dict[str, Any]]:
    subs = state.get("subagents")
    if not isinstance(subs, list):
        subs = []
        state["subagents"] = subs
    return subs


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _find(state: dict[str, Any], event: dict[str, Any]) -> dict[str, Any] | None:
    subs = _subs(state)
    sid, child = event.get("subagent_id"), event.get("child_session_id")
    for sub in subs:
        if isinstance(sid, str) and sid and sid in (sub.get("id"), sub.get("_sid")):
            return sub
    for sub in subs:
        if isinstance(child, str) and child and sub.get("_session") == child:
            return sub
    # No ids: the same batch slot (delegation + index) still running.
    index, batch = event.get("task_index"), event.get("delegation_id")
    if isinstance(index, int) and not (isinstance(sid, str) and sid):
        for sub in reversed(subs):
            if sub.get("_index") == index and sub.get("_batch") == batch and sub.get("status") == "running":
                return sub
    return None


def _new(state: dict[str, Any], now: float, *, sid: str | None, goal: Any, session: str | None) -> dict[str, Any]:
    subs = _subs(state)
    n = int(state.get("next_s") or 1)
    state["next_s"] = n + 1
    clean_goal = A.preview_block(goal, GOAL_CHARS) if goal else None
    sub: dict[str, Any] = {"id": sid if isinstance(sid, str) and sid else f"s{n}", "status": "running",
                           "started_at": round(now, 3), "_seen": round(now, 3), "_act": A.new_state(),
                           "_cursor": 0}
    if clean_goal:
        sub["goal"] = clean_goal
    title = instant_title(goal)
    if title:
        sub["title"], sub["_title_src"] = title, "instant"
    if session:
        sub["_session"] = session
    subs.append(sub)
    if len(subs) > MAX_SUBAGENTS:  # drop the oldest settled one first
        old = next((s for s in subs if s.get("status") != "running"), subs[0])
        subs.remove(old)
    return sub


def on_start(state: dict[str, Any], event: dict[str, Any], now: float) -> dict[str, Any]:
    goal = event.get("goal") or event.get("preview")
    session = event.get("child_session_id") if isinstance(event.get("child_session_id"), str) else None
    sub = _find(state, event)
    if sub is None:
        sub = _new(state, now, sid=event.get("subagent_id"), goal=goal, session=session)
    else:
        if session and not sub.get("_session"):
            sub["_session"] = session
        sid = event.get("subagent_id")
        if isinstance(sid, str) and sid and sub.get("id") != sid:
            sub["_sid"] = sid  # found by its session before Hermes named it: its row id stays put
    for src, dst in (("task_index", "_index"), ("delegation_id", "_batch")):
        if event.get(src) is not None:
            sub[dst] = event[src]
    depth = event.get("depth")
    if isinstance(depth, int) and not isinstance(depth, bool) and depth > 1:
        sub["depth"] = depth
        if isinstance(event.get("parent_id"), str) and event["parent_id"]:
            sub["parent_id"] = event["parent_id"]
    model = A.plain(event.get("model"), 40) if isinstance(event.get("model"), str) else None
    if model:
        sub["model"] = model
    sub["_seen"] = round(now, 3)
    return sub


def on_complete(state: dict[str, Any], event: dict[str, Any], now: float) -> dict[str, Any]:
    sub = _find(state, event)
    if sub is None:  # the start was missed (a resumed stream): the end still names it
        sub = on_start(state, event, now - (_num(event.get("duration_seconds")) or 0))
    raw = str(event.get("status") or "").lower()
    status = _DONE.get(raw) or ("done" if event.get("summary") else "failed")
    _settle(sub, status, now, summary=event.get("summary") or event.get("preview"))
    count = event.get("tool_count")
    if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
        sub["tool_count"] = count
    return sub


def _settle(sub: dict[str, Any], status: str, now: float, *, summary: Any = None, note: str | None = None) -> None:
    if sub.get("status") != "running" and sub.get("status") in STATUSES:
        if summary and not sub.get("summary"):
            safe = A.preview_block(summary, SUMMARY_CHARS)
            if safe:
                sub["summary"] = safe
        return
    sub["status"] = status
    sub["ended_at"] = round(now, 3)
    sub["_seen"] = round(now, 3)
    safe = A.preview_block(summary, SUMMARY_CHARS) if summary else None
    if safe:
        sub["summary"] = safe
    if note:
        sub["note"] = note
    for item in sub.get("_act", {}).get("items") or []:  # its own steps take its outcome
        if item.get("status") == "running":
            item["status"] = "failed" if status == "failed" else "done"
            if "tool" in item:
                item["ended_at"] = round(now, 3)


def on_run_end(state: dict[str, Any], end: str, now: float) -> None:
    """The task's run ended. A helper still running when the run was stopped or failed stops with
    it; when the run completed it may be a background helper still at work, so the watch keeps
    following it (one with no transcript to follow can't report again: stopped)."""
    state["ended"], state["ended_at"] = end, round(now, 3)
    for sub in _subs(state):
        if sub.get("status") != "running":
            continue
        if end != "completed":
            _settle(sub, "stopped", now, note="Stopped with the task")
        elif not sub.get("_session"):
            _settle(sub, "stopped", now, note="No word back from this helper")


def apply_child_events(sub: dict[str, Any], events: list[dict[str, Any]], now: float) -> bool:
    """A helper's own tool calls, read back from its transcript, into its steps."""
    if not events:
        return False
    act = A.JobActivity(sub.get("_act"))
    changed = False
    for event in events:
        changed = act.observe(event, now) or changed
    items = act.state["items"]
    del items[:-MAX_CHILD_STEPS]
    sub["_act"] = act.state
    if changed:
        sub["_seen"] = round(max(now, float(sub.get("_seen") or 0)), 3)
    return changed


def settle_from_transcript(sub: dict[str, Any], info: dict[str, Any] | None, state: dict[str, Any], now: float) -> bool:
    """Settle a helper that never reported: its session closed (its last answer, or stopped without
    one), or the task ended and it has been quiet too long. True when it changed."""
    if sub.get("status") != "running":
        return False
    info = info or {}
    if info.get("ended_at"):
        answer = info.get("answer")
        if answer:
            _settle(sub, "done", float(info["ended_at"]), summary=answer)
        else:
            _settle(sub, "stopped", float(info["ended_at"]), note="It ended without an answer")
        return True
    last = max(float(sub.get("_seen") or 0), float(info.get("last_at") or 0))
    if state.get("ended") and now - last > IDLE_SETTLE_S:
        _settle(sub, "stopped", now, note="No word back from this helper")
        return True
    return False


# -- the public shape --------------------------------------------------------------------------

def _now_line(sub: dict[str, Any]) -> str | None:
    items = sub.get("_act", {}).get("items") or []
    running = next((i for i in reversed(items) if i.get("status") == "running"), None)
    item = running or (items[-1] if items else None)
    return A.clip(item["text"], NOW_CHARS) if item else None


def public(state: dict[str, Any] | None, *, detail: bool = False) -> list[dict[str, Any]]:
    """``subagents`` for a task's work object, oldest first; [] when it started none. ``detail``
    (``GET /voice/work/{run_id}``) adds each helper's steps with their argument/result previews."""
    if not isinstance(state, dict):
        return []
    out = []
    for sub in _subs(state) if isinstance(state.get("subagents"), list) else []:
        status = sub.get("status") if sub.get("status") in STATUSES else "running"
        entry: dict[str, Any] = {"id": sub["id"], "title": sub.get("title") or "Helper", "status": status}
        for key in ("goal", "started_at", "ended_at", "summary", "note", "depth", "parent_id", "model", "tool_count"):
            if sub.get(key) not in (None, ""):
                entry[key] = sub[key]
        act = A.public(sub.get("_act"), detail=detail)
        steps = act.get("activity") or []
        if steps:
            entry["steps_total"] = act.get("activity_total", len(steps))
            if detail:
                entry["activity"] = steps
        line = _now_line(sub) if status == "running" else None
        if line:
            entry["now"] = line
        if detail and act.get("touched"):
            entry["touched"] = act["touched"]
        out.append(entry)
    return out


# -- reading children back from Hermes' state.db -------------------------------------------------

def _ro(state_db: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None


def discover_children(state_db: Path, parent_session: str, since: float) -> list[dict[str, Any]]:
    """Child sessions Hermes' delegate_task made under *parent_session* since *since*:
    ``[{session, started_at, goal}]`` (goal = the child's first user turn). Read-only; [] when off."""
    db = _ro(state_db)
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT s.id, s.started_at, (SELECT m.content FROM messages m WHERE m.session_id=s.id AND "
            "m.role='user' ORDER BY m.id LIMIT 1) FROM sessions s WHERE s.parent_session_id=? AND "
            "s.started_at>=? AND coalesce(s.model_config,'') LIKE '%_delegate_from%' ORDER BY s.started_at LIMIT ?",
            (parent_session, float(since), MAX_SUBAGENTS)).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        db.close()
    return [{"session": sid, "started_at": float(at or 0), "goal": goal or ""}
            for sid, at, goal in rows if isinstance(sid, str)]


def child_info(state_db: Path, session: str) -> dict[str, Any]:
    """``{ended_at, last_at, answer}`` for one child session: when it closed (None while it runs),
    its newest row's time, and its last answer (an assistant message with text and no tool calls,
    only once it closed)."""
    db = _ro(state_db)
    if db is None:
        return {}
    try:
        row = db.execute("SELECT ended_at FROM sessions WHERE id=?", (session,)).fetchone()
        last = db.execute("SELECT MAX(timestamp) FROM messages WHERE session_id=?", (session,)).fetchone()
        answer = None
        if row and row[0]:
            hit = db.execute("SELECT content FROM messages WHERE session_id=? AND role='assistant' AND "
                             "coalesce(content,'')<>'' AND coalesce(tool_calls,'') IN ('','[]','null') "
                             "ORDER BY id DESC LIMIT 1", (session,)).fetchone()
            answer = hit[0] if hit else None
    except sqlite3.Error:
        return {}
    finally:
        db.close()
    return {"ended_at": _num(row[0]) if row else None, "last_at": _num(last[0]) if last else None,
            "answer": answer if isinstance(answer, str) and answer.strip() else None}


# -- following a task's helpers ------------------------------------------------------------------

_WATCHES: dict[tuple[int, str], "SubagentWatch"] = {}
_WATCHES_LOCK = threading.Lock()


class SubagentWatch:
    """Follows one task's helpers while any runs: new children under the task's session, each
    running child's transcript (its steps), settling the ones that never report, and the titler's
    name for each. Saves through the task's activity tracker (throttled like the rest)."""

    def __init__(self, store: Any, key: str, state_db: Path, session_of: Callable[[], str | None] | None,
                 *, on_flush: Callable[[], None] | None = None, titler: Callable[[str], str | None] | None = None,
                 since: float | None = None, poll_s: float = POLL_S, clock: Callable[[], float] = time.time):
        self.store, self.key, self.state_db = store, key, Path(state_db)
        self.session_of, self.on_flush, self.titler = session_of, on_flush, titler
        self.clock = clock
        self.since = (clock() if since is None else since) - 5.0
        self.poll_s = max(0.05, float(poll_s))
        self.started = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._titled: set[str] = set()

    # -- one pass --
    def poll(self) -> bool:
        """Read once. True while there is still something to follow."""
        now = self.clock()
        try:
            parent = self.session_of() if self.session_of else None
        except Exception:
            parent = None
        found = discover_children(self.state_db, parent, self.since) if parent else []
        with A._LOCK:
            job = A.tracker(self.store, self.key)
            state = job.state
            changed = False
            if not state.get("helpers_watched"):
                state["helpers_watched"] = True  # so the task's end is noted even before a helper shows up
                changed = True
            known = {s.get("_session") for s in _subs(state)}
            for child in found:
                if child["session"] in known:
                    continue
                _new(state, child["started_at"] or now, sid=None, goal=child["goal"], session=child["session"])
                changed = True
            follow = [(s["id"], s["_session"], int(s.get("_cursor") or 0)) for s in _subs(state)
                      if s.get("status") == "running" and s.get("_session")]
            to_title = [(s["id"], s.get("goal") or "") for s in _subs(state)
                        if s.get("_title_src") != "titler" and s.get("goal") and s["id"] not in self._titled]
        from . import threads
        reads = []
        for sid, session, cursor in follow:
            events, newest = threads.session_tool_events(self.state_db, session, cursor)
            reads.append((sid, events, newest, child_info(self.state_db, session)))
        with A._LOCK:
            job = A.tracker(self.store, self.key)
            state = job.state
            by_id = {s["id"]: s for s in _subs(state)}
            for sid, events, newest, info in reads:
                sub = by_id.get(sid)
                if sub is None:
                    continue
                sub["_cursor"] = max(int(sub.get("_cursor") or 0), int(newest))
                changed = apply_child_events(sub, events, now) or changed
                changed = settle_from_transcript(sub, info, state, now) or changed
            running = any(s.get("status") == "running" for s in _subs(state))
            ended = bool(state.get("ended"))
            if changed:
                job.dirty = True
                saved = A._flush_locked(self.store, self.key, job, force=True, on_flush=None, now=None)
            else:
                saved = False
        if saved and self.on_flush:
            self._flush_cb()
        self._name(to_title)
        return running or not ended

    def _name(self, pending: list[tuple[str, str]]) -> None:
        if not self.titler:
            return
        for sid, goal in pending[:3]:
            self._titled.add(sid)
            try:
                title = self.titler(goal)
            except Exception:
                title = None
            safe = A.plain(title, TITLE_CHARS) if title else None
            if not safe:
                continue
            with A._LOCK:
                job = A.tracker(self.store, self.key)
                sub = next((s for s in _subs(job.state) if s["id"] == sid), None)
                if sub is None or sub.get("_title_src") == "titler":
                    continue
                sub["title"], sub["_title_src"] = safe, "titler"
                job.dirty = True
                saved = A._flush_locked(self.store, self.key, job, force=True, on_flush=None, now=None)
            if saved and self.on_flush:
                self._flush_cb()

    def _flush_cb(self) -> None:
        if self.on_flush is None:
            return
        try:
            self.on_flush()
        except Exception:
            pass

    # -- the loop --
    def start(self) -> "SubagentWatch":
        thread = threading.Thread(target=self._run, name="speakeasy-subagents", daemon=True)
        self._thread = thread
        thread.start()
        return self

    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    def _run(self) -> None:
        idle_after_end = 0
        try:
            while not self._stop.wait(self.poll_s):
                try:
                    more = self.poll()
                except Exception:
                    logger.debug("speakeasy: helper watch failed", exc_info=True)
                    more = True
                if self.clock() - self.started > WATCH_MAX_S:
                    break
                idle_after_end = 0 if more else idle_after_end + 1
                if idle_after_end >= 3:  # the task ended and nothing runs: one last look, then stop
                    break
        finally:
            with _WATCHES_LOCK:
                if _WATCHES.get((id(self.store), self.key)) is self:
                    _WATCHES.pop((id(self.store), self.key), None)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)


def ensure_watch(store: Any, key: str, state_db: Path, session_of: Callable[[], str | None] | None, **kw: Any) -> SubagentWatch:
    """The task's helper watch, started when it isn't running."""
    with _WATCHES_LOCK:
        found = _WATCHES.get((id(store), key))
        if found is not None and found.alive():
            return found
        watch = SubagentWatch(store, key, state_db, session_of, **kw)
        _WATCHES[(id(store), key)] = watch
    return watch.start()


def watches_helpers(event: Any) -> bool:
    """Whether an event means the task is starting helpers."""
    if not isinstance(event, dict):
        return False
    kind = event.get("event")
    return kind == "subagent.start" or (kind in {"tool.started", "tool.start"} and event.get("tool") == "delegate_task")
