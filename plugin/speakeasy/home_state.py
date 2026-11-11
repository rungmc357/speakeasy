"""Build 73 Home state (North Star v3, "Home is a dispatch zone"): what Home shows besides the
transcript, computed on the plugin so every app agrees.

1. Current exchange: the latest message you sent, Todd's reply to it, and the status line (card) of
   the job it started. Sending a new message makes the previous exchange transcript-only. The
   transcript (``stream.entries``) stays complete and ordered; entries of the current exchange carry
   ``current: true`` and ``stream.current`` names them.
2. Reset: after ``RESET_AFTER_S`` without a message from either side, Home is clean (no current
   exchange). A reset never touches the widget stack.
3. Widget stack: finished results Todd wants to show and things that need a decision (a question,
   an approval, a stopped job); never running work. Kept in the ``home_stack`` table so it survives
   a plugin reload and outlives Home's own card rules.
4. Dismiss: on Home -> off the stack, still listed for Work (``on_work``); on Work -> gone for good.
   A dealt-with item (answered, replied to, retried) leaves; work it starts is a normal job, and its
   result enters the stack as a new item (``follows`` names the item it came from).
5. Recommendations v1 (no model call): up to three, only things genuinely waiting on you (an email
   draft nobody has touched since, a skipped question, a stopped job or a question you put off), each
   naming its source and ``why`` it qualified; a dismissed one never returns. A finished job is never
   a recommendation ("Follow up on ..."): its result is its widget on the stack. Recomputed only
   when its inputs change (a job finished, something was dismissed, a draft changed) and served from
   the saved list otherwise. When nothing qualifies the list is empty: no filler.

Everything here is additive: apps that don't know these keys keep drawing ``items`` and ``stream``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from typing import Any, Callable

from .confirms import pending

logger = logging.getLogger(__name__)

RESET_AFTER_S = 20 * 60          # Home resets to clean after this long without a message (North Star: "about 20 minutes")
RESULT_KEPT_S = 3 * 86400        # a result nobody dismissed or answered leaves the stack after this long
STACK_MAX = 30                   # stack items served (the app shows two and tucks the rest)
ON_WORK_MAX = 30
RECS_MAX = 3                     # North Star: "Max three"
UNFINISHED_S = 7 * 86400         # unfinished business older than this is no longer recommended
# An email draft is recommended only while the plugin can still vouch that nothing happened to it:
# it cannot see the mailbox, so past this age a reply sent from Mail or Gmail is likely and unknown.
DRAFT_FRESH_S = 2 * 86400
DRAFT_WORK_SLACK_S = 5.0         # the draft's own job posting its result right after storing it
SUBJECT_MATCH_CHARS = 6          # a later request naming a subject this short is not evidence
TITLE_CHARS = 120
LINE_CHARS = 240

# Stack item kinds and the Home card kinds that feed them.
DECISION, STOPPED, RESULT = "decision", "stopped", "result"
_GROUP = {DECISION: "q", STOPPED: "s", RESULT: "r"}
OPEN, DONE, SKIPPED, EXPIRED, GONE = "open", "done", "skipped", "expired", "gone"


def _subject_key(subject: Any) -> str:
    """A thread's subject without reply/forward prefixes, lower-cased, for matching."""
    text = " ".join(str(subject or "").split()).lower()
    while True:
        cut = re.sub(r"^(re|fwd?|aw|sv)\s*:\s*", "", text)
        if cut == text:
            return text
        text = cut


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def approval_state(result_json: str | None) -> str | None:
    """A job's approval card from its saved result: "pending" until Post or Cancel, "settled" after
    either, None when it has none."""
    try:
        result = json.loads(result_json) if result_json else {}
    except ValueError:
        return None
    if pending(result) is not None:
        return "pending"
    views = result.get("views") if isinstance(result, dict) else None
    return "settled" if any(isinstance(v, dict) and v.get("kind") == "confirm" for v in views or []) else None


# -- 1 + 2: the current exchange and the reset -----------------------------------------------------

