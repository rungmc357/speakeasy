"""Home (SPK-124): one ordered list of what's live and new, the same on every app.

``GET /voice/now?tz=<IANA zone>`` answers::

    {rev, as_of, sentence, counts, tiers, call, items[], background, earlier, notice}

Each item is one thing that needs the user or is happening: a question (tier 1), something that
stopped (2), work under way (3), a new answer (4), something waiting on someone else (5) or an
answer already seen today (6). The plugin owns the order and the header sentence so the iPhone,
iPad, Mac, widgets and Live Activity never disagree; device caps ("2 more questions") are the
client's, from ``tiers``. There are no pins: Home is what's live or new. A seen answer moves to
Earlier at the 4:00 AM rollover in the phone's zone (or the one after, when it was opened in the six
hours before), and "Later" puts an item off until a time or until it changes.

Everything here is read from the store and the Hermes state DB; nothing is invented or guessed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
import time
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import runstate as RS
from .failures import UNKNOWN, failure_reason

ROLLOVER_HOUR = 4
OPENED_GRACE_S = 6 * 3600       # a seen answer opened in the 6 h before a rollover stays to the next one
UNREAD_KEPT_S = 3 * 86400       # an answer nobody opened leaves Home after three days (it stays unread in Earlier)
ROLLOVER_NOTICES = 3            # "Moved 3 seen answers to Earlier." on the first three mornings that move any
MAX_ITEMS = 60
BACKGROUND_ITEMS = 5
LIVE_LINES = 8                  # Hermes conversations (not Speakeasy tasks) given a live line per build
LINE_CHARS = 90
TITLE_CHARS = 80
LATER_KEPT_S = 7 * 86400

QUESTION, STOPPED, WORKING, WAITING, NEW_ANSWER, SEEN_ANSWER = (
    "question", "stopped", "working", "waiting", "new_answer", "seen_answer")
TIER = {QUESTION: 1, STOPPED: 2, WORKING: 3, NEW_ANSWER: 4, WAITING: 5, SEEN_ANSWER: 6}
KINDS = tuple(TIER)

# failures.py kind -> the one action that fixes it (the copy is failures.py's own sentence).
FIX = {"billing": "credits", "auth": "sign_in", "rate_limit": "retry", "content_refused": "retry",
       "model_not_found": "setup", "hermes_key": "setup", "hermes_unreachable": "setup", UNKNOWN: "open"}

_NUMBERS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_HTTPS_RE = re.compile(r"https://[^\s<>\"']{4,500}")


# -- words ---------------------------------------------------------------------------------------

# Home on a call (phase 3): the voice's card stays lit this long after it last spoke of it, and the
# "Your call" card shows for at most this long after the call (or until the next 4 AM / cleared).
TALKING_S = 600
CALL_CARD_S = 6 * 3600


def number(n: int) -> str:
    """One to nine as words, 10 and up as numerals."""
    return _NUMBERS[n] if 0 <= n < 10 else str(n)


def _plural(n: int, one: str, many: str) -> str:
    return f"{number(n)} {one if n == 1 else many}"


def _clip(text: Any, limit: int) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= limit else words[:limit - 1].rstrip(" ,;:.") + "\u2026"


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def sentence(items: list[dict[str, Any]]) -> str:
    """The header sentence: one clause for every kind of card the list shows, in the list's own
    order (questions, stopped, in the works, new answers, waiting), so nothing below it goes
    unmentioned. With exactly one item of the only kind there is, it is named."""
    counts = {k: sum(1 for i in items if i["kind"] == k) for k in KINDS}
    order = [k for k in (QUESTION, STOPPED, WORKING, NEW_ANSWER, WAITING) if counts[k]]
    if not order:
        return "All caught up." if counts[SEEN_ANSWER] else "All quiet."
    first = order[0]
    if len(order) == 1 and counts[first] == 1:
        title = _clip(next(i["title"] for i in items if i["kind"] == first), 40)
        return {QUESTION: f"{title} has a question for you.", STOPPED: f"{title} stopped.",
                NEW_ANSWER: f"{title} has a new answer.", WORKING: f"{title} is in the works.",
                WAITING: "Waiting on one reply."}[first]

    def clause(kind: str, lead: bool) -> str:
        n = counts[kind]
        if kind == QUESTION:
            return _plural(n, "question", "questions") + " for you"
        if kind == STOPPED:
            return _plural(n, "thing", "things") + " stopped"
        if kind == NEW_ANSWER:
            return _plural(n, "new answer", "new answers")
        if kind == WORKING:
            return _plural(n, "thing", "things") + " in the works"
        if lead:
            return "Waiting on " + _plural(n, "reply", "replies")
        return number(n) + (" waiting on a reply" if n == 1 else " waiting on replies")

    parts = [clause(k, i == 0) for i, k in enumerate(order)]
    if len(parts) == 1:
        return _cap(parts[0]) + "."
    return _cap(", ".join(parts[:-1]) + ", and " + parts[-1]) + "."


# -- time ----------------------------------------------------------------------------------------

def zone(tz: str | None) -> dt.tzinfo:
    """The phone's zone (``?tz=``), else this computer's."""
    if tz:
        try:
            return ZoneInfo(str(tz)[:64])
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return dt.datetime.now().astimezone().tzinfo or dt.timezone.utc


def rollover(now: float, tz: dt.tzinfo) -> float:
    """The most recent 4:00 AM in *tz* at or before *now* (epoch seconds)."""
    local = dt.datetime.fromtimestamp(now, tz)
    edge = local.replace(hour=ROLLOVER_HOUR, minute=0, second=0, microsecond=0)
    if edge > local:
        edge = (local - dt.timedelta(days=1)).replace(hour=ROLLOVER_HOUR, minute=0, second=0, microsecond=0)
    return edge.timestamp()


def seen_stays(finished: float, opened: float | None, edge: float) -> bool:
    """A seen answer stays on Home until the rollover after it finished, or the one after that when
    it was opened in the six hours before that rollover."""
    if finished >= edge:
        return True
    return opened is not None and edge - OPENED_GRACE_S <= opened < edge and finished >= edge - 86400


# -- the build -----------------------------------------------------------------------------------

class NowModel:
    """Builds ``/voice/now`` from a VoiceService (store, sessions, live calls). ``rev`` moves only
    when something a user can see changes (kept in the store's meta, so it survives restarts)."""

    LIVE_CACHE_S = 10.0

    def __init__(self, service: Any, clock: Callable[[], float] = time.time) -> None:
        self.svc = service
        self.clock = clock
        self.lock = threading.Lock()
        self._live: dict[str, tuple[float, str | None]] = {}
        self._last_sessions: set[str] = set()

    # -- helpers ---------------------------------------------------------------------------------
    @property
    def store(self) -> Any:
        return self.svc.store

    def _meta_float(self, key: str) -> float:
        try:
            return float(self.store.get_meta(key) or 0)
        except ValueError:
            return 0.0

    def live_call(self) -> Any:
        with self.svc.lock:
            live = [i for i in self.svc.interactions.values()
                    if not i.call_closed and i.status not in {"closed", "rejected"} and not i.paused
                    and i.worker is not None]
        return live[-1] if live else None

    def last_call(self, now: float, edge: float, cleared: float) -> Any:
        """The call that ended most recently, while its "Your call" card still shows: until it's
        cleared, opened, or the next 4 AM; and for at most CALL_CARD_S."""
        with self.svc.lock:
            ended = [i for i in self.svc.interactions.values()
                     if getattr(i, "ended_at", None) and i.successor is None]
        if not ended:
            return None
        last = max(ended, key=lambda i: i.ended_at)
        seen = self._meta_float(f"now_call_seen:{last.interaction_id}")
        if last.ended_at < max(edge, cleared, seen) or now - last.ended_at > CALL_CARD_S:
            return None
        return last

    def call_summary(self, call: Any, now: float) -> dict[str, Any] | None:
        """The "Your call · 6 min" card: what the call did, and the decisions said on it."""
        with call.lock:
            runs = [(t, r.idem_key, r.status) for t, r in call.runs.items()
                    if r.idem_key and r.status != "rejected" and r.idem_key not in call.carried_keys]
        things, decisions = [], []
        for task_id, key, _status in runs:
            if task_id.startswith("choice_"):
                answer = self.store.question_answer(key)
                if answer:
                    decisions.append(_clip(answer, 80))
                continue
            title = self.store.title(key) or self.store.request_text(key)
            if title:
                things.append(_clip(title, TITLE_CHARS))
            answer = self.store.question_answer(key)
            if answer:
                decisions.append(_clip(answer, 80))
        minutes = max(1, round((call.ended_at - call.started_at) / 60))
        return {"id": f"call:{call.interaction_id}", "interaction_id": call.interaction_id,
                "title": f"Your call \u00b7 {minutes} min", "started_at": round(call.started_at, 3),
                "ended_at": round(call.ended_at, 3), "minutes": minutes,
                "session_id": getattr(call, "live_session_id", None), "things": things[:4],
                "more_things": max(0, len(things) - 4), "decisions": decisions[:3]}

    def call_state(self, call: Any, now: float) -> dict[str, Any]:
        """What Home needs from a live call: which task the voice is on (and since when), which
        answers it read out, and which tasks belong to it. Vision Pro's focus wins over the voice's."""
        with call.lock:
            keys = {r.idem_key: t for t, r in call.runs.items() if r.idem_key}
            talking, at = call.focus_task_id or call.talking_about, call.talking_at
            if call.focus_task_id:
                at = now
            spoken = dict(call.spoken_tasks)
        if talking and not call.focus_task_id and now - at > TALKING_S:
            talking = None
        return {"keys": keys, "talking": talking, "spoken": spoken}

    def _session_line(self, sid: str) -> str | None:
        """A running Hermes conversation's live line (what it's doing), cached for 10 s."""
        now = self.clock()
        hit = self._live.get(sid)
        if hit and now - hit[0] < self.LIVE_CACHE_S:
            return hit[1]
        line = None
        try:
            from . import history as H
            entries = H.session_messages(self.svc.rt.state_db, sid, limit=120)["entries"]
            live = H.session_live(self.svc.rt.state_db, sid, entries, now=now)
            line = (live or {}).get("now")
        except Exception:
            line = None
        self._live[sid] = (now, line)
        return line

    # -- one Speakeasy task ----------------------------------------------------------------------
    def _task_item(self, row: dict[str, Any], work: dict[str, Any], open_question: bool,
                   approvals: dict[str, dict[str, Any]], read_at: float | None, edge: float,
                   cleared: float, now: float, call: Any,
                   call_info: dict[str, Any] | None = None) -> dict[str, Any] | None:
        status = work.get("status") or row["status"]
        run_id = work.get("run_id") or row["run_id"]
        result = work.get("result") or {}
        title = _clip((work.get("job") or {}).get("name") or work.get("title") or "A task", TITLE_CHARS)
        session = row.get("linked_source") or work.get("session_id") or row.get("session_id")
        finished = float(row["settled_at"]) if row.get("settled_at") else None
        started = float(row["created"] or work.get("started_at") or 0) or None
        seen = bool(row["seen"]) or bool(work.get("seen")) or (
            read_at is not None and finished is not None and read_at + 1 >= finished)
        item: dict[str, Any] = {"session_id": session, "run_id": run_id, "title": title, "source": "speakeasy",
                                "started_at": started, "updated_at": float(row.get("updated") or started or now)}
        if finished is not None:
            item["finished_at"] = finished
        approval = approvals.get(row["key"])
        question = result.get("question") if isinstance(result.get("question"), dict) else None
        if approval is not None:
            item.update(kind=QUESTION, id=f"q:{run_id}", line=_clip(approval["description"] or
                                                                    "Hermes needs your OK to go on.", 240),
                        question={"shape": "approval", "prompt": approval["description"],
                                  "approval": {"choices": approval["choices"]},
                                  "asked_at": float(row.get("updated") or now)})
        elif question and open_question and status in RS.TERMINAL | RS.WORKING:
            q = public_question(question, run_id, finished or now)
            if q.get("expires_at") and q["expires_at"] <= now:
                item.update(kind=WAITING, id=f"q:{run_id}", line="This one timed out.",
                            question={**q, "expired": {"at": q["expires_at"]}})
            else:
                item.update(kind=QUESTION, id=f"q:{run_id}", line=_clip(q["prompt"], 240), question=q)
        elif status in RS.ASKING:
            item.update(kind=QUESTION, id=f"q:{run_id}", line="Hermes needs your OK to go on.",
                        question={"shape": "approval", "prompt": "Hermes needs your OK to go on.",
                                  "asked_at": float(row.get("updated") or now)})
        elif status in RS.UNDER_WAY or status in {RS.RunState.CANCEL_REQUESTED, RS.RunState.AMBIGUOUS}:
            line = work.get("now_line") or work.get("short_status") or "Working on it"
            item.update(kind=WORKING, line=_clip(line, LINE_CHARS))
            if isinstance(work.get("quiet_s"), (int, float)) and work["quiet_s"] >= 120:
                item["idle_seconds"] = int(work["quiet_s"])
        elif status == RS.RunState.WAITING_ON_TASK:
            behind = (work.get("waiting") or {}).get("name") or ""
            item.update(kind=WAITING, line=_clip(work.get("detail") or (
                f"Starts when \u201c{behind}\u201d finishes." if behind else "Waiting for another task."), LINE_CHARS))
            item["waiting_on"] = _clip(behind, 60) if behind else "another task"
        elif status in {RS.RunState.FAILED, RS.RunState.INTERRUPTED}:
            if seen:
                return None  # opened: it lives in its conversation (Earlier) from here
            item.update(kind=STOPPED, line=_clip(work.get("failure_reason") or failure_reason(
                status, row.get("failure")) or "It stopped.", 240))
            kind = row.get("failure") or (UNKNOWN if status == RS.RunState.FAILED else None)
            item["fix"] = {"kind": "retry" if status == RS.RunState.INTERRUPTED else FIX.get(kind or "", "open")}
        elif status == RS.RunState.COMPLETED:
            line = _clip(result.get("headline") or work.get("summary") or result.get("spoken") or "Done.", 240)
            if question and row.get("question_closed") == "dismissed":
                if (finished or 0) < max(edge, cleared):
                    return None
                item.update(kind=WAITING, line="Paused. Its question is in the conversation.",
                            dismissed_question=True)
            elif not seen:
                if now - (finished or now) > UNREAD_KEPT_S:
                    return None
                item.update(kind=NEW_ANSWER, line=line, unread=True)
            else:
                if (finished or 0) < cleared or not seen_stays(finished or 0, read_at, edge):
                    return None
                item.update(kind=SEEN_ANSWER, line=line)
        else:
            return None  # cancelled, rejected: nothing to look at
        item.setdefault("id", f"s:{session}" if session else f"t:{run_id or row['key']}")
        item.setdefault("unread", False)
        if finished is not None and finished < edge and item["kind"] in {NEW_ANSWER, SEEN_ANSWER, STOPPED}:
            item["provenance"] = {"kind": "yesterday", "at": finished}
        elif call is not None and row.get("interaction_id") != call.interaction_id and item["kind"] in {
                NEW_ANSWER, QUESTION, STOPPED} and (finished or 0) >= 0:
            started_call = getattr(call, "started_at", None)
            if started_call and (started or 0) < started_call:
                item["provenance"] = {"kind": "earlier_call", "at": started}
        task_id = (call_info or {}).get("keys", {}).get(row["key"])
        if task_id is not None:
            if item["kind"] in {NEW_ANSWER, SEEN_ANSWER} and task_id in call_info["spoken"]:
                # Told on the call: already read, and says so.
                item.update(kind=SEEN_ANSWER, unread=False, on_call="heard", line="Told you on the call.")
            elif item["kind"] == QUESTION and (task_id == call_info["talking"] or float(
                    (item.get("question") or {}).get("asked_at") or 0) >= float(getattr(call, "started_at", 0) or 0)):
                # Asked on this call: tier 0, the one card that moves during a call.
                options = (item.get("question") or {}).get("options") or []
                risky = any(o.get("risk") == "high" for o in options)
                item.update(on_call="asking", say_ok=not risky, asking_label="Asking now",
                            **({"lock_line": "Can't be undone \u00b7 Needs a tap"} if risky else {}))
            item["_task_id"] = task_id
        thumbs = _thumbs(result, run_id)
        if thumbs:
            item["thumbs"] = thumbs
        return item

    # -- the whole list --------------------------------------------------------------------------
    def build(self, tz: str | None = None, *, peek: bool = False) -> dict[str, Any]:
        now = self.clock()
        zone_ = zone(tz)
        edge = rollover(now, zone_)
        cleared = self._meta_float("now_cleared_at")
        svc = self.svc
        marks = self.store.session_marks()
        call = self.live_call()
        call_info = self.call_state(call, now) if call is not None else None
        rows = self.store.now_runs(since=min(edge - 86400, now - UNREAD_KEPT_S))
        rows = [r for r in rows if not r["dismissed"]]
        # Which question each conversation still waits on: the one rule every surface uses.
        open_keys = set(svc._open_questions([r["key"] for r in rows], now)) if rows else set()
        approvals = {}
        for a in svc._session_approvals([r["key"] for r in rows if r["status"] in RS.ASKING]):
            key = self.store.key_for_run(a["run_id"]) if a.get("run_id") else None
            if key:
                approvals[key] = a
        name = svc.settings.get()["assistant_name"]
        by_place: dict[str, dict[str, Any]] = {}
        items: list[dict[str, Any]] = []
        for row in rows:  # newest first: a conversation's newest task speaks for it
            place = row.get("linked_source") or row.get("session_id") or row["key"]
            work = self.store.work(idem_key=row["key"], assistant_name=name) or {}
            read_at = (marks.get(place) or {}).get("read_at")
            item = self._task_item(row, work, row["key"] in open_keys, approvals, read_at, edge, cleared, now, call,
                                   call_info)
            if item is None:
                continue
            held = by_place.get(place)
            if held is not None:
                # One card per conversation, except a question, which keeps its own card.
                if item["kind"] == QUESTION and held["kind"] != QUESTION:
                    items.append(item)
                continue
            by_place[place] = item
            items.append(item)
        # Hermes conversations Speakeasy didn't start: running ones, and replies nobody has read yet.
        background: list[dict[str, Any]] = []
        mine = {i["session_id"] for i in items if i.get("session_id")}
        try:
            page = svc.history({"kinds": "conversations,scheduled,kanban", "limit": "40"}, for_now=True)
        except Exception:
            page = {"sessions": []}
        lines_left = LIVE_LINES
        for s in page.get("sessions") or []:
            sid = s["id"]
            if sid in mine or s.get("source") == "speakeasy":
                continue
            if s.get("automated"):
                if s.get("state") == "running":
                    line = self._session_line(sid) if lines_left > 0 else None
                    lines_left -= 1
                    background.append({"session_id": sid, "title": _clip(s.get("title"), TITLE_CHARS),
                                       "line": _clip(line or "Working on it", LINE_CHARS),
                                       "source": s.get("source"), "source_label": s.get("source_label"),
                                       "started_at": s.get("last_activity")})
                continue
            base = {"id": f"s:{sid}", "session_id": sid, "title": _clip(s.get("title"), TITLE_CHARS),
                    "source": s.get("source"), "source_label": s.get("source_label"),
                    "updated_at": s.get("last_activity"), "started_at": s.get("last_activity"), "unread": False}
            if s.get("state") == "running":
                line = self._session_line(sid) if lines_left > 0 else None
                lines_left -= 1
                items.append({**base, "kind": WORKING, "line": _clip(line or "Working on it", LINE_CHARS)})
            elif s.get("unread") and now - float(s.get("last_activity") or 0) <= 86400:
                items.append({**base, "kind": NEW_ANSWER, "line": _clip(s.get("preview"), 240), "unread": True,
                              "finished_at": s.get("last_activity")})
        items = self._apply_later(items, now)
        items = order(items, self._first_seen(items, now))
        # On a call: the question the voice is asking right now goes first (tier 0).
        items.sort(key=lambda i: 0 if i.get("on_call") == "asking" else 1)
        total = len(items)
        items = items[:MAX_ITEMS]
        focus = None
        for i in items:
            i["tier"] = 0 if i.get("on_call") == "asking" else TIER[i["kind"]]
            if call_info and call_info["talking"] and i.pop("_task_id", None) == call_info["talking"]:
                focus = focus or i["id"]
            i.pop("_task_id", None)
        counts = {"needs_you": sum(i["kind"] == QUESTION for i in items),
                  "stopped": sum(i["kind"] == STOPPED for i in items),
                  "working": sum(i["kind"] == WORKING for i in items),
                  "waiting": sum(i["kind"] == WAITING for i in items),
                  "finished_unread": sum(i["kind"] == NEW_ANSWER for i in items),
                  "on_its_own_running": len(background)}
        tiers: dict[str, int] = {}
        for i in items:
            tiers[str(i["tier"])] = tiers.get(str(i["tier"]), 0) + 1
        body: dict[str, Any] = {
            "sentence": sentence(items), "counts": counts, "tiers": tiers,
            "call": ({"interaction_id": call.interaction_id, "started_at": getattr(call, "started_at", None),
                      "focus": focus, "focus_label": "Talking about this" if focus else None}
                     if call is not None else None),
            "call_summary": None if call is not None else (
                self.call_summary(ended, now) if (ended := self.last_call(now, edge, cleared)) is not None else None),
            "items": items, "background": {"running": len(background), "items": background[:BACKGROUND_ITEMS]},
            "earlier": {"today_more": max(0, total - len(items))},
        }
        body["rev"] = self._rev(body)
        body["as_of"] = round(now, 3)
        body["notice"] = None if peek else self._notice(rows, marks, edge, cleared)
        self._last_sessions = {i["session_id"] for i in items if i.get("session_id")}
        return body

    def sessions_on_now(self) -> set[str]:
        """The conversations Home shows (Earlier's ``on_now`` / ``exclude_now``)."""
        if not self._last_sessions:
            try:
                self.build(peek=True)
            except Exception:
                return set()
        return set(self._last_sessions)

    # -- order -----------------------------------------------------------------------------------
    def _first_seen(self, items: list[dict[str, Any]], now: float) -> dict[str, float]:
        """When each working card first showed (kept in meta): "an existing card never changes
        position while it stays in this tier"."""
        try:
            known = json.loads(self.store.get_meta("now_working_order") or "{}")
        except ValueError:
            known = {}
        working = [i["id"] for i in items if i["kind"] == WORKING]
        out = {k: float(v) for k, v in known.items() if k in working}
        for i in items:
            if i["kind"] == WORKING and i["id"] not in out:
                out[i["id"]] = round(now, 3)  # a new card goes on top; the ones already there stay put
        if out != known:
            self.store.set_meta("now_working_order", json.dumps(out))
        return out

    # -- Later -----------------------------------------------------------------------------------
    @staticmethod
    def signature(item: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps([item.get("kind"), item.get("line"), item.get("finished_at")],
                                         sort_keys=True).encode()).hexdigest()[:16]

    def _apply_later(self, items: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        later = self.store.later_items()
        if not later:
            return items
        out = []
        for item in items:
            mark = later.get(item["id"])
            if mark is None:
                out.append(item)
                continue
            due = mark["until"] is not None and now >= float(mark["until"])
            changed = mark["on_change"] and mark["seen_sig"] != self.signature(item)
            if mark["woke_at"] is None and not due and not changed:
                continue  # put off: back when it's due or when it changes
            if mark["woke_at"] is None:
                self.store.wake_later(item["id"], now)
                mark["woke_at"] = now
            item["provenance"] = {"kind": "later", "at": mark["woke_at"]}
            out.append(item)
        for item_id, mark in later.items():
            if mark["woke_at"] and now - float(mark["woke_at"]) > LATER_KEPT_S:
                self.store.clear_later(item_id)
        return out

    def find(self, item_id: str, tz: str | None = None) -> dict[str, Any] | None:
        """An item as Home shows it (or would, were it not put off)."""
        later = self.store.later_items().get(item_id)
        if later is not None:
            self.store.clear_later(item_id)
        try:
            body = self.build(tz, peek=True)
        finally:
            if later is not None:
                self.store.set_later(item_id, later["until"], later["on_change"], later["seen_sig"])
        return next((i for i in body["items"] if i["id"] == item_id), None)

    # -- rev, notice -----------------------------------------------------------------------------
    def _rev(self, body: dict[str, Any]) -> int:
        stable = {**body, "items": [{k: v for k, v in i.items() if k not in {"idle_seconds"}} for i in body["items"]]}
        sig = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
        with self.lock:
            rev = int(self._meta_float("now_rev"))
            if self.store.get_meta("now_sig") != sig:
                rev += 1
                self.store.set_meta("now_rev", str(rev))
                self.store.set_meta("now_sig", sig)
            return rev

    def _notice(self, rows: list[dict[str, Any]], marks: dict[str, Any], edge: float,
                cleared: float) -> dict[str, Any] | None:
        """"Moved 3 seen answers to Earlier." once, the first morning after a rollover that moved
        some, on the first three such mornings; cleared once any device fetched it."""
        if self._meta_float("now_rollover_at") >= edge:
            return None
        shown = int(self._meta_float("now_rollover_notices"))
        moved = 0
        for r in rows:
            fin = float(r["settled_at"] or 0)
            if r["status"] != "completed" or not fin or fin >= edge or fin < edge - 86400 or fin < cleared:
                continue
            read_at = (marks.get(r.get("linked_source") or r.get("session_id") or "") or {}).get("read_at")
            if (r["seen"] or (read_at and read_at + 1 >= fin)) and not seen_stays(fin, read_at, edge):
                moved += 1
        self.store.set_meta("now_rollover_at", repr(edge))
        if not moved or shown >= ROLLOVER_NOTICES:
            return None
        self.store.set_meta("now_rollover_notices", str(shown + 1))
        return {"kind": "rollover", "moved": moved,
                "text": f"Moved {_plural(moved, 'seen answer', 'seen answers')} to Earlier."}


def order(items: list[dict[str, Any]], first_seen: dict[str, float]) -> list[dict[str, Any]]:
    """Tier order (design §2.3): questions expiring soonest, then high risk, then oldest asked;
    stopped newest first; working by when the card first showed, newest first (a card never moves
    while it stays working); new answers newest first; waiting oldest first; seen newest first."""
    risk = {"high": 0, "medium": 1, "low": 2}

    def key(i: dict[str, Any]) -> tuple[Any, ...]:
        kind = i["kind"]
        if kind == QUESTION:
            q = i.get("question") or {}
            return (TIER[kind], float(q.get("expires_at") or 1e12), risk.get(q.get("risk") or "", 3),
                    float(q.get("asked_at") or 0))
        if kind == WORKING:
            return (TIER[kind], -first_seen.get(i["id"], 0.0), -float(i.get("started_at") or 0), i["id"])
        if kind == WAITING:
            return (TIER[kind], float(i.get("updated_at") or 0))
        stamp = float(i.get("finished_at") or i.get("updated_at") or 0)
        return (TIER[kind], -stamp)
    return sorted(items, key=key)


def public_question(view: dict[str, Any], run_id: str | None, asked_at: float) -> dict[str, Any]:
    """A task's decision card as Home shows it: options carry an ``id`` (o1..o4) to answer with, an
    image (the card it points at), and the https link and up to three facts the task gave."""
    images = view.get("images") or []
    options = []
    any_image = False
    for n, raw in enumerate(view.get("options") or []):
        opt: dict[str, Any] = {"id": f"o{n + 1}"}
        if isinstance(raw, dict):
            opt.update({k: raw[k] for k in ("text", "detail", "tags", "risk", "reversible") if k in raw})
            link = raw.get("link")
            if isinstance(link, str) and _HTTPS_RE.fullmatch(link.strip()):
                opt["link"] = link.strip()
            facts = [_clip(f, 80) for f in raw.get("facts") or [] if isinstance(f, str) and f.strip()][:3]
            if facts:
                opt["facts"] = facts
        else:
            opt["text"] = str(raw)
        if n < len(images) and isinstance(images[n], int) and images[n] > 0 and run_id:
            opt["image"] = f"/voice/card-image/{run_id}/{images[n]}"
            any_image = True
        options.append(opt)
    rec = view.get("recommended")
    out: dict[str, Any] = {"shape": "things" if any_image else "words", "prompt": view.get("question") or "",
                           "options": options, "asked_at": asked_at}
    for k in ("context", "why", "affects"):
        if view.get(k):
            out[k] = view[k]
    if isinstance(rec, int) and 0 <= rec < len(options):
        out["recommended"] = rec
        out["risk"] = options[rec].get("risk") or "high"
    out["approvable"] = bool(view.get("approvable"))
    exp = view.get("expires_in")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool) and 60 <= exp <= 7 * 86400:
        out["expires_at"] = asked_at + float(exp)
    return out


def _thumbs(result: dict[str, Any], run_id: str | None) -> list[dict[str, Any]]:
    """The newest two pictures a task produced (never file chips)."""
    cards = result.get("cards") if isinstance(result, dict) else None
    if not run_id or not isinstance(cards, list):
        return []
    nums = [n + 1 for n, c in enumerate(cards[:8]) if isinstance(c, dict) and c.get("kind") == "image"]
    return [{"url": f"/voice/card-image/{run_id}/{n}"} for n in nums[-2:]]


def option_text(item: dict[str, Any], option_id: str) -> str | None:
    for opt in (item.get("question") or {}).get("options") or []:
        if opt.get("id") == option_id:
            return opt.get("text")
    return None


def ids(items: Iterable[dict[str, Any]]) -> list[str]:
    return [i["id"] for i in items]
