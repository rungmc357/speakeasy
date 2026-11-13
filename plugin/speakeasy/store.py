"""Durable call/task state (sqlite under ``<HERMES_HOME>/speakeasy/``).

Admission and run receipts make retries non-duplicating; work events and results feed the task
list; notices are deduplicated across restarts; email drafts carry their exact-content hash.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Iterable

from . import activity as activity_mod
from . import sqlbudget
from .sqlbudget import SlowStatement  # noqa: F401  (re-exported: callers catch it from here)
from . import live_status
from . import runstate as RS
from .runstate import FINAL, RunState
from .emails import draft_sha256, stored_json, with_email_views
from .failures import failure_reason, public_failure
from .text import (MAX_CARDS, AUTHORED_PRECEDENCE_S, SETTLED, TERMINAL, notice_text, public_result, safe_user_text,
                   short_title, valid_short_status, NEW_TASK_TITLE, fallback_title, title_text)
from .text import TITLE_META, fix_product_names, outcome_headline, request_notes, stand_in_titles as _stand_ins, vague_title
from .views import stamp_as_of

STALE_AFTER_S = 90
# A task that ended this way never sends its held messages or reads its steers: both settle unused,
# in the same write as its status (a failed turn still sends what was held, as before).
UNUSED_ON = frozenset({RunState.CANCELLED.value, RunState.INTERRUPTED.value})
MAX_REVIEWS = 3  # finished-image review cards carried onto the call panel
SHARED_KINDS = frozenset({"screen", "picture", "file"})
MAX_SHARED = 12  # things shared with one task kept on its card (a running task can be shown more): the
                 # first ones, so a position never renumbers (the app fetches images by position)


JOB_NAME_TAIL = {"a", "an", "and", "the", "of", "for", "to", "in", "on", "with", "from", "by", "at", "or", "my", "your"}


def job_name(title: str | None) -> str | None:
    """A job's short name: the first two to four words of a task title, without a dangling
    "and"/"for" at the end. None when there's nothing to name it from."""
    words = re.findall(r"[^\s]+", re.sub(r"[\"“”]", "", title or ""))[:4]
    while len(words) > 2 and words[-1].lower().strip(",.:;") in JOB_NAME_TAIL:
        words.pop()
    name = " ".join(words).strip(" ,.:;-")
    return name[:40] or None


def _question_text(result_json: str | None) -> str | None:
    """The question a task's stored result asks (whitespace-normalized), else None."""
    try:
        q = ((json.loads(result_json) or {}).get("question") or {}) if result_json else {}
    except (ValueError, AttributeError):
        return None
    text = q.get("question") if isinstance(q, dict) else None
    return " ".join(text.split()) if isinstance(text, str) and text.strip() else None


# A question's lifecycle (docs/state-machines.md, item 3). ``open`` is the only state that waits on
# the user; every other one is closed, and every close goes through ``Store.close_question`` (the
# service then publishes decision_resolved and withdraws the Needs you notification).
QUESTION_STATES = ("open", "answered", "set_aside", "dismissed", "moved_on", "expired", "superseded")
QUESTION_CLOSES = frozenset(QUESTION_STATES) - {"open"}
# Closes that carry over to the same question asked again by another task of the conversation (a
# joined turn, a retry): one decision, closed once everywhere. A conversation moving on, a day
# passing or a newer task replacing this one close only this task's ask.
_TWIN_CLOSES = frozenset({"answered", "set_aside", "dismissed"})


def _question_state(state: str | None, result_json: str | None, answered: Any = None, set_aside: Any = None,
                    closed: str | None = None) -> str | None:
    """A row's question state: the recorded one, else (a row written before question_state, or a
    result stored without set_result) read from the older columns. None when it asks nothing."""
    if not _question_text(result_json):
        return None
    if state in QUESTION_STATES:
        return state
    if answered:
        return "answered"
    if closed in QUESTION_CLOSES:
        return closed
    if set_aside:
        return "set_aside"
    return "open"


def _placed_session(continued: str | None) -> str | None:
    """The session id a task's placement names (the conversation it continued), if any."""
    try:
        placed = json.loads(continued) if continued else {}
    except ValueError:
        return None
    sid = placed.get("session_id") if isinstance(placed, dict) else None
    return sid if isinstance(sid, str) and sid else None


def local_run_id(idem_key: str) -> str:
    """The id for a task Hermes ran without a run id (same scheme as calls.LOCAL_RUN_PREFIX)."""
    return "lr_" + hashlib.sha256(idem_key.encode()).hexdigest()[:32]


def _is_stand_in(title: str | None, request: str | None, continued: str | None = None,
                 asked: str | None = None) -> bool:
    """A task title that is only the word-based label made from its own request (or missing). A task
    in a chat thread shows the thread's name, which a rename here would split from the thread itself."""
    if '"thread_id"' in (continued or ""):
        return False
    if not title or title in PLACEHOLDER_TITLES or title.endswith("…"):
        return True  # no name, a generic placeholder, or a sentence cut off with an ellipsis
    if vague_title(title):
        return True  # "Check work status", "Confirm the earlier task": names nothing, renamed on read
    return title in _stand_ins(request) or bool(asked and title in _stand_ins(asked))


# Generic labels older plugins stored as a task's title. None is ever shown: a task under one is
# named from its own words (fallback_title) until the titler names it.
PLACEHOLDER_TITLES = {"Voice task", "Quick answer", "A quick choice", NEW_TASK_TITLE}


def plain_chat(activity: dict[str, Any] | None, work: dict[str, Any]) -> bool:
    """A finished run that was only a reply (SPK-290): no tool call or helper in its activity, no
    picture, card or file on its result, nothing shared with it. Thinking and its own status lines
    don't count as work."""
    state = activity if isinstance(activity, dict) else {}
    for item in state.get("items") or []:
        if isinstance(item, dict) and (item.get("tool") or item.get("kind") not in ("think", "message")):
            return False
    if state.get("subagents") or state.get("touched") or work.get("shared") or work.get("review"):
        return False
    result = work.get("result")
    if not isinstance(result, dict):
        return True
    return not (result.get("cards") or result.get("views") or result.get("images") or result.get("files"))


CHOICE_PREFIX = "choice_"   # a spoken pick shown as a card (calls.offer_choice): no Hermes run
TWIN_WINDOW_S = 180         # the same ask started twice this close together is one task