def current_exchange(entries: list[dict[str, Any]], now: float, extra_runs: set[str] | None = None,
                     reset_after: float = RESET_AFTER_S) -> dict[str, Any]:
    """Which transcript entries are the current exchange. Pure: *entries* is ``stream.entries`` in
    order, *extra_runs* any more jobs the latest message started (``home_jobs``). Marks those entries
    ``current: true`` in place and returns ``stream.current``."""
    users = [e for e in entries if e.get("kind") == "message" and e.get("role") == "user"]
    clean = {"clean": True, "entry_ids": [], "message_id": None, "reply_ids": [], "parts": [], "pending": False,
             "card_ids": [], "run_ids": [], "last_activity_at": None, "resets_at": None}
    for e in entries:
        e.pop("current", None)
    if not users:
        return clean
    last = users[-1]
    at = float(last.get("at") or 0)
    after = [e for e in entries if float(e.get("at") or 0) >= at and e is not last]
    replies = [e for e in after if e.get("kind") == "message" and e.get("role") == "assistant"
               and not e.get("answer")   # an SPK-314 answer line belongs to an older job, not this exchange
               # A slow answer to an earlier message stays with that message (reply_to), never the next one.
               and e.get("reply_to") in (None, last.get("id"))]
    runs = {str(r) for r in (last.get("run_id"), *(e.get("run_id") for e in replies)) if r}
    runs |= set(extra_runs or ())
    activity = max([at, *(float(e.get("at") or 0) for e in replies)])
    if now - activity >= reset_after:
        out = dict(clean)
        out["last_activity_at"] = activity
        return out
    cards = [e for e in entries if e.get("kind") == "card" and str(e.get("id", ""))[2:] in runs]
    # The job's own result (a posted email) and its answer line are part of it too, when they exist.
    tied = [e for e in entries if str(e.get("run_id") or "") in runs
            and (e.get("kind") == "result" or (e.get("kind") == "message" and e.get("answer")))]
    ids: list[str] = []
    for e in [last, *replies, *cards, *tied]:
        if e["id"] not in ids:
            ids.append(e["id"])
            e["current"] = True
    return {"clean": False, "entry_ids": ids, "message_id": last["id"], "reply_ids": [e["id"] for e in replies],
            # home-ack: the agent's side of the exchange, in order: an acknowledgement (above the
            # status capsule), then the answer; a reply that came in time is the one "reply" part.
            "parts": [{"id": e["id"], "part": e.get("part") or "reply"} for e in replies],
            "pending": bool(last.get("pending")),
            "card_ids": [e["id"] for e in cards], "run_ids": sorted(runs), "last_activity_at": activity,
            "resets_at": activity + reset_after}


# -- 3 + 4: the widget stack -----------------------------------------------------------------------

# home-live-fix (F1, build 74 live: no finished job reached the stack after Nov 10 01:44, 7 of 8 with
# views). A finished job is on the stack because of what it brought back (views, cards, pictures,
# files, an email draft, a decision), not because nobody read or heard it yet: an answer spoken on a
# call, or one that belongs to the call that just ended (``call_id``, which only collapses Home's
# cards), enters too. It leaves when opened in full or dismissed, or after RESULT_KEPT_S.
ANSWER_KINDS = {"new_answer", "seen_answer"}
LOOK_GAP_S = 5 * 60   # a real read of Home this long after the previous one starts a new look


def substantive(result: Any, drafts: Any = None) -> bool:
    """A finished job's result is worth a widget: something to see or act on, not just words."""
    if drafts:
        return True
    if not isinstance(result, dict):
        return False
    return any(result.get(k) for k in ("views", "cards", "images", "files", "question"))


def candidates(items: list[dict[str, Any]], eligible: Callable[[str], bool] | None = None,
               call_since: float | None = None) -> list[dict[str, Any]]:
    """Home's final cards -> the stack rows they would make. Never running work; a canceled job is
    not waiting on anyone; a finished answer while it is new (unread), and once read or heard on a
    call too when its result has something to see or act on (``eligible(run_id)``; without it,
    every finished answer); during a call (``call_since``) any answer of this call that finished on
    it, heard or not (SPK-376, Claire); conversations Speakeasy didn't start only when they ask something. A
    group of stopped jobs is one row per job."""
    out: list[dict[str, Any]] = []
    for item in items:
        kind, run = item.get("kind"), item.get("run_id")
        if item.get("canceled"):
            continue
        if kind == "question" and not item.get("replaced"):
            asked = int(float(((item.get("question") or {}).get("asked_at")) or item.get("updated_at") or 0))
            ref = f"{run}:{asked}" if run else str(item["id"])
            out.append(_cand(item, DECISION, ref, run))
        elif kind == "new_answer" and item.get("needs_you") and run:
            # An approval card (Post / Cancel) nobody has tapped: a decision, drawn as its answer.
            out.append(_cand(item, DECISION, f"{run}:{int(float(item.get('finished_at') or 0))}", run))
        elif kind == "stopped":
            members = item.get("group") or [{"run_id": run, "session_id": item.get("session_id"),
                                              "title": item.get("title")}]
            for m in members:
                one = {**item, "run_id": m.get("run_id"), "session_id": m.get("session_id"),
                       "title": m.get("title") or item.get("title")}
                if one.get("run_id"):
                    out.append(_cand(one, STOPPED, str(one["run_id"]), one["run_id"]))
        elif kind in ANSWER_KINDS and run and (kind == "new_answer" or eligible is None or eligible(str(run))):
            out.append(_cand(item, RESULT, str(run), run))
        elif kind == "seen_answer" and run and _call_answer(item, call_since):
            out.append(_cand(item, RESULT, str(run), run))
    return out


