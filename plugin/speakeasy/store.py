"""Durable call/task state (sqlite under ``<HERMES_HOME>/speakeasy/``).

Admission and run receipts make retries non-duplicating; work events and results feed the task
list; notices are deduplicated across restarts; email drafts carry their exact-content hash.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .emails import canonical_json, draft_sha256
from .failures import failure_reason, public_failure
from .text import (MAX_CARDS, AUTHORED_PRECEDENCE_S, SETTLED, TERMINAL, notice_text, public_result, safe_user_text,
                   legacy_short_title, short_title, valid_short_status)

STALE_AFTER_S = 90
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


def _is_stand_in(title: str | None, request: str | None, continued: str | None = None) -> bool:
    """A task title that is only the word-based label made from its own request (or missing). A task
    in a chat thread shows the thread's name, which a rename here would split from the thread itself."""
    if '"thread_id"' in (continued or ""):
        return False
    asked = notice_text(request, 1200) or ""
    return not title or title in {short_title(asked), legacy_short_title(asked)}


RECEIPTS = {"Request received", "Started working"}  # milestones that are receipts, not progress


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
    done = count if status == "completed" else min(
        sum(1 for e in events if e.get("kind") == "milestone" and e.get("text") not in RECEIPTS), count - 1)
    out["step_count"] = count
    out["step_index"] = count if status == "completed" else done + 1
    if status not in TERMINAL and created and done >= 2:
        elapsed = max(0.0, now - created)
        out["eta_seconds"] = int(round(elapsed / done * (count - done)))
    return out