def _ask_key(text: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def fold_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The task list without rows that are only part of another task: a spoken pick that was
    answered or set aside ("Decide on holding" / Set aside) never lists as its own item, and a
    finished twin of the same ask started moments before a later one folds into the later one."""
    def finished_choice(t: dict[str, Any]) -> bool:
        return str(t.get("task_id") or "").startswith(CHOICE_PREFIX) and bool(t.get("set_aside") or t.get("answered"))

    kept = [t for t in tasks if not finished_choice(t)]

    def rank(i: int) -> tuple[int, int]:
        t = kept[i]
        status = t.get("status")
        return (2 if status not in TERMINAL else 1 if status == "completed" else 0, i)

    def twins(a: dict[str, Any], b: dict[str, Any]) -> bool:
        sa, sb = a.get("started_at"), b.get("started_at")
        return (bool(_ask_key(a.get("request"))) and _ask_key(a.get("request")) == _ask_key(b.get("request"))
                and isinstance(sa, (int, float)) and isinstance(sb, (int, float)) and abs(sa - sb) <= TWIN_WINDOW_S)

    out: list[dict[str, Any]] = []
    for i, task in enumerate(kept):
        # Only a finished twin with nothing waiting on the user folds, into the better of the pair
        # (one still running, else one that completed, else the newer).
        foldable = (task.get("status") in TERMINAL and not task.get("email_drafts") and not task.get("review")
                    and not ((task.get("result") or {}).get("question") and not task.get("answered")
                             and not task.get("set_aside")))
        if foldable and any(j != i and twins(task, other) and rank(j) > rank(i) for j, other in enumerate(kept)):
            continue
        out.append(task)
    return out


RECEIPTS = {"Request received", "Started working"}  # milestones that are receipts, not progress
# Milestones about where a task went or what was added to it: bookkeeping, never a plan step done.
PLUMBING = re.compile(r"^(?:Continuing in |Sent into |You added: |Moved to another task|Replaced by your correction|You corrected |"
                      r"Taken back|Queued|Waiting for |Started in |Answer will land)")
NOT_STARTED = RS.NOT_STARTED
logger = logging.getLogger(__name__)
_UNSET: Any = object()  # transition(): no result given
_PLAN_STOP = set("""a an the and or to of in on for with at by from is are be it its this that your my our me you we
it's do does get go up out as""".split())
# Words of a plan step that say nothing about the actual work ("Recall prior points", "Continue the
# topic", "Add your input", "Find the answer", "Your review").
_GENERIC_STEP = set("""recall prior points point continue topic add input read context find answer answers trace
confusion explain confirm clarify discuss discussion respond response reply review call thought thoughts speak
understand request question questions info information details detail check look gather collect summarize
summary wrap next steps step previous earlier talk talking listen note notes share update
checked done finished status progress verify verified handled cause causes fix fixes fixed test tests testing
retest bug bugs issue issues problem problems stuck state flow debug investigate investigation root try it
solution solve research plan options option result results work changes change apply""".split())


_REFERENCE_WORDS = set("""task tasks job jobs thing things work one ones earlier previous last prior
before again it that this those these was were been being has have had did can could should would will
whether if""".split())


def weak_job_name(title: str | None) -> bool:
    """A title that only points at other work ("Check the earlier task", "Look into that"):
    no word of it names the actual subject, so it must not name a whole job."""
    words = [w for w in re.findall(r"[a-z0-9']+", (title or "").lower())
             if w not in _PLAN_STOP and w not in _REFERENCE_WORDS]
    return not words or all(w in _GENERIC_STEP for w in words)


def concrete_plan(plan: list[str], request: str | None = None, title: str | None = None) -> list[str]:
    """A model-written plan only when it names real work: dropped when most of its steps are
    generic ("Recall prior points / Continue the topic / Add your input"). The tool log shows what
    is really happening; a generic plan is worse than none."""
    def generic(step: str) -> bool:
        words = [w for w in re.findall(r"[a-z0-9']+", step.lower()) if w not in _PLAN_STOP]
        return not words or all(w in _GENERIC_STEP for w in words)
    if not plan:
        return plan
    return [] if sum(map(generic, plan)) * 2 > len(plan) else plan


def job_progress(status: str, created: float | None, settled_at: float | None, plan: list[str],
                 events: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """The additive job-progress fields (docs/API.md "Job progress"). ``step_index`` is the step it is
    on (1-based) out of its plan, counted from its milestones the way the apps already count them;
    ``eta_seconds`` only once two steps are done at a measured pace (never a guess from nothing)."""
    now = time.time() if now is None else now
    finished = settled_at if status in TERMINAL else None
    out: dict[str, Any] = {"started_at": created, "finished_at": finished, "step_index": None,
                           "step_count": None, "eta_seconds": None}
    if not plan:
        return out
    count = len(plan)
    if status in NOT_STARTED:
        out["step_count"] = count  # not started: no step is done, none is under way
        return out
    done = count if status == "completed" else min(
        sum(1 for e in events if e.get("kind") == "milestone" and e.get("text") not in RECEIPTS
            and not PLUMBING.match(str(e.get("text") or ""))), count - 1)
    out["step_count"] = count
    out["step_index"] = count if status == "completed" else done + 1
    if status not in TERMINAL and created and done >= 2:
        elapsed = max(0.0, now - created)
        out["eta_seconds"] = int(round(elapsed / done * (count - done)))
    return out


def _current_steps(result: Any, status: Any) -> Any:
    """A stored result's next steps as they read now (home_words.expire_steps)."""
    if not isinstance(result, dict) or not isinstance(result.get("views"), list):
        return result
    from .home_words import expire_steps
    from .runstate import TERMINAL
    return {**result, "views": expire_steps(result["views"], status in TERMINAL, time.time())}


_OPEN: "weakref.WeakSet[StateStore]" = weakref.WeakSet()


class StateStore:
    def __init__(self, path: Path, hermes_home: Path | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.hermes_home = hermes_home  # Hermes' home (kept for callers; failure text no longer names a log path)
        self._db = sqlite3.connect(path, check_same_thread=False)
        sqlbudget.guard(self._db)   # queries inside a statement_budget are bounded (SPK-465)
        self._lock = threading.Lock()
        self.closed = False
        _OPEN.add(self)  # close_all() reaches it even if the service that opened it never finished
        # Called (key, status) after a task's status became terminal (the queued-message drain).
        self.settle_listeners: list[Any] = []
        # Called (key, follow_up_id, state) when a follow-up receipt settles (landed / not_read).
        self.follow_up_listeners: list[Any] = []
        # Called (key, queued client ids, steered client ids) after a stopped or interrupted task's
        # held and steered messages were settled unused (in the same write as its status).
        self.unused_listeners: list[Any] = []
        # This process's own mark on a message receipt being sent: a receipt still "sending" under
        # another mark was cut off by a restart (never sent again: it may already have gone).
        self.boot = secrets.token_hex(6)
        with self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("""CREATE TABLE IF NOT EXISTS admissions (
                request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                interaction_id TEXT NOT NULL, response_json TEXT, created REAL NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS runs (
                idem_key TEXT PRIMARY KEY, interaction_id TEXT NOT NULL,
                delegation_id TEXT NOT NULL, revision INTEGER NOT NULL,
                run_id TEXT, status TEXT NOT NULL, updated REAL NOT NULL,
                short_status TEXT, detail TEXT, progress_updated REAL, status_source TEXT,
                authored_at REAL, result_json TEXT, settled_at REAL, title TEXT, dismissed INTEGER,
                session_id TEXT, summary TEXT, continued TEXT)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS work_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, idem_key TEXT NOT NULL,
                kind TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS work_events_key ON work_events(idem_key, seq)")
            self._db.execute("""CREATE TABLE IF NOT EXISTS notices (
                dedupe_key TEXT PRIMARY KEY, created REAL NOT NULL, outcome TEXT)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS email_drafts (
                draft_id TEXT PRIMARY KEY, idem_key TEXT NOT NULL, session_id TEXT,
                draft_json TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, action_run_id TEXT, error TEXT)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS email_drafts_key ON email_drafts(idem_key, created)")
            # Images a finished task returned wait on a review card until dismissed (per run and card),
            # across hang-ups, like email drafts.
            self._db.execute("""CREATE TABLE IF NOT EXISTS review_dismissals (
                run_id TEXT NOT NULL, card INTEGER NOT NULL, dismissed REAL NOT NULL, PRIMARY KEY (run_id, card))""")
            # The latest image a task produced or is looking at, updated during the run (the live view).
            # ``ref`` is a vetted absolute path under the image roots or a vetted HTTPS URL; never sent to clients.
            self._db.execute("""CREATE TABLE IF NOT EXISTS live_images (
                idem_key TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, name TEXT NOT NULL,
                source TEXT NOT NULL, seq INTEGER NOT NULL, updated REAL NOT NULL)""")
            # Speakeasy's copies of files a task carried (attachments.SharedFolder paths): deleted when
            # the task is cleared and no other task still records the same copy.
            self._db.execute("""CREATE TABLE IF NOT EXISTS shared_files (
                idem_key TEXT NOT NULL, path TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY (idem_key, path))""")
            self._db.execute("CREATE INDEX IF NOT EXISTS shared_files_path ON shared_files(path)")
            # A job is the Hermes session its tasks live in (across calls and days); its short name is
            # kept here once, from the first task's own title (never a chat thread's name).
            self._db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY, name TEXT NOT NULL, from_key TEXT, updated REAL NOT NULL)""")
            # A task's job when its session doesn't say it: a follow-up joins its parent's job; a new
            # request placed into a chat thread that other, unrelated work also uses is its own job.
            self._db.execute("""CREATE TABLE IF NOT EXISTS job_links (
                idem_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, created REAL NOT NULL)""")
            # What the user typed or said for a task whose request is a brief the assistant wrote
            # (a Home message): its name, until the titler's, comes from these words.
            self._db.execute("""CREATE TABLE IF NOT EXISTS asked_words (
                idem_key TEXT PRIMARY KEY, text TEXT NOT NULL, created REAL NOT NULL)""")
            # What the user added to a running task by voice (/steer), and whether it is known to have
            # reached the agent: queued -> landed | not_read (see receipts.py). ``body`` is the exact text
            # sent, kept server-side only to look for it in the transcript; clients get ``words``.
            self._db.execute("""CREATE TABLE IF NOT EXISTS follow_ups (
                follow_up_id TEXT PRIMARY KEY, idem_key TEXT NOT NULL, words TEXT NOT NULL, body TEXT NOT NULL,
                milestone TEXT NOT NULL, state TEXT NOT NULL, after_id INTEGER NOT NULL,
                created REAL NOT NULL, settled REAL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS follow_ups_key ON follow_ups(idem_key, created)")
            # What a task is doing, has found and has touched (activity.py), saved as it runs so a
            # reopened app shows the same feed. ``state_json`` is the tracker's state; clients get
            # activity.public() of it on the work object.
            self._db.execute("""CREATE TABLE IF NOT EXISTS activity (
                idem_key TEXT PRIMARY KEY, state_json TEXT NOT NULL, updated REAL NOT NULL)""")
            # Other ids a task was handed out under: a run id Hermes refused at the door (the task then
            # ran in a chat thread), or the local id a still-running thread task was given before it
            # had one. GET /voice/work/{id} and messages by id resolve these to the task.
            self._db.execute("""CREATE TABLE IF NOT EXISTS run_aliases (
                alias TEXT PRIMARY KEY, idem_key TEXT NOT NULL, created REAL NOT NULL)""")
            # A new task held back because a running task is already editing the same project: it
            # starts when that one ends. Clients get the blocker's name in ``waiting``.
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(runs)")}
            if "waiting" not in columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN waiting TEXT")
            if "answered" not in columns:  # when the user answered this task's question (any device)
                self._db.execute("ALTER TABLE runs ADD COLUMN answered REAL")
            if "question_closed" not in columns:
                # Its question closed without an answer: "dismissed" (the user's ✕, undoable) or
                # "moved_on" (the conversation went on without it). Tells the agent nothing.
                self._db.execute("ALTER TABLE runs ADD COLUMN question_closed TEXT")
                self._db.execute("ALTER TABLE runs ADD COLUMN question_closed_at REAL")
            if "own_title" not in columns:  # the task's name from its words or the titler, never a thread title
                self._db.execute("ALTER TABLE runs ADD COLUMN own_title TEXT")
            self._db.execute("CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id)")
            # Home's build looks tasks up by run id (each stack item, card action and result) and by
            # the call they ran on (a task's parent on that call): without these every lookup read
            # the whole runs table, once per card (SPK-465).
            self._db.execute("CREATE INDEX IF NOT EXISTS runs_run_id ON runs(run_id)")
            self._db.execute("CREATE INDEX IF NOT EXISTS runs_interaction ON runs(interaction_id)")
            # A message from a history page to a chat Speakeasy can't write into without the reply
            # going back to that chat (Discord, Telegram, ...): it runs as a Speakeasy task linked
            # to that conversation (history.py). source_session = the chat's Hermes session.
            self._db.execute("""CREATE TABLE IF NOT EXISTS history_links (
                idem_key TEXT PRIMARY KEY, source_session TEXT NOT NULL, created REAL NOT NULL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS history_links_source ON history_links(source_session)")
            # Per-conversation marks every device shares (history pages): pinned (when), read up to
            # (a last-activity time), so the list's Pinned section and unread dots agree everywhere.
            self._db.execute("""CREATE TABLE IF NOT EXISTS session_marks (
                session_id TEXT PRIMARY KEY, pinned REAL, read_at REAL, updated REAL NOT NULL)""")
            if "user_title" not in {row[1] for row in self._db.execute("PRAGMA table_info(session_marks)")}:
                # The name the user gave a conversation from the app (SPK-320): it always wins over
                # the name of the newest task that ran in it.
                self._db.execute("ALTER TABLE session_marks ADD COLUMN user_title TEXT")
            if "timings" not in columns:  # added after the first release
                self._db.execute("ALTER TABLE runs ADD COLUMN timings TEXT")
            if "failure" not in columns:  # why a run failed: a failures.py kind, never provider text
                self._db.execute("ALTER TABLE runs ADD COLUMN failure TEXT")
            if "failure_hint" not in columns:  # the coarse shape of an unexplained failure (failures.failure_hint)
                self._db.execute("ALTER TABLE runs ADD COLUMN failure_hint TEXT")
            if "created" not in columns:  # when the task was admitted (job progress: started_at)
                self._db.execute("ALTER TABLE runs ADD COLUMN created REAL")
            if "shared" not in columns:  # what the user shared with the task (its card), see add_shared
                self._db.execute("ALTER TABLE runs ADD COLUMN shared TEXT")
            if "plan" not in columns:  # the steps a handed-off task will go through (JSON list)
                self._db.execute("ALTER TABLE runs ADD COLUMN plan TEXT")
            if "set_aside" not in columns:  # when the user set its question aside (handled, or not needed)
                self._db.execute("ALTER TABLE runs ADD COLUMN set_aside REAL")
            if "seen" not in columns:  # nothing to read (a finished home command): never a new answer
                self._db.execute("ALTER TABLE runs ADD COLUMN seen INTEGER")
            if "parent" not in columns:  # the task this one answers or joins (its idem key): what names a follow-up
                self._db.execute("ALTER TABLE runs ADD COLUMN parent TEXT")
            if "model" not in columns:  # the model/reasoning it was started with and what Hermes served (models.py)
                self._db.execute("ALTER TABLE runs ADD COLUMN model TEXT")
            if "question_state" not in columns:
                # The one lifecycle of a task's question (QUESTION_STATES): NULL = it asks nothing.
                # Every close goes through close_question; answered/set_aside/question_closed stay
                # written for apps that read them. Rows from before it get their state from those.
                self._db.execute("ALTER TABLE runs ADD COLUMN question_state TEXT")
                self._db.execute("ALTER TABLE runs ADD COLUMN question_state_at REAL")
                self._db.execute(
                    "UPDATE runs SET question_state = CASE WHEN answered IS NOT NULL THEN 'answered' "
                    "WHEN question_closed IS NOT NULL THEN question_closed WHEN set_aside IS NOT NULL THEN 'set_aside' "
                    "ELSE 'open' END, question_state_at = COALESCE(answered, question_closed_at, set_aside, updated) "
                    "WHERE result_json LIKE '%\"question\"%'")
                for key, result in self._db.execute(
                        "SELECT idem_key, result_json FROM runs WHERE question_state IS NOT NULL").fetchall():
                    if not _question_text(result):
                        self._db.execute("UPDATE runs SET question_state=NULL, question_state_at=NULL WHERE idem_key=?",
                                         (key,))
            if "question_answer" not in {row[1] for row in self._db.execute("PRAGMA table_info(runs)")}:
                self._db.execute("ALTER TABLE runs ADD COLUMN question_answer TEXT")  # its answer's words, when known
            if "context" not in columns:  # its last model call when it finished: model + context fill (context_use.py)
                self._db.execute("ALTER TABLE runs ADD COLUMN context TEXT")
            # A chosen model/reasoning (models.py): scope "session:<hermes session id>" (Use for this chat)
            # or "task:<idem key>" (one task: a voice preset, a new chat's first job).
            self._db.execute("""CREATE TABLE IF NOT EXISTS model_choices (
                scope TEXT PRIMARY KEY, model TEXT, reasoning TEXT, updated REAL NOT NULL)""")
            # A message sent "when it's done" (mode queue): held here, across reloads, until the task it
            # waits on (watch_key) completes or fails, then sent as the next turn of the same session.
            # kind/target: how it was sent (task = a run id, session = a history page id). state:
            # queued -> sending -> sent | cancelled. page_session: the session page that lists it.
            self._db.execute("""CREATE TABLE IF NOT EXISTS queued_messages (
                client_id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, page_session TEXT,
                watch_key TEXT NOT NULL, text TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL,
                updated REAL NOT NULL, reply_json TEXT, error TEXT)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS queued_messages_watch ON queued_messages(watch_key, state)")
            # A typed message steered into a running task (mode steer), so its page can say what became of
            # it: sent (Hermes took it) -> seen (in the task's transcript: its next model call has it) |
            # follow_up (the turn ended before reading it: sent again as the next turn) | unused (stopped).
            # Matched to its follow_ups receipt by task and words (receipts.py does the looking).
            self._db.execute("""CREATE TABLE IF NOT EXISTS steered_messages (
                client_id TEXT PRIMARY KEY, idem_key TEXT NOT NULL, page_session TEXT, text TEXT NOT NULL,
                sent_at REAL NOT NULL, state TEXT NOT NULL, seen_at REAL, follow_up_run TEXT, updated REAL NOT NULL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS steered_messages_key ON steered_messages(idem_key, sent_at)")
            # Home (now.py): an item the user put off ("Later"): off Home until *until* (epoch) or, with
            # on_change, until the item's words change from *seen_sig*. woke_at: when it came back.
            self._db.execute("""CREATE TABLE IF NOT EXISTS later (
                item_id TEXT PRIMARY KEY, until REAL, on_change INTEGER NOT NULL DEFAULT 0, seen_sig TEXT,
                set_at REAL NOT NULL, woke_at REAL)""")
            # One row per typed message with a client_id (SPK-80): the durable idempotency record and
            # its receipt. state: queued (held, see queued_messages) -> sending -> done. A failed send
            # deletes its row (the same client_id may be tried again). delivered_as: steer | queued |
            # follow_up | new_run | answer | linked | pending (not known yet). page_session: the history
            # page that shows its user row; message_rows binds it to that row once, for good.
            self._db.execute("""CREATE TABLE IF NOT EXISTS message_receipts (
                client_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, route TEXT NOT NULL, page_session TEXT,
                said TEXT, state TEXT NOT NULL, delivered_as TEXT, reply_json TEXT, boot TEXT,
                created REAL NOT NULL, updated REAL NOT NULL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS message_receipts_page ON message_receipts(page_session)")
            self._db.execute("""CREATE TABLE IF NOT EXISTS message_rows (
                page TEXT NOT NULL, row_id TEXT NOT NULL, client_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY (page, row_id))""")
            # A held send cut off by a reload: sent already (its receipt says so) -> sent, never again;
            # cut off mid-send -> unused (it may have gone: shown, Steer now resends it); else held again.
            self._db.execute("UPDATE queued_messages SET state='sent', reply_json=(SELECT reply_json FROM "
                             "message_receipts r WHERE r.client_id=queued_messages.client_id) WHERE state='sending' "
                             "AND client_id IN (SELECT client_id FROM message_receipts WHERE state='done')")
            self._db.execute("UPDATE queued_messages SET state='unused', error='interrupted' WHERE state='sending' "
                             "AND client_id IN (SELECT client_id FROM message_receipts WHERE state='sending')")
            self._db.execute("UPDATE queued_messages SET state='queued' WHERE state='sending'")
            # The Hermes profile (agents.py) a chat ("session:<id>") or a new chat's first task
            # ("task:<task id>") runs on. No row = Speakeasy's own profile.
            self._db.execute("""CREATE TABLE IF NOT EXISTS agent_choices (
                scope TEXT PRIMARY KEY, agent TEXT NOT NULL, updated REAL NOT NULL)""")
            # The newest model call per Hermes session: which model answered and how full its context
            # was (context_use.py; Hermes never saves the figure, so it is recorded as the work runs).
            self._db.execute("""CREATE TABLE IF NOT EXISTS context_use (
                session_id TEXT PRIMARY KEY, record TEXT NOT NULL, at REAL NOT NULL)""")
            # Finished tasks that never had a Hermes run id (quick answers, home control) could not be
            # cleared: "Clear done" and the x address tasks by run id. Give them their local id.
            settled = sorted(RS.FINAL)
            for (key,) in self._db.execute(
                    f"SELECT idem_key FROM runs WHERE run_id IS NULL AND status IN ({','.join('?' * len(settled))})",
                    settled).fetchall():
                self._db.execute("UPDATE runs SET run_id=? WHERE idem_key=?", (local_run_id(key), key))
            # Tasks stay on the board across calls until cleared (board_carry). Finished ones from
            # before that existed stay off a new call's board: an upgrade doesn't bring back history.
            self._db.execute("INSERT OR IGNORE INTO meta (key,value) VALUES ('board_carry_since',?)",
                             (repr(time.time()),))

    # -- session admission -------------------------------------------------------------
    def reserve_session(self, request_id: str, fingerprint: str, interaction_id: str) -> tuple[str, dict[str, Any] | None]:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT fingerprint,response_json FROM admissions WHERE request_id=?", (request_id,)).fetchone()
            if row:
                if not hmac.compare_digest(row[0], fingerprint):
                    return "conflict", None
                return ("replay", json.loads(row[1])) if row[1] else ("pending", None)
            self._db.execute("INSERT INTO admissions VALUES (?,?,?,?,?)",
                             (request_id, fingerprint, interaction_id, None, time.time()))
            return "created", None

    def complete_session(self, request_id: str, response: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE admissions SET response_json=? WHERE request_id=?",
                             (json.dumps(response, separators=(",", ":")), request_id))

    def fail_session(self, request_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM admissions WHERE request_id=? AND response_json IS NULL", (request_id,))

    # -- runs --------------------------------------------------------------------------
    def reserve_run(self, key: str, interaction_id: str, delegation_id: str, revision: int) -> tuple[str, str | None]:
        with self._lock, self._db:
            row = self._db.execute("SELECT run_id,status FROM runs WHERE idem_key=?", (key,)).fetchone()
            if row:
                return row[1], row[0]
            self._db.execute(
                """INSERT INTO runs (idem_key,interaction_id,delegation_id,revision,run_id,status,updated,created)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (key, interaction_id, delegation_id, revision, None, "admitting", time.time(), time.time()))
            return "created", None

    def record_finished_run(self, key: str, interaction_id: str, delegation_id: str, title: str | None,
                            result: dict[str, Any] | None, status: str = "completed",
                            about: str | None = None) -> bool:
        """A task that is finished the moment it exists (a spoken choice card): row, title, its job
        link and parent (*about*), status and result written in one transaction, so a board fetched
        meanwhile never sees it half-made (a card with no question). False when the key was already
        recorded."""
        status = RunState(status).value
        with self._lock, self._db:
            if self._db.execute("SELECT 1 FROM runs WHERE idem_key=?", (key,)).fetchone():
                return False
            now = time.time()
            self._db.execute(
                """INSERT INTO runs (idem_key,interaction_id,delegation_id,revision,run_id,status,updated,created,result_json)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (key, interaction_id, delegation_id, 0, local_run_id(key) if status in FINAL else None, status,
                 now, now, json.dumps(result) if result else None))
            if about and about != key:
                self._db.execute("INSERT OR REPLACE INTO job_links (idem_key, job_id, created) VALUES (?,?,?)",
                                 (key, about, now))
                self._db.execute("UPDATE runs SET parent=? WHERE idem_key=?", (about, key))
            self._set_title_locked(key, title, None, False)
            if status in SETTLED:
                self._db.execute("UPDATE runs SET settled_at=? WHERE idem_key=?", (now, key))
        if status in TERMINAL:
            for listener in list(self.settle_listeners):
                try:
                    listener(key, status)
                except Exception:  # a listener never breaks a status write
                    pass
        return True

    def set_session(self, key: str, session_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET session_id=? WHERE idem_key=?", (session_id, key))

    # -- model choices (models.py) ------------------------------------------------------------------

    def set_model_choice(self, scope: str, model: str | None, reasoning: str | None) -> None:
        """Save a choice; both halves None removes it (back to the default)."""
        with self._lock, self._db:
            if model is None and reasoning is None:
                self._db.execute("DELETE FROM model_choices WHERE scope=?", (scope,))
            else:
                self._db.execute("INSERT OR REPLACE INTO model_choices (scope, model, reasoning, updated) "
                                 "VALUES (?,?,?,?)", (scope, model, reasoning, time.time()))

    def model_choice(self, scope: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT model, reasoning, updated FROM model_choices WHERE scope=?",
                                   (scope,)).fetchone()
        return {"model": row[0], "reasoning": row[1], "updated": row[2]} if row else None

    # -- agents (agents.py) ---------------------------------------------------------------------------

    def set_agent(self, scope: str, agent: str | None) -> None:
        with self._lock, self._db:
            if agent:
                self._db.execute("INSERT OR REPLACE INTO agent_choices (scope, agent, updated) VALUES (?,?,?)",
                                 (scope, agent, time.time()))
            else:
                self._db.execute("DELETE FROM agent_choices WHERE scope=?", (scope,))

    def agent(self, scope: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT agent FROM agent_choices WHERE scope=?", (scope,)).fetchone()
        return row[0] if row else None

    def interaction_of(self, key: str) -> str | None:
        """The call a task (by idem key) was started on, if any."""
        with self._lock:
            row = self._db.execute("SELECT interaction_id FROM runs WHERE idem_key=?", (key,)).fetchone()
        return row[0] if row and row[0] else None

    def agent_sessions(self) -> dict[str, str]:
        """{session id: agent} for every chat Speakeasy started on another agent."""
        with self._lock:
            rows = self._db.execute("SELECT scope, agent FROM agent_choices WHERE scope LIKE 'session:%'").fetchall()
        return {scope[8:]: agent for scope, agent in rows}

    def agent_lookup(self, kind: str, ident: str) -> str | None:
        """HermesAPI.agent_lookup: the agent of a session, or of a run (through its task's session)."""
        if kind == "session":
            return self.agent("session:" + ident)
        if kind == "run":
            with self._lock:
                row = self._db.execute("SELECT session_id FROM runs WHERE run_id=? AND session_id IS NOT NULL",
                                       (ident,)).fetchone()
            return self.agent("session:" + row[0]) if row else None
        return None
    def record_context(self, session_id: str, record: dict[str, Any]) -> None:
        """Keep one session's newest context record (context_use.after_api_request); records older
        than 60 days are dropped now and then so the table stays small."""
        now = time.time()
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO context_use (session_id, record, at) VALUES (?,?,?)",
                             (session_id, json.dumps(record), now))
            self._context_writes = getattr(self, "_context_writes", 0) + 1
            if self._context_writes % 500 == 1:
                self._db.execute("DELETE FROM context_use WHERE at < ?", (now - 60 * 86400,))

    def context_record(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT record FROM context_use WHERE session_id=?", (session_id,)).fetchone()
        try:
            value = json.loads(row[0]) if row else None
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def set_run_model(self, key: str, record: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET model=? WHERE idem_key=?", (json.dumps(record), key))

    def run_model(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT model FROM runs WHERE idem_key=?", (key,)).fetchone()
        try:
            value = json.loads(row[0]) if row and row[0] else None
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def reply_model_runs(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        """Persisted per-turn evidence, including ended turns, not just the latest job."""
        from .reply_models import object_value
        with self._lock:
            rows = self._db.execute(
                "SELECT model, created, coalesce(settled_at, updated), status FROM runs WHERE session_id=? ORDER BY created",
                (session_id,)).fetchall()
        out = []
        for row in rows:
            record, start, end, status = row
            if start is None:
                continue
            end = float(end) if end is not None and status not in {'running', 'starting', 'waiting_for_approval', 'resolving_approval'} else None
            out.append({'start': float(start), 'end': end, 'record': object_value(record)})
        return {session_id: out}

    def served_model(self, key: str, runtime: dict[str, Any], bare: bool = False) -> bool:
        """Hermes said which provider/model served the run (its ``runtime``): kept beside what was asked.
        ``bare``: only the model is known (a session's recorded model); a full runtime is never replaced."""
        provider, model = runtime.get("provider"), runtime.get("model")
        if not (isinstance(provider, str) and isinstance(model, str) and model and (provider or bare)):
            return False
        if bare and ((self.run_model(key) or {}).get("served") or {}).get("provider"):
            return False
        record = self.run_model(key) or {}
        served = {"provider": provider[:80], "model": model[:200]}
        if record.get("served") == served:
            return False
        record["served"] = served
        self.set_run_model(key, record)
        return True

    def last_default_served(self, scan: int = 40) -> str | None:
        """``provider/model`` Hermes served the newest new task (not a continued chat) that asked for no
        model, or None: what Speakeasy's default really runs on."""
        with self._lock:
            rows = self._db.execute("SELECT model FROM runs WHERE model IS NOT NULL AND continued IS NULL "
                                    "ORDER BY created DESC LIMIT ?", (int(scan),)).fetchall()
        for (raw,) in rows:
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            served = record.get("served") if isinstance(record, dict) else None
            if (record.get("model") or record.get("source") not in (None, "hermes", "default")
                    or not isinstance(served, dict)):
                continue
            if served.get("provider") and served.get("model"):
                return f"{served['provider']}/{served['model']}"
        return None

    def session_for(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT session_id FROM runs WHERE idem_key=?", (key,)).fetchone()
        return row[0] if row and row[0] else None

    def set_continued(self, key: str, session_id: str, label: str) -> None:
        """This task runs inside an existing Hermes conversation (thread continuity)."""
        with self._lock, self._db:
            row = self._db.execute("SELECT continued FROM runs WHERE idem_key=?", (key,)).fetchone()
            try:
                known = json.loads(row[0]) if row and row[0] else {}
            except ValueError:
                known = {}
            known = known if isinstance(known, dict) else {}
            known.update(session_id=session_id, label=label)
            self._db.execute("UPDATE runs SET session_id=?, continued=? WHERE idem_key=?",
                             (session_id, json.dumps(known), key))

    def set_thread(self, key: str, platform: str, thread_id: str, label: str) -> None:
        """The thread a task opened, kept before its session exists so it can be found again later."""
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET continued=? WHERE idem_key=? AND continued IS NULL",
                             (json.dumps({"platform": platform, "thread_id": str(thread_id), "label": label}), key))

    def unsettled_thread_tasks(self) -> list[dict[str, Any]]:
        """Thread tasks still marked running or unconfirmed: a gateway restart or plugin reload ends
        the wait for their answer, so nothing else will ever settle them."""
        with self._lock:
            rows = self._db.execute(
                "SELECT idem_key, interaction_id, status, updated, continued, created FROM runs "
                "WHERE run_id IS NULL AND status IN ('running','ambiguous')").fetchall()
        out = []
        for key, interaction, status, updated, continued, created in rows:
            try:
                placed = json.loads(continued) if continued else {}
            except ValueError:
                placed = {}
            placed = placed if isinstance(placed, dict) else {}
            out.append({"key": key, "interaction_id": interaction, "status": status, "updated": float(updated or 0),
                        "created": float(created) if created else None,
                        **{k: placed.get(k) for k in ("session_id", "platform", "thread_id", "label")}})
        return out

    def continued_for(self, key: str) -> dict[str, str] | None:
        with self._lock:
            row = self._db.execute("SELECT continued FROM runs WHERE idem_key=?", (key,)).fetchone()
        try:
            return json.loads(row[0]) if row and row[0] else None
        except ValueError:
            return None

    def key_for_run(self, run_id: str) -> str | None:
        """The task a run id names: its current run id, else an id it was handed out under before
        (``run_aliases``: a refused run id, or a thread task's local id)."""
        with self._lock:
            row = self._db.execute("SELECT idem_key FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                row = self._db.execute("SELECT idem_key FROM run_aliases WHERE alias=?", (run_id,)).fetchone()
        return row[0] if row else None

    def add_run_alias(self, alias: str, key: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR IGNORE INTO run_aliases (alias, idem_key, created) VALUES (?,?,?)",
                             (alias, key, time.time()))

    def openable_run_id(self, key: str) -> str | None:
        """The id a client can open this task by (GET /voice/work/{id}) from now on: its run id, else
        (a thread task still running, no run id yet) the local id it will settle under, saved as an
        alias so it resolves while it runs too."""
        with self._lock:
            row = self._db.execute("SELECT run_id FROM runs WHERE idem_key=?", (key,)).fetchone()
        if row is None:
            return None
        if row[0]:
            return row[0]
        local = local_run_id(key)
        self.add_run_alias(local, key)
        return local

    def unconfirmed_runs(self) -> list[dict[str, Any]]:
        """Hermes runs left unconfirmed: the event stream ended before a final event and Hermes
        couldn't say how the run went then. The run may well have finished afterwards, but nothing
        listens to it any more, so the board showed it stopped forever and it couldn't be dismissed."""
        with self._lock:
            rows = self._db.execute(
                "SELECT idem_key, run_id, session_id, updated, created, rowid FROM runs "
                "WHERE run_id IS NOT NULL AND status='ambiguous'").fetchall()
            out = []
            for key, run_id, session_id, updated, created, rowid in rows:
                newer = bool(session_id) and self._db.execute(
                    "SELECT 1 FROM runs WHERE session_id=? AND rowid>? LIMIT 1", (session_id, rowid)).fetchone() is not None
                out.append({"key": key, "run_id": run_id, "session_id": session_id, "updated": float(updated or 0),
                            "created": float(created) if created else None, "superseded": newer})
        return out

    def keys_for_session(self, session_id: str) -> list[tuple[str, str | None, str]]:
        """Every task that ran in a Hermes session, newest first: (idem key, run id, status). Kept after
        a task is dismissed from the board, so the history still knows the session is Speakeasy's."""
        with self._lock:
            rows = self._db.execute("SELECT idem_key, run_id, status FROM runs WHERE session_id=? ORDER BY rowid DESC",
                                    (session_id,)).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def link_history(self, key: str, source_session: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO history_links (idem_key, source_session, created) VALUES (?,?,?)",
                             (key, source_session, time.time()))

    def history_links(self, source_session: str) -> list[str]:
        """The Speakeasy tasks that continued a chat from a history page, oldest first."""
        with self._lock:
            rows = self._db.execute("SELECT idem_key FROM history_links WHERE source_session=? ORDER BY created",
                                    (source_session,)).fetchall()
        return [r[0] for r in rows]

    def linked_source(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT source_session FROM history_links WHERE idem_key=?", (key,)).fetchone()
        return row[0] if row else None

    @property
    def revision(self) -> int:
        """Changes this store has made so far (every write goes through this one connection): a cached
        history reply built at an older revision is stale."""
        with self._lock:
            return self._db.total_changes

    def statement_budget(self, label: str, per_statement: float | None = None, total: float | None = None,
                         log: list[str] | None = None):
        """Bound the queries this thread runs inside the block (sqlbudget.budget, SPK-465): a query that
        runs away is stopped, raises SlowStatement and is named in the log. Other threads untouched."""
        return sqlbudget.budget(label, per_statement, total, log)

    def session_marks(self) -> dict[str, dict[str, float | None]]:
        """{session id: {pinned, read_at}} for every conversation with a mark."""
        with self._lock:
            rows = self._db.execute("SELECT session_id, pinned, read_at FROM session_marks").fetchall()
        return {r[0]: {"pinned": r[1], "read_at": r[2]} for r in rows}

    def set_pinned(self, session_id: str, pinned: bool) -> None:
        now = time.time()
        with self._lock, self._db:
            self._db.execute("INSERT INTO session_marks (session_id, pinned, updated) VALUES (?,?,?) "
                             "ON CONFLICT(session_id) DO UPDATE SET pinned=excluded.pinned, updated=excluded.updated",
                             (session_id, now if pinned else None, now))

    def set_user_title(self, session_id: str, title: str) -> None:
        """The user renamed this conversation (SPK-320): that name always wins in the list."""
        now = time.time()
        with self._lock, self._db:
            self._db.execute("INSERT INTO session_marks (session_id, user_title, updated) VALUES (?,?,?) "
                             "ON CONFLICT(session_id) DO UPDATE SET user_title=excluded.user_title, "
                             "updated=excluded.updated", (session_id, title, now))

    def user_title(self, session_id: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT user_title FROM session_marks WHERE session_id=?", (session_id,)).fetchone()
        return row[0] if row and row[0] else None

    def task_names(self, session_id: str) -> list[tuple[str | None, str | None, bool]]:
        """Every name Speakeasy gave the tasks that ran in a session, newest first:
        (title, own title, continued)."""
        with self._lock:
            rows = self._db.execute("SELECT title, own_title, continued FROM runs WHERE session_id=? "
                                    "ORDER BY rowid DESC", (session_id,)).fetchall()
        return [(r[0], r[1], bool(r[2])) for r in rows]

    def set_read(self, session_id: str, at: float | None = None) -> None:
        """Read up to *at* (default now): its unread dot clears on every device."""
        now = time.time()
        with self._lock, self._db:
            self._db.execute("INSERT INTO session_marks (session_id, read_at, updated) VALUES (?,?,?) "
                             "ON CONFLICT(session_id) DO UPDATE SET read_at=max(coalesce(read_at,0), excluded.read_at), "
                             "updated=excluded.updated", (session_id, now if at is None else at, now))

    def mark_session_runs_seen(self, session_id: str, at: float | None = None) -> int:
        """One read state (SPK-124): reading a conversation marks its finished tasks seen, the ones
        that ran in it and the ones that continued it, settled up to *at* (default: all of them)."""
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE runs SET seen=1 WHERE coalesce(seen,0)=0 AND settled_at IS NOT NULL AND (session_id=? OR "
                "idem_key IN (SELECT idem_key FROM history_links WHERE source_session=?)) AND settled_at <= ?",
                (session_id, session_id, time.time() + 1 if at is None else at + 1))
            return cur.rowcount

    def job_full_title(self, job_id: str) -> str | None:
        """The whole title a job's short name was cut from (its naming task's own title)."""
        with self._lock:
            row = self._db.execute("SELECT coalesce(r.own_title, r.title) FROM jobs j JOIN runs r ON r.idem_key = j.from_key "
                                   "WHERE j.job_id=?", (job_id,)).fetchone()
        return row[0] if row and row[0] else None

    def run_settled(self, key: str) -> tuple[str | None, float | None]:
        """A task's status and when it settled."""
        with self._lock:
            row = self._db.execute("SELECT status, settled_at FROM runs WHERE idem_key=?", (key,)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def now_runs(self, since: float, limit: int = 120) -> list[dict[str, Any]]:
        """Home's candidates (now.py): every task that isn't finished, and every one that settled
        since *since*, newest first, helpers (a task's own parts) left to the task they belong to."""
        with self._lock:
            rows = self._db.execute(
                "SELECT r.idem_key, r.run_id, r.status, r.created, r.settled_at, r.session_id, r.interaction_id, "
                "coalesce(r.dismissed,0), coalesce(r.seen,0), r.set_aside, r.question_closed, r.answered, "
                "r.failure, r.updated, h.source_session, r.continued, r.parent, r.failure_hint FROM runs r LEFT JOIN history_links h ON h.idem_key = r.idem_key "
                "WHERE r.status != 'rejected' AND (r.settled_at IS NULL OR r.settled_at >= ? OR r.status NOT IN "
                "('completed','failed','cancelled','interrupted')) ORDER BY r.rowid DESC LIMIT ?",
                (since, limit)).fetchall()
        names = ("key", "run_id", "status", "created", "settled_at", "session_id", "interaction_id", "dismissed",
                 "seen", "set_aside", "question_closed", "answered", "failure", "updated", "linked_source",
                 "continued", "parent", "failure_hint")
        return [dict(zip(names, r)) for r in rows]

    def later_items(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT item_id, until, on_change, seen_sig, set_at, woke_at FROM later").fetchall()
        return {r[0]: {"until": r[1], "on_change": bool(r[2]), "seen_sig": r[3], "set_at": r[4], "woke_at": r[5]}
                for r in rows}

    def set_later(self, item_id: str, until: float | None, on_change: bool, seen_sig: str | None) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO later (item_id, until, on_change, seen_sig, set_at, woke_at) "
                             "VALUES (?,?,?,?,?,NULL)", (item_id, until, 1 if on_change else 0, seen_sig, time.time()))

    def wake_later(self, item_id: str, at: float | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE later SET woke_at=? WHERE item_id=? AND woke_at IS NULL",
                             (time.time() if at is None else at, item_id))

    def clear_later(self, item_id: str) -> bool:
        with self._lock, self._db:
            return self._db.execute("DELETE FROM later WHERE item_id=?", (item_id,)).rowcount > 0

    def folded_sessions(self) -> dict[str, str]:
        """{Hermes session a linked task ran in: the chat it continues}. The history lists the chat once:
        its linked tasks' sessions (and their follow-ups, which run in the same session) fold into it."""
        with self._lock:
            rows = self._db.execute(
                "SELECT r.session_id, h.source_session FROM history_links h JOIN runs r ON r.idem_key = h.idem_key "
                "WHERE r.session_id IS NOT NULL").fetchall()
        return {r[0]: r[1] for r in rows if r[0] and r[1] and r[0] != r[1]}

    def question_rows(self, keys: list[str]) -> list[dict[str, Any]]:
        """For deciding which question (if any) a session still waits on: each task's status, when it
        was created, whether its question was set aside, and its question's text (None without one)."""
        if not keys:
            return []
        marks = ",".join("?" * len(keys))
        with self._lock:
            rows = self._db.execute(
                f"SELECT idem_key, status, COALESCE(created, updated), set_aside, dismissed, result_json, rowid, answered, "
                f"question_closed, session_id, settled_at, question_state FROM runs WHERE idem_key IN ({marks})",
                tuple(keys)).fetchall()
        out = []
        for key, status, created, aside, dismissed, result, rowid, answered, closed, session_id, settled, state in rows:
            state = _question_state(state, result, answered, aside, closed)
            aside = aside or answered or state in {"answered", "set_aside"}
            closed = state if state in {"dismissed", "moved_on", "expired", "superseded"} else None
            try:
                question = ((json.loads(result) or {}).get("question") or {}) if result else {}
            except (ValueError, AttributeError):
                question = {}
            text = question.get("question") if isinstance(question, dict) else None
            out.append({"key": key, "status": status, "created": float(created or 0), "rowid": rowid, "state": state,
                        "set_aside": bool(aside), "dismissed": bool(dismissed), "closed": closed or None,
                        "session_id": session_id, "settled": float(settled or created or 0),
                        "question": " ".join(text.split()) if isinstance(text, str) and text.strip() else None})
        return out

    def speakeasy_sessions(self) -> set[str]:
        """The Hermes sessions Speakeasy's tasks ran in."""
        with self._lock:
            rows = self._db.execute("SELECT DISTINCT session_id FROM runs WHERE session_id IS NOT NULL").fetchall()
        return {r[0] for r in rows if r[0]}

    def run_row(self, key: str) -> dict[str, Any] | None:
        """One task's bookkeeping row: what a message sent to it by id needs (messages.py)."""
        with self._lock:
            row = self._db.execute("SELECT delegation_id, revision, run_id, status, dismissed, session_id, "
                                   "interaction_id, settled_at FROM runs WHERE idem_key=?", (key,)).fetchone()
        if row is None:
            return None
        names = ("delegation_id", "revision", "run_id", "status", "dismissed", "session_id", "interaction_id", "settled_at")
        return dict(zip(names, row), idem_key=key)

    def newest_created(self, keys: list[str]) -> float | None:
        """When the newest of these tasks was created, or None."""
        if not keys:
            return None
        with self._lock:
            row = self._db.execute(f"SELECT max(created) FROM runs WHERE idem_key IN ({','.join('?' * len(keys))})",
                                   tuple(keys)).fetchone()
        return float(row[0]) if row and isinstance(row[0], (int, float)) else None

    def newest_in_job(self, key: str) -> str | None:
        """The newest task still shown of the same job (a follow-up replaces its parent's row), or None."""
        with self._lock:
            row = self._db.execute("SELECT session_id FROM runs WHERE idem_key=?", (key,)).fetchone()
            if not row or not row[0]:
                return None
            newer = self._db.execute("SELECT idem_key FROM runs WHERE session_id=? AND COALESCE(dismissed,0)=0 "
                                     "AND status!='rejected' ORDER BY rowid DESC LIMIT 1", (row[0],)).fetchone()
        return newer[0] if newer else None

    def title(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT title FROM runs WHERE idem_key=?", (key,)).fetchone()
        return row[0] if row and row[0] else None

    def set_title(self, key: str, title: str | None, summary: str | None = None, thread: bool = False) -> None:
        """A task's display title. ``thread`` marks a chat thread's own name: shown on the task, but
        never used to name its job. A task's own new title refines the job it first named."""
        with self._lock, self._db:
            self._set_title_locked(key, title, summary, thread)

    def _set_title_locked(self, key: str, title: str | None, summary: str | None, thread: bool) -> None:
        if title:
            title = fix_product_names(title)
            self._db.execute("UPDATE runs SET title=? WHERE idem_key=?", (title, key))
            name = None if thread else job_name(title)
            if name:
                self._db.execute("UPDATE runs SET own_title=? WHERE idem_key=?", (title, key))
                self._db.execute("UPDATE jobs SET name=?, updated=? WHERE from_key=?", (name, time.time(), key))
        if summary:
            self._db.execute("UPDATE runs SET summary=? WHERE idem_key=?", (summary, key))

    def _older_title(self, job_id: str, key: str) -> tuple[str, str] | None:
        """A task saved before tasks kept their own title: its shown title, unless it ran in a new
        chat thread (then the title may be the thread's name). Call with the lock held."""
        for idem, title, continued in self._db.execute(
                "SELECT idem_key, title, continued FROM runs WHERE (session_id=? OR idem_key=?) AND title IS NOT NULL "
                "ORDER BY rowid", (job_id, key)):
            if '"thread_id"' not in (continued or ""):
                return idem, title
        return None

    def link_job(self, key: str, parent: str | None) -> None:
        """Record a task's job: its parent's job (a follow-up), else its own (``parent`` None)."""
        # Stored as the parent's key and resolved when read: the parent's session may not be known yet.
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO job_links (idem_key, job_id, created) VALUES (?,?,?)",
                             (key, parent if parent and parent != key else key, time.time()))

    def has_job_link(self, key: str) -> bool:
        with self._lock:
            return self._db.execute("SELECT 1 FROM job_links WHERE idem_key=?", (key,)).fetchone() is not None

    def speakeasy_session(self, session_id: str | None) -> bool:
        """A Hermes session a Speakeasy task started itself (a job's own session), not an outside chat."""
        if not session_id:
            return False
        with self._lock:
            return self._db.execute("SELECT 1 FROM runs WHERE session_id=? AND continued IS NULL LIMIT 1",
                                    (session_id,)).fetchone() is not None

    def _job_id(self, key: str, session_id: str | None, continued: str | None) -> str:
        """A task's job id: a recorded link (link_job) wins; else its Hermes session (or the one it
        continued), unless that session is a chat the task was only placed into and the task was
        linked as its own job; else the task itself."""
        seen: set[str] = set()
        while key not in seen:
            seen.add(key)
            with self._lock:
                row = self._db.execute("SELECT job_id FROM job_links WHERE idem_key=?", (key,)).fetchone()
                if row is None:
                    return session_id or _placed_session(continued) or key
                if row[0] == key:
                    return key  # its own job
                parent = self._db.execute("SELECT session_id, continued FROM runs WHERE idem_key=?",
                                          (row[0],)).fetchone()
            if parent is None:
                return session_id or _placed_session(continued) or key
            key, session_id, continued = row[0], parent[0], parent[1]
        return key

    def queued_behind(self, key: str, session_id: str | None, status: str) -> str | None:
        """The title of the task this one waits behind in its chat thread: it hasn't started (no
        activity) and an older task in the same session is still working. None otherwise."""
        if status != "admitting" or not session_id:
            return None
        with self._lock:
            mine = self._db.execute("SELECT rowid FROM runs WHERE idem_key=?", (key,)).fetchone()
            started = self._db.execute("SELECT 1 FROM activity WHERE idem_key=?", (key,)).fetchone()
            if mine is None or started is not None:
                return None
            ahead = self._db.execute(
                f"SELECT title, summary FROM runs WHERE session_id=? AND rowid<? AND status IN "
                f"({','.join('?' * len(RS.IN_FLIGHT))}) AND COALESCE(dismissed,0)=0 "
                "ORDER BY rowid LIMIT 1", (session_id, mine[0], *sorted(RS.IN_FLIGHT))).fetchone()
        if ahead is None:
            return None
        return (ahead[0] or ahead[1] or "another task")[:120]

    def job(self, key: str, session_id: str | None, continued: str | None) -> dict[str, str]:
        """The job a task belongs to: {id, name}. The id is _job_id's; the name is set once, from the
        job's first task."""
        job_id = self._job_id(key, session_id, continued)
        with self._lock, self._db:
            row = self._db.execute("SELECT name FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            titled = self._db.execute(
                "SELECT idem_key, own_title FROM runs WHERE (session_id=? OR idem_key=? OR idem_key=?) "
                "AND own_title IS NOT NULL ORDER BY rowid", (job_id, job_id, key)).fetchall()
            # A job named by a follow-up that names nothing ("any luck?" -> "Check the earlier
            # task") takes the first real name one of its tasks gets.
            strong = next((t for t in titled if not weak_job_name(t[1])), None)
            if row:
                if strong and weak_job_name(row[0]) and job_name(strong[1]):
                    name = job_name(strong[1])
                    self._db.execute("UPDATE jobs SET name=?, from_key=?, updated=? WHERE job_id=?",
                                     (name, strong[0], time.time(), job_id))
                    return {"id": job_id, "name": name}
                return {"id": job_id, "name": row[0]}
            first = strong or (titled[0] if titled else None)
            if first is None:
                first = self._older_title(job_id, key)
            name = job_name(first[1]) if first else None
            if not name:
                # Not named yet (the titler is still working): named from the user's own words, not
                # stored. Never a generic stand-in.
                request = self._db.execute("SELECT text FROM work_events WHERE idem_key=? AND kind='request' "
                                           "ORDER BY seq LIMIT 1", (key,)).fetchone()
                asked = self._db.execute("SELECT text FROM asked_words WHERE idem_key=?", (key,)).fetchone()
                shown = fallback_title(asked[0] if asked else None, request[0] if request else None)
                return {"id": job_id, "name": job_name(shown) or shown}
            self._db.execute("INSERT OR IGNORE INTO jobs (job_id,name,from_key,updated) VALUES (?,?,?,?)",
                             (job_id, name, first[0], time.time()))
        return {"id": job_id, "name": name}

    def set_headline(self, key: str, headline: str) -> bool:
        """A finished result's outcome headline (the side model's), replacing the one taken from
        the reply's words. False when there's no stored result or nothing changed."""
        with self._lock, self._db:
            row = self._db.execute("SELECT result_json FROM runs WHERE idem_key=?", (key,)).fetchone()
            try:
                result = json.loads(row[0]) if row and row[0] else None
            except ValueError:
                result = None
            if not isinstance(result, dict) or result.get("headline") == headline:
                return False
            result["headline"] = headline
            self._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?", (json.dumps(result), key))
            return True

    def add_shape(self, key: str, shape: dict[str, Any]) -> bool:
        """Attach a result's structure to its stored result (kept beside spoken/full)."""
        with self._lock, self._db:
            row = self._db.execute("SELECT result_json FROM runs WHERE idem_key=?", (key,)).fetchone()
            try:
                result = json.loads(row[0]) if row and row[0] else None
            except ValueError:
                result = None
            if not isinstance(result, dict):
                return False
            result["shape"] = shape
            self._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?", (json.dumps(result), key))
            return True

    def add_view(self, key: str, view: dict[str, Any]) -> bool:
        """Add a derived view card to a stored result that has none of its own."""
        with self._lock, self._db:
            row = self._db.execute("SELECT result_json FROM runs WHERE idem_key=?", (key,)).fetchone()
            try:
                result = json.loads(row[0]) if row and row[0] else None
            except ValueError:
                result = None
            if not isinstance(result, dict) or result.get("views"):
                return False
            result["views"] = [view]
            self._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?", (json.dumps(result), key))
            return True

    def set_plan(self, key: str, steps: list[str]) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET plan=? WHERE idem_key=?", (json.dumps(steps[:5]), key))

    def plan(self, key: str) -> list[str]:
        with self._lock:
            row = self._db.execute("SELECT plan FROM runs WHERE idem_key=?", (key,)).fetchone()
        try:
            steps = json.loads(row[0]) if row and row[0] else []
        except ValueError:
            return []
        return [str(s) for s in steps][:5] if isinstance(steps, list) else []

    def dismiss(self, run_ids: list[str]) -> list[str]:
        """Hide finished tasks from the task list. Running tasks are never dismissed."""
        settled = sorted(RS.DISMISSABLE)  # unconfirmed runs can be cleared too
        marks = ",".join("?" * len(settled))
        done = []
        with self._lock, self._db:
            for run_id in run_ids:
                cursor = self._db.execute(
                    f"UPDATE runs SET dismissed=1 WHERE run_id=? AND status IN ({marks})", (run_id, *settled))
                if cursor.rowcount:
                    done.append(run_id)
        return done

    # -- shared files ------------------------------------------------------------------
    def add_shared_files(self, key: str, paths: list[str]) -> None:
        """Record the saved copies a task carried (screens, pictures and files shared with it)."""
        now = time.time()
        with self._lock, self._db:
            self._db.executemany("INSERT OR IGNORE INTO shared_files VALUES (?,?,?)",
                                 [(key, str(p), now) for p in paths if p])

    def shared_files(self, key: str) -> list[str]:
        with self._lock:
            rows = self._db.execute("SELECT path FROM shared_files WHERE idem_key=? ORDER BY created", (key,)).fetchall()
        return [r[0] for r in rows]

    def forget_shared_files(self, keys: list[str]) -> list[str]:
        """These tasks were cleared: drop their records and return the copies they named (the
        caller deletes the ones nothing else still uses)."""
        if not keys:
            return []
        marks = ",".join("?" * len(keys))
        with self._lock, self._db:
            rows = self._db.execute(f"SELECT DISTINCT path FROM shared_files WHERE idem_key IN ({marks})",
                                    keys).fetchall()
            self._db.execute(f"DELETE FROM shared_files WHERE idem_key IN ({marks})", keys)
        return [r[0] for r in rows]

    def shared_paths_in_use(self, paths: set[str]) -> set[str]:
        """Which of these copies a task still records."""
        if not paths:
            return set()
        wanted = sorted(paths)
        marks = ",".join("?" * len(wanted))
        with self._lock:
            rows = self._db.execute(f"SELECT DISTINCT path FROM shared_files WHERE path IN ({marks})",
                                    wanted).fetchall()
        return {r[0] for r in rows}

    def prune_shared_files(self, before: float) -> int:
        """Forget records older than ``before`` (their copies are pruned on the same schedule)."""
        with self._lock, self._db:
            return self._db.execute("DELETE FROM shared_files WHERE created < ?", (before,)).rowcount

    def add_shared(self, key: str, items: list[dict[str, Any]]) -> None:
        """What the user shared with this task, for its card: ``{kind, app, name, path, at}`` per
        screen, picture or file. Paths stay on the server (``shared_items``); the app gets
        ``work()["shared"]`` and fetches images by position (``shared_image``), so the first MAX_SHARED
        are kept and a later one never shifts them (its saved copy is still recorded and cleared with
        the task: ``add_shared_files``)."""
        clean = [{k: item.get(k) for k in ("kind", "app", "name", "path", "at")} for item in items
                 if item.get("kind") in SHARED_KINDS]
        if not clean:
            return
        with self._lock, self._db:
            row = self._db.execute("SELECT shared FROM runs WHERE idem_key=?", (key,)).fetchone()
            if row is None:
                return
            known = self._shared_list(row[0])
            self._db.execute("UPDATE runs SET shared=? WHERE idem_key=?",
                             (json.dumps((known + clean)[:MAX_SHARED]), key))

    @staticmethod
    def _shared_list(raw: str | None) -> list[dict[str, Any]]:
        try:
            items = json.loads(raw) if raw else []
        except ValueError:
            return []
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    def shared_items(self, key: str) -> list[dict[str, Any]]:
        """Server-side view of what was shared with a task, paths included."""
        with self._lock:
            row = self._db.execute("SELECT shared FROM runs WHERE idem_key=?", (key,)).fetchone()
        return self._shared_list(row[0] if row else None)

    def shared_image(self, run_id: str, number: int) -> str | None:
        """The saved copy of the ``number``-th thing (1-based) shared with this run, when it is an image."""
        with self._lock:
            row = self._db.execute("SELECT shared FROM runs WHERE run_id=?", (run_id,)).fetchone()
        items = self._shared_list(row[0] if row else None)
        if not 1 <= number <= len(items):
            return None
        item = items[number - 1]
        path = item.get("path")
        return path if item.get("kind") in {"screen", "picture"} and isinstance(path, str) else None

    def last_screen_at(self, session_id: str | None) -> float | None:
        """When a screenshot last went into this Hermes session (it stays in the session's history and
        is sent again with every later run there), or None."""
        if not session_id:
            return None
        with self._lock:
            rows = self._db.execute("SELECT shared FROM runs WHERE session_id=? AND shared LIKE '%\"screen\"%'",
                                    (session_id,)).fetchall()
        times = [float(i.get("at") or 0) for (raw,) in rows for i in self._shared_list(raw)
                 if i.get("kind") == "screen" and i.get("at")]
        return max(times) if times else None

    def hide_key(self, key: str) -> None:
        """Keep a row out of the task list regardless of status (a tail waiting to join its task)."""
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET dismissed=1 WHERE idem_key=?", (key,))

    def unhide_key(self, key: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET dismissed=NULL WHERE idem_key=?", (key,))

    def dismiss_key(self, key: str) -> bool:
        settled = sorted(RS.DISMISSABLE)  # unconfirmed runs can be cleared too
        marks = ",".join("?" * len(settled))
        with self._lock, self._db:
            cursor = self._db.execute(
                f"UPDATE runs SET dismissed=1 WHERE idem_key=? AND status IN ({marks})", (key, *settled))
        return bool(cursor.rowcount)

    def set_aside(self, key: str) -> bool:
        """The user set this task's question (or approval) aside: already handled, or not needed.
        It settles as no longer needed, never as failed, and stops waiting on them. Only a settled
        task (finished, or waiting for approval) has anything to set aside."""
        settled = sorted(SETTLED | RS.FINAL)
        marks = ",".join("?" * len(settled))
        now = time.time()
        with self._lock, self._db:
            cursor = self._db.execute(
                f"UPDATE runs SET set_aside=COALESCE(set_aside, ?) WHERE idem_key=? AND status IN ({marks})",
                (now, key, *settled))
            closed = self._close_locked(key, "set_aside", now) if cursor.rowcount else []
        self._closed(closed)
        return bool(cursor.rowcount)

    def mark_answered(self, key: str, answer: str | None = None) -> bool:
        """The user answered this task's question (a tapped option, a typed or spoken reply): it waits on
        nobody any more, on every device (the board, the panel's Needs you, the session page). True when
        it had an open question. *answer*: the words, so every device shows which option was taken."""
        return bool(self.close_question(key, "answered", answer=answer))

    def close_question(self, key: str, reason: str, answer: str | None = None) -> list[str]:
        """The one way a question closes (``reason`` in QUESTION_CLOSES). Returns the idem keys whose
        question closed (this task's, plus the same question asked again by another task of the
        conversation for an answer, a set-aside or a dismiss); empty when it wasn't open.

        ``answered`` also closes a question already closed some other way (a tap on a card that had
        just moved on is still the user's answer); nothing closes an answered one again."""
        if reason not in QUESTION_CLOSES:
            raise ValueError(reason)
        with self._lock, self._db:
            closed = self._close_locked(key, reason, time.time())
            if closed and reason == "answered" and isinstance(answer, str) and answer.strip():
                for k in closed:
                    self._db.execute("UPDATE runs SET question_answer=? WHERE idem_key=?",
                                     (" ".join(answer.split())[:500], k))
        self._closed(closed)
        return closed

    # Told (outside the lock, after the write committed) which tasks' questions just closed, by every
    # close path (questions.wire: decision_resolved to every device + the Needs you push withdrawn).
    on_question_closed: Any = None

    def _closed(self, keys: list[str]) -> None:
        hook = self.on_question_closed
        if keys and hook is not None:
            try:
                hook(list(keys))
            except Exception:
                logger.warning("speakeasy: question-closed hook failed", exc_info=True)

    def _close_locked(self, key: str, reason: str, now: float) -> list[str]:
        row = self._db.execute("SELECT result_json, question_state, answered, set_aside, question_closed, "
                               "session_id, interaction_id FROM runs WHERE idem_key=?", (key,)).fetchone()
        asked = _question_text(row[0]) if row else None
        if not asked:
            return []
        state = _question_state(row[1], row[0], row[2], row[3], row[4])
        if not self._may_close(state, reason):
            return []
        self._write_close(key, reason, now)
        closed = [key]
        if reason in _TWIN_CLOSES:
            for other, result, o_state, o_answered, o_aside, o_closed in self._db.execute(
                    "SELECT idem_key, result_json, question_state, answered, set_aside, question_closed FROM runs "
                    "WHERE idem_key != ? AND result_json IS NOT NULL AND "
                    "((? IS NOT NULL AND session_id = ?) OR interaction_id = ?)",
                    (key, row[5], row[5], row[6])).fetchall():
                if _question_text(result) != asked:
                    continue
                if self._may_close(_question_state(o_state, result, o_answered, o_aside, o_closed), reason):
                    self._write_close(other, reason, now)
                    closed.append(other)
        return closed

    @staticmethod
    def _may_close(state: str | None, reason: str) -> bool:
        if state is None or state == "answered":
            return False
        return state == "open" or reason == "answered"

    def _write_close(self, key: str, reason: str, now: float) -> None:
        """Lock held. The state, plus the columns older apps read (answered / set_aside /
        question_closed), so a card closes there too."""
        self._db.execute("UPDATE runs SET question_state=?, question_state_at=? WHERE idem_key=?", (reason, now, key))
        if reason == "answered":
            # An answer after a dismiss (✕, then the user typed the answer anyway) wins outright: the
            # older column must not keep saying "dismissed", or Home keeps the card on "Paused" (SPK-303).
            self._db.execute("UPDATE runs SET answered=COALESCE(answered, ?), question_closed=NULL, "
                             "question_closed_at=NULL WHERE idem_key=?", (now, key))
        elif reason == "set_aside":
            self._db.execute("UPDATE runs SET set_aside=COALESCE(set_aside, ?) WHERE idem_key=?", (now, key))
        else:
            self._db.execute("UPDATE runs SET question_closed=?, question_closed_at=? WHERE idem_key=?",
                             (reason, now, key))

    def reopen_question(self, key: str) -> bool:
        """Undo a dismiss (only a dismiss: an answer or a moved-on conversation stays closed). Its twins
        (the same question on another task, closed in the same moment) reopen with it."""
        with self._lock, self._db:
            row = self._db.execute("SELECT question_closed, question_closed_at, answered, question_state "
                                   "FROM runs WHERE idem_key=?", (key,)).fetchone()
            if not row or row[2] or (row[3] or row[0]) != "dismissed":
                return False
            now = time.time()
            for (other,) in self._db.execute(
                    "SELECT idem_key FROM runs WHERE question_closed='dismissed' AND answered IS NULL AND "
                    "(idem_key=? OR question_closed_at=?)", (key, row[1])).fetchall():
                self._db.execute("UPDATE runs SET question_closed=NULL, question_closed_at=NULL, "
                                 "question_state='open', question_state_at=? WHERE idem_key=?", (now, other))
        return True

    def open_question_candidates(self, since: float = 0.0) -> list[str]:
        """Tasks whose question is still open (oldest first). *since* is kept for callers that only
        want recent ones; the sweep passes 0 so a day-old question can close as expired."""
        with self._lock:
            rows = self._db.execute(
                "SELECT idem_key, result_json, question_state, answered, set_aside, question_closed FROM runs "
                "WHERE COALESCE(created, updated) >= ? AND (question_state='open' OR (question_state IS NULL AND "
                "answered IS NULL AND set_aside IS NULL AND question_closed IS NULL)) "
                "AND result_json LIKE '%\"question\"%' ORDER BY COALESCE(created, updated), rowid",
                (since,)).fetchall()
        return [key for key, result, *rest in rows if _question_state(rest[0], result, *rest[1:]) == "open"]

    def question_state(self, key: str) -> str | None:
        """This task's question state (QUESTION_STATES), None when it asks nothing."""
        with self._lock:
            row = self._db.execute("SELECT question_state, result_json, answered, set_aside, question_closed "
                                   "FROM runs WHERE idem_key=?", (key,)).fetchone()
        return _question_state(*row) if row else None

    def question_answer(self, key: str) -> str | None:
        """The words the user answered this task's question with, when the server saw them."""
        with self._lock:
            row = self._db.execute("SELECT question_answer FROM runs WHERE idem_key=?", (key,)).fetchone()
        return row[0] if row and row[0] else None

    def question_closed(self, key: str) -> str | None:
        """How this task's question closed unanswered (dismissed / moved_on / expired / superseded),
        else None (open, answered, set aside, or no question)."""
        state = self.question_state(key)
        return state if state in {"dismissed", "moved_on", "expired", "superseded"} else None

    def mark_seen(self, key: str) -> None:
        """Nothing in this task's answer the user needs to read (a home command that went through):
        apps leave it out of New answers. Opening it still shows the answer."""
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET seen=1 WHERE idem_key=?", (key,))

    def is_seen(self, key: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT seen FROM runs WHERE idem_key=?", (key,)).fetchone()
        return bool(row and row[0])

    def is_set_aside(self, key: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT set_aside FROM runs WHERE idem_key=?", (key,)).fetchone()
        return bool(row and row[0])

    def set_timing(self, key: str, name: str, ms: int) -> None:
        """Internal per-task timings (e.g. routing latency), kept for diagnostics, never spoken."""
        with self._lock, self._db:
            row = self._db.execute("SELECT timings FROM runs WHERE idem_key=?", (key,)).fetchone()
            if row is None:
                return
            try:
                timings = json.loads(row[0] or "{}")
            except ValueError:
                timings = {}
            timings[name] = int(ms)
            self._db.execute("UPDATE runs SET timings=? WHERE idem_key=?", (json.dumps(timings), key))

    def timings(self, key: str) -> dict[str, int]:
        with self._lock:
            row = self._db.execute("SELECT timings FROM runs WHERE idem_key=?", (key,)).fetchone()
        try:
            return json.loads(row[0]) if row and row[0] else {}
        except ValueError:
            return {}

    def forget_run_id(self, key: str) -> None:
        """Drop a run id Hermes refused at the door, so nothing polls a run that never ran."""
        with self._lock, self._db:
            row = self._db.execute("SELECT run_id FROM runs WHERE idem_key=?", (key,)).fetchone()
            if row and row[0]:
                # It may already have been handed out (a typed follow-up's reply): keep it openable.
                self._db.execute("INSERT OR IGNORE INTO run_aliases (alias, idem_key, created) VALUES (?,?,?)",
                                 (row[0], key, time.time()))
            self._db.execute("UPDATE runs SET run_id=NULL WHERE idem_key=?", (key,))

    def update_run(self, key: str, run_id: str | None, status: str) -> bool:
        """A checked status change (``transition`` with the allowed states for *status*)."""
        return self.transition(key, status, run_id=run_id)

    def run_status(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT status FROM runs WHERE idem_key=?", (key,)).fetchone()
        return row[0] if row else None

    def settle(self, key: str, status: str, *, result: dict[str, Any] | None,
               failure: tuple[str | None, str | None] = (None, None),
               drafts: Iterable[dict[str, Any]] = (), run_id: str | None = None) -> bool:
        """Commit the entire outcome before listeners (or another database connection) see it.

        ``settled_at`` is the committed finish marker. Repeated final events cannot replace the
        payload or supersede drafts. Draft insertion failure rolls back the status and marker too.
        """
        if status not in TERMINAL:
            raise ValueError("Only a terminal outcome can be settled")
        return self.transition(key, status, result=result, failure=failure, drafts=drafts, run_id=run_id)

    def transition(self, key: str, to: str, expect: Iterable[str] | None = None, *, run_id: str | None = None,
                   mirror: Any = None, orphaned: bool = False, result: Any = _UNSET,
                   failure: Any = _UNSET, drafts: Iterable[dict[str, Any]] = ()) -> bool:
        """Move a run to *to* only from one of the *expect* states (default: every state *to* may be
        entered from, runstate.ALLOWED_FROM) — a compare-and-set, like ``transition_draft``.

        True when the row moved. *mirror* (the in-memory copy, anything with ``.status``) is set
        under the same lock when it moved, and set to the stored status when that is final and the
        change was refused, so memory never drifts from the row. A move to a final state stamps a
        run id, fires the settle listeners and settles the task's activity and helpers (one
        ``run.<outcome>`` event): every path that ends a task — its own events, Stop, the sweeps —
        stops the card spinning the same way. *result*: the answer, stored in the same write, so
        no reader ever sees a finished task without it (and a refused write never replaces it).
        *orphaned*: settled by a sweep; helpers stop too (nothing follows them any more)."""
        to = RunState(to).value
        allowed = sorted(RS.allowed_from(to) if expect is None else {RunState(s).value for s in expect})
        if not allowed:
            return False
        now = time.time()
        snapshot = self._context_snapshot(key) if to in TERMINAL else None
        marks = ",".join("?" * len(allowed))
        unused: tuple[list[str], list[str]] = ([], [])
        with self._lock, self._db:
            moved = self._db.execute(
                f"UPDATE runs SET run_id=COALESCE(?,run_id),status=?,updated=? WHERE idem_key=? AND status IN ({marks})",
                (run_id, to, now, key, *allowed)).rowcount == 1
            if moved:
                if result is not _UNSET:
                    self._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?",
                                     (json.dumps(result) if result else None, key))
                if failure is not _UNSET:
                    self._db.execute("UPDATE runs SET failure=?,failure_hint=? WHERE idem_key=?", (*failure, key))
                session = self._db.execute("SELECT session_id FROM runs WHERE idem_key=?", (key,)).fetchone()
                for draft in drafts:
                    self._add_draft_locked(key, session[0], draft, now)
                if to in FINAL:
                    # Every finished task needs a run id so "Clear done" and the x can clear it.
                    self._db.execute("UPDATE runs SET run_id=? WHERE idem_key=? AND run_id IS NULL",
                                     (local_run_id(key), key))
                if to in SETTLED:
                    self._db.execute("UPDATE runs SET settled_at=? WHERE idem_key=?", (now, key))
                    if snapshot is not None:
                        # Frozen as it finished: a later task in the same chat must not change this one's figure.
                        self._db.execute("UPDATE runs SET context=? WHERE idem_key=? AND context IS NULL",
                                         (json.dumps(snapshot), key))
                if mirror is not None:
                    mirror.status = to
                    if run_id is not None and hasattr(mirror, "run_id"):
                        mirror.run_id = run_id
                if to in UNUSED_ON:
                    unused = self._settle_unused(key, now)
            else:
                row = self._db.execute("SELECT status FROM runs WHERE idem_key=?", (key,)).fetchone()
                if row is not None and run_id is not None:
                    # Hermes' run id is a fact, whatever the status: a Stop that got in before the
                    # run.started event still needs it to reach the run.
                    self._db.execute("UPDATE runs SET run_id=? WHERE idem_key=? AND run_id IS NULL", (run_id, key))
                    if mirror is not None and getattr(mirror, "run_id", "") is None:
                        mirror.run_id = run_id
                if row is not None:
                    logger.info("speakeasy: run status %s -> %s refused (not from %s)", row[0], to, ",".join(allowed))
                    if mirror is not None and row[0] in FINAL:
                        mirror.status = row[0]
        if moved and (unused[0] or unused[1]):
            for listener in list(self.unused_listeners):
                try:
                    listener(key, *unused)
                except Exception:  # a listener never breaks a status write
                    pass
        if moved and to in TERMINAL:
            for listener in list(self.settle_listeners):
                try:
                    listener(key, to)
                except Exception:  # a listener never breaks a status write
                    pass
            try:
                activity_mod.settle(self, key, to, orphaned=orphaned)
            except Exception:
                logger.debug("speakeasy: activity not settled", exc_info=True)
        return moved

    def _context_record_since(self, session_id: str | None, since: float | None) -> dict[str, Any] | None:
        """The session's newest model call (context_use) if it happened after ``since`` (the task's start)."""
        from . import context_use as CU
        if not session_id:
            return None
        rec = CU.latest(session_id) or self.context_record(session_id)
        if (rec is None or (since and (rec.get("at") or 0) < since - 1)) and self.hermes_home:
            # Hermes compressed the conversation mid-task: the work goes on in a continuation session.
            tip = self._compression_tip(session_id)
            if tip != session_id:
                rec = CU.latest(tip) or self.context_record(tip)
        if rec is None or (since and (rec.get("at") or 0) < since - 1):
            return None
        return rec

    def _compression_tip(self, session_id: str) -> str:
        """The newest compression continuation of a Hermes session (cached 30 s; state.db read-only)."""
        cache = self.__dict__.setdefault("_tip_cache", {})
        hit = cache.get(session_id)
        if hit and time.time() - hit[1] < 30:
            return hit[0]
        from . import history as H
        tip = session_id
        db = H._open(Path(self.hermes_home) / "state.db") if self.hermes_home else None
        if db is not None:
            try:
                tip = H._tips(db, {session_id}).get(session_id, session_id)
            except Exception:
                tip = session_id
            finally:
                db.close()
        if len(cache) > 500:
            cache.clear()
        cache[session_id] = (tip, time.time())
        return tip

    def _context_snapshot(self, key: str) -> dict[str, Any] | None:
        try:
            with self._lock:
                row = self._db.execute("SELECT session_id, created FROM runs WHERE idem_key=?", (key,)).fetchone()
            return self._context_record_since(row[0], row[1]) if row else None
        except Exception:
            return None

    def run_context(self, key: str, session_id: str | None, status: str, created: float | None) -> dict[str, Any] | None:
        """The context record for one task: frozen when it finished, live while it runs."""
        if status in TERMINAL:
            with self._lock:
                row = self._db.execute("SELECT context FROM runs WHERE idem_key=?", (key,)).fetchone()
            try:
                value = json.loads(row[0]) if row and row[0] else None
            except ValueError:
                value = None
            return value if isinstance(value, dict) else None
        return self._context_record_since(session_id, created)
    def open_runs(self) -> list[dict[str, Any]]:
        """Every run that is not final (runstate.OPEN), for the orphan sweep."""
        states = sorted(RS.OPEN)
        with self._lock:
            rows = self._db.execute(
                f"SELECT idem_key, run_id, status, updated, created, continued FROM runs "
                f"WHERE status IN ({','.join('?' * len(states))})", states).fetchall()
        out = []
        for key, run_id, status, updated, created, continued in rows:
            try:
                placed = json.loads(continued) if continued else {}
            except ValueError:
                placed = {}
            placed = placed if isinstance(placed, dict) else {}
            out.append({"key": key, "run_id": run_id, "status": status, "updated": float(updated or 0),
                        "created": float(created) if created else None,
                        "thread": bool(placed.get("thread_id") or placed.get("platform"))})
        return out

    # -- queued messages ("Send when it's done") ---------------------------------------------------
    _QUEUED_COLS = "client_id,kind,target,page_session,watch_key,text,state,created,updated,reply_json,error"

    def _queued_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        out: dict[str, Any] = dict(zip(self._QUEUED_COLS.split(","), row))
        out["reply"] = json.loads(out.pop("reply_json")) if out.get("reply_json") else None
        return out

    def queue_message(self, client_id: str, kind: str, target: str, page_session: str | None,
                      watch_key: str, text: str) -> tuple[dict[str, Any], bool]:
        """Hold one message (idempotent on client_id): (its row, whether this call added it)."""
        now = time.time()
        with self._lock, self._db:
            added = self._db.execute(
                f"INSERT OR IGNORE INTO queued_messages ({self._QUEUED_COLS}) VALUES (?,?,?,?,?,?,'queued',?,?,NULL,NULL)",
                (client_id, kind, target, page_session, watch_key, text, now, now)).rowcount == 1
            row = self._db.execute(f"SELECT {self._QUEUED_COLS} FROM queued_messages WHERE client_id=?",
                                   (client_id,)).fetchone()
        return self._queued_dict(row), added

    def queued_message(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(f"SELECT {self._QUEUED_COLS} FROM queued_messages WHERE client_id=?",
                                   (client_id,)).fetchone()
        return self._queued_dict(row) if row else None

    def queued_messages(self, watch_keys: list[str] | None = None, page_session: str | None = None,
                        states: tuple[str, ...] = ("queued",)) -> list[dict[str, Any]]:
        """Held messages, oldest first: those waiting on any of *watch_keys* or listed on *page_session*
        (both None: every one)."""
        where, args = [f"state IN ({','.join('?' * len(states))})"], list(states)
        if watch_keys is not None or page_session is not None:
            keys = list(watch_keys or [])
            any_of = []
            if keys:
                any_of.append(f"watch_key IN ({','.join('?' * len(keys))})")
                args += keys
            if page_session is not None:
                any_of.append("page_session=?")
                args.append(page_session)
            if not any_of:
                return []
            where.append("(" + " OR ".join(any_of) + ")")
        with self._lock:
            rows = self._db.execute(f"SELECT {self._QUEUED_COLS} FROM queued_messages WHERE {' AND '.join(where)} "
                                    "ORDER BY created, rowid", args).fetchall()
        return [self._queued_dict(r) for r in rows]

    def queue_position(self, client_id: str) -> int | None:
        """1 = sent next, among the messages waiting on the same task; None once it's no longer held."""
        row = self.queued_message(client_id)
        if row is None or row["state"] != "queued":
            return None
        ahead = [r["client_id"] for r in self.queued_messages([row["watch_key"]])]
        return ahead.index(client_id) + 1 if client_id in ahead else None

    def claim_queued(self, client_id: str, unused: bool = False) -> bool:
        """queued -> sending, exactly once (a drain and a Steer now never both send it). *unused*: a
        held message its stopped task never sent may be claimed too (Steer now)."""
        states = "('queued','unused')" if unused else "('queued')"
        with self._lock, self._db:
            return self._db.execute("UPDATE queued_messages SET state='sending', updated=? "
                                    f"WHERE client_id=? AND state IN {states}", (time.time(), client_id)).rowcount == 1

    def finish_queued(self, client_id: str, reply: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE queued_messages SET state='sent', reply_json=?, error=NULL, updated=? "
                             "WHERE client_id=?", (json.dumps(reply), time.time(), client_id))

    def release_queued(self, client_id: str, error: str) -> None:
        """A send that failed: back in the queue (tried again on the next settle or idle check)."""
        with self._lock, self._db:
            self._db.execute("UPDATE queued_messages SET state='queued', error=?, updated=? "
                             "WHERE client_id=? AND state='sending'", (error, time.time(), client_id))

    def cancel_queued(self, client_id: str) -> str | None:
        """Cancel a held message: its state after (cancelled), or what it already was (sending/sent);
        None when there is no such message."""
        with self._lock, self._db:
            row = self._db.execute("SELECT state FROM queued_messages WHERE client_id=?", (client_id,)).fetchone()
            if row is None:
                return None
            if row[0] not in {"queued", "unused"}:
                return row[0]
            self._db.execute("UPDATE queued_messages SET state='cancelled', updated=? WHERE client_id=?",
                             (time.time(), client_id))
            return "cancelled"

    def rewatch_queued(self, old_key: str, new_key: str) -> None:
        """The task the rest of a queue waits on became *new_key* (the turn the first one started)."""
        with self._lock, self._db:
            self._db.execute("UPDATE queued_messages SET watch_key=?, updated=? WHERE watch_key=? AND state='queued'",
                             (new_key, time.time(), old_key))

    def _settle_unused(self, key: str, now: float) -> tuple[list[str], list[str]]:
        """A stopped or interrupted task (UNUSED_ON): its held messages and the steers it never read
        are unused, in the caller's transaction (under the lock). Never sent on their own after that."""
        queued = [r[0] for r in self._db.execute(
            "SELECT client_id FROM queued_messages WHERE watch_key=? AND state='queued'", (key,)).fetchall()]
        steered = [r[0] for r in self._db.execute(
            "SELECT client_id FROM steered_messages WHERE idem_key=? AND state='sent'", (key,)).fetchall()]
        if queued:
            self._db.execute("UPDATE queued_messages SET state='unused', updated=? WHERE watch_key=? AND state='queued'",
                             (now, key))
        if steered:
            self._db.execute("UPDATE steered_messages SET state='unused', updated=? WHERE idem_key=? AND state='sent'",
                             (now, key))
        return queued, steered

    # -- message receipts (SPK-80: one per client_id, across restarts) ---------------------------
    _RECEIPT_COLS = "client_id,receipt_id,route,page_session,said,state,delivered_as,reply_json,boot,created,updated"

    def _receipt_dict(self, row: tuple[Any, ...]) -> dict[str, Any]:
        out: dict[str, Any] = dict(zip(self._RECEIPT_COLS.split(","), row))
        try:
            out["reply"] = json.loads(out.pop("reply_json")) if out.get("reply_json") else None
        except ValueError:
            out["reply"] = None
        out["stale"] = out["state"] == "sending" and out["boot"] != self.boot
        return out

    def receipt(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(f"SELECT {self._RECEIPT_COLS} FROM message_receipts WHERE client_id=?",
                                   (client_id,)).fetchone()
        return self._receipt_dict(row) if row else None

    def begin_receipt(self, client_id: str, route: str, page_session: str | None = None,
                      said: str | None = None, state: str = "sending") -> tuple[dict[str, Any], bool]:
        """Claim a client_id for one send (*state* sending) or one held message (queued): (its row,
        whether this call owns the send). A held one moves queued -> sending here, once."""
        now = time.time()
        with self._lock, self._db:
            mine = self._db.execute(
                f"INSERT OR IGNORE INTO message_receipts ({self._RECEIPT_COLS}) VALUES (?,?,?,?,?,?,NULL,NULL,?,?,?)",
                (client_id, "rcpt_" + secrets.token_hex(8), route, page_session, said, state, self.boot,
                 now, now)).rowcount == 1
            if not mine and state == "sending":
                mine = self._db.execute("UPDATE message_receipts SET state='sending', boot=?, updated=? "
                                        "WHERE client_id=? AND state='queued'",
                                        (self.boot, now, client_id)).rowcount == 1
            row = self._db.execute(f"SELECT {self._RECEIPT_COLS} FROM message_receipts WHERE client_id=?",
                                   (client_id,)).fetchone()
        return self._receipt_dict(row), mine

    def finish_receipt(self, client_id: str, reply: dict[str, Any], page_session: str | None = None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE message_receipts SET state='done', delivered_as=?, reply_json=?, "
                             "page_session=COALESCE(?,page_session), updated=? WHERE client_id=?",
                             (reply.get("delivered_as"), json.dumps(reply), page_session, time.time(), client_id))

    def resolve_receipt(self, client_id: str, delivered_as: str) -> bool:
        """A send answered as pending learned where it went (steer / follow_up / new_run)."""
        with self._lock, self._db:
            row = self._db.execute("SELECT reply_json FROM message_receipts WHERE client_id=? AND state='done' "
                                   "AND delivered_as='pending'", (client_id,)).fetchone()
            if row is None:
                return False
            try:
                reply = json.loads(row[0]) if row[0] else {}
            except ValueError:
                reply = {}
            reply["delivered_as"] = delivered_as
            self._db.execute("UPDATE message_receipts SET delivered_as=?, reply_json=?, updated=? WHERE client_id=?",
                             (delivered_as, json.dumps(reply), time.time(), client_id))
        return True

    def drop_receipt(self, client_id: str, back_to: str | None = None) -> None:
        """A send that failed: forgotten (the same client_id may be tried again), or a held one back
        in the queue (*back_to* = queued)."""
        with self._lock, self._db:
            if back_to:
                self._db.execute("UPDATE message_receipts SET state=?, updated=? WHERE client_id=? AND state!='done'",
                                 (back_to, time.time(), client_id))
            else:
                self._db.execute("DELETE FROM message_receipts WHERE client_id=? AND state!='done'", (client_id,))

    def set_failure(self, key: str, kind: str | None, hint: str | None = None) -> None:
        """Why the run failed, as a ``failures`` kind (the app and voice get Speakeasy's own words),
        plus, when no kind fits, the coarse shape of the error (``failures.failure_hint``)."""
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET failure=?, failure_hint=? WHERE idem_key=?", (kind, hint, key))

    # -- notices / meta ----------------------------------------------------------------
    def claim_notice(self, dedupe_key: str) -> bool:
        """True exactly once per key, across restarts."""
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO notices (dedupe_key,created) VALUES (?,?)", (dedupe_key, time.time()))
            return cursor.rowcount == 1

    def notice_outcome(self, dedupe_key: str, outcome: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE notices SET outcome=? WHERE dedupe_key=?", (outcome, dedupe_key))

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", (key, value))

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def mark_call_ended(self, at: float | None = None) -> None:
        self.set_meta("last_call_end", repr(at if at is not None else time.time()))

    # -- work events -------------------------------------------------------------------
    def request_text(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT text FROM work_events WHERE idem_key=? AND kind='request' ORDER BY seq LIMIT 1", (key,)).fetchone()
        return row[0] if row else None

    def set_asked(self, key: str, text: str | None) -> None:
        """Record the user's own words for a task whose request is a brief (a typed Home message)."""
        words = " ".join(str(text or "").split())[:400]
        if not words:
            return
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO asked_words (idem_key,text,created) VALUES (?,?,?)",
                             (key, words, time.time()))

    def asked_text(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT text FROM asked_words WHERE idem_key=?", (key,)).fetchone()
        return row[0] if row else None

    def shown_title(self, key: str) -> str:
        """The name a task shows anywhere (a notification, the Live Activity): its title, else one
        from the user's own words. Never a generic stand-in."""
        title = self.title(key)
        if title and title not in PLACEHOLDER_TITLES:
            return title
        return fallback_title(self.asked_text(key), self.request_text(key))

    # -- corrections (corrections.py) -------------------------------------------------------
    def corrections(self, key: str) -> list[dict[str, Any]]:
        """Every correction made to this task's words, oldest first: {"wrong", "right", "at"}."""
        try:
            items = json.loads(self.get_meta("corrections:" + key) or "[]")
        except ValueError:
            return []
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []

    def correct_request(self, key: str, request: str, wrong: str | None, right: str | None) -> str | None:
        """A correction rewrites what the task was asked (its request and, where it holds the misheard
        words, its title) and clears the status lines written around those words. Returns the new
        title when it changed."""
        from . import corrections as corrections_mod
        now = time.time()
        title = self.title(key)
        with self._lock, self._db:
            row = self._db.execute("SELECT seq FROM work_events WHERE idem_key=? AND kind='request' ORDER BY seq LIMIT 1",
                                   (key,)).fetchone()
            if row and request:
                self._db.execute("UPDATE work_events SET text=? WHERE seq=?", (re.sub(r"\s+", " ", request).strip(), row[0]))
            got = self._db.execute("SELECT value FROM meta WHERE key=?", ("corrections:" + key,)).fetchone()
            try:
                items = json.loads(got[0]) if got else []
            except ValueError:
                items = []
            items = (items if isinstance(items, list) else [])[-9:] + [{"wrong": wrong, "right": right, "at": now}]
            self._db.execute("INSERT OR REPLACE INTO meta (key,value) VALUES (?,?)", ("corrections:" + key, json.dumps(items)))
            detail = safe_user_text(f"Now working with {right}" if right else "Dropping the misheard words", 500)
            self._db.execute(
                """UPDATE runs SET short_status=?, detail=?, status_source='system',
                   authored_at=NULL, progress_updated=?, updated=? WHERE idem_key=? AND status NOT IN
                   ('completed','failed','cancelled','interrupted','ambiguous','rejected')""",
                (corrections_mod.CORRECTING, detail, now, now, key))
        summarizer = getattr(self, "live_summarizer", None)
        forget = getattr(summarizer, "forget", None)
        if callable(forget):
            forget(key)  # its last line was written from the misheard request
        new_title = None
        if title and wrong and right:
            fixed = corrections_mod.rewrite(title, wrong, right)
            if fixed != title:
                self.set_title(key, fixed)
                new_title = fixed
        self.progress(key, "milestone", f"You corrected it: {right}" if right else "You corrected a misheard word")
        return new_title

    def _drop_misheard(self, work: dict[str, Any], key: str) -> None:
        """Status lines that still say the misheard words aren't shown (or spoken) after a correction."""
        fixes = self.corrections(key)
        if not fixes:
            return
        from . import corrections as corrections_mod
        wrongs = [f["wrong"] for f in fixes if f.get("wrong")]
        right = next((f["right"] for f in reversed(fixes) if f.get("right")), None)
        for field in ("now_line", "short_status"):
            if corrections_mod.mentions(work.get(field), wrongs):
                work[field] = corrections_mod.CORRECTING
        for field in ("so_far", "detail"):
            if corrections_mod.mentions(work.get(field), wrongs):
                work[field] = f"Now working with {right}" if right else None
        if corrections_mod.mentions(work.get("title"), wrongs) and right:
            work["title"] = corrections_mod.rewrite(work["title"], wrongs[-1], right)

    def told(self, key: str, limit: int = 600) -> str:
        """What the task has told the user so far (progress updates and result), newest last, capped."""
        with self._lock:
            rows = self._db.execute(
                "SELECT text FROM work_events WHERE idem_key=? AND kind IN ('milestone','result') "
                "ORDER BY seq DESC LIMIT 8", (key,)).fetchall()
        text = " ".join(" ".join(r[0].split()) for r in reversed(rows))
        return text[-limit:]

    def recent_placements(self, since_s: float = 7 * 86400, limit: int = 20) -> list[dict[str, Any]]:
        """Chats this assistant recently sent voice work into (thread tasks and continued
        conversations), newest first, across calls: the strongest hint for "where we were working"."""
        with self._lock:
            rows = self._db.execute(
                "SELECT idem_key, continued, updated FROM runs WHERE continued IS NOT NULL AND updated > ? "
                "ORDER BY updated DESC LIMIT ?", (time.time() - since_s, limit)).fetchall()
        out = []
        for key, continued, updated in rows:
            try:
                placed = json.loads(continued)
            except ValueError:
                continue
            if isinstance(placed, dict) and placed.get("session_id"):
                out.append({"session_id": str(placed["session_id"]), "request": self.request_text(key) or "",
                            "at": float(updated or 0)})
        return out

    def call_written_answers(self, interaction_id: str) -> list[dict[str, Any]]:
        """Written answers for this call only, in completion order (no carried work)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT delegation_id, title, result_json FROM runs WHERE interaction_id = ? "
                "AND status = 'completed' AND result_json IS NOT NULL ORDER BY COALESCE(settled_at, updated)",
                (interaction_id,)).fetchall()
        out = []
        for task_id, title, raw in rows:
            if not task_id or task_id.startswith("choice_"):
                continue
            try:
                result = json.loads(raw)
                if not isinstance(result, dict):
                    continue
                full = result.get("full") or result.get("spoken")
                if isinstance(full, str) and full.strip():
                    out.append({"task_id": task_id, "title": "" if title in PLACEHOLDER_TITLES else title or "", "full": full,
                                "spoken": result.get("spoken") or ""})
            except (ValueError, TypeError):
                continue
        return out

    def calls_from_tasks(self, since_s: float = 7 * 86400) -> list[dict[str, Any]]:
        """Recent calls reconstructed from their tasks: what was asked, how it ended, the answer."""
        with self._lock:
            rows = self._db.execute(
                "SELECT idem_key, interaction_id, status, updated, title, result_json FROM runs "
                "WHERE updated > ? ORDER BY updated", (time.time() - since_s,)).fetchall()
        calls: dict[str, dict[str, Any]] = {}
        for key, interaction, status, updated, title, result_json in rows:
            request = self.request_text(key)
            if not request:
                continue
            result = ""
            if result_json:
                try:
                    result = str((json.loads(result_json) or {}).get("spoken") or "")
                except ValueError:
                    result = ""
            call = calls.setdefault(interaction, {"id": interaction, "ended": 0.0, "turns": [], "tasks": []})
            call["ended"] = max(call["ended"], float(updated or 0))
            shown = title if title and title not in PLACEHOLDER_TITLES else fallback_title(request)
            call["tasks"].append({"request": request, "title": shown, "status": status, "result": result})
        return list(calls.values())

    def recent_subject_tasks(self, since_s: float = 12 * 3600, limit: int = 40) -> list[Any]:
        """Voice tasks of the last ``since_s`` (any call), newest first, as recap.Earlier: what a
        repeat "where are we on X?" is matched against. Rejected and dismissed rows are left out."""
        from .recap import Earlier
        with self._lock:
            rows = self._db.execute(
                """SELECT idem_key, delegation_id, status, title, result_json, created, settled_at,
                          interaction_id, session_id, seen FROM runs
                   WHERE COALESCE(settled_at, created, updated) > ? AND status != 'rejected'
                     AND COALESCE(dismissed, 0) = 0
                   ORDER BY COALESCE(settled_at, created, updated) DESC LIMIT ?""",
                (time.time() - since_s, limit)).fetchall()
        out = []
        for key, delegation, status, title, result_json, created, settled_at, interaction, session, seen in rows:
            request = self.request_text(key)
            if not request:
                continue
            spoken = ""
            if result_json:
                try:
                    spoken = str((json.loads(result_json) or {}).get("spoken") or "")
                except (ValueError, AttributeError):
                    spoken = ""
            out.append(Earlier(key=key, task_id=delegation or key, request=request, status=status,
                               title=title or "", result=spoken, created=float(created or 0),
                               settled_at=float(settled_at) if settled_at else None,
                               interaction_id=interaction or "", session_id=session, seen=bool(seen)))
        return out

    def away(self, limit: int = 3) -> list[dict[str, Any]]:
        """Runs that settled (terminal or approval-needed) since the previous call ended."""
        ended = self.get_meta("last_call_end")
        if ended is None:
            return []
        with self._lock:
            rows = self._db.execute(
                """SELECT idem_key,run_id,status,settled_at,result_json,failure FROM runs
                   WHERE settled_at > ? AND run_id IS NOT NULL AND set_aside IS NULL
                   ORDER BY settled_at DESC LIMIT ?""",
                (float(ended), limit)).fetchall()
        items = []
        for key, run_id, status, settled_at, result_json, failure in rows:
            if status not in SETTLED:
                continue
            spoken = None
            if result_json:
                try:
                    spoken = json.loads(result_json).get("spoken")
                except ValueError:
                    spoken = None
            item = {"run_id": run_id, "request": notice_text(self.request_text(key), 140),
                    "status": status, "spoken": notice_text(spoken, 300), "finished_at": settled_at}
            why = public_failure(failure, self.hermes_home) if status == "failed" else None
            if why:
                item["failure"] = why  # the same object as on the task (work()["failure"])
            items.append(item)
        return items

    # -- follow-ups added to a running task (receipts.py) --------------------------------
    def add_follow_up(self, key: str, words: str, body: str, milestone: str, after_id: int) -> str:
        follow_up_id = "fu_" + secrets.token_hex(8)
        with self._lock, self._db:
            self._db.execute("INSERT INTO follow_ups VALUES (?,?,?,?,?,?,?,?,NULL)",
                             (follow_up_id, key, notice_text(words, 200) or "", body[:4000], re.sub(r"\s+", " ", milestone).strip()[:240],
                              "queued", int(after_id or 0), time.time()))
            self._db.execute("UPDATE runs SET updated=? WHERE idem_key=?", (time.time(), key))
        return follow_up_id

    def open_follow_ups(self, key: str) -> list[dict[str, Any]]:
        """Follow-ups to this task still queued (not yet seen in its transcript), oldest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT follow_up_id, words, body, after_id FROM follow_ups WHERE idem_key=? AND state='queued' "
                "ORDER BY created", (key,)).fetchall()
        return [{"id": i, "words": w, "body": b, "after_id": a} for i, w, b, a in rows]

    def settle_follow_up(self, follow_up_id: str, state: str) -> bool:
        """queued -> landed | not_read, once (a settled receipt never changes again)."""
        if state not in {"landed", "not_read"}:
            return False
        with self._lock, self._db:
            cur = self._db.execute("UPDATE follow_ups SET state=?, settled=? WHERE follow_up_id=? AND state='queued'",
                                   (state, time.time(), follow_up_id))
            if cur.rowcount:
                self._db.execute("UPDATE runs SET updated=? WHERE idem_key=(SELECT idem_key FROM follow_ups "
                                 "WHERE follow_up_id=?)", (time.time(), follow_up_id))
            row = self._db.execute("SELECT idem_key FROM follow_ups WHERE follow_up_id=?", (follow_up_id,)).fetchone()
        if cur.rowcount and row:
            for listener in list(self.follow_up_listeners):
                try:
                    listener(row[0], follow_up_id, state)
                except Exception:  # a listener never breaks a receipt
                    pass
        return cur.rowcount > 0

    def follow_up_receipt(self, key: str, words: str, since: float,
                          client_id: str | None = None) -> dict[str, Any] | None:
        """App sends match their stored transport id, never words/time. Voice-only legacy callers
        without a client id retain their existing matching rule."""
        if client_id is not None:
            import base64
            encoded = base64.urlsafe_b64encode(client_id.encode()).decode().rstrip("=")
            marker = f"[Speakeasy message id: {encoded}]"
            where, args = "instr(body,?) > 0", (key, marker)
        else:
            where, args = "words=? AND created>=?", (key, notice_text(words, 200) or "", since - 5)
        with self._lock:
            row = self._db.execute(
                f"SELECT follow_up_id, state, settled FROM follow_ups WHERE idem_key=? AND {where} "
                "ORDER BY created LIMIT 1", args).fetchone()
        return {"id": row[0], "state": row[1], "settled": row[2]} if row else None

    # -- messages steered into a running task (Steer now) --------------------------------------
    _STEERED_COLS = "client_id,idem_key,page_session,text,sent_at,state,seen_at,follow_up_run,updated"

    def add_steered(self, client_id: str, key: str, page_session: str | None, text: str) -> None:
        now = time.time()
        with self._lock, self._db:
            self._db.execute(f"INSERT OR IGNORE INTO steered_messages ({self._STEERED_COLS}) "
                             "VALUES (?,?,?,?,?,'sent',NULL,NULL,?)", (client_id, key, page_session, text, now, now))

    def steered_messages(self, keys: list[str] | None = None, page_session: str | None = None,
                         states: tuple[str, ...] | None = None, limit: int = 20) -> list[dict[str, Any]]:
        where, args = [], []
        if keys is not None or page_session is not None:
            any_of = []
            if keys:
                any_of.append(f"idem_key IN ({','.join('?' * len(keys))})")
                args += list(keys)
            if page_session is not None:
                any_of.append("page_session=?")
                args.append(page_session)
            if not any_of:
                return []
            where.append("(" + " OR ".join(any_of) + ")")
        if states:
            where.append(f"state IN ({','.join('?' * len(states))})")
            args += list(states)
        sql = f"SELECT {self._STEERED_COLS} FROM steered_messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY sent_at DESC LIMIT ?", (*args, limit)).fetchall()
        return [dict(zip(self._STEERED_COLS.split(","), r)) for r in reversed(rows)]

    def settle_steered(self, client_id: str, state: str, seen_at: float | None = None,
                       follow_up_run: str | None = None) -> bool:
        """sent -> seen | follow_up | unused, once."""
        with self._lock, self._db:
            return self._db.execute(
                "UPDATE steered_messages SET state=?, seen_at=COALESCE(?,seen_at), follow_up_run=?, updated=? "
                "WHERE client_id=? AND state='sent'", (state, seen_at, follow_up_run, time.time(), client_id)).rowcount == 1

    def adjusting(self, key: str, status: str) -> bool:
        """The task's live line, briefly, after it read a steered message: 'Adjusting: …'. A system status,
        so the run's next own status replaces it; never on a finished task."""
        safe = safe_user_text(status, 80)
        if not safe:
            return False
        now = time.time()
        with self._lock, self._db:
            return self._db.execute(
                """UPDATE runs SET short_status=?,progress_updated=?,updated=?,status_source='system'
                   WHERE idem_key=? AND status NOT IN
                   ('completed','failed','cancelled','interrupted','ambiguous','rejected')""",
                (safe, now, now, key)).rowcount > 0

    def follow_ups(self, key: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT words, milestone, state, created FROM follow_ups WHERE idem_key=? ORDER BY created DESC LIMIT 10",
                (key,)).fetchall()
        return [{"text": w, "milestone": m, "state": s, "at": c} for w, m, s, c in reversed(rows)]

    # -- a new task waiting for another to finish (same-project fence) -------------------
    def set_waiting(self, key: str, waiting: dict[str, Any] | None) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET waiting=?, updated=? WHERE idem_key=?",
                             (json.dumps(waiting) if waiting else None, time.time(), key))

    def waiting_of(self, key: str) -> dict[str, Any] | None:
        """What a held task waits on, as saved by set_waiting ({name, project}), or None."""
        with self._lock:
            row = self._db.execute("SELECT waiting FROM runs WHERE idem_key=?", (key,)).fetchone()
        try:
            held = json.loads(row[0]) if row and row[0] else None
        except ValueError:
            return None
        return held if isinstance(held, dict) else None

    def progress(self, key: str, kind: str, text: str) -> None:
        """Persist only bounded, user-facing milestones; never tool arguments or reasoning."""
        if kind not in {"request", "milestone", "tool", "result"} or not text.strip():
            return
        text = re.sub(r"\s+", " ", text).strip()[:4000 if kind == "result" else 240]
        with self._lock, self._db:
            if not self._db.execute("SELECT 1 FROM runs WHERE idem_key=?", (key,)).fetchone():
                return
            previous = self._db.execute(
                "SELECT kind,text FROM work_events WHERE idem_key=? ORDER BY seq DESC LIMIT 1", (key,)).fetchone()
            if previous == (kind, text):
                return
            self._db.execute("UPDATE runs SET updated=? WHERE idem_key=?", (time.time(), key))
            self._db.execute("INSERT INTO work_events (idem_key,kind,text,created) VALUES (?,?,?,?)",
                             (key, kind, text, time.time()))
            self._db.execute("""DELETE FROM work_events WHERE idem_key=? AND kind!='request' AND seq NOT IN
                (SELECT seq FROM work_events WHERE idem_key=? ORDER BY seq DESC LIMIT 30)""", (key, key))

    def user_progress(self, key: str, short_status: str, detail: str) -> None:
        """Persist an authored, sanitized display update for one exact run."""
        safe_status = valid_short_status(short_status)
        safe_detail = safe_user_text(detail, 500)
        if not safe_status or not safe_detail:
            return
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                """UPDATE runs SET authored_at=?,updated=? WHERE idem_key=? AND short_status=?
                   AND detail=? AND status_source='authored'""", (now, now, key, safe_status, safe_detail))
            self._db.execute(
                """UPDATE runs SET short_status=?,detail=?,progress_updated=?,updated=?,
                   status_source='authored',authored_at=?
                   WHERE idem_key=? AND run_id IS NOT NULL AND NOT (short_status IS ? AND detail IS ?
                   AND status_source IS 'authored')""",
                (safe_status, safe_detail, now, now, now, key, safe_status, safe_detail))

    def touch(self, key: str) -> None:
        """The task just showed it is alive (a thread task's tool step): keep it from looking stale."""
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET updated=? WHERE idem_key=? AND status NOT IN "
                             "('completed','failed','cancelled','interrupted')", (time.time(), key))

    def thread_progress(self, key: str, detail: str) -> None:
        """A thread task's own words while it works (it has no Hermes run id, so ``user_progress``,
        which is bound to one exact run, never applied and the card read "Status unconfirmed")."""
        safe_detail = safe_user_text(detail, 500)
        if not safe_detail:
            return
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                """UPDATE runs SET detail=?, progress_updated=?, updated=?,
                   short_status=CASE WHEN status_source='authored' THEN short_status ELSE 'Working in the thread' END,
                   status_source=CASE WHEN status_source='authored' THEN status_source ELSE 'system' END
                   WHERE idem_key=? AND run_id IS NULL""", (safe_detail, now, now, key))

    def handoff_status(self, key: str, short_status: str, detail: str) -> bool:
        """What the task was handed, shown until the run reports anything itself. Never overwrites
        a status the run (or a tool) already set; real progress replaces it as usual."""
        safe_status = valid_short_status(short_status)
        safe_detail = safe_user_text(detail, 500)
        if not safe_status or not safe_detail:
            return False
        now = time.time()
        with self._lock, self._db:
            cur = self._db.execute(
                """UPDATE runs SET short_status=?,detail=?,progress_updated=?,updated=?,status_source='system'
                   WHERE idem_key=? AND short_status IS NULL AND status NOT IN
                   ('completed','failed','cancelled','interrupted','ambiguous','rejected')""",
                (safe_status, safe_detail, now, now, key))
        return cur.rowcount > 0

    def tool_progress(self, key: str, short_status: str, detail: str) -> bool:
        """Persist a tool-derived status unless an authored status is still fresh."""
        safe_status = valid_short_status(short_status)
        safe_detail = safe_user_text(detail, 500)
        if not safe_status or not safe_detail:
            return False
        now = time.time()
        with self._lock, self._db:
            cursor = self._db.execute(
                """UPDATE runs SET short_status=?,detail=?,progress_updated=?,updated=?, status_source='tool'
                   WHERE idem_key=? AND run_id IS NOT NULL AND (authored_at IS NULL OR authored_at <= ?)
                   AND NOT (short_status IS ? AND detail IS ? AND status_source IS 'tool')""",
                (safe_status, safe_detail, now, now, key, now - AUTHORED_PRECEDENCE_S, safe_status, safe_detail))
            return cursor.rowcount > 0

    def set_result(self, key: str, result: dict[str, Any] | None) -> None:
        raw = json.dumps(result) if result else None
        asks = _question_text(raw) is not None
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?", (raw, key))
            if asks:  # a result that asks opens its question (once: a rewrite keeps how it closed)
                self._db.execute("UPDATE runs SET question_state='open', question_state_at=? "
                                 "WHERE idem_key=? AND question_state IS NULL", (time.time(), key))
            else:
                self._db.execute("UPDATE runs SET question_state=NULL, question_state_at=NULL "
                                 "WHERE idem_key=? AND question_state='open'", (key,))

    def run_result(self, run_id: str) -> tuple[str, dict[str, Any]] | None:
        """(idem key, stored result) for one run, server-side fields included; None if unknown."""
        with self._lock:
            row = self._db.execute("SELECT idem_key,result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        try:
            result = json.loads(row[1]) if row[1] else {}
        except ValueError:
            result = {}
        return row[0], result if isinstance(result, dict) else {}

    def result_cards(self, run_id: str) -> list[Any]:
        """Stored cards for one exact finished run, including server-side image paths."""
        with self._lock:
            row = self._db.execute("SELECT status,result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row[0] not in TERMINAL or not row[1]:
            return []
        try:
            cards = (json.loads(row[1]) or {}).get("cards")
        except (ValueError, AttributeError):
            return []
        return cards if isinstance(cards, list) else []

    def latest_tasks(self, limit: int = 12) -> list[dict[str, Any]]:
        """Every task of the most recent call (oldest first), for the after-call Work view."""
        with self._lock:
            row = self._db.execute("SELECT interaction_id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
            if row is None or not row[0]:
                return []
            rows = self._db.execute(
                "SELECT idem_key, delegation_id FROM runs WHERE interaction_id=? AND status!='rejected' ORDER BY rowid",
                (row[0],)).fetchall()
        tasks = []
        for key, delegation_id in rows[-limit:]:
            work = self.work(idem_key=key)
            if work is not None and not work.get("dismissed"):
                work["task_id"] = delegation_id or key
                tasks.append(work)
        # Earlier calls' open and not-cleared tasks stay too (the workspace outlives a call).
        exclude = {t["task_id"] for t in tasks} | {k for k, _ in rows}
        earlier = self.board_carry(exclude)
        exclude |= {t["task_id"] for t in earlier}
        return fold_tasks(earlier + tasks + self.carried_draft_tasks(exclude))

    def board_carry_keys(self, exclude: set[str], limit: int = 12) -> list[tuple[str, str]]:
        """Earlier calls' tasks that stay on the board until the user clears them, oldest first, as
        (idem_key, task_id): every one still running or waiting on the user, and every finished one
        not cleared (those that settled before carrying existed excepted). Never a rejected one."""
        try:
            since = float(self.get_meta("board_carry_since") or 0)
        except ValueError:
            since = 0.0
        with self._lock:
            rows = self._db.execute(
                "SELECT idem_key, delegation_id, status, settled_at FROM runs WHERE status!='rejected' "
                "AND COALESCE(dismissed,0)=0 ORDER BY rowid").fetchall()
        out = []
        for key, delegation_id, status, settled_at in rows:
            if key in exclude or (delegation_id and delegation_id in exclude):
                continue
            if status in TERMINAL and float(settled_at or 0) < since:
                continue
            out.append((key, delegation_id or key))
        return out[-limit:]

    def board_carry(self, exclude: set[str], limit: int = 12) -> list[dict[str, Any]]:
        """board_carry_keys as work rows marked ``carried`` (not answers of the current call)."""
        out = []
        for key, task_id in self.board_carry_keys(exclude, limit):
            work = self.work(idem_key=key)
            if work is not None and not work.get("dismissed"):
                work["task_id"] = task_id
                work["carried"] = True
                out.append(work)
        return out

    def carried_draft_tasks(self, exclude: set[str]) -> list[dict[str, Any]]:
        """Earlier calls' tasks that still hold something waiting for the user: an email draft
        (Send / Deny / Revise) or finished images on a review card (until dismissed)."""
        with self._lock:
            ids = dict(self._db.execute("SELECT idem_key, delegation_id FROM runs").fetchall())
        out = []
        seen: set[str] = set()
        for key in self.keys_with_pending_drafts() + self.keys_with_pending_reviews():
            task_id = ids.get(key) or key
            if key in exclude or task_id in exclude or key in seen:
                continue
            seen.add(key)
            work = self.work(idem_key=key)
            # A task the user cleared stays cleared, even with a draft still waiting on it.
            if work is not None and not work.get("dismissed"):
                work["task_id"] = task_id
                work["carried"] = True
                out.append(work)
        return out

    def task_id_for(self, key: str) -> str:
        """The task id the app knows this row by (its delegation id, as on carried rows)."""
        with self._lock:
            row = self._db.execute("SELECT delegation_id FROM runs WHERE idem_key=?", (key,)).fetchone()
        return (row[0] if row and row[0] else key)

    # -- AI thread titles ----------------------------------------------------------------------

    def key_for_task(self, task_id: str) -> str | None:
        """A run's idem key by the id the app knows it by (its delegation id, else the key)."""
        with self._lock:
            row = self._db.execute("SELECT idem_key FROM runs WHERE delegation_id=? OR idem_key=?",
                                   (task_id, task_id)).fetchone()
        return row[0] if row else None

    def stand_in_titles(self, limit: int = 40) -> list[dict[str, Any]]:
        """Past tasks whose shown title is still the stand-in label built from their own words
        (the titler never landed: out of credits, reload mid-name) or that never got one.
        These are what the Mac app's on-device Apple Intelligence titler may rename. Only
        finished tasks: a running one may still get the cloud titler's name."""
        marks = ",".join("?" * len(TERMINAL))
        with self._lock:
            rows = self._db.execute(
                "SELECT r.idem_key, r.delegation_id, r.title, r.updated, r.continued, "
                "(SELECT e.text FROM work_events e WHERE e.idem_key=r.idem_key AND e.kind='request' "
                " ORDER BY e.seq LIMIT 1) FROM runs r "
                f"WHERE r.status IN ({marks}) ORDER BY r.updated DESC LIMIT 400",
                tuple(sorted(TERMINAL))).fetchall()
        out = []
        for key, delegation_id, title, updated, continued, request_text in rows:
            asked = self.asked_text(key)
            request = title_text(request_text, 1200) or title_text(asked, 1200)
            if not request or not _is_stand_in(title, request_text, continued, asked):
                continue  # a real name (the titler's, a chat thread's, an older row's), or nothing to title from
            out.append({"task_id": delegation_id or key,
                        "title": "" if title in PLACEHOLDER_TITLES else title or "",
                        "request": request, "updated": float(updated or 0)})
            if len(out) >= limit:
                break
        return out

    def set_parent(self, key: str, parent: str | None) -> None:
        """The task this one answers or joins (a decision's answer, a follow-up, a joined turn)."""
        if not parent or parent == key:
            return
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET parent=? WHERE idem_key=?", (parent, key))

    def parent_of(self, key: str) -> str | None:
        """The task this one answers or joins, as recorded when it started; for tasks saved before
        that was recorded, only what the store itself shows they joined (see ``title_context``)."""
        with self._lock:
            row = self._db.execute("SELECT parent FROM runs WHERE idem_key=?", (key,)).fetchone()
            if row and row[0]:
                return row[0]
            return self._recorded_parent(key)

    def _recorded_parent(self, key: str) -> str | None:
        """An older task's parent from what was recorded at the time, never a guess from position:
        a turn that joined a running task left "You added: <its words>" on that task; a follow-up
        to a finished one was asked as "<words> (following up: <the earlier request>)". Call with
        the lock held."""
        row = self._db.execute(
            "SELECT r.interaction_id, r.rowid, (SELECT e.text FROM work_events e WHERE e.idem_key=r.idem_key "
            "AND e.kind='request' ORDER BY e.seq LIMIT 1), r.delegation_id FROM runs r WHERE r.idem_key=?",
            (key,)).fetchone()
        if not row or not row[2]:
            return None
        interaction, rowid, request, delegation_id = row
        if str(delegation_id or "").startswith("answer_"):
            # A tapped answer on a task's question card: only a task with a question shows one, one
            # decision at a time, so it answers the newest such task before it on the call.
            for other, result in self._db.execute(
                    "SELECT idem_key, result_json FROM runs WHERE interaction_id=? AND rowid<? AND result_json "
                    "IS NOT NULL ORDER BY rowid DESC LIMIT 20", (interaction, rowid)):
                try:
                    if (json.loads(result) or {}).get("question"):
                        return other
                except (ValueError, AttributeError):
                    continue
            return None
        said = notice_text(request, 200) or ""
        joined = self._db.execute(
            "SELECT r.idem_key FROM runs r JOIN work_events e ON e.idem_key=r.idem_key WHERE r.interaction_id=? "
            "AND r.rowid<? AND e.kind='milestone' AND e.text=? ORDER BY r.rowid DESC LIMIT 1",
            (interaction, rowid, f"You added: {said}")).fetchone()
        if joined:
            return joined[0]
        match = re.search(r"\(following up: (.+)\)\s*$", " ".join(request.split()))
        if match:
            earlier = match.group(1).strip()
            for other, text in self._db.execute(
                    "SELECT r.idem_key, (SELECT e.text FROM work_events e WHERE e.idem_key=r.idem_key AND "
                    "e.kind='request' ORDER BY e.seq LIMIT 1) FROM runs r WHERE r.interaction_id=? AND r.rowid<? "
                    "ORDER BY r.rowid DESC", (interaction, rowid)):
                if text and earlier in {notice_text(text, 200) or "",
                                        notice_text(request_notes(text)[0], 200) or ""}:
                    return other
        return None

    def parent_name(self, key: str | None) -> str | None:
        """A parent task's own name: never the chat thread's name it ran in (a thread task's shown
        title is the thread's), so a follow-up is never named after an unrelated older conversation."""
        if not key:
            return None
        with self._lock:
            row = self._db.execute("SELECT own_title, title, continued FROM runs WHERE idem_key=?", (key,)).fetchone()
        if not row:
            return None
        own, title, continued = row
        name = own or (title if not continued else None)  # an older row that never ran in a chat: its title is its own
        # A meta name ("Continue previous discussion") says nothing; the parent's request still goes along.
        return name if name and not TITLE_META.search(name) else None

    def title_context(self, key: str) -> tuple[str | None, str | None]:
        """The task this one really answers or joins: (its own name, its request), or (None, None).
        Never the task that merely ran before it on the call, and never a chat thread's name."""
        parent = self.parent_of(key)
        if not parent:
            return None, None
        return self.parent_name(parent), self.request_text(parent)

    def rename_stand_in(self, key: str, title: str) -> bool:
        """Set the app's on-device title only while the task still shows its stand-in label, checked
        and written under one lock: a name the cloud titler or a chat thread gave it always wins."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT r.title, r.continued, (SELECT e.text FROM work_events e WHERE e.idem_key=r.idem_key "
                "AND e.kind='request' ORDER BY e.seq LIMIT 1) FROM runs r WHERE r.idem_key=?", (key,)).fetchone()
            asked = self._db.execute("SELECT text FROM asked_words WHERE idem_key=?", (key,)).fetchone()
            asked = asked[0] if asked else None
            if not row or not _is_stand_in(row[0], row[2], row[1], asked):
                return False
            title = fix_product_names(title)
            self._db.execute("UPDATE runs SET title=? WHERE idem_key=?", (title, key))
            name = job_name(title)
            if name:
                self._db.execute("UPDATE runs SET own_title=? WHERE idem_key=?", (title, key))
                # The job this task named takes the new name only while it still has the stand-in's
                # name; a job named any other way keeps its name.
                stand_ins = {n for n in (job_name(row[0]), *(job_name(t) for t in _stand_ins(row[2])),
                                         *(job_name(t) for t in (_stand_ins(asked) if asked else ())),
                                         *(job_name(t) for t in PLACEHOLDER_TITLES)) if n}
                marks = ",".join("?" * len(stand_ins))
                self._db.execute(f"UPDATE jobs SET name=?, updated=? WHERE from_key=? AND name IN ({marks})",
                                 (name, time.time(), key, *stand_ins))
        return True

    # -- live view ("what it's looking at") -------------------------------------------------
    def set_live_image(self, key: str, kind: str, ref: str, name: str, source: str) -> bool:
        """Record the task's latest image (already vetted by the caller). False when unchanged."""
        if kind not in {"path", "url"}:
            raise ValueError("kind must be path or url")
        now = time.time()
        with self._lock, self._db:
            row = self._db.execute("SELECT kind,ref,seq FROM live_images WHERE idem_key=?", (key,)).fetchone()
            if row is not None and row[0] == kind and row[1] == ref:
                return False
            seq = (row[2] if row else 0) + 1
            self._db.execute("INSERT OR REPLACE INTO live_images VALUES (?,?,?,?,?,?,?)",
                             (key, kind, ref, name[:120] or "Image", source, seq, now))
        return True

    def live_image(self, key: str) -> dict[str, Any] | None:
        """Server-side view, including the ref. Use ``public_live_image`` for clients."""
        with self._lock:
            row = self._db.execute("SELECT kind,ref,name,source,seq,updated FROM live_images WHERE idem_key=?",
                                   (key,)).fetchone()
        if row is None:
            return None
        kind, ref, name, source, seq, updated = row
        return {"kind": kind, "ref": ref, "name": name, "source": source, "seq": seq, "at": updated}

    def public_live_image(self, key: str) -> dict[str, Any] | None:
        live = self.live_image(key)
        return None if live is None else {k: live[k] for k in ("name", "source", "seq", "at")}

    # -- image review cards --------------------------------------------------------------
    @staticmethod
    def _image_numbers(result_json: str | None) -> list[int]:
        """Card numbers (1-based positions in the stored card list) that are images."""
        try:
            cards = (json.loads(result_json or "null") or {}).get("cards")
        except (ValueError, AttributeError):
            return []
        if not isinstance(cards, list):
            return []
        return [i + 1 for i, card in enumerate(cards[:MAX_CARDS]) if isinstance(card, dict) and card.get("kind") == "image"]

    def _dismissed_cards(self, run_id: str) -> set[int]:
        rows = self._db.execute("SELECT card FROM review_dismissals WHERE run_id=?", (run_id,)).fetchall()
        return {int(r[0]) for r in rows}

    def review_images(self, run_id: str | None, status: str | None, result_json: str | None) -> list[int]:
        """Image cards of a finished run the user hasn't dismissed from its review card yet."""
        if not run_id or status != "completed":
            return []
        numbers = self._image_numbers(result_json)
        if not numbers:
            return []
        with self._lock:
            dismissed = self._dismissed_cards(run_id)
        return [n for n in numbers if n not in dismissed]

    def dismiss_review(self, run_id: str, cards: list[int] | None = None) -> list[int]:
        """Dismiss review cards of one finished run (all its images when ``cards`` is None).
        Returns the card numbers now dismissed; unknown cards are ignored."""
        with self._lock:
            row = self._db.execute("SELECT status,result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or row[0] != "completed":
            return []
        numbers = self._image_numbers(row[1])
        chosen = [n for n in numbers if cards is None or n in cards]
        now = time.time()
        with self._lock, self._db:
            for n in chosen:
                self._db.execute("INSERT OR IGNORE INTO review_dismissals VALUES (?,?,?)", (run_id, n, now))
        return chosen

    def keys_with_pending_reviews(self, limit: int = MAX_REVIEWS, max_age_s: float = 7 * 86400) -> list[str]:
        """The most recent finished tasks (any call) whose images still wait on a review card,
        oldest first; at most ``limit``. Older ones stay inside their tasks."""
        cutoff = time.time() - max_age_s
        with self._lock:
            rows = self._db.execute(
                """SELECT idem_key, run_id, result_json FROM runs WHERE status='completed' AND run_id IS NOT NULL
                   AND settled_at >= ? AND (dismissed IS NULL OR dismissed=0) AND result_json LIKE '%"image"%'
                   ORDER BY settled_at DESC LIMIT 40""", (cutoff,)).fetchall()
        keys = []
        for key, run_id, result_json in rows:
            if self.review_images(run_id, "completed", result_json):
                keys.append(key)
            if len(keys) >= limit:
                break
        return list(reversed(keys))

    def work(self, run_id: str | None = None, idem_key: str | None = None,
             assistant_name: str = "Hermes", activity_detail: bool = False) -> dict[str, Any] | None:
        """The latest admitted job or one exact server-owned run, including past calls.
        ``activity_detail``: every kept activity item with its tool call's args/result previews
        (``GET /voice/work/{run_id}``); otherwise the stream's smaller summary."""
        cols = """idem_key,run_id,status,updated,short_status,detail,progress_updated,
                  status_source,result_json,title,dismissed,summary,continued,settled_at,failure,shared,session_id,set_aside,seen,waiting,
                  created,failure_hint,answered,authored_at"""
        with self._lock:
            if idem_key:
                row = self._db.execute(f"SELECT {cols} FROM runs WHERE idem_key=?", (idem_key,)).fetchone()
            elif run_id:
                row = self._db.execute(f"SELECT {cols} FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:  # an id it was handed out under before (run_aliases)
                    alias = self._db.execute("SELECT idem_key FROM run_aliases WHERE alias=?", (run_id,)).fetchone()
                    if alias is not None:
                        row = self._db.execute(f"SELECT {cols} FROM runs WHERE idem_key=?", (alias[0],)).fetchone()
            else:
                row = self._db.execute(f"SELECT {cols} FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
            if row is None:
                return None
            (key, actual_run_id, status, updated, short_status, detail, progress_updated,
             status_source, result_json, title, dismissed, summary, continued, settled_at, failure, shared,
             session_id, set_aside, seen, waiting, created, failure_hint, answered, authored_at) = row
            events = self._db.execute(
                "SELECT kind,text,created FROM work_events WHERE idem_key=? ORDER BY seq DESC LIMIT 30", (key,)).fetchall()
            if not created:  # saved before tasks kept their start time: the first thing recorded
                first = self._db.execute("SELECT MIN(created) FROM work_events WHERE idem_key=?", (key,)).fetchone()
                created = first[0] if first and first[0] else None
        stale = status not in TERMINAL | {"waiting_for_approval", "waiting_on_task"} and time.time() - updated > STALE_AFTER_S
        display_status, display_detail, display_updated = short_status, detail, progress_updated
        source = (status_source or "authored") if short_status else None
        if stale:
            display_status = "Status unconfirmed"
            display_detail = detail or "No verified update has arrived recently."
            display_updated, source = updated, "system"
        elif status == "waiting_for_approval":
            display_status = "Needs your approval"
            display_detail = detail or f"{assistant_name} is waiting for an explicit approval decision."
            display_updated, source = updated, "system"
        result = None
        if status in TERMINAL and result_json:
            try:
                result = json.loads(result_json)
            except ValueError:
                result = None
        if isinstance(result, dict) and not result.get("headline") and not result.get("question"):
            # Saved before results carried their outcome headline: taken from the reply's words on
            # read, so an older "Okay, I'm dropping it." opener never stands as the headline.
            headline = outcome_headline(result.get("spoken"), result.get("full"))
            if headline:
                result = {**result, "headline": headline}
        asked = next((t for k, t, _ in reversed(events) if k == "request"), None)
        if asked is None:
            with self._lock:
                first_req = self._db.execute("SELECT text FROM work_events WHERE idem_key=? AND kind='request' "
                                             "ORDER BY seq LIMIT 1", (key,)).fetchone()
            asked = first_req[0] if first_req else None
        if not title or title in PLACEHOLDER_TITLES:
            # Not named yet (or an old generic label): the user's own words name it, everywhere a
            # client shows it (Work, Chats, Home, notifications, the Live Activity).
            title = fallback_title(self.asked_text(key), asked)
        drafts = self.drafts_for(key)
        if drafts and isinstance(result, dict):
            # The email it drafted is the answer's card (Home shows it in place of the done card).
            result = {**result, "views": with_email_views(result.get("views"), drafts)}
        work = {
            "run_id": actual_run_id, "status": status, "stale": stale, "updated": updated,
            "events": [{"kind": kind, "text": text, "at": created} for kind, text, created in reversed(events)],
            "short_status": display_status, "detail": display_detail,
            "source_run_id": actual_run_id, "updated_at": display_updated,
            "status_source": source, "result": stamp_as_of(_current_steps(public_result(result), status),
                                                           settled_at or updated),
            "title": title, "summary": summary, "dismissed": bool(dismissed),
            "email_drafts": drafts,
            "plan": concrete_plan(self.plan(key), next((t for k, t, _ in reversed(events) if k == "request"), None),
                                  title),
            "job": self.job(key, session_id, continued),
        }
        # What the user asked, always (``events`` keeps only the newest 30, so a long task's request
        # falls off them): the top of every task view says it.
        asked_text = safe_user_text(re.sub(r"\s*\(following up: .+\)\s*$", "", " ".join(str(asked or "").split())), 400)
        if asked_text:
            work["request"] = asked_text
        if session_id or _placed_session(continued):
            # The Hermes session its conversation lives in: "Show full session" opens it (history).
            work["session_id"] = session_id or _placed_session(continued)
        model_record = self.run_model(key)
        describe = getattr(self, "describe_model", None)
        if model_record and describe is not None:
            # Which model did (or is doing) the work, and how hard it thinks (models.describe).
            described = describe(model_record)
            if described:
                work["model"] = described
        try:
            ctx_rec = self.run_context(key, session_id, status, created)
        except Exception:
            ctx_rec = None
        if ctx_rec:
            # Which model answered its latest call and how full the context was (context_use.py), with how
            # long it took once finished: the chat-platform footer, shown quietly on the card. An estimate.
            from . import context_use as CU
            from .models import friendly_name
            took = (settled_at - created) if status in TERMINAL and settled_at and created else None
            ctx = CU.describe(ctx_rec, seconds=took, name_for=friendly_name)
            if ctx:
                work["context"] = ctx
        behind = self.queued_behind(key, session_id, status)
        if behind:
            # Queued in its chat thread behind another task's turn: shown as waiting for that task
            # (the same contract as a project hold), never "working" with a timer and steps done.
            work["status"], work["stale"] = "waiting_on_task", False
            work["queued_behind"] = behind
            work["waiting"] = {"name": behind, "project": ""}
            work["short_status"] = f"Queued behind \u201c{notice_text(behind, 60)}\u201d"
            work["detail"] = f"Starts when \u201c{notice_text(behind, 80)}\u201d finishes in the same chat."
            work["status_source"] = "system"
        if set_aside:
            # Its question was set aside: nothing waits on the user any more (only sent when true).
            work["set_aside"] = True
        state = self.question_state(key) if result_json and '"question"' in result_json else None
        if state:
            # Its question's one state (open / answered / set_aside / dismissed / moved_on / expired /
            # superseded): apps show the card from this alone (the fields below are for older apps).
            work["question_state"] = state
            if state == "answered":
                answer = self.question_answer(key)
                if answer:
                    work["question_answer"] = answer  # which option was taken, on every device
        closed = state if state in {"dismissed", "moved_on", "expired", "superseded"} else None
        answered = answered or state == "answered"
        if closed and not answered and not set_aside:
            # Its question closed unanswered (dismissed, or the conversation moved on): nothing waits on
            # the user. set_aside too, so apps from before question_closed stop showing the card.
            work["question_closed"] = closed
            work["set_aside"] = True
        if answered:
            # Its question was answered (on any device): nothing waits on the user (only sent when true).
            work["answered"] = True
        if seen:
            # Nothing to read (a home command that went through): not a new answer (only sent when true).
            work["seen"] = True
        follow_ups = self.follow_ups(key)
        if follow_ups:
            # What the user added while it ran, each with what is known of it: queued (Hermes took it, the
            # task hasn't read it yet), landed (it is in the task's transcript) or not_read (it ended first).
            # ``milestone`` is the matching "You added: ..." step text.
            work["follow_ups"] = follow_ups
        if waiting and status == "waiting_on_task":
            try:
                held = json.loads(waiting)
            except ValueError:
                held = None
            if isinstance(held, dict) and held.get("name"):
                # Held back so it doesn't edit the same project at once: starts when that task ends.
                work["waiting"] = {"name": str(held["name"])[:120], "project": str(held.get("project") or "")[:80]}
                if isinstance(held.get("key"), str) and held["key"]:
                    work["waiting_key"] = held["key"]   # the job it waits for (Home names it by its title)
                work["short_status"] = "Waiting for another task"
                work["detail"] = f"Starts when \"{work['waiting']['name']}\" finishes (same project)."
                work["status_source"] = "system"
        # The opening plan is a guess made at handoff: never the progress indicator. It rides as
        # ``started_with`` (a faint "Started with: ..." at most); ``plan`` and the step fields stay
        # empty so no client draws "step 2 of 4" or a plan step as what it is doing now.
        if work["plan"]:
            work["started_with"] = notice_text("; ".join(work["plan"]), 160)
        work["plan"] = []
        work.update(job_progress(work["status"], created, settled_at, [], work["events"]))
        # One short plain sentence for a failed (or restart-interrupted) task, never "unknown".
        work["failure_reason"] = failure_reason(status, failure, failure_hint)
        why = public_failure(failure, self.hermes_home) if status == "failed" else None
        if why:
            # Speakeasy's own words for why it failed: a row label when the reason is known, and a
            # sentence for the detail. The provider's error text never reaches here.
            work["failure"] = why
        # Live activity (capability job_activity): activity, findings, touched, hero; each only when known.
        activity_state = self.activity_state(key)
        work.update(activity_mod.public(activity_state, detail=activity_detail))
        self._live_line(work, key, activity_state, created, authored_at, status_source, progress_updated, title,
                        opened=activity_detail)
        self._drop_misheard(work, key)
        live = self.public_live_image(key)
        if live is not None:
            # Addressed by /voice/live-image/<run_id>; the path or URL stays on the server.
            work["live_image"] = live
        review = self.review_images(actual_run_id, status, result_json)
        if review:
            # Image cards still waiting on the call panel's review card (numbers address /voice/card-image).
            work["review"] = {"images": review, "settled_at": settled_at}
        items = self._shared_list(shared)
        if items:
            # What the user shared with it (a thumbnail or file chip each); images are fetched by
            # position from /voice/shared-image/<run_id>/<n>. Paths never leave the server.
            work["shared"] = [{k: i[k] for k in ("kind", "app", "name") if i.get(k)} for i in items]
        if status == "completed" and plain_chat(activity_state, work):
            # SPK-290: a plain chat reply (no tool call, helper, card or file): the apps show it as a
            # message, without the task header ("Started · Took 4 s") or a "Looked into it" card.
            # Older apps ignore the field.
            work["chat"] = True
        if continued:
            try:
                work["continued_in"] = json.loads(continued).get("label")
            except ValueError:
                pass
        return work

    # -- the live line (live_status.py) -------------------------------------------------------
    def _live_line(self, work: dict[str, Any], key: str, state: dict[str, Any] | None, created: float | None,
                   authored_at: float | None, status_source: str | None, progress_updated: float | None,
                   title: str | None, opened: bool = False) -> None:
        """A running task's ``now_line`` / ``so_far`` from what it really did (live_status.py), and its
        ``short_status`` / ``detail`` from them unless the task itself wrote a status recently. The
        handoff guess ("Investigating the stopped task") stops reading as "now" after a minute.
        *opened*: the read is someone opening the task (its view or session page), the only read
        that may ask the side model for a line; liveness loops and pushes never do."""
        if work["status"] in TERMINAL or work["status"] in NOT_STARTED or work["status"] in {
                "waiting_for_approval", "cancel_requested"} or work.get("queued_behind") or work.get("waiting"):
            return
        summarizer = getattr(self, "live_summarizer", None)
        request = next((e["text"] for e in reversed(work["events"]) if e["kind"] == "request"), None) or title
        on_ready = getattr(self, "live_publish", None)
        try:
            if summarizer is not None:
                live = summarizer.line(key, state, request=request, started=created,
                                       on_ready=(lambda: on_ready(key)) if on_ready else None, ask=opened)
            else:
                live = live_status.deterministic(state, now=time.time(), started=created)
        except Exception:
            logger.debug("speakeasy: live line failed", exc_info=True)
            return
        if not live or not live.get("now"):
            return
        work["now_line"] = live["now"]
        if live.get("so_far"):
            work["so_far"] = live["so_far"]
        if live.get("quiet_s"):
            work["quiet_s"] = live["quiet_s"]
        now = time.time()
        authored_fresh = (status_source == "authored" and authored_at is not None
                          and now - authored_at < live_status.IDLE_AFTER_S)
        if authored_fresh:
            # The task's own words, just written, win over a summary of its steps, and over "No new
            # steps" / "No steps reported yet": writing a status is a step (the voice reads this too).
            work.pop("quiet_s", None)
            return
        work["short_status"] = live["now"]
        work["detail"] = live.get("so_far") or work.get("detail")
        work["status_source"] = "tool"
        work["updated_at"] = now if live["source"] != "idle" else work.get("updated_at")
        if work.get("stale") and live["source"] == "activity":
            work["stale"] = False  # it did something in the last two minutes: not unconfirmed

    # -- live activity (activity.py) ---------------------------------------------------------
    def set_activity(self, key: str, state: dict[str, Any]) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO activity VALUES (?,?,?)",
                             (key, json.dumps(state, separators=(",", ":")), time.time()))

    def activity_state(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT state_json FROM activity WHERE idem_key=?", (key,)).fetchone()
        if row is None:
            return None
        try:
            state = json.loads(row[0])
        except ValueError:
            return None
        return state if isinstance(state, dict) else None

    # -- email drafts ------------------------------------------------------------------
    def add_draft(self, key: str, session_id: str | None, draft: dict[str, Any]) -> dict[str, Any]:
        """Store one draft for a task. Older pending/revising drafts of that task (or of the same
        Hermes session, e.g. a spoken "revise it" follow-up) are superseded and can no longer be
        approved."""
        now = time.time()
        with self._lock, self._db:
            draft_id = self._add_draft_locked(key, session_id, draft, now)
        return self.draft(draft_id) or {}

    def _add_draft_locked(self, key: str, session_id: str | None, draft: dict[str, Any], now: float) -> str:
        """Insert without a nested connection context (which would commit the outer transaction)."""
        draft_id = "ed_" + secrets.token_hex(12)
        body, digest = stored_json(draft), draft_sha256(draft)
        self._db.execute(
            "UPDATE email_drafts SET status='superseded', updated=? WHERE status IN ('pending','revising') "
            "AND (idem_key=? OR (? IS NOT NULL AND session_id=?))", (now, key, session_id, session_id))
        self._db.execute("INSERT INTO email_drafts VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (draft_id, key, session_id, body, digest, "pending", now, now, None, None))
        return draft_id

    def draft(self, draft_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT draft_id,idem_key,session_id,draft_json,sha256,status,created,updated,action_run_id,error "
                "FROM email_drafts WHERE draft_id=?", (draft_id,)).fetchone()
        return self._draft_row(row) if row else None

    @staticmethod
    def _draft_row(row: tuple) -> dict[str, Any]:
        draft_id, key, session_id, body, digest, status, created, updated, action_run_id, error = row
        out = {"draft_id": draft_id, **json.loads(body), "sha256": digest, "status": status,
               "created_at": created, "updated_at": updated}
        if error:
            out["error"] = error
        out["_key"], out["_session_id"], out["_action_run_id"] = key, session_id, action_run_id
        return out

    def keys_with_pending_drafts(self, max_age_s: float = 7 * 86400) -> list[str]:
        """Tasks (any call) whose email draft still waits for Send / Deny / Revise, oldest first.
        A draft outlives the call that made it: ending the call must not lose the card."""
        cutoff = time.time() - max_age_s
        with self._lock:
            rows = self._db.execute(
                """SELECT idem_key, MIN(created) FROM email_drafts WHERE status IN ('pending','revising')
                   AND created >= ? GROUP BY idem_key ORDER BY MIN(created)""", (cutoff,)).fetchall()
        return [r[0] for r in rows]

    def drafts_for(self, key: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT draft_id,idem_key,session_id,draft_json,sha256,status,created,updated,action_run_id,error "
                "FROM email_drafts WHERE idem_key=? AND status!='superseded' ORDER BY created", (key,)).fetchall()
        return [{k: v for k, v in self._draft_row(r).items() if not k.startswith("_")} for r in rows]

    def draft_chain(self, key: str) -> list[dict[str, Any]]:
        """Every draft of one task, superseded ones too, oldest first (server-side fields kept)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT draft_id,idem_key,session_id,draft_json,sha256,status,created,updated,action_run_id,error "
                "FROM email_drafts WHERE idem_key=? ORDER BY created, rowid", (key,)).fetchall()
        return [self._draft_row(r) for r in rows]

    def transition_draft(self, draft_id: str, expected: set[str], status: str, *,
                         action_run_id: str | None = None, error: str | None = None) -> bool:
        """Compare-and-set a draft's status; False when it was not in an expected state."""
        marks = ",".join("?" * len(expected))
        with self._lock, self._db:
            cursor = self._db.execute(
                f"UPDATE email_drafts SET status=?, updated=?, action_run_id=COALESCE(?,action_run_id), error=? "
                f"WHERE draft_id=? AND status IN ({marks})",
                (status, time.time(), action_run_id, error, draft_id, *sorted(expected)))
        return cursor.rowcount == 1

    def draft_for_action_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT draft_id,idem_key,session_id,draft_json,sha256,status,created,updated,action_run_id,error "
                "FROM email_drafts WHERE action_run_id=?", (run_id,)).fetchone()
        return self._draft_row(row) if row else None

    def close(self) -> None:
        """Close the connection (idempotent). A reload must leave no connection behind: leftovers
        from old code generations kept stale -wal/-shm handles open in the gateway process, and every
        new connection to the same file then failed with "disk I/O error" (SPK-443)."""
        with self._lock:
            if self.closed:
                return
            self.closed = True
            _OPEN.discard(self)
            try:
                self._db.close()
            except sqlite3.Error:
                logger.debug("speakeasy: closing the state store failed", exc_info=True)


def close_all() -> int:
    """Close every StateStore this copy of the code opened (a reload, before the next code loads).
    Returns how many were still open."""
    stores = list(_OPEN)
    for store in stores:
        store.close()
    return len(stores)
