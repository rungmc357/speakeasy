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
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import runstate as RS
from .failures import NO_REASON, UNKNOWN, failure_reason
from .home_words import (clean_ask, collapse, fold_duplicates, hero_box, near_duplicate, picture_caption,
                         pill_text, replaced_questions, short_title, status_line)

ROLLOVER_HOUR = 4
OPENED_GRACE_S = 6 * 3600       # a seen answer opened in the 6 h before a rollover stays to the next one
UNREAD_KEPT_S = 3 * 86400       # how far back Home looks for candidates
ANSWER_KEPT_S = 12 * 3600       # a finished answer leaves Home once opened, or 12 h after it finished (it stays in Earlier)
CHAT_KEPT_S = 24 * 3600         # a job started from Home's chat stays in that thread as long as its messages (home_chat.SHOWN_S)
ANSWER_CARD_S = 4 * 3600        # an answer older than this folds into the one "Earlier today · N" row (earlier.today)
EARLIER_TODAY_ITEMS = 20
BASELINE_GRACE_S = 3600         # first use of Home: answers older than this count as seen, so an upgrade never floods it
DISMISS_KEPT_S = 7 * 86400
ROLLOVER_NOTICES = 3            # "Moved 3 seen answers to Earlier." on the first three mornings that move any
MAX_ITEMS = 60
RECENT_KEPT_S = 14 * 86400      # Recent looks back this far
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

# The one next step a stopped card offers, by its fix (the app's button does it).
NEXT_STEP = {"retry": "Retry or dismiss.", "credits": "Add credits, then retry.",
             "sign_in": "Sign in again, then retry.", "setup": "See how to fix it, or dismiss.",
             "open": "Open it to retry, or dismiss."}
_LOWER_LEAD = re.compile(r"^(?:The|Your|A|An|It|This|That|Its|There)\b")

_NUMBERS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine")
_HTTPS_RE = re.compile(r"https://[^\s<>\"']{4,500}")


# -- words ---------------------------------------------------------------------------------------

# Home on a call (phase 3): the voice's card stays lit this long after it last spoke of it, and the
# "Your call" card shows for at most this long after the call (or until the next 4 AM / cleared).
TALKING_S = 600
CALL_CARD_S = 6 * 3600
CALL_LIST_KEEP = 3              # the call card lists this many jobs/answers (four when that's all), then "N more"


def number(n: int) -> str:
    """One to nine as words, 10 and up as numerals."""
    return _NUMBERS[n] if 0 <= n < 10 else str(n)


def _plural(n: int, one: str, many: str) -> str:
    return f"{number(n)} {one if n == 1 else many}"


def _clip(text: Any, limit: int) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= limit else words[:limit - 1].rstrip(" ,;:.") + "\u2026"


def full_title(name: str | None, full: str | None) -> str | None:
    """A job's whole title when its short name is the start of it ("Check email for pressing" ->
    "Check email for pressing messages"): Home never shows a phrase cut mid-way."""
    if not name:
        return full
    norm = lambda t: " ".join(re.sub(r"[\"\u201c\u201d]", "", t or "").split()).lower()
    whole = " ".join(re.sub(r"[\"\u201c\u201d]", "", full or "").split()).strip(" ,.:;-")
    if whole and len(whole) <= TITLE_CHARS and norm(whole).startswith(norm(name)) and len(whole) > len(name):
        return whole
    return name


