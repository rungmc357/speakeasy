"""Kanban boards, read-only, from the apps: columns, times, Needs you, the session a card ran in, the
board list and its suggested pick, and the HTTP routes. Boards are invented in a temp root."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from fakes import http
from speakeasy import kanban_board as K

NOW = 1_800_000_000

SCHEMA = """
CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, assignee TEXT, status TEXT NOT NULL,
    priority INTEGER DEFAULT 0, created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
    completed_at INTEGER, current_run_id INTEGER, block_kind TEXT, session_id TEXT);
CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
    kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL);
CREATE TABLE task_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, profile TEXT,
    status TEXT NOT NULL, started_at INTEGER NOT NULL, ended_at INTEGER, outcome TEXT, summary TEXT);
"""


def make_board(root: Path, slug: str, name: str | None = None, archived: bool = False) -> sqlite3.Connection:
    d = root if slug == "default" else root / "kanban" / "boards" / slug
    d.mkdir(parents=True, exist_ok=True)
    if name is not None or archived:
        meta_dir = root / "kanban" if slug == "default" else d
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "board.json").write_text(json.dumps({"slug": slug, "name": name, "archived": archived}))
    conn = sqlite3.connect(d / "kanban.db")
    conn.executescript(SCHEMA)
    return conn


def task(conn, tid, title, status, created, started=None, completed=None, priority=0, block_kind=None,
         run_started=None, summary=None):
    run_id = None
    if run_started is not None or summary is not None:
        cur = conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, summary) VALUES (?,?,?,?,?)",
                           (tid, "default", "done" if summary else "running", run_started or created, summary))
        run_id = cur.lastrowid
    conn.execute("INSERT INTO tasks (id, title, assignee, status, priority, created_at, started_at, completed_at,"
                 " current_run_id, block_kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (tid, title, "default", status, priority, created, started, completed, run_id, block_kind))
    conn.commit()


def event(conn, tid, kind, payload, at):
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                 (tid, kind, json.dumps(payload), at))
    conn.commit()


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "hermes"


def boards(root: Path, state_db: Path | None = None) -> K.KanbanBoards:
    return K.KanbanBoards(root, state_db, clock=lambda: NOW)


def test_columns_times_and_needs_you(root):
    state = root / "state.db"
    c = make_board(root, "garden", "Garden club site")
    task(c, "t_ready", "Write the **sign-up** page", "ready", NOW - 600, priority=2)
    task(c, "t_todo", "Add a photo gallery", "todo", NOW - 300)
    task(c, "t_run", "Fix the RSVP form", "running", NOW - 3600, started=NOW - 1800, run_started=NOW - 900)
    task(c, "t_block", "Choose a color for the header", "blocked", NOW - 7200, started=NOW - 7000,
         block_kind="needs_input")
    event(c, "t_block", "blocked", {"reason": "Need a pick between `green` and blue", "kind": "needs_input"}, NOW - 400)
    task(c, "t_review", "Rename the newsletter", "review", NOW - 5000)
    event(c, "t_review", "review_requested", {}, NOW - 200)
    task(c, "t_done", "Move the event calendar", "done", NOW - 9000, completed=NOW - 120,
         summary="Moved the calendar to the top of the page and checked it on a phone.")
    task(c, "t_old", "Old work from last month", "done", NOW - 40 * 86400, completed=NOW - 30 * 86400)
    task(c, "t_gone", "Archived work", "archived", NOW - 100)
    event(c, "t_run", "owner_admitted", {"db": str(state), "session_id": "local-run-1"}, NOW - 900)
    event(c, "t_done", "owner_admitted", {"db": str(root / "profiles" / "work" / "state.db"),
                                         "session_id": "local-other-profile"}, NOW - 9000)

    b = boards(root, state).board("garden")
    assert b["name"] == "Garden club site" and b["now"] == NOW
    cols = {col["key"]: col for col in b["columns"]}
    assert [col["key"] for col in b["columns"]] == ["ready", "running", "blocked", "done"]
    assert [x["id"] for x in cols["ready"]["cards"]] == ["t_ready", "t_todo"]  # priority first
    assert cols["ready"]["cards"][0]["title"] == "Write the sign-up page"
    assert cols["ready"]["cards"][1]["note"] == "Waiting on another card"
    run = cols["running"]["cards"][0]
    assert run["since"] == NOW - 900 and run["session_id"] == "local-run-1" and not run["needs_you"]
    blocked = {x["id"]: x for x in cols["blocked"]["cards"]}
    assert blocked["t_block"]["needs_you"] and blocked["t_block"]["since"] == NOW - 400
    assert blocked["t_block"]["reason"] == "Need a pick between green and blue"
    assert blocked["t_review"]["note"] == "Ready for review" and blocked["t_review"]["since"] == NOW - 200
    done = cols["done"]["cards"]
    assert [x["id"] for x in done] == ["t_done"]  # last month's work is not "recent"
    assert done[0]["since"] == NOW - 120 and done[0]["summary"].startswith("Moved the calendar")
    assert "session_id" not in done[0]  # that session lives in another profile


def test_board_list_names_counts_and_suggested_pick(root):
    a = make_board(root, "default")
    task(a, "t1", "Tidy notes", "done", NOW - 100, completed=NOW - 50)
    event(a, "t1", "completed", {}, NOW - 50)
    b = make_board(root, "bake-sale", "Bake sale")
    task(b, "t2", "Order napkins", "blocked", NOW - 9000)
    event(b, "t2", "blocked", {"reason": "Which color?"}, NOW - 8000)
    make_board(root, "old-trip", "Old trip", archived=True)
    (root / "kanban" / "boards" / "not a board").mkdir(parents=True)

    listed = boards(root).list()
    slugs = [r["slug"] for r in listed["boards"]]
    assert slugs == ["default", "bake-sale"]  # newest activity first; archived and junk dirs skipped
    by = {r["slug"]: r for r in listed["boards"]}
    assert by["default"]["name"] == "Main board" and by["default"]["counts"]["done"] == 1
    assert by["bake-sale"]["needs_you"] == 1
    assert listed["suggested"] == "bake-sale"  # something waits on you there


def test_unknown_board_and_bad_slug(root):
    make_board(root, "garden")
    kb = boards(root)
    for slug in ("nope", "../garden", ""):
        with pytest.raises(K.BoardError) as e:
            kb.board(slug)
        assert e.value.status == 404


def test_reads_never_write(root):
    c = make_board(root, "garden")
    task(c, "t1", "Water the plants", "running", NOW - 60, started=NOW - 60)
    c.close()
    db = root / "kanban" / "boards" / "garden" / "kanban.db"
    before = db.read_bytes()
    boards(root).board("garden")
    boards(root).list()
    assert db.read_bytes() == before
    assert not (root / "kanban.db").exists()  # never creates the default board


def test_kanban_root_for_profiles(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_HOME", raising=False)
    assert K.kanban_root(tmp_path / "h" / "profiles" / "work") == tmp_path / "h"
    assert K.kanban_root(tmp_path / "h") == tmp_path / "h"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "elsewhere"))
    assert K.kanban_root(tmp_path / "h") == tmp_path / "elsewhere"


def test_routes(server, service):
    root = service.boards.root
    c = make_board(root, "garden", "Garden club site")
    task(c, "t1", "Fix the RSVP form", "running", NOW - 60, started=NOW - 60)
    status, body = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert status == 200 and body["capabilities"]["boards"] is True
    status, listed = http(server.base_url, "GET", "/voice/boards", token=server.token)
    assert status == 200 and [r["slug"] for r in listed["boards"]] == ["garden"]
    status, board = http(server.base_url, "GET", "/voice/boards/garden", token=server.token)
    assert status == 200 and board["columns"][1]["cards"][0]["title"] == "Fix the RSVP form"
    status, missing = http(server.base_url, "GET", "/voice/boards/nope", token=server.token)
    assert status == 404 and "board" in missing["error"].lower()
    assert http(server.base_url, "GET", "/voice/boards")[0] == 401
