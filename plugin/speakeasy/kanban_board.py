"""Hermes Kanban boards, seen read-only from the apps (docs/API.md "Boards"; capability ``boards``).

Hermes keeps one Kanban SQLite file per board: ``<root>/kanban.db`` for ``default`` and
``<root>/kanban/boards/<slug>/kanban.db`` for every other one, with its name in ``board.json`` next to
it. ``<root>`` is shared by every profile (``HERMES_KANBAN_HOME``, else the default Hermes root), so a
profile under ``<root>/profiles/<name>`` sees the same boards.

Every read opens the file read-only (``mode=ro``): this module never creates, migrates or locks a
board, so it can't get in the dispatcher's way. Cards are grouped into the four columns the apps
show:

* **Ready**: triage, todo (waiting on another card), scheduled and ready;
* **Running**: running;
* **Blocked**: blocked and review, both waiting on a person ("Needs you");
* **Done**: done in the last ``DONE_DAYS`` days, newest first, at most ``DONE_LIMIT``.

A card opens the chat it ran in: the worker session named by the newest ``owner_admitted`` event of
the card, when that session lives in this profile's ``state.db`` (other profiles' sessions can't be
opened here, so those cards simply don't link).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BOARD = "default"
DONE_DAYS = 7
DONE_LIMIT = 30
COLUMN_LIMIT = 200
SUMMARY_CHARS = 200
REASON_CHARS = 240
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

COLUMNS = (("ready", "Ready"), ("running", "Running"), ("blocked", "Blocked"), ("done", "Done"))
COLUMN_OF = {"triage": "ready", "todo": "ready", "scheduled": "ready", "ready": "ready",
             "running": "running", "blocked": "blocked", "review": "blocked", "done": "done"}
# The small line under a card's title, when its own status says more than its column.
STATUS_NOTE = {"triage": "Being written up", "todo": "Waiting on another card", "scheduled": "Scheduled",
               "review": "Ready for review"}


class BoardError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def kanban_root(home: Path) -> Path:
    """Hermes' shared Kanban root for a profile home (mirrors ``hermes_cli.kanban_db.kanban_home``)."""
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    home = Path(home)
    if home.parent.name == "profiles":
        return home.parent.parent
    return home


def _plain(text: Any, limit: int) -> str:
    """One line of plain words: markdown marks and runs of whitespace gone, cut at a word."""
    s = str(text or "")
    s = re.sub(r"`{1,3}([^`]*)`{1,3}", r"\1", s)
    s = re.sub(r"\*\*|__|^#+\s*", "", s, flags=re.M)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = " ".join(s.split())
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(",;:.-")
    return cut + "…"


def _int(v: Any) -> int | None:
    try:
        return int(float(v)) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