def _call_answer(item: dict[str, Any], call_since: float | None) -> bool:
    """This live call's own answer, finished while the call was on (heard or not). Older answers a
    call brings in stay where they were: a result someone already opened doesn't come back."""
    if call_since is None or not item.get("on_current_call"):
        return False
    return float(item.get("finished_at") or 0) >= float(call_since)


def _cand(item: dict[str, Any], kind: str, ref: str, run: Any) -> dict[str, Any]:
    source: dict[str, Any] = {"kind": "job" if run else "chat"}
    if run:
        source["run_id"] = run
    if item.get("session_id"):
        source["session_id"] = item["session_id"]
    return {"id": f"w:{_GROUP[kind]}:{ref}", "kind": kind, "run_id": run or None, "item_id": item["id"],
            "card_id": "c:" + str(run or item["id"]), "title": _clip(item.get("title"), TITLE_CHARS),
            "line": _clip(item.get("line"), LINE_CHARS), "source": source,
            "at": float(item.get("finished_at") or item.get("updated_at") or item.get("started_at") or 0)}


class HomeState:
    """The stack and recommendations, persisted in the plugin's store (``home_stack``,
    ``home_recs_dismissed``, meta ``home_recs``)."""

    def __init__(self, service: Any, clock: Callable[[], float] = time.time):
        self.svc = service
        self.store = service.store
        self.clock = clock
        self._lock = threading.RLock()
        self._fetched: dict[str, float] = {}       # delivery timing (note_fetched)
        self._fetch_born = clock()
        with self.store._lock, self.store._db:
            self.store._db.execute("""CREATE TABLE IF NOT EXISTS home_stack (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, run_id TEXT, item_id TEXT, card_id TEXT,
                title TEXT, line TEXT, source TEXT, result_id TEXT, follows TEXT,
                added_at REAL NOT NULL, at REAL, state TEXT NOT NULL DEFAULT 'open', state_at REAL,
                home_dismissed_at REAL, acked_at REAL)""")
            self.store._db.execute("""CREATE TABLE IF NOT EXISTS home_recs_dismissed (
                id TEXT PRIMARY KEY, at REAL NOT NULL)""")

    # -- rows ------------------------------------------------------------------------------------
    _COLS = ("id", "kind", "run_id", "item_id", "card_id", "title", "line", "source", "result_id", "follows",
             "added_at", "at", "state", "state_at", "home_dismissed_at", "acked_at")

    def _rows(self, where: str = "", args: tuple = ()) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._db.execute(f"SELECT {', '.join(self._COLS)} FROM home_stack {where}", args).fetchall()
        out = []
        for r in rows:
            d = dict(zip(self._COLS, r))
            try:
                d["source"] = json.loads(d["source"]) if d["source"] else {}
            except ValueError:
                d["source"] = {}
            out.append(d)
        return out

    def _set(self, sid: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.store._lock, self.store._db:
            self.store._db.execute(f"UPDATE home_stack SET {cols} WHERE id=?", (*fields.values(), sid))

    def _run_state(self, run_id: str) -> dict[str, Any]:
        with self.store._lock:
            row = self.store._db.execute("SELECT idem_key, status, answered, question_closed, result_json"
                                         " FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row:
            return {}
        return {"key": row[0], "status": str(row[1] or ""), "answered": row[2], "question_closed": row[3],
                "approval": approval_state(row[4])}

    def _eligible(self, run_id: str) -> bool:
        """F1: the finished job brought back something to see or act on (``substantive``). A
        finished result doesn't change, so the answer is kept per run."""
        cache: dict[str, bool] = self.__dict__.setdefault("_worth", {})
        if run_id in cache:
            return cache[run_id]
        with self.store._lock:
            row = self.store._db.execute("SELECT idem_key, result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return False
        try:
            result = json.loads(row[1]) if row[1] else None
        except ValueError:
            result = None
        try:
            drafts = self.store.drafts_for(row[0]) if row[0] else None
        except Exception:  # noqa: BLE001
            drafts = None
        worth = substantive(result, drafts)
        if row[1]:
            if len(cache) > 2000:
                cache.clear()
            cache[run_id] = worth
        return worth

    def _opened_at(self, run_id: str | None) -> float:
        """F1: when this job was opened in full in an app (``POST /voice/tasks/{run}/seen``), 0 if never."""
        if not run_id:
            return 0.0
        key = self._run_state(run_id).get("key")
        if not key:
            return 0.0
        try:
            return float(self.store.get_meta(f"now_opened:{key}") or 0)
        except ValueError:
            return 0.0

    def _looked(self, now: float) -> None:
        """F1 ordering: a real read of Home this long after the previous one is a new look; what was
        added since the last look ended is new. Kept in memory, written at most once a minute."""
        last = self.__dict__.get("_last_read")
        if last is None:
            try:
                last = float(self.store.get_meta("home_last_read") or 0)
                self._seen_through = float(self.store.get_meta("home_seen_through") or 0)
            except ValueError:
                last, self._seen_through = 0.0, 0.0
            self._last_written = last
        if now - last > LOOK_GAP_S and last:
            self._seen_through = last
            self.store.set_meta("home_seen_through", repr(last))
        self._last_read = now
        if now - self.__dict__.get("_last_written", 0.0) >= 60:
            self._last_written = now
            self.store.set_meta("home_last_read", repr(now))

    def seen_through(self) -> float:
        if "_seen_through" not in self.__dict__:
            try:
                self._seen_through = float(self.store.get_meta("home_seen_through") or 0)
            except ValueError:
                self._seen_through = 0.0
        return self._seen_through

    # -- feeding the stack (called from now.build) ------------------------------------------------
    def feed(self, items: list[dict[str, Any]], entries: list[dict[str, Any]], now: float,
             call_since: float | None = None) -> None:
        """Home's final cards in, the stack table updated: new rows for new results/decisions,
        snapshots refreshed, and rows whose item was dealt with closed. ``call_since``: when the
        live call started (its answers join the stack heard or not), None off a call."""
        with self._lock:
            results = {str(e.get("run_id")): e["id"] for e in entries if e.get("kind") == "result" and e.get("run_id")}
            lines = {str(e.get("run_id")): e["id"] for e in entries
                     if e.get("kind") == "message" and e.get("answer") and e.get("run_id")}
            cands = candidates(items, self._eligible, call_since)
            self._looked(now)
            seen = {c["id"] for c in cands}
            known = {r["id"]: r for r in self._rows()}
            hidden = set(self.store.later_items())
            for row in known.values():
                if row["state"] == OPEN and row["kind"] == RESULT and row["run_id"] \
                        and self._opened_at(row["run_id"]) >= float(row["at"] or row["added_at"] or 0) - 1:
                    self._set(row["id"], state=DONE, state_at=now)   # F1: opened in full, so it leaves
                    row["state"] = DONE
                    continue
                if row["state"] != OPEN or row["id"] in seen:
                    continue
                if row["kind"] == RESULT:
                    if now - float(row["added_at"]) >= RESULT_KEPT_S:
                        self._set(row["id"], state=EXPIRED, state_at=now)
                    continue   # a result waits for a dismiss or a reply, not for Home's 12 h rule
                if row["item_id"] in hidden:
                    continue   # put off with Later: still waiting on you
                run = self._run_state(row["run_id"]) if row["run_id"] else {}
                if row["kind"] == DECISION:
                    if run and row["run_id"] and (run["approval"] == "pending" or self._still_open(run, now)):
                        continue
                    done = not run or run.get("answered") or run["approval"] == "settled"
                    self._set(row["id"], state=DONE if done else SKIPPED, state_at=now)
                else:   # stopped: retried, followed up or cleared elsewhere
                    if run and run["status"] in {"failed", "interrupted", "canceled", "cancelled"} \
                            and not self._superseded(row["run_id"], items):
                        continue
                    self._set(row["id"], state=DONE, state_at=now)
            known = {r["id"]: r for r in self._rows()}   # closed rows first, so a new result can name them
            for c in cands:
                result_id = (results.get(str(c["run_id"])) or lines.get(str(c["run_id"]))) if c["kind"] == RESULT else None
                row = known.get(c["id"])
                if row is None:
                    if c["kind"] == RESULT and c["run_id"] and c["at"] \
                            and self._opened_at(c["run_id"]) >= float(c["at"]) - 1:
                        continue   # F1: already opened in full after it finished: nothing new to show
                    follows = self._follows(c, known)
                    with self.store._lock, self.store._db:
                        self.store._db.execute(
                            "INSERT OR IGNORE INTO home_stack (id, kind, run_id, item_id, card_id, title, line, source,"
                            " result_id, follows, added_at, at, state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (c["id"], c["kind"], c["run_id"], c["item_id"], c["card_id"], c["title"], c["line"],
                             json.dumps(c["source"]), result_id, follows, now, c["at"], OPEN))
                elif row["state"] == OPEN:
                    self._set(c["id"], item_id=c["item_id"], title=c["title"], line=c["line"],
                              result_id=result_id or row["result_id"], at=c["at"])

    def _still_open(self, run: dict[str, Any], now: float) -> bool:
        try:
            return run["key"] in set(self.svc._open_questions([run["key"]], now))
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _superseded(run_id: str, items: list[dict[str, Any]]) -> bool:
        return any(i.get("run_id") == run_id and i.get("kind") != "stopped" for i in items)

    @staticmethod
    def _follows(c: dict[str, Any], known: dict[str, dict[str, Any]]) -> str | None:
        """A result whose job already had a stack item that was dealt with (a question answered, a
        stopped job retried in place, a result replied to) says which item it came from."""
        if c["kind"] != RESULT or not c["run_id"]:
            return None
        prior = [r for r in known.values() if r["run_id"] == c["run_id"] and r["id"] != c["id"]
                 and r["state"] in {DONE, SKIPPED}]
        if not prior:
            prior = [r for r in known.values() if r["state"] == DONE and (r.get("source") or {}).get("started_run")
                     == c["run_id"]]
        prior.sort(key=lambda r: float(r.get("state_at") or 0))
        return prior[-1]["id"] if prior else None

    # -- reading ---------------------------------------------------------------------------------
    @staticmethod
    def _shape(row: dict[str, Any], current_runs: set[str]) -> dict[str, Any]:
        out = {"id": row["id"], "kind": row["kind"], "needs_you": row["kind"] in {DECISION, STOPPED},
               "item_id": row["item_id"], "card_id": row["card_id"], "result_id": row["result_id"],
               "title": row["title"], "line": row["line"], "source": {k: v for k, v in (row["source"] or {}).items()
                                                                      if k != "started_run"},
               "added_at": row["added_at"], "at": row["at"]}
        if row["run_id"]:
            out["run_id"] = row["run_id"]
        if row["follows"]:
            out["follows"] = row["follows"]
        if row["run_id"] and row["run_id"] in current_runs:
            out["in_current"] = True   # the current exchange already shows it in place
        if row["home_dismissed_at"]:
            out["dismissed_on_home_at"] = row["home_dismissed_at"]
        return out

    def stack(self, current_runs: set[str] | None = None) -> dict[str, Any]:
        """``{items, on_work}``: the stack (needs-you first, then results; newest first within each)
        and what was dismissed on Home but is still open, for Work."""
        current_runs = current_runs or set()
        rows = self._rows("WHERE state='open'")
        # F1: what came in since you last looked first; then needs-you before results, newest first.
        through = self.seen_through()
        fresh = lambda r: float(r["added_at"] or 0) > through  # noqa: E731
        order = lambda r: (0 if fresh(r) else 1, 0 if r["kind"] in {DECISION, STOPPED} else 1,  # noqa: E731
                           -float(r["added_at"] or 0), r["id"])
        on_home = sorted((r for r in rows if not r["home_dismissed_at"]), key=order)
        on_work = sorted((r for r in rows if r["home_dismissed_at"]), key=order)
        shaped = [self._shape(r, current_runs) for r in on_home[:STACK_MAX]]
        return {"items": shaped,
                "count": len(on_home),
                "on_work": [self._shape(r, current_runs) for r in on_work[:ON_WORK_MAX]]}

    # -- dismiss and dealt-with ------------------------------------------------------------------
    def dismiss(self, sid: str, where: str) -> dict[str, Any]:
        """Home: off the stack, still on Work. Work: gone for good. Idempotent."""
        if where not in {"home", "work"}:
            raise ValueError("from must be home or work")
        with self._lock:
            rows = self._rows("WHERE id=?", (sid,))
            if not rows:
                raise KeyError(sid)
            row, now = rows[0], self.clock()
            if where == "work":
                if row["state"] != GONE:
                    self._set(sid, state=GONE, state_at=now)
                state = GONE
            else:
                if not row["home_dismissed_at"]:
                    self._set(sid, home_dismissed_at=now)
                state = row["state"] if row["state"] != OPEN else "on_work"
        self.invalidate()
        return {"ok": True, "id": sid, "from": where, "state": state}

    def dismiss_for_item(self, item_id: str) -> None:
        """An older app's Home swipe (``POST /voice/now/{id}/dismiss``) is a Home dismiss here too."""
        with self._lock:
            ids = [r["id"] for r in self._rows("WHERE item_id=? AND state='open' AND home_dismissed_at IS NULL",
                                               (item_id,))]
            for sid in ids:
                self._set(sid, home_dismissed_at=self.clock())
        if ids:
            self.invalidate()

    def dealt_with(self, run_id: str | None = None, item_id: str | None = None, sid: str | None = None,
                   started_run: str | None = None) -> list[str]:
        """The user answered or replied to an item: it leaves the stack (and Work's list) because
        nothing more is required of it. Any work that starts is a normal job; its result enters the
        stack later as a new item. Returns the ids closed."""
        clauses, args = [], []
        for col, val in (("run_id", run_id), ("item_id", item_id), ("id", sid)):
            if val:
                clauses.append(f"{col}=?")
                args.append(val)
        if not clauses:
            return []
        with self._lock:
            rows = self._rows(f"WHERE state='open' AND ({' OR '.join(clauses)})", tuple(args))
            now = self.clock()
            for r in rows:
                source = dict(r["source"] or {})
                if started_run and started_run != r["run_id"]:
                    source["started_run"] = started_run
                self._set(r["id"], state=DONE, state_at=now, source=json.dumps(source))
        if rows:
            self.invalidate()
        return [r["id"] for r in rows]

    def row(self, sid: str) -> dict[str, Any] | None:
        rows = self._rows("WHERE id=?", (sid,))
        return rows[0] if rows else None

    # -- delivery timing -------------------------------------------------------------------------
    def note_fetched(self, items: list[dict[str, Any]]) -> list[str]:
        """A phone just fetched Home: each result it gets for the first time is logged once, with the
        gap since it landed on the stack (and since its job finished), so phone-side delays can be
        measured. Returns the ids logged (tests)."""
        now = self.clock()
        seen, born = self._fetched, self._fetch_born
        out = []
        for it in items or []:
            if not isinstance(it, dict) or it.get("kind") != RESULT or it.get("id") in seen:
                continue
            seen[it["id"]] = now
            landed = float(it.get("added_at") or 0)
            finished = float(it.get("at") or 0)
            if landed and landed < born:
                continue   # landed before this process started: its first fetch was already logged (or lost)
            logger.info("speakeasy: delivery.first_fetch id=%s run=%s landed_to_fetch_ms=%d finished_to_fetch_ms=%s",
                        it["id"], it.get("run_id") or "-", int(max(0.0, now - landed) * 1000) if landed else -1,
                        int(max(0.0, now - finished) * 1000) if finished else "-")
            out.append(it["id"])
        if len(seen) > 2000:   # bounded: forget the oldest
            for k in sorted(seen, key=lambda k: seen[k])[:1000]:
                seen.pop(k, None)
        return out

    # -- 6: recommendations ----------------------------------------------------------------------
    def invalidate(self) -> None:
        self.store.set_meta("home_recs_sig", "")

    def _dismissed_recs(self) -> set[str]:
        with self.store._lock:
            return {r[0] for r in self.store._db.execute("SELECT id FROM home_recs_dismissed").fetchall()}

    def _pending_drafts(self, now: float) -> list[dict[str, Any]]:
        """Email drafts genuinely waiting on you, each with ``why``. A draft qualifies only when it is
        pending (not sent, saved, denied or being revised), younger than ``DRAFT_FRESH_S``, the newest
        draft on its thread (no later draft there, whatever became of it), no job has touched it since
        (no later step of its own job, no later job in its Hermes session or whose request names its
        subject), and its job's result was not replied to or cleared from Work. When in doubt: not."""
        try:
            with self.store._lock:
                db = self.store._db
                rows = db.execute(
                    "SELECT d.draft_id, d.idem_key, d.session_id, d.draft_json, d.created, d.updated, r.run_id "
                    "FROM email_drafts d LEFT JOIN runs r ON r.idem_key=d.idem_key WHERE d.status='pending' "
                    "AND d.created >= ? ORDER BY d.created DESC LIMIT 10", (now - DRAFT_FRESH_S,)).fetchall()
                if not rows:
                    return []
                since = min(float(r[4] or 0) for r in rows)
                keys = sorted({r[1] for r in rows})
                marks = ",".join("?" * len(keys))
                drafts = db.execute(
                    "SELECT draft_id, draft_json, created FROM email_drafts WHERE created > ? AND status != 'superseded'",
                    (since,)).fetchall()
                events = db.execute(f"SELECT idem_key, created FROM work_events WHERE created > ? AND idem_key IN ({marks})",
                                    (since, *keys)).fetchall()
                requests = db.execute(
                    "SELECT e.idem_key, e.text, e.created, r.session_id FROM work_events e LEFT JOIN runs r "
                    "ON r.idem_key=e.idem_key WHERE e.kind='request' AND e.created > ?", (since,)).fetchall()
                stack = {r[0]: r[1] for r in db.execute(
                    "SELECT run_id, state FROM home_stack WHERE run_id IS NOT NULL AND kind=?", (RESULT,)).fetchall()}
        except Exception:  # noqa: BLE001  (no drafts table yet)
            return []

        def load(body: Any) -> dict[str, Any]:
            try:
                out = json.loads(body) or {}
            except ValueError:
                return {}
            return out if isinstance(out, dict) else {}

        def thread(d: dict[str, Any]) -> tuple[str, str]:
            ctx = d.get("context")
            ctx = ctx if isinstance(ctx, dict) else {}
            return str(d.get("reply_to_message_id") or ""), _subject_key(ctx.get("thread_subject") or d.get("subject"))

        later = [(did, thread(load(body)), float(created or 0)) for did, body, created in drafts]
        out = []
        for draft_id, key, session_id, body, created, updated, run_id in rows:
            d = load(body)
            created = float(created or 0)
            touched = max(created, float(updated or 0))
            reply_to, subject = thread(d)
            if any(did != draft_id and at > created and ((reply_to and t[0] == reply_to) or (subject and t[1] == subject))
                   for did, t, at in later):
                continue   # a later draft on the same thread (sent, saved, or another one waiting)
            if any(k == key and at > touched + DRAFT_WORK_SLACK_S for k, at in events):
                continue   # its own job did more after it
            if any(k != key and at > created and ((session_id and sess == session_id)
                                                 or (len(subject) >= SUBJECT_MATCH_CHARS
                                                     and subject in _subject_key(text)))
                   for k, text, at, sess in requests):
                continue   # another job worked on it (or its thread) since
            if run_id and stack.get(run_id) in {DONE, GONE}:
                continue   # its result was replied to, or cleared from Work
            out.append({"draft_id": draft_id, "key": key, "subject": d.get("subject"), "at": created,
                        "run_id": run_id,
                        "why": "unsent draft, newest on its thread, no reply or job on it since it was written"})
        return out

    def _inputs(self, now: float) -> dict[str, Any]:
        rows = self._rows("WHERE state IN ('open','skipped') AND added_at >= ?", (now - UNFINISHED_S,))
        return {"rows": rows, "drafts": self._pending_drafts(now), "dismissed": self._dismissed_recs()}

    @staticmethod
    def _signature(inputs: dict[str, Any]) -> str:
        sig = {"rows": sorted((r["id"], r["state"], bool(r["home_dismissed_at"])) for r in inputs["rows"]),
               "drafts": sorted(d["draft_id"] for d in inputs["drafts"]),
               "dismissed": sorted(inputs["dismissed"])}
        return hashlib.sha256(json.dumps(sig, sort_keys=True).encode()).hexdigest()

    def compute(self, inputs: dict[str, Any]) -> list[dict[str, Any]]:
        """Up to three, only things waiting on you: unsent drafts nobody touched since (see
        ``_pending_drafts``), skipped questions, stopped jobs you already acknowledged, and questions
        you put off on Home that are still open. Never a finished job (no "Follow up on ..."): its
        result is its widget. One per job; a dismissed recommendation never returns; nothing
        qualifying means an empty list. Each item says ``why`` it qualified."""
        dismissed = inputs["dismissed"]
        recs: list[dict[str, Any]] = []
        runs: set[str] = set()

        def add(rid: str, text: str, prompt: str, source: dict[str, Any], run: str | None, why: str) -> None:
            if rid in dismissed or len(recs) >= RECS_MAX or (run and run in runs):
                return
            if run:
                runs.add(run)
            recs.append({"id": rid, "text": text, "prompt": prompt, "source": source, "generic": False, "why": why})

        for d in inputs["drafts"]:
            subject = _clip(d["subject"] or "your email", 80)
            add(f"rec:draft:{d['draft_id']}", f"Finish the email \u201c{subject}\u201d",
                f"Let's finish the email draft \u201c{subject}\u201d.",
                {"kind": "unsent_draft", "label": "Unsent draft", "draft_id": d["draft_id"], "run_id": d["run_id"]},
                d["run_id"], d["why"])
        rows = inputs["rows"]
        skipped = sorted((r for r in rows if r["state"] == SKIPPED), key=lambda r: -float(r["state_at"] or 0))
        for r in skipped:
            add(f"rec:skipped:{r['id']}", f"Answer the question on {r['title'] or 'that job'}",
                f"Let's go back to the question on {r['title'] or 'that job'}.",
                {"kind": "skipped_question", "label": "Skipped question", "stack_id": r["id"], "run_id": r["run_id"]},
                r["run_id"], "question closed without your answer")
        # Put off on Home and still open on Work. A finished result is not here: it is its widget on
        # Work, never a prompt to send back ("Pick back up: <job>" was a follow-up by another name).
        parked = sorted((r for r in rows if r["state"] == OPEN and r["home_dismissed_at"] and r["kind"] != RESULT),
                        key=lambda r: (r["kind"] != STOPPED, -float(r["home_dismissed_at"] or 0)))
        for r in parked:
            if r["kind"] == STOPPED:
                add(f"rec:stopped:{r['id']}", f"Retry {r['title'] or 'the job that stopped'}",
                    f"Retry {r['title'] or 'the job that stopped'}.",
                    {"kind": "stopped_job", "label": "Stopped job", "stack_id": r["id"], "run_id": r["run_id"]},
                    r["run_id"], "job stopped, not retried, put off on Home")
            else:
                add(f"rec:parked:{r['id']}", f"Pick back up: {r['title'] or 'an open item'}",
                    f"Let's pick back up {r['title'] or 'that'}.",
                    {"kind": "dismissed_on_home", "label": "Still open on Work", "stack_id": r["id"],
                     "run_id": r["run_id"]}, r["run_id"], "question still open, put off on Home")
        return recs

    def recommendations(self, now: float | None = None) -> dict[str, Any]:
        """The saved list; recomputed only when its inputs changed since it was saved."""
        now = self.clock() if now is None else now
        with self._lock:
            inputs = self._inputs(now)
            sig = self._signature(inputs)
            saved = None
            if self.store.get_meta("home_recs_sig") == sig:
                try:
                    saved = json.loads(self.store.get_meta("home_recs") or "null")
                except ValueError:
                    saved = None
            if saved is None:
                items = self.compute(inputs)
                saved = {"items": items, "generic": bool(items) and all(i["generic"] for i in items),
                         "computed_at": now}
                self.store.set_meta("home_recs", json.dumps(saved))
                self.store.set_meta("home_recs_sig", sig)
            return saved

    def dismiss_rec(self, rid: str) -> dict[str, Any]:
        with self._lock:
            with self.store._lock, self.store._db:
                self.store._db.execute("INSERT OR IGNORE INTO home_recs_dismissed (id, at) VALUES (?,?)",
                                       (rid, self.clock()))
            self.invalidate()
        return {"ok": True, "id": rid, "dismissed": True}