def origin(at: float, now: float, tz: dt.tzinfo) -> dict[str, Any] | None:
    """Where a card came from, by real calendar days in the phone's zone: "From yesterday · 8:13 PM",
    "From Wednesday · 8:13 PM" within the week, else "From Nov 4". None for today."""
    day = dt.datetime.fromtimestamp(at, tz)
    today = dt.datetime.fromtimestamp(now, tz).date()
    ago = (today - day.date()).days
    if ago <= 0:
        return None
    clock = day.strftime("%I:%M %p").lstrip("0")
    if ago == 1:
        return {"kind": "yesterday", "at": at, "label": f"From yesterday \u00b7 {clock}"}
    if ago < 7:
        return {"kind": "earlier_day", "at": at, "label": f"From {day.strftime('%A')} \u00b7 {clock}"}
    return {"kind": "earlier_day", "at": at, "label": f"From {day.strftime('%b')} {day.day}"}


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
            runs = [(t, r.idem_key, r.idem_key in call.carried_keys) for t, r in call.runs.items()
                    if r.idem_key and r.status != "rejected"]
        things, decisions, answered, asking = [], [], [], []
        heard: list[str] = []          # what the call answered, one plain line each (the card's list)
        heard_text: list[str] = []
        opens: str | None = None       # the conversation the card opens: the newest task the call started
        keys = [k for t, k, _ in runs if not t.startswith("choice_")]
        try:
            open_keys = set(self.svc._open_questions(keys, now)) if keys else set()
        except Exception:
            open_keys = set()
        for task_id, key, carried in runs:
            answer = self.store.question_answer(key)
            if answer:
                decisions.append(_clip(answer, 80))
            if task_id.startswith("choice_"):
                continue
            title = _clip(self.store.title(key) or self.store.request_text(key) or "", TITLE_CHARS)
            if not title:
                continue
            title = short_title(title) or title
            if not carried and not any(near_duplicate(title, t) for t in things):
                things.append(title)   # near-duplicate jobs from one call are one thing it started
            status, settled = self.store.run_settled(key)
            row = self.store.run_row(key) or {}
            for sid in (self.store.linked_source(key), row.get("session_id")):
                if opens is None and self._session_opens(sid):
                    opens = sid
            if key in open_keys:
                asking.append(title)
            elif status == "completed" and settled and call.started_at - 1 <= settled <= call.ended_at + 5:
                answered.append(title)
                result = self.store.work(idem_key=key) or {}
                result = result.get("result") or {}
                said = result.get("headline") or first_sentence(result.get("spoken") or "")
                if said and not any(near_duplicate(said, h, 0.7) for h in heard):
                    heard.append(_clip(said, 120))
                    heard_text.append(" ".join(str(result.get(k) or "") for k in ("spoken", "full", "headline")))
        # Inline answers the voice gave with no task (a time, a flight, a number) leave a line too.
        for said in inline_facts(getattr(call, "history", None) or [], heard_text):
            heard.append(said)
        if opens is None and self._session_opens(getattr(call, "live_session_id", None)):
            opens = call.live_session_id
        minutes = max(1, round((call.ended_at - call.started_at) / 60))
        shown_things, more_things = collapse(things, CALL_LIST_KEEP)
        shown_answers, more_answers = collapse(heard, CALL_LIST_KEEP)
        return {"id": f"call:{call.interaction_id}", "interaction_id": call.interaction_id,
                "title": f"Your call \u00b7 {minutes} min", "started_at": round(call.started_at, 3),
                "ended_at": round(call.ended_at, 3), "minutes": minutes,
                # Only a conversation the apps can open (the realtime session id is not one: build 68
                # showed "I couldn't find that conversation."). None: the card opens nothing.
                "session_id": opens, "run_id": None,
                # Build 68 draws "Started " + things; a title glued onto "Started" reads wrong, so each
                # thing leads with a (zero-width-spaced) colon: "Started: Check the flight time."
                # A few rows, then "N more": a 10-job call must not grow the card past the screen.
                "things": [THING_LEAD + t for t in shown_things],
                "more_things": more_things, "decisions": decisions[:3],
                "answers": shown_answers, "more_answers": more_answers,
                "line": call_line(things, answered, asking, decisions, heard)}

    _last_call_id: str | None = None

    def _opened(self, key: str) -> bool:
        """Opened in an app (POST /voice/tasks/{run}/seen), as opposed to heard on a call."""
        return self._meta_float(f"now_opened:{key}") > 0

    def _session_opens(self, sid: str | None) -> bool:
        """A conversation the apps can open: it's in the Hermes state DB."""
        if not sid:
            return False
        try:
            from . import history as H
            return H.session_meta(self.svc.rt.state_db, sid) is not None
        except Exception:
            return False

    def _raw_cards(self, run_id: str | None) -> list[Any] | None:
        """The stored cards (with their file paths, which the public result leaves out)."""
        try:
            return self.store.result_cards(run_id) if run_id else None
        except Exception:
            return None

    def _title(self, work: dict[str, Any]) -> str:
        job = work.get("job") or {}
        name = job.get("name") or work.get("title") or "A task"
        full = None
        if job.get("id") and job.get("name"):
            try:
                full = self.store.job_full_title(job["id"])
            except Exception:
                full = None
        return _clip(full_title(name, full) or name, TITLE_CHARS)

    def _named(self, item: dict[str, Any]) -> dict[str, Any]:
        """Home's card title is a short name (home_words.short_title); the whole one stays as
        ``full_title`` for the job's own page."""
        whole = item.get("title")
        if not isinstance(whole, str) or not whole:
            return item
        head, sep, more = whole.rpartition(" \u00b7 ")
        group = re.fullmatch(r"(.+?)((?: and)? \d+ more)?", whole)
        if item.get("kind") == STOPPED and str(item.get("id") or "").startswith("g:") and group:
            # A folded group names its jobs: "<job> · <job> and 1 more"; each name is shortened.
            names = [short_title(n) or n for n in group.group(1).split(" \u00b7 ")]
            short = " \u00b7 ".join(names) + (group.group(2) or "")
        elif sep and re.fullmatch(r"\d+ more", more):      # "<first job> · 2 more"
            short = f"{short_title(head) or head}{sep}{more}"
        else:
            short = short_title(whole)
        if short and short != whole:
            item["full_title"] = whole
            item["title"] = short
        return item

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
        title = self._title(work)
        session = row.get("linked_source") or work.get("session_id") or row.get("session_id")
        finished = float(row["settled_at"]) if row.get("settled_at") else None
        started = float(row["created"] or work.get("started_at") or 0) or None
        seen = bool(row["seen"]) or bool(work.get("seen")) or (
            read_at is not None and finished is not None and read_at + 1 >= finished) or (
            finished is not None and finished < getattr(self, "_baseline", 0) - BASELINE_GRACE_S) or delivered_elsewhere(row)
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
            line = live_line(work.get("now_line") or work.get("short_status"), work.get("short_status"))
            item.update(kind=WORKING, line=_clip(line, LINE_CHARS))
            if isinstance(work.get("quiet_s"), (int, float)) and work["quiet_s"] >= 120:
                item["idle_seconds"] = int(work["quiet_s"])
        elif status == RS.RunState.WAITING_ON_TASK:
            behind = (work.get("waiting") or {}).get("name") or ""
            item.update(kind=WAITING, line=_clip(work.get("detail") or (
                f"Starts when \u201c{behind}\u201d finishes." if behind else "Waiting for another task."), LINE_CHARS))
            item["waiting_on"] = _clip(behind, 60) if behind else "another task"
        elif status in {RS.RunState.FAILED, RS.RunState.INTERRUPTED}:
            # Clear is an answers-only cutoff: unread stopped work is not an answer, so only the
            # rollover (not Clear) moves it to Earlier.
            if self._in_chat(run_id):
                if (finished or now) < cleared:
                    return None
            elif seen or (finished or now) < edge:
                return None  # opened: it lives in its conversation (Earlier) from here
            kind = row.get("failure") or (UNKNOWN if status == RS.RunState.FAILED else None)
            fix = "retry" if status == RS.RunState.INTERRUPTED else FIX.get(kind or "", "open")
            reason = work.get("failure_reason") or failure_reason(status, row.get("failure"), row.get("failure_hint"))
            item.update(kind=STOPPED, line=_clip(stopped_line(title, reason, fix), 240), fix={"kind": fix})
            # What it stopped on, for folding several stopped jobs with one cause into one card.
            item["_cause"] = stopped_cause(status, row.get("failure"), row.get("failure_hint"), reason)
        elif status == RS.RunState.COMPLETED:
            line = _clip(result.get("headline") or work.get("summary") or result.get("spoken") or "Done.", 240)
            if question and row.get("question_closed") == "dismissed":
                if (finished or 0) < max(edge, cleared):
                    return None
                item.update(kind=WAITING, line="Paused. Its question is in the conversation.",
                            dismissed_question=True)
            else:
                # Home is what's live or new: an answer leaves once it's opened, at the 4 AM rollover
                # after it finished (read or not), 12 h after it finished, or on Clear. A seen one
                # stays only while the call it belongs to is on.
                on_this_call = call_info is not None and row["key"] in call_info.get("keys", {})
                at = finished or now
                # Heard on a call is read, not opened: a call's answers stay (seen styling) under the
                # call card until the 12 h / 4 AM rule; opening it in an app is what lets it leave early.
                left = seen
                if seen and from_a_call(row):
                    left = self._opened(row["key"]) or (read_at is not None and finished is not None
                                                        and read_at + 1 >= finished)
                if self._in_chat(run_id):
                    # Started from Home's chat: its result is a message in that thread, so it stays
                    # while the thread does (seen or not), not by the leave rules for answers.
                    if at < cleared or now - at > CHAT_KEPT_S:
                        return None
                    left, on_this_call = False, True
                # Clear is explicit: it moves the call's answers too. Hearing one only keeps it under
                # the call card when Clear was not used.
                if at < cleared or (not on_this_call and (left or at < edge or now - at > ANSWER_KEPT_S)):
                    return None
                if from_a_call(row) and self._last_call_id == row.get("interaction_id"):
                    item["call_id"] = f"call:{row['interaction_id']}"
                item.update(kind=SEEN_ANSWER if seen else NEW_ANSWER, line=line, unread=not seen)
        else:
            return None  # cancelled, rejected: nothing to look at
        item.setdefault("id", f"s:{session}" if session else f"t:{run_id or row['key']}")
        item.setdefault("unread", False)
        if self._in_chat(run_id):
            item["in_chat"] = True   # Home's chat started it: it lives in the thread beside its messages
        came = origin(finished, now, self._zone) if finished is not None and item["kind"] in {
            NEW_ANSWER, SEEN_ANSWER, STOPPED} else None
        if came is not None:
            item["provenance"] = came
        elif call is not None and row.get("interaction_id") != call.interaction_id and item["kind"] in {
                NEW_ANSWER, QUESTION, STOPPED} and (finished or 0) >= 0:
            started_call = getattr(call, "started_at", None)
            if started_call and (started or 0) < started_call:
                item["provenance"] = {"kind": "earlier_call", "at": started}
        task_id = (call_info or {}).get("keys", {}).get(row["key"])
        item["on_current_call"] = bool(call is not None and (
            row.get("interaction_id") == call.interaction_id or task_id is not None))
        if task_id is not None:
            if item["kind"] in {NEW_ANSWER, SEEN_ANSWER} and task_id in call_info["spoken"]:
                # Told on the call: already read; the card keeps its own answer line.
                item.update(kind=SEEN_ANSWER, unread=False, on_call="heard")
            elif item["kind"] == QUESTION and (task_id == call_info["talking"] or float(
                    (item.get("question") or {}).get("asked_at") or 0) >= float(getattr(call, "started_at", 0) or 0)):
                # Asked on this call: tier 0, the one card that moves during a call.
                options = (item.get("question") or {}).get("options") or []
                risky = any(o.get("risk") == "high" for o in options)
                item.update(on_call="asking", say_ok=not risky, asking_label="Asking now",
                            **({"lock_line": "Can't be undone \u00b7 Needs a tap"} if risky else {}))
            item["_task_id"] = task_id
        thumbs = _thumbs(result, run_id, self._raw_cards(run_id) if (result.get("cards")) else None)
        if thumbs:
            item["thumbs"] = thumbs
        # For "replaced" questions and one card per thing a call asked (build()).
        if from_a_call(row):
            item["_call"] = row["interaction_id"]
        if item["kind"] == QUESTION and approval is None and status not in RS.ASKING:
            item["_asked"] = float((item.get("question") or {}).get("asked_at") or finished or now)
            item["_topic"] = (item.get("question") or {}).get("prompt") or title
        return item

    # -- the whole list --------------------------------------------------------------------------
    @staticmethod
    def _threads_by_id(items: list[dict[str, Any]]) -> dict[str, Any]:
        """The conversation each card's job runs in: a newer question there replaces an older one."""
        return {i["id"]: i.get("session_id") for i in items}

    def _in_chat(self, run_id: Any) -> bool:
        return bool(run_id) and str(run_id) in getattr(self, "_chat_runs", set())

    def build(self, tz: str | None = None, *, peek: bool = False, every: bool = False) -> dict[str, Any]:
        now = self.clock()
        zone_ = zone(tz)
        edge = rollover(now, zone_)
        cleared = self._meta_float("now_cleared_at")
        self._zone = zone_
        chat = getattr(self.svc, "home_chat", None)
        try:
            self._chat_runs = chat.started_runs() if chat is not None else set()
            self._chat_runs |= self._chat_children(self._chat_runs)
        except Exception:
            self._chat_runs = set()
        self._baseline = self._meta_float("now_baseline_at")
        if not self._baseline:
            # First use of Home (or of this rule, after an upgrade): older answers count as seen.
            self._baseline = now
            self.store.set_meta("now_baseline_at", repr(now))
        svc = self.svc
        marks = self.store.session_marks()
        call = self.live_call()
        call_info = self.call_state(call, now) if call is not None else None
        ended = self.last_call(now, edge, cleared) if call is None else None
        self._last_call_id = getattr(ended, "interaction_id", None)
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
        by_thread: dict[str, dict[str, Any]] = {}
        items: list[dict[str, Any]] = []
        answered_keys: set[str] = set()
        gone = superseded(rows)
        threads = thread_keys(rows)
        live_interaction = getattr(call, "interaction_id", None) if call is not None else None
        for row in rows:  # newest first: a conversation's newest task speaks for it
            if row["key"] in gone:
                continue  # stopped, then retried (or followed up) in the same conversation: the retry speaks
            place = row.get("linked_source") or row.get("session_id") or row["key"]
            work = self.store.work(idem_key=row["key"], assistant_name=name) or {}
            read_at = (marks.get(place) or {}).get("read_at")
            item = self._task_item(row, work, row["key"] in open_keys, approvals, read_at, edge, cleared, now, call,
                                   call_info)
            if item is None:
                continue
            item["_thread"] = threads.get(row["key"]) or place
            item["_run_key"] = row["key"]
            if row.get("answered"):
                answered_keys.add(row["key"])
            held = by_place.get(place)
            if held is not None:
                # One card per conversation, except a question, which keeps its own card.
                if item["kind"] == QUESTION and held["kind"] != QUESTION:
                    items.append(item)
                elif item["kind"] in ANSWERS and held["kind"] in ANSWERS:
                    held["folded"] = int(held.get("folded") or 0) + 1
                continue
            if item["kind"] in ANSWERS and row.get("interaction_id") != live_interaction:
                # Repeated answers in one conversation (a call's follow-ups, a retry's chain) fold into
                # the newest; the live call keeps one card per answer.
                newer = by_thread.get(item["_thread"])
                if newer is not None:
                    newer["folded"] = int(newer.get("folded") or 0) + 1
                    continue
                by_thread[item["_thread"]] = item
            by_place[place] = item
            items.append(item)
        # Hermes conversations Speakeasy didn't start: running ones, and replies nobody has read yet.
        background: list[dict[str, Any]] = []
        mine = {i["session_id"] for i in items if i.get("session_id")}
        # A task's own Hermes session, and the conversation that started it, are that task's card:
        # never a second card of their own (a conversation and the task it spawned are one item).
        mine |= {r["session_id"] for r in rows if r.get("session_id")}
        live_states = {str(getattr(x, "value", x)) for x in (*RS.UNDER_WAY, *RS.ASKING)}
        mine |= {r["linked_source"] for r in rows if r.get("linked_source") and str(r.get("status")) in live_states}
        spawned = {i["session_id"]: i for i in items if i.get("session_id") and i.get("run_id")}
        try:
            page = svc.history({"kinds": "conversations,scheduled,kanban", "limit": "40"}, for_now=True)
        except Exception:
            page = {"sessions": []}
        lines_left = LIVE_LINES
        for s in page.get("sessions") or []:
            sid = s["id"]
            if sid in spawned and s.get("source") != "speakeasy":
                # The task's origin line names the conversation it came from.
                task = spawned[sid]
                where = s.get("source_label") or ""
                origin = _clip(s.get("title"), TITLE_CHARS) if s.get("title") else None
                if origin and origin != task.get("title"):
                    task["source_label"] = f"{where} \u00b7 {origin}" if where else origin
                    task["origin"] = {"session_id": sid, "title": origin, "label": where or None}
            if sid in mine or s.get("source") == "speakeasy":
                continue
            if s.get("automated"):
                if s.get("state") == "running":
                    line = self._session_line(sid) if lines_left > 0 else None
                    lines_left -= 1
                    background.append({"session_id": sid, "title": short_title(_clip(s.get("title"), TITLE_CHARS)),
                                       "line": _clip(live_line(line), LINE_CHARS),
                                       "source": s.get("source"), "source_label": s.get("source_label"),
                                       "started_at": s.get("last_activity")})
                continue
            base = {"id": f"s:{sid}", "session_id": sid, "title": _clip(s.get("title"), TITLE_CHARS),
                    "source": s.get("source"), "source_label": s.get("source_label"),
                    "updated_at": s.get("last_activity"), "started_at": s.get("last_activity"), "unread": False}
            if s.get("state") == "running":
                line = self._session_line(sid) if lines_left > 0 else None
                lines_left -= 1
                items.append({**base, "kind": WORKING, "line": _clip(live_line(line), LINE_CHARS)})
            elif s.get("unread") and str(s.get("source") or "").lower() not in OTHER_SURFACES and float(
                    s.get("last_activity") or 0) >= max(
                    edge, cleared, self._baseline - BASELINE_GRACE_S, now - ANSWER_KEPT_S):
                items.append({**base, "kind": NEW_ANSWER, "line": _clip(s.get("preview"), 240), "unread": True,
                              "finished_at": s.get("last_activity")})
        items = group_stopped(items)
        items = self._apply_later(items, now)
        items = self._apply_dismissed(items, now)
        items = mark_replaced(items, self._threads_by_id(items))
        everything = list(items)  # what find() searches: also what folds into "Earlier today"
        items = fold_call_duplicates(items)
        for i in items:
            self._named(i)
        items, earlier_today = fold_earlier(items, now)
        items = order(items, self._first_seen(items, now))
        # On a call: the question the voice is asking right now goes first (tier 0).
        items.sort(key=lambda i: 0 if i.get("on_call") == "asking" else 1)
        total = len(items)
        items = items[:MAX_ITEMS]
        stream = self._stream(items, answered_keys, now, zone_)
        focus = None
        for i in items:
            i["tier"] = 0 if i.get("on_call") == "asking" else TIER[i["kind"]]
            if call_info and call_info["talking"] and i.pop("_task_id", None) == call_info["talking"]:
                focus = focus or i["id"]
            i.pop("_task_id", None)
            i.pop("_thread", None)
            i.pop("_cause", None)
            i.pop("_run_key", None)
            for k in ("_call", "_asked", "_topic"):
                i.pop(k, None)
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
            "call_summary": self._undismissed_call(
                self._latered_call(self.call_summary(ended, now) if ended is not None else None, now)),
            "items": items, "background": {"running": len(background), "items": background[:BACKGROUND_ITEMS]},
            # "Earlier today · N": answers more than 4 h old, one row instead of N cards. Outside
            # ``items`` so a build 68 app (which knows no such row) simply doesn't show it.
            "earlier": {"today_more": max(0, total - len(items)), "today": earlier_today},
            "stream": stream,
            # The tracker pill's words at any volume ("3 need you · 5 running"): counts, never a cut title.
            "pill": pill_text(counts["needs_you"], counts["working"] + counts["on_its_own_running"],
                              one_title=next((i["title"] for i in items if i["kind"] == QUESTION), None)),
        }
        body["rev"] = self._rev(body)
        body["as_of"] = round(now, 3)
        if every:
            body["_everything"] = everything  # find() only; never sent
        body["notice"] = None if peek else self._notice(rows, marks, edge, cleared)
        self._last_sessions = {i["session_id"] for i in items if i.get("session_id")}
        return body

    def _stream(self, items: list[dict[str, Any]], answered: set[str], now: float, zone_: dt.tzinfo) -> dict[str, Any]:
        """The same cards as one thread: each ask, then its card, in time order (stream.py)."""
        from .stream import build_stream
        asks: dict[str, str] = {}
        try:
            assistant = self.svc.settings.get().get("assistant_name")
        except Exception:
            assistant = None
        for i in items:
            key = i.get("_run_key")
            if key:
                try:
                    text = self.store.request_text(key)
                except Exception:
                    text = None
                if text:
                    asks[i["id"]] = clean_ask(text, assistant) or text
        marked = {i["id"] for i in items if i.get("_run_key") in answered}
        chat = getattr(self.svc, "home_chat", None)
        messages: list[dict[str, Any]] = []
        from_chat: set[str] = set()
        if chat is not None:
            try:
                messages = [chat.entry(r) for r in chat.shown(now)]
                from_chat = chat.started_runs()
                from_chat |= self._chat_descendants(items, from_chat)
            except Exception:
                logging.getLogger(__name__).debug("speakeasy: Home's chat messages failed", exc_info=True)
        return build_stream(items, asks, marked, now, zone_, messages=messages, from_chat=from_chat)

    def _chat_children(self, runs: set[str]) -> set[str]:
        """Runs that continue a chat-born job (answering its question, a follow-up): they belong to
        the chat too, so their result stays in the thread where the job's card was."""
        if not runs:
            return set()
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT idem_key, run_id, parent FROM runs WHERE parent IS NOT NULL AND run_id IS NOT NULL").fetchall()
            keys = dict(self.store._db.execute(
                "SELECT idem_key, run_id FROM runs WHERE run_id IS NOT NULL").fetchall())
        out: set[str] = set()
        for _ in range(8):
            grew = False
            for key, run, parent in rows:
                if run not in runs and run not in out and keys.get(parent) in runs | out:
                    out.add(run)
                    grew = True
            if not grew:
                break
        return out

    def _chat_descendants(self, items: list[dict[str, Any]], from_chat: set[str]) -> set[str]:
        """Runs that continue a chat-born job (answering its question, a follow-up it spawned): their
        card belongs to the chat too, so no "you asked" bubble repeats the job's internal request."""
        if not from_chat:
            return set()
        with self.store._lock:
            rows = self.store._db.execute("SELECT idem_key, run_id FROM runs WHERE run_id IS NOT NULL").fetchall()
        run_of = {k: r for k, r in rows}
        out: set[str] = set()
        for item in items:
            key, run = item.get("_run_key"), item.get("run_id")
            if not key or not run or run in from_chat:
                continue
            seen: set[str] = set()
            while key and key not in seen and len(seen) < 8:
                seen.add(key)
                key = self.store.parent_of(key)
                if key and run_of.get(key) in from_chat:
                    out.add(str(run))
                    break
        return out

    def recent(self, tz: str | None = None, limit: int = 30) -> dict[str, Any]:
        """``GET /voice/now/recent``: the last tasks, finished or not, whatever Home's leave rules did to
        them, so \"Recent\" shows previous work without leaving Home. Newest first."""
        now = self.clock()
        name = self.svc.settings.get()["assistant_name"]
        out: list[dict[str, Any]] = []
        for row in self.store.now_runs(since=now - RECENT_KEPT_S, limit=limit * 2):
            work = self.store.work(idem_key=row["key"], assistant_name=name) or {}
            status = str(getattr(work.get("status") or row["status"], "value", work.get("status") or row["status"]))
            at = float(row.get("settled_at") or row.get("updated") or row.get("created") or 0)
            result = work.get("result") or {}
            out.append({"id": f"t:{work.get('run_id') or row['run_id'] or row['key']}", "title": self._title(work),
                        "state": status, "at": at, "session_id": row.get("linked_source") or row.get("session_id"),
                        "line": _clip(result.get("headline") or work.get("summary") or "", 140) or None,
                        "dismissed": bool(row.get("dismissed")),
                        "origin": origin(at, now, zone(tz))})
            if len(out) >= limit:
                break
        return {"items": out, "as_of": round(now, 3)}

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

    # -- Dismiss -------------------------------------------------------------------------------
    @staticmethod
    def dismiss_signature(item: dict[str, Any]) -> str:
        """What a dismissed card must change for it to come back: a new answer, a new question, a
        new run (a working card's line changing doesn't count).

        A call recap has no kind, finished_at, run_id or question, so its own material is what it
        comes back on: what the call did, what it answered, what it was asked, and what it still
        asks. That is how an answer landing a few seconds after hang-up brings the card back
        (SPK-221); with the usual four fields it was the same four nulls forever."""
        if "kind" not in item and item.get("interaction_id"):
            material: list[Any] = [item.get("things"), item.get("answers"), item.get("decisions"),
                                   item.get("line"), item.get("more_things")]
        else:
            material = [item.get("kind"), item.get("finished_at"), item.get("run_id"),
                        (item.get("question") or {}).get("prompt")]
        return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:16]

    def dismissed(self) -> dict[str, dict[str, Any]]:
        try:
            marks = json.loads(self.store.get_meta("now_dismissed") or "{}")
        except ValueError:
            marks = {}
        return marks if isinstance(marks, dict) else {}

    def set_dismissed(self, marks: dict[str, dict[str, Any]]) -> None:
        self.store.set_meta("now_dismissed", json.dumps(marks, sort_keys=True))

    def _apply_dismissed(self, items: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
        marks = self.dismissed()
        if not marks:
            return items
        out = [i for i in items if (marks.get(i["id"]) or {}).get("sig") != self.dismiss_signature(i)]
        kept = {k: v for k, v in marks.items() if now - float(v.get("at") or 0) <= DISMISS_KEPT_S}
        if kept != marks:
            self.set_dismissed(kept)
        return out

    def find(self, item_id: str, tz: str | None = None) -> dict[str, Any] | None:
        """An item as Home shows it (or would, were it not put off)."""
        later = self.store.later_items().get(item_id)
        if later is not None:
            self.store.clear_later(item_id)
        try:
            body = self.build(tz, peek=True, every=True)
        finally:
            if later is not None:
                self.store.set_later(item_id, later["until"], later["on_change"], later["seen_sig"])
        # Every id /voice/now hands out: the cards, the call card, and the answers folded into the
        # "Earlier today · N" row (build 69 swipes those too). Not only body["items"].
        pool = [*body["items"], *body.get("_everything", [])]
        if body.get("call_summary"):
            pool.append(body["call_summary"])
        return next((i for i in pool if i.get("id") == item_id), None)

    def _undismissed_call(self, card: dict[str, Any] | None) -> dict[str, Any] | None:
        """The call card, unless it was swiped away (and hasn't changed since)."""
        if not card:
            return card
        mark = self.dismissed().get(card["id"]) or {}
        return None if mark.get("sig") == self.dismiss_signature(card) else card

    def _latered_call(self, card: dict[str, Any] | None, now: float) -> dict[str, Any] | None:
        """The call card takes Later like any other card (SPK-221): off Home until its time or until
        it changes, waking with the same "put off" provenance, and back at once when undone."""
        if not card:
            return card
        kept = self._apply_later([card], now)
        return kept[0] if kept else None

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


# -- stopped, superseded, folded -------------------------------------------------------------------

ANSWERS = (NEW_ANSWER, SEEN_ANSWER)
_LIVE_STATES = None


def _state(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _alive() -> set[str]:
    global _LIVE_STATES
    if _LIVE_STATES is None:
        _LIVE_STATES = {_state(x) for x in (*RS.UNDER_WAY, *RS.ASKING, RS.RunState.WAITING_ON_TASK)}
    return _LIVE_STATES


def _reason_clause(reason: str) -> str:
    """"The disk was full." -> "the disk was full" (names like Speakeasy keep their capital)."""
    text = reason.strip().rstrip(".").strip()
    return text[0].lower() + text[1:] if _LOWER_LEAD.match(text) else text


def stopped_line(title: str | None, reason: str | None, fix: str | None) -> str:
    """A stopped card's line: what stopped, why, in plain words, and the one next step.
    "Photo archive move stopped: the disk was full. Retry or dismiss." Never a bare "It stopped."."""
    what = _clip(title or "", 60) or "A task"
    step = NEXT_STEP.get(fix or "", NEXT_STEP["open"])
    if not reason or not reason.strip() or reason.strip() == NO_REASON:
        return f"Stopped without finishing: {what}. {step}"
    return f"{what} stopped: {_reason_clause(reason)}. {step}"


def stopped_cause(status: Any, failure: str | None, hint: str | None, reason: str | None) -> str | None:
    """What a stopped task stopped on, when it's known: several with one cause fold into one card."""
    if _state(status) == "interrupted":
        return "interrupted"
    if failure and failure != UNKNOWN:
        return f"kind:{failure}"
    if hint:
        return f"hint:{hint}"
    return None if not reason or reason == NO_REASON else f"reason:{reason}"


def thread_keys(rows: list[dict[str, Any]]) -> dict[str, str]:
    """{task key: its conversation}: the chat it continues, else its own Hermes session, else its
    parent's conversation (a follow-up), else the call it came from."""
    by_key = {r["key"]: r for r in rows}
    out: dict[str, str] = {}

    def of(r: dict[str, Any], depth: int = 0) -> str:
        if r["key"] in out:
            return out[r["key"]]
        t = r.get("linked_source") or r.get("session_id")
        if not t and r.get("parent") in by_key and depth < 8:
            t = of(by_key[r["parent"]], depth + 1)
        if not t:
            t = f"call:{r['interaction_id']}" if r.get("interaction_id") else r["key"]
        out[r["key"]] = t
        return t
    for r in rows:
        of(r)
    return out


def superseded(rows: list[dict[str, Any]]) -> set[str]:
    """Stopped tasks a later run replaced: a later task in the same conversation (the chat it
    continues or its Hermes session) that finished or is still going, or a retry/follow-up of it.
    *rows* are newest first."""
    out: set[str] = set()
    for n, r in enumerate(rows):
        if _state(r.get("status")) not in {"failed", "interrupted"}:
            continue
        mine = {x for x in (r.get("linked_source"), r.get("session_id")) if x}
        when = float(r.get("created") or r.get("settled_at") or 0)
        for later in rows[:n]:
            if _state(later.get("status")) != "completed" and _state(later.get("status")) not in _alive():
                continue
            if float(later.get("created") or later.get("settled_at") or 0) < when:
                continue
            theirs = {x for x in (later.get("linked_source"), later.get("session_id")) if x}
            if (mine & theirs) or later.get("parent") == r["key"]:
                out.add(r["key"])
                break
    return out


def group_stopped(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Several stopped tasks with one cause (or from one conversation) are one card:
    "3 jobs stopped: the Studio was offline." The newest one's conversation opens from it."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for i in items:
        if i["kind"] == STOPPED:
            groups.setdefault(i.get("_cause") or f"thread:{i.get('_thread') or i['id']}", []).append(i)
    out: list[dict[str, Any]] = []
    done: set[int] = set()
    for i in items:
        if i["kind"] != STOPPED:
            out.append(i)
            continue
        if id(i) in done:
            continue
        group = groups[i.get("_cause") or f"thread:{i.get('_thread') or i['id']}"]
        done |= {id(g) for g in group}
        if len(group) == 1:
            out.append(i)
            continue
        group = sorted(group, key=lambda g: -float(g.get("finished_at") or g.get("updated_at") or 0))
        head = group[0]
        n = len(group)
        line = head.get("line") or ""
        m = re.match(r"^(?:.*?) stopped: (.*)$", line)
        why_and_step = m.group(1) if m else "they didn't finish. Open one to retry, or dismiss."
        for fix in ("retry", "open"):
            why_and_step = why_and_step.replace(NEXT_STEP[fix], "Open one to retry, or dismiss.")
        for one, many in (("this one", "them"), ("this request", "these requests"), ("while it was running", "while they were running"),
                          ("lost track of it", "lost track of them")):
            why_and_step = why_and_step.replace(one, many)
        names = [g.get("title") or "A task" for g in group]
        title = " \u00b7 ".join(names[:2]) + (f" and {n - 2} more" if n > 2 else "")
        key = hashlib.sha256(json.dumps(sorted(str(g.get("run_id") or g["id"]) for g in group)).encode()).hexdigest()[:12]
        merged = {**head, "id": f"g:stopped:{key}", "title": _clip(title, TITLE_CHARS),
                  "line": _clip(f"{n} jobs stopped: {why_and_step}", 240),
                  "fix": {"kind": "open"}, "stopped_count": n,
                  "group": [{"run_id": g.get("run_id"), "session_id": g.get("session_id"), "title": g.get("title")}
                            for g in group]}
        merged.pop("thumbs", None)
        out.append(merged)
    return out


REPLACED_LINE = "Replaced by a newer question."


def mark_replaced(items: list[dict[str, Any]], threads: dict[str, Any]) -> list[dict[str, Any]]:
    """A question nobody answered that a newer one took over (same conversation, or the same call
    asking about the same thing again) is no longer waiting on the user: it keeps its card, in a
    "replaced" state that names when, and leaves the needs-you count."""
    qs = []
    for i in items:
        if "_asked" not in i:
            continue
        qs.append({"id": i["id"], "thread": threads.get(i["id"]), "asked_at": i["_asked"], "answered": False,
                   "call": i.get("_call"), "topic": i.get("_topic") or i.get("title")})
    gone = replaced_questions(qs)
    # The same call asking about the same thing again replaces the earlier ask too.
    by_call: dict[Any, list[dict[str, Any]]] = {}
    for q in qs:
        if q["call"]:
            by_call.setdefault(q["call"], []).append(q)
    for group in by_call.values():
        group.sort(key=lambda q: q["asked_at"])
        for n, older in enumerate(group):
            if older["id"] in gone:
                continue
            newer = next((q for q in group[n + 1:] if q["asked_at"] > older["asked_at"]
                          and near_duplicate(older["topic"], q["topic"], 0.5)), None)
            if newer is not None:
                gone[older["id"]] = {"by": newer["id"], "at": newer["asked_at"]}
    if not gone:
        return items
    for i in items:
        mark = gone.get(i["id"])
        if mark is None:
            continue
        i["replaced"] = {"by": mark["by"], "at": mark["at"]}
        if i["kind"] == QUESTION:
            i.update(kind=WAITING, line=REPLACED_LINE, unread=False)
            if isinstance(i.get("question"), dict):
                i["question"] = {**i["question"], "replaced": {"at": mark["at"], "by": mark["by"]}}
    return items


def fold_call_duplicates(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One call, one card per thing it asked for: near-duplicate jobs from the same call (\"Fix
    picture sending and carousels\" twice, worded apart) fold into the newest live one, which
    counts the rest as ``also``. Questions and stopped work keep their own cards."""
    foldable = {WORKING, NEW_ANSWER, SEEN_ANSWER}
    for i in items:
        if i.get("_call") and i["kind"] in foldable:
            # Work the live call brought in or is talking about stays its own card.
            i["_fold"] = (i["_call"], i["kind"] == WORKING, bool(i.get("on_current_call")), i.get("on_call"))
    out = fold_duplicates(items, key="title", group="_fold")
    for i in items:
        i.pop("_fold", None)
    return out


def fold_earlier(items: list[dict[str, Any]], now: float,
                 keep_s: float = ANSWER_CARD_S) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Answers more than 4 h old leave Home's cards for one "Earlier today · N" row. An answer on
    the live call keeps its card."""
    keep, old = [], []
    for i in items:
        at = i.get("finished_at")
        if i["kind"] in ANSWERS and not i.get("on_call") and not i.get("call_id") and not i.get("in_chat") \
                and at is not None \
                and now - float(at) > keep_s:
            old.append(i)
        else:
            keep.append(i)
    if not old:
        return keep, None
    old.sort(key=lambda i: -float(i.get("finished_at") or 0))
    n = len(old) + sum(int(i.get("folded") or 0) for i in old)
    return keep, {"count": n, "label": f"Earlier today \u00b7 {n}",
                  "unread": sum(1 for i in old if i.get("unread")),
                  "items": [{"id": i["id"], "session_id": i.get("session_id"), "run_id": i.get("run_id"),
                             "title": i.get("title"), "finished_at": i.get("finished_at"),
                             "unread": bool(i.get("unread"))} for i in old[:EARLIER_TODAY_ITEMS]]}


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


# Home's live line is plain words, never Hermes' raw tool text ("Running sleep 240 + 3 commands").
_RAW_BITS = re.compile(r"[`$|<>&;={}\[\]\\]|(?<!\w)/\w|\+ ?\d+ (?:more )?(?:commands?|calls?|tools?)\b|(?:^|\s)--?[A-Za-z]"
                       r"|\b[a-z]+_[a-z_]+\b|\w\(|\b(?:sudo|bash|zsh|curl|ssh|pkill|xcodebuild|pytest|npm|pip3?)\b")
_PLAIN_AFTER_RUNNING = frozenset({"the", "a", "an", "your", "some", "tests", "test", "checks", "check", "it", "them",
                                  "this", "that", "these", "those", "into", "through", "late", "now", "again", "low",
                                  "out", "one", "two", "three", "every", "each", "all", "more", "its", "their"})


def plain_line(line: str | None, fallback: str | None = None) -> str:
    """A live line fit for Home: the line itself when it's plain words, else the task's last plain
    status (*fallback*), else nothing: a running card says only real status, never a stock
    "Working…" placeholder."""
    def ok(text: str | None) -> bool:
        if not text or not text.strip() or _RAW_BITS.search(text):
            return False
        m = re.match(r"\s*Running\s+(\S+)", text)
        return not m or m.group(1).lower().strip(",.") in _PLAIN_AFTER_RUNNING
    if ok(line):
        return line.strip()
    if ok(fallback):
        return fallback.strip()
    return ""


# Placeholders that say nothing about the work (history.session_live's floor, old stock lines).
_STOCK_LINES = frozenset({"working", "working on it", "running a command", "thinking", "on it"})


_URL_PATH = re.compile(r"(?i)\b((?:[a-z0-9-]+\.)+[a-z]{2,})/\S*")


def live_line(line: str | None, fallback: str | None = None) -> str:
    """A running card's one short present-tense line (home_words.status_line): the newest real
    step, else the task's last plain status, else nothing (the card shows only its title until the
    job reports a real step). Never tool narration, never a three-clause worker report, never a
    stock "Working…"."""
    for text in (line, fallback):
        if text and text.strip().rstrip(".\u2026").lower() in _STOCK_LINES:
            continue
        if text:
            # "Reading api.example.com/v1/forecast" -> "Reading api.example.com": the site, not its path.
            text = _URL_PATH.sub(r"\1", text)
        if text and plain_line(text):
            short = status_line(text)
            if short:
                return short
    return ""


THING_LEAD = "\u200b: "


def from_a_call(row: dict[str, Any]) -> bool:
    """A task a voice call started (typed chats and Hermes' own work aren't calls)."""
    iid = str(row.get("interaction_id") or "")
    return iid.startswith("vi_") and not iid.startswith("vi_typed")
_FACT_RE = re.compile(r"\d")
_FILLER_RE = re.compile(r"^(?:pulling that up(?: now)?|checking(?: that)?(?: now)?|one sec(?:ond)?|let me (?:check|look)"
                        r"|okay|ok|right|sure|got it)[.,!\s]*", re.I)


def first_sentence(text: str) -> str:
    text = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    return (m.group(1) if m else text).strip()


def inline_facts(history: list[Any], task_text: list[str]) -> list[str]:
    """Facts the voice said with no task behind them: an assistant turn with a number in it whose
    numbers no task's answer already holds. One sentence each, filler stripped."""
    pool = " ".join(task_text)
    have = set(re.findall(r"\d+", pool))
    out: list[str] = []
    for turn in history[-60:]:
        role = turn.get("role") if isinstance(turn, dict) else None
        text = str((turn.get("text") or turn.get("content") or "") if isinstance(turn, dict) else "")
        if role != "assistant" or not _FACT_RE.search(text):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
            sentence = _FILLER_RE.sub("", sentence).strip()
            nums = set(re.findall(r"\d+", sentence))
            if nums and not nums <= have and not sentence.endswith("?"):
                out.append(_clip(sentence, 120))
                have |= nums
    return out[:3]


def call_line(things: list[str], answered: list[str], asking: list[str], decisions: list[str],
              heard: list[str] | None = None) -> str:
    """The "Your call" card's line: what the call started, what got answered, what's asking you,
    and what you said. "Short call, nothing started." when it did none of that."""
    def join(xs: list[str]) -> str:
        xs = xs[:3]
        return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]
    started = [t for t in things if t not in answered and t not in asking]
    parts = []
    if heard:
        # A real sentence from the result: "Lands Fri 6:40 PM at Pier 11." Never "Started <title>".
        parts.append(" ".join(h if h.endswith((".", "!", "?")) else h + "." for h in heard[:2]))
    if started:
        parts.append(f"Started: {join(started)}.")
    if answered and not heard:
        parts.append(f"Answered: {join(answered)}.")
    if asking:
        parts.append(f"{join(asking)} {'has a question' if len(asking) == 1 else 'have questions'} for you.")
    if decisions:
        parts.append(f"You said: {'; '.join(decisions[:2])}.")
    return _cap(" ".join(parts)) if parts else "Short call, nothing started."


# Chat surfaces an answer is delivered to (a Discord thread, a Telegram chat): read there, not new here.
OTHER_SURFACES = frozenset({"discord", "telegram", "slack", "whatsapp", "signal", "imessage", "bluebubbles", "sms",
                            "email", "matrix", "mattermost", "feishu", "dingtalk", "wecom", "weixin", "homeassistant"})


def delivered_elsewhere(row: dict[str, Any]) -> bool:
    """A task whose answer went to another surface (``continued.platform``) counts as read."""
    raw = row.get("continued")
    try:
        cont = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return False
    return isinstance(cont, dict) and str(cont.get("platform") or "").lower() in OTHER_SURFACES


def _image_there(card: dict[str, Any]) -> bool:
    """A picture Home can actually show: its file is still on disk, or it has a web address."""
    path = card.get("path")
    if isinstance(path, str):
        return os.path.isfile(os.path.expanduser(path))
    url = card.get("image_url")
    return isinstance(url, str) and url.startswith(("https://", "http://"))


def _thumbs(result: dict[str, Any], run_id: str | None, raw: list[Any] | None = None) -> list[dict[str, Any]]:
    """Up to six pictures in answer order that can still be shown (never file chips, never a
    picture whose file is gone: the app would draw an empty tile)."""
    cards = result.get("cards") if isinstance(result, dict) else None
    if not run_id or not isinstance(cards, list):
        return []
    raw = raw if isinstance(raw, list) else cards
    nums = [n + 1 for n, c in enumerate(cards[:8]) if isinstance(c, dict) and c.get("kind") == "image"
            and n < len(raw) and isinstance(raw[n], dict) and _image_there(raw[n])]
    out = []
    for n in nums[:6]:
        card = raw[n - 1]
        thumb: dict[str, Any] = {"url": f"/voice/card-image/{run_id}/{n}"}
        caption = picture_caption(card.get("caption"), card.get("title"), card.get("name"), card.get("alt"))
        if caption:
            thumb["caption"] = caption     # words a person wrote, never the file's name
        size = _picture_size(card.get("path"))
        if size:
            thumb["aspect"] = round(size[0] / size[1], 3)
            if hero_box(*size):
                # A wide contact sheet: the readable portrait crop a tile or thumbnail shows.
                thumb["hero"] = f"/voice/card-image/{run_id}/{n}?v=hero"
        out.append(thumb)
    return out


def _picture_size(path: Any) -> tuple[int, int] | None:
    """(width, height) from the file's header only, or None."""
    if not isinstance(path, str):
        return None
    try:
        from PIL import Image
        with Image.open(os.path.expanduser(path)) as img:
            w, h = img.size
        return (w, h) if w > 0 and h > 0 else None
    except Exception:
        return None


def option_text(item: dict[str, Any], option_id: str) -> str | None:
    for opt in (item.get("question") or {}).get("options") or []:
        if opt.get("id") == option_id:
            return opt.get("text")
    return None


def ids(items: Iterable[dict[str, Any]]) -> list[str]:
    return [i["id"] for i in items]
