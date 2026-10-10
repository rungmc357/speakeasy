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
from .failures import public_failure
from .text import (MAX_CARDS, AUTHORED_PRECEDENCE_S, SETTLED, TERMINAL, notice_text, public_result, safe_user_text,
                   valid_short_status)

STALE_AFTER_S = 90
MAX_REVIEWS = 3  # finished-image review cards carried onto the call panel


def local_run_id(idem_key: str) -> str:
    """The id for a task Hermes ran without a run id (same scheme as calls.LOCAL_RUN_PREFIX)."""
    return "lr_" + hashlib.sha256(idem_key.encode()).hexdigest()[:32]


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
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(runs)")}
            if "timings" not in columns:  # added after the first release
                self._db.execute("ALTER TABLE runs ADD COLUMN timings TEXT")
            if "failure" not in columns:  # why a run failed: a failures.py kind, never provider text
                self._db.execute("ALTER TABLE runs ADD COLUMN failure TEXT")
            if "plan" not in columns:  # the steps a handed-off task will go through (JSON list)
                self._db.execute("ALTER TABLE runs ADD COLUMN plan TEXT")
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
                """INSERT INTO runs (idem_key,interaction_id,delegation_id,revision,run_id,status,updated)
                   VALUES (?,?,?,?,?,?,?)""",
                (key, interaction_id, delegation_id, revision, None, "admitting", time.time()))
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

    def title(self, key: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT title FROM runs WHERE idem_key=?", (key,)).fetchone()
        return row[0] if row and row[0] else None

    def set_title(self, key: str, title: str | None, summary: str | None = None) -> None:
        with self._lock, self._db:
            if title:
                self._db.execute("UPDATE runs SET title=? WHERE idem_key=?", (title, key))
            if summary:
                self._db.execute("UPDATE runs SET summary=? WHERE idem_key=?", (summary, key))

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

    def set_failure(self, key: str, kind: str | None) -> None:
        """Why the run failed, as a ``failures`` kind (the app and voice get Speakeasy's own words)."""
        with self._lock, self._db:
            self._db.execute("UPDATE runs SET failure=? WHERE idem_key=?", (kind, key))

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
                   WHERE settled_at > ? AND run_id IS NOT NULL ORDER BY settled_at DESC LIMIT ?""",
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
                out.append(work)
        return out

    def task_id_for(self, key: str) -> str:
        """The task id the app knows this row by (its delegation id, as on carried rows)."""
        with self._lock:
            row = self._db.execute("SELECT delegation_id FROM runs WHERE idem_key=?", (key,)).fetchone()
        return (row[0] if row and row[0] else key)

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
                  status_source,result_json,title,dismissed,summary,continued,settled_at,failure"""
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
             status_source, result_json, title, dismissed, summary, continued, settled_at, failure) = row
            events = self._db.execute(
                "SELECT kind,text,created FROM work_events WHERE idem_key=? ORDER BY seq DESC LIMIT 30", (key,)).fetchall()
        stale = status not in TERMINAL | {"waiting_for_approval"} and time.time() - updated > STALE_AFTER_S
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
        }
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