class KanbanBoards:
    """The boards under one Kanban root, read-only."""

    def __init__(self, root: Path, state_db: Path | None = None, clock=time.time):
        self.root = Path(root)
        self.state_db = Path(state_db) if state_db else None
        self.clock = clock

    # -- where boards live -------------------------------------------------------------------------
    def _db(self, slug: str) -> Path:
        if slug == DEFAULT_BOARD:
            return self.root / "kanban.db"
        return self.root / "kanban" / "boards" / slug / "kanban.db"

    def _meta(self, slug: str) -> dict[str, Any]:
        path = (self.root / "kanban" / "board.json") if slug == DEFAULT_BOARD else \
            self.root / "kanban" / "boards" / slug / "board.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _name(self, slug: str, meta: dict[str, Any]) -> str:
        name = _plain(meta.get("name"), 60)
        if name:
            return name
        return "Main board" if slug == DEFAULT_BOARD else slug.replace("-", " ").replace("_", " ").capitalize()

    def _slugs(self) -> list[str]:
        slugs = [DEFAULT_BOARD] if self._db(DEFAULT_BOARD).is_file() else []
        boards = self.root / "kanban" / "boards"
        if boards.is_dir():
            for child in sorted(boards.iterdir(), key=lambda p: p.name.lower()):
                if child.is_dir() and SLUG.match(child.name) and child.name != DEFAULT_BOARD \
                        and (child / "kanban.db").is_file():
                    slugs.append(child.name)
        return slugs

    def _open(self, slug: str) -> sqlite3.Connection:
        path = self._db(slug)
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        conn.text_factory = lambda b: b.decode("utf-8", "replace")
        return conn

    # -- the list of boards ------------------------------------------------------------------------
    def list(self) -> dict[str, Any]:
        rows = []
        for slug in self._slugs():
            meta = self._meta(slug)
            if meta.get("archived"):
                continue
            try:
                counts, active = self._summary(slug)
            except sqlite3.Error as exc:
                logger.debug("speakeasy: kanban board %s unreadable: %s", slug, exc)
                continue
            rows.append({"slug": slug, "name": self._name(slug, meta), "counts": counts,
                         "needs_you": counts["blocked"], "active_at": active})
        rows.sort(key=lambda r: -(r["active_at"] or 0))
        # The board to show when the app has no remembered pick: one with work running or waiting on
        # the person, else the most recently active.
        busy = [r for r in rows if r["counts"]["running"] or r["counts"]["blocked"]]
        suggested = (busy or rows or [{"slug": None}])[0]["slug"]
        return {"boards": rows, "suggested": suggested}

    def _summary(self, slug: str) -> tuple[dict[str, int], int | None]:
        since = int(self.clock()) - DONE_DAYS * 86400
        counts = {key: 0 for key, _ in COLUMNS}
        with _closing(self._open(slug)) as conn:
            for status, n, recent in conn.execute(
                    "SELECT status, COUNT(*), SUM(CASE WHEN COALESCE(completed_at, created_at) >= ? THEN 1 ELSE 0 END)"
                    " FROM tasks WHERE status != 'archived' GROUP BY status", (since,)):
                col = COLUMN_OF.get(status)
                if col == "done":
                    counts["done"] += int(recent or 0)
                elif col:
                    counts[col] += int(n)
            active = conn.execute("SELECT MAX(created_at) FROM task_events").fetchone()[0]
            if active is None:
                active = conn.execute("SELECT MAX(created_at) FROM tasks").fetchone()[0]
        return counts, _int(active)

    # -- one board ---------------------------------------------------------------------------------
    def board(self, slug: str) -> dict[str, Any]:
        if not SLUG.match(slug or "") or slug not in self._slugs():
            raise BoardError(404, "That board isn't on your Hermes any more.")
        meta = self._meta(slug)
        now = int(self.clock())
        since = now - DONE_DAYS * 86400
        try:
            with _closing(self._open(slug)) as conn:
                tasks = conn.execute(
                    "SELECT id, title, status, assignee, priority, created_at, started_at, completed_at,"
                    " current_run_id, block_kind FROM tasks"
                    " WHERE status != 'archived' AND (status != 'done' OR COALESCE(completed_at, created_at) >= ?)",
                    (since,)).fetchall()
                ids = [t["id"] for t in tasks]
                events = self._events(conn, ids)
                runs = self._runs(conn, [t["current_run_id"] for t in tasks if t["current_run_id"]])
                summaries = self._summaries(conn, [t["id"] for t in tasks if t["status"] == "done"])
                change = conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
        except sqlite3.Error as exc:
            logger.debug("speakeasy: kanban board %s unreadable: %s", slug, exc)
            raise BoardError(503, "Your Hermes couldn't read that board just now. It will try again.") from None
        cards: dict[str, list[dict[str, Any]]] = {key: [] for key, _ in COLUMNS}
        for t in tasks:
            col = COLUMN_OF.get(t["status"])
            if not col:
                continue
            cards[col].append(self._card(t, col, events.get(t["id"], {}), runs.get(t["current_run_id"]),
                                         summaries.get(t["id"])))
        cards["ready"].sort(key=lambda c: (-c["priority"], c["since"] or 0))
        cards["running"].sort(key=lambda c: c["since"] or 0)
        cards["blocked"].sort(key=lambda c: -(c["since"] or 0))
        cards["done"].sort(key=lambda c: -(c["since"] or 0))
        cards["done"] = cards["done"][:DONE_LIMIT]
        columns = [{"key": key, "title": title, "count": len(cards[key]), "cards": cards[key][:COLUMN_LIMIT]}
                   for key, title in COLUMNS]
        return {"slug": slug, "name": self._name(slug, meta), "now": now, "change": f"{change or 0}",
                "done_days": DONE_DAYS, "columns": columns}

    def _card(self, t: sqlite3.Row, col: str, ev: dict[str, Any], run: sqlite3.Row | None,
              summary: str | None) -> dict[str, Any]:
        status = t["status"]
        if col == "running":
            since = _int(run["started_at"]) if run is not None and run["started_at"] else _int(t["started_at"])
        elif col == "done":
            since = _int(t["completed_at"])
        elif col == "blocked":
            since = ev.get("blocked_at") if status == "blocked" else ev.get("review_at")
            since = since or _int(t["started_at"]) or _int(t["created_at"])
        else:
            since = _int(t["created_at"])
        card: dict[str, Any] = {
            "id": t["id"], "title": _plain(t["title"], 160) or "Untitled card", "status": status,
            "column": col, "priority": _int(t["priority"]) or 0, "since": since,
            "created_at": _int(t["created_at"]), "needs_you": col == "blocked",
            "session_id": ev.get("session_id"),
        }
        note = STATUS_NOTE.get(status)
        if status == "blocked":
            card["reason"] = _plain(ev.get("reason"), REASON_CHARS) or None
            note = {"capability": "Can't go on without you", "transient": "Hit a problem"}.get(t["block_kind"] or "")
        if note:
            card["note"] = note
        if col == "done" and summary:
            card["summary"] = _plain(summary, SUMMARY_CHARS)
        if t["assignee"]:
            card["assignee"] = _plain(t["assignee"], 40)
        return {k: v for k, v in card.items() if v is not None}

    def _events(self, conn: sqlite3.Connection, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Per card: the session it ran in, when it was last blocked (and why), when it went to review."""
        out: dict[str, dict[str, Any]] = {i: {} for i in ids}
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                    f"SELECT task_id, kind, payload, created_at FROM task_events WHERE task_id IN ({marks})"
                    " AND kind IN ('owner_admitted', 'blocked', 'review_requested', 'review') ORDER BY id",
                    chunk):
                info = out.setdefault(row["task_id"], {})
                try:
                    payload = json.loads(row["payload"] or "{}")
                except ValueError:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                if row["kind"] == "owner_admitted":
                    sid = payload.get("session_id")
                    if isinstance(sid, str) and sid and self._here(payload.get("db")):
                        info["session_id"] = sid
                elif row["kind"] == "blocked":
                    info["blocked_at"] = _int(row["created_at"])
                    info["reason"] = payload.get("reason")
                else:
                    info["review_at"] = _int(row["created_at"])
        return out

    def _here(self, db: Any) -> bool:
        """The worker's session is in this profile's state.db (an unnamed db is the default one)."""
        if self.state_db is None:
            return True
        if not db:
            return True
        try:
            return Path(str(db)).expanduser().resolve() == self.state_db.expanduser().resolve()
        except OSError:
            return False

    @staticmethod
    def _runs(conn: sqlite3.Connection, run_ids: list[int]) -> dict[int, sqlite3.Row]:
        out: dict[int, sqlite3.Row] = {}
        for chunk in _chunks(run_ids):
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(f"SELECT id, started_at FROM task_runs WHERE id IN ({marks})", chunk):
                out[row["id"]] = row
        return out

    @staticmethod
    def _summaries(conn: sqlite3.Connection, ids: list[str]) -> dict[str, str]:
        """A done card's handoff: its newest finished run's summary."""
        out: dict[str, str] = {}
        for chunk in _chunks(ids):
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                    f"SELECT task_id, summary FROM task_runs WHERE task_id IN ({marks}) AND summary IS NOT NULL"
                    " AND summary != '' ORDER BY id", chunk):
                out[row["task_id"]] = row["summary"]
        return out


def _chunks(items: list, size: int = 400):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class _closing:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        return self.conn

    def __exit__(self, *exc) -> None:
        self.conn.close()