class StateStore:
    def __init__(self, path: Path, hermes_home: Path | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.hermes_home = hermes_home  # where Hermes' errors.log lives, named when a failure has no known reason
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
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
            # What the user added to a running task by voice (/steer), and whether it is known to have
            # reached the agent: queued -> landed | not_read (see receipts.py). ``body`` is the exact text
            # sent, kept server-side only to look for it in the transcript; clients get ``words``.
            self._db.execute("""CREATE TABLE IF NOT EXISTS follow_ups (
                follow_up_id TEXT PRIMARY KEY, idem_key TEXT NOT NULL, words TEXT NOT NULL, body TEXT NOT NULL,
                milestone TEXT NOT NULL, state TEXT NOT NULL, after_id INTEGER NOT NULL,
                created REAL NOT NULL, settled REAL)""")
            self._db.execute("CREATE INDEX IF NOT EXISTS follow_ups_key ON follow_ups(idem_key, created)")
            # A new task held back because a running task is already editing the same project: it
            # starts when that one ends. Clients get the blocker's name in ``waiting``.
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(runs)")}
            if "waiting" not in columns:
                self._db.execute("ALTER TABLE runs ADD COLUMN waiting TEXT")
            if "own_title" not in columns:  # the task's name from its words or the titler, never a thread title
                self._db.execute("ALTER TABLE runs ADD COLUMN own_title TEXT")
            self._db.execute("CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id)")
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
            # Finished tasks that never had a Hermes run id (quick answers, home control) could not be
            # cleared: "Clear done" and the x address tasks by run id. Give them their local id.
            settled = sorted(TERMINAL | {"rejected"})
            for (key,) in self._db.execute(
                    f"SELECT idem_key FROM runs WHERE run_id IS NULL AND status IN ({','.join('?' * len(settled))})",
                    settled).fetchall():
                self._db.execute("UPDATE runs SET run_id=? WHERE idem_key=?", (local_run_id(key), key))

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

    def set_session(self, key: str, session_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET session_id=? WHERE idem_key=?", (session_id, key))

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
                "SELECT idem_key, interaction_id, status, updated, continued FROM runs "
                "WHERE run_id IS NULL AND status IN ('running','ambiguous')").fetchall()
        out = []
        for key, interaction, status, updated, continued in rows:
            try:
                placed = json.loads(continued) if continued else {}
            except ValueError:
                placed = {}
            placed = placed if isinstance(placed, dict) else {}
            out.append({"key": key, "interaction_id": interaction, "status": status, "updated": float(updated or 0),
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
        with self._lock:
            row = self._db.execute("SELECT idem_key FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return row[0] if row else None

    def run_row(self, key: str) -> dict[str, Any] | None:
        """One task's bookkeeping row: what a message sent to it by id needs (messages.py)."""
        with self._lock:
            row = self._db.execute("SELECT delegation_id, revision, run_id, status, dismissed, session_id, "
                                   "interaction_id FROM runs WHERE idem_key=?", (key,)).fetchone()
        if row is None:
            return None
        names = ("delegation_id", "revision", "run_id", "status", "dismissed", "session_id", "interaction_id")
        return dict(zip(names, row), idem_key=key)

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
            if title:
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

    def job(self, key: str, session_id: str | None, continued: str | None) -> dict[str, str]:
        """The job a task belongs to: {id, name}. The id is its Hermes session (else the session it
        continued, else the task itself); the name is set once, from the job's first task."""
        job_id = session_id or _placed_session(continued) or key
        with self._lock, self._db:
            row = self._db.execute("SELECT name FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row:
                return {"id": job_id, "name": row[0]}
            first = self._db.execute(
                "SELECT idem_key, own_title FROM runs WHERE (session_id=? OR idem_key=?) AND own_title IS NOT NULL "
                "ORDER BY rowid LIMIT 1", (job_id, key)).fetchone()
            if first is None:
                first = self._older_title(job_id, key)
            name = job_name(first[1]) if first else None
            if not name:
                # Not named yet (the titler is still working): a stand-in from the request, not stored.
                request = self._db.execute("SELECT text FROM work_events WHERE idem_key=? AND kind='request' "
                                           "ORDER BY seq LIMIT 1", (key,)).fetchone()
                return {"id": job_id, "name": job_name(short_title(request[0]) if request else None) or "Voice task"}
            self._db.execute("INSERT OR IGNORE INTO jobs (job_id,name,from_key,updated) VALUES (?,?,?,?)",
                             (job_id, name, first[0], time.time()))
        return {"id": job_id, "name": name}

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
        settled = sorted(TERMINAL | {"rejected"})
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
        settled = sorted(TERMINAL | {"rejected"})
        marks = ",".join("?" * len(settled))
        with self._lock, self._db:
            cursor = self._db.execute(
                f"UPDATE runs SET dismissed=1 WHERE idem_key=? AND status IN ({marks})", (key, *settled))
        return bool(cursor.rowcount)

    def set_aside(self, key: str) -> bool:
        """The user set this task's question (or approval) aside: already handled, or not needed.
        It settles as no longer needed, never as failed, and stops waiting on them. Only a settled
        task (finished, or waiting for approval) has anything to set aside."""
        settled = sorted(SETTLED | {"rejected"})
        marks = ",".join("?" * len(settled))
        with self._lock, self._db:
            cursor = self._db.execute(
                f"UPDATE runs SET set_aside=COALESCE(set_aside, ?) WHERE idem_key=? AND status IN ({marks})",
                (time.time(), key, *settled))
        return bool(cursor.rowcount)

    def mark_seen(self, key: str) -> None:
        """Nothing in this task's answer the user needs to read (a home command that went through):
        apps leave it out of New answers. Opening it still shows the answer."""
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET seen=1 WHERE idem_key=?", (key,))

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
            self._db.execute("UPDATE runs SET run_id=NULL WHERE idem_key=?", (key,))

    def update_run(self, key: str, run_id: str | None, status: str) -> None:
        now = time.time()
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET run_id=COALESCE(?,run_id),status=?,updated=? WHERE idem_key=?",
                             (run_id, status, now, key))
            if status in TERMINAL or status == "rejected":
                # Every finished task needs a run id so "Clear done" and the x can clear it.
                self._db.execute("UPDATE runs SET run_id=? WHERE idem_key=? AND run_id IS NULL",
                                 (local_run_id(key), key))
            if status in SETTLED:
                self._db.execute("UPDATE runs SET settled_at=? WHERE idem_key=?", (now, key))

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
            call["tasks"].append({"request": request, "title": title or "", "status": status, "result": result})
        return list(calls.values())

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
        return cur.rowcount > 0

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
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?",
                             (json.dumps(result) if result else None, key))

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
        return tasks + self.carried_draft_tasks({t["task_id"] for t in tasks} | {k for k, _ in rows})

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
            if work is not None:
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
            request = notice_text(request_text, 1200)
            if not request or not _is_stand_in(title, request_text, continued):
                continue  # a real name (the titler's, a chat thread's, an older row's), or nothing to title from
            out.append({"task_id": delegation_id or key, "title": title or "",
                        "request": request, "updated": float(updated or 0)})
            if len(out) >= limit:
                break
        return out

    def rename_stand_in(self, key: str, title: str) -> bool:
        """Set the app's on-device title only while the task still shows its stand-in label, checked
        and written under one lock: a name the cloud titler or a chat thread gave it always wins."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT r.title, r.continued, (SELECT e.text FROM work_events e WHERE e.idem_key=r.idem_key "
                "AND e.kind='request' ORDER BY e.seq LIMIT 1) FROM runs r WHERE r.idem_key=?", (key,)).fetchone()
            if not row or not _is_stand_in(row[0], row[2], row[1]):
                return False
            self._db.execute("UPDATE runs SET title=? WHERE idem_key=?", (title, key))
            name = job_name(title)
            if name:
                self._db.execute("UPDATE runs SET own_title=? WHERE idem_key=?", (title, key))
                # The job this task named takes the new name only while it still has the stand-in's
                # name; a job named any other way keeps its name.
                stand_ins = {n for n in (job_name(row[0]), job_name(short_title(notice_text(row[2], 1200) or "")),
                                         job_name(legacy_short_title(notice_text(row[2], 1200) or "")),
                                         "Voice task") if n}
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
             assistant_name: str = "Hermes") -> dict[str, Any] | None:
        """The latest admitted job or one exact server-owned run, including past calls."""
        cols = """idem_key,run_id,status,updated,short_status,detail,progress_updated,
                  status_source,result_json,title,dismissed,summary,continued,settled_at,failure,shared,session_id,set_aside,seen,waiting,
                  created,failure_hint"""
        with self._lock:
            if idem_key:
                row = self._db.execute(f"SELECT {cols} FROM runs WHERE idem_key=?", (idem_key,)).fetchone()
            elif run_id:
                row = self._db.execute(f"SELECT {cols} FROM runs WHERE run_id=?", (run_id,)).fetchone()
            else:
                row = self._db.execute(f"SELECT {cols} FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
            if row is None:
                return None
            (key, actual_run_id, status, updated, short_status, detail, progress_updated,
             status_source, result_json, title, dismissed, summary, continued, settled_at, failure, shared,
             session_id, set_aside, seen, waiting, created, failure_hint) = row
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
        work = {
            "run_id": actual_run_id, "status": status, "stale": stale, "updated": updated,
            "events": [{"kind": kind, "text": text, "at": created} for kind, text, created in reversed(events)],
            "short_status": display_status, "detail": display_detail,
            "source_run_id": actual_run_id, "updated_at": display_updated,
            "status_source": source, "result": public_result(result),
            "title": title, "summary": summary, "dismissed": bool(dismissed),
            "email_drafts": self.drafts_for(key),
            "plan": self.plan(key),
            "job": self.job(key, session_id, continued),
        }
        if set_aside:
            # Its question was set aside: nothing waits on the user any more (only sent when true).
            work["set_aside"] = True
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
                work["short_status"] = "Waiting for another task"
                work["detail"] = f"Starts when \"{work['waiting']['name']}\" finishes (same project)."
                work["status_source"] = "system"
        work.update(job_progress(status, created, settled_at, work["plan"], work["events"]))
        # One short plain sentence for a failed (or restart-interrupted) task, never "unknown".
        work["failure_reason"] = failure_reason(status, failure, failure_hint)
        why = public_failure(failure, self.hermes_home) if status == "failed" else None
        if why:
            # Speakeasy's own words for why it failed: a row label when the reason is known, and a
            # sentence for the detail. The provider's error text never reaches here.
            work["failure"] = why
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
        if continued:
            try:
                work["continued_in"] = json.loads(continued).get("label")
            except ValueError:
                pass
        return work

    # -- email drafts ------------------------------------------------------------------
    def add_draft(self, key: str, session_id: str | None, draft: dict[str, Any]) -> dict[str, Any]:
        """Store one draft for a task. Older pending/revising drafts of that task (or of the same
        Hermes session, e.g. a spoken "revise it" follow-up) are superseded and can no longer be
        approved."""
        now = time.time()
        draft_id = "ed_" + secrets.token_hex(12)
        body, digest = canonical_json(draft), draft_sha256(draft)
        with self._lock, self._db:
            self._db.execute(
                "UPDATE email_drafts SET status='superseded', updated=? WHERE status IN ('pending','revising') "
                "AND (idem_key=? OR (? IS NOT NULL AND session_id=?))", (now, key, session_id, session_id))
            self._db.execute("INSERT INTO email_drafts VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (draft_id, key, session_id, body, digest, "pending", now, now, None, None))
        return self.draft(draft_id) or {}

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
        with self._lock:
            self._db.close()
