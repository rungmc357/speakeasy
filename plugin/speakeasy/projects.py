"""Projects (SPK-254): Hermes' own first-class projects, shown and filled from Speakeasy.

Hermes keeps projects in ``$HERMES_HOME/projects.db`` (``hermes_cli.projects_db``): a name, an
optional icon/colour, an optional Kanban board and folders. A session belongs to a project when its
working folder is inside one of the project's folders (``project_for_path``). That covers coding
sessions; chats, calls and Home jobs have no folder, so they never land in a project on their own.

So this module:

* reads Hermes' projects through Hermes' own module (never raw SQL against its file, never a write
  to a column we don't know), and only opens the file when it exists (a read never creates it);
* creates a project from the app as a name-only Hermes project (``create_project`` takes no folder),
  so ``hermes project list`` and Hermes' desktop show the same project;
* keeps the links Hermes has no place for in the Speakeasy store: a chat (Hermes session id) or a job
  (its first task's key) filed into a project, a declined suggestion, a project's standing notes and
  "don't offer a project for X again";
* suggests a project for a new job from its own words (only existing projects, plain word matching,
  no model call) and offers once to make a project when three or more chats share a subject.

Nothing here ever files anything on its own: a link is made only by an explicit user action (a
"for <project>, ..." message at Home, the job card's chip, Move to project).
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import runstate as RS

logger = logging.getLogger(__name__)

NAME_MAX = 60
NOTES_MAX = 1200          # what a project's standing notes may hold (they ride in every filed job's brief)
NOTES_IN_BRIEF = 800      # how much of them a brief carries
ICON_RE = re.compile(r"^[a-z0-9]+(?:\.[a-z0-9]+){0,4}$")  # an SF Symbol name ("hammer", "leaf.fill")
COLORS = ("blue", "teal", "green", "yellow", "orange", "red", "pink", "purple", "gray")
OFFER_AFTER = 3           # chats on one subject before "Make a project for X?"
OFFER_WINDOW_S = 21 * 86400
PENDING_S = 120.0         # a "for X, ..." job is matched to its start within this long
SELECTED_META = "projects_home_selected"   # the composer chip: where Home files new work
AUTO_META = "project_auto:"                 # + job key: filed by the chip, not by words or a tap
ASKED_META = "project_asked:"               # + job key: its once-only "file into X?" was answered

# Leading words that start most requests: never a subject ("Find ...", "Check ...").
_NOT_SUBJECTS = set("""
a an the and or but for to of in on at by with from about into over under after before why how what
when where who which whose is are was were be been do does did can could should would will may might
find check fix book plan add make look get update write draft send show tell help start stop set
compare research review build test ship debug clean move order buy call email text read summarize
summary list create remove delete open close new old my your our their this that these those it its
today tomorrow tonight yesterday morning evening week weekend monday tuesday wednesday thursday friday
saturday sunday january february march april may june july august september october november december
i im ive id me we us you he she they them his her task tasks job jobs chat chats project projects
speakeasy hermes quick question answer reply again still more some any all one two three
""".split())


def _hermes_projects():
    """``hermes_cli.projects_db``, or None on a Hermes without projects."""
    try:
        from hermes_cli import projects_db  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - an older Hermes: the feature is simply off
        return None
    return projects_db if hasattr(projects_db, "create_project") else None


def available() -> bool:
    return _hermes_projects() is not None


def _words(text: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


def _phrase_in(name: str, text: str) -> bool:
    """*name*'s words appear together, whole-word, in *text* (case-insensitive)."""
    want = _words(name)
    have = _words(text)
    if not want or not have:
        return False
    n = len(want)
    return any(have[i:i + n] == want for i in range(len(have) - n + 1))


class ProjectError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class Projects:
    """Hermes' projects plus Speakeasy's links. *state_db*: Hermes' sessions (read-only), for
    folder-resolved membership. *db_path*: Hermes' projects.db (tests pass their own)."""

    def __init__(self, store: Any, hermes_home: Path, state_db: Callable[[], Path],
                 db_path: Path | None = None, clock: Callable[[], float] = time.time):
        self.store = store
        self.home = Path(hermes_home)
        self.state_db = state_db
        self.db_path = Path(db_path) if db_path else self.home / "projects.db"
        self.clock = clock
        self._pending: dict[str, tuple[str, float]] = {}   # normalized job words -> (project id, at)
        self._lock = threading.Lock()
        self._cwd_cache: tuple[Any, dict[str, str]] | None = None
        with store._lock, store._db:
            db = store._db
            # kind: "session" (a Hermes session id) or "job" (a job's first task key). project_id ""
            # records a declined suggestion for that job (its chip never shows again).
            db.execute("""CREATE TABLE IF NOT EXISTS project_links (
                kind TEXT NOT NULL, target TEXT NOT NULL, project_id TEXT NOT NULL, at REAL NOT NULL,
                PRIMARY KEY (kind, target))""")
            db.execute("""CREATE TABLE IF NOT EXISTS project_notes (
                project_id TEXT PRIMARY KEY, notes TEXT NOT NULL, updated REAL NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS project_offers (
                subject TEXT PRIMARY KEY, dismissed REAL NOT NULL)""")

    # -- Hermes' projects -----------------------------------------------------------------------

    def _read(self) -> list[Any]:
        """Hermes' projects (not archived), oldest first; [] when Hermes has none or no file yet."""
        pdb = _hermes_projects()
        if pdb is None or not self.db_path.exists():
            return []
        try:
            with pdb.connect_closing(self.db_path) as conn:
                return list(pdb.list_projects(conn))
        except Exception as exc:  # noqa: BLE001 - an unreadable file lists nothing, never a 500
            logger.warning("speakeasy: couldn't read Hermes projects (%s)", type(exc).__name__)
            return []

    def _get(self, project_id: str) -> Any:
        for p in self._read():
            if p.id == project_id or p.slug == project_id:
                return p
        raise ProjectError(404, "I couldn't find that project.")

    @staticmethod
    def _public(p: Any) -> dict[str, Any]:
        color = (p.color or "").strip().lower() or None
        icon = (p.icon or "").strip() or None
        return {"id": p.id, "slug": p.slug, "name": p.name, "icon": icon if icon and ICON_RE.fullmatch(icon) else None,
                "color": color if color in COLORS else None, "board": p.board_slug or None,
                "has_folders": bool(p.folders)}

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        """A name-only Hermes project (optional SF Symbol + colour). *from_offer*: the subject of a
        "Make a project for X?" offer; its chats are filed into the new project."""
        pdb = _hermes_projects()
        if pdb is None:
            raise ProjectError(501, "This Hermes doesn't have projects yet. Update Hermes to use them.")
        if not isinstance(body, dict) or not set(body) <= {"name", "icon", "color", "from_offer", "sessions"}:
            raise ProjectError(400, "A project takes a name, and optionally an icon and a colour.")
        name = body.get("name")
        if not isinstance(name, str) or not " ".join(name.split()) or len(name) > NAME_MAX:
            raise ProjectError(400, f"A project name is 1 to {NAME_MAX} characters.")
        name = " ".join(name.split())
        icon, color = body.get("icon"), body.get("color")
        if icon is not None and (not isinstance(icon, str) or not ICON_RE.fullmatch(icon)):
            raise ProjectError(400, "icon is an SF Symbol name.")
        if color is not None and color not in COLORS:
            raise ProjectError(400, f"color is one of: {', '.join(COLORS)}.")
        if any(p.name.lower() == name.lower() for p in self._read()):
            raise ProjectError(409, "You already have a project with that name.")
        sessions = body.get("sessions") or []
        if not isinstance(sessions, list) or not all(isinstance(s, str) and len(s) <= 200 for s in sessions):
            raise ProjectError(400, "sessions is a list of conversation ids.")
        with pdb.connect_closing(self.db_path) as conn:
            pid = pdb.create_project(conn, name=name, icon=icon, color=color)
        offer = body.get("from_offer")
        if isinstance(offer, str) and offer.strip():
            sessions = list(sessions) + [s for s in self._offer_sessions(offer) if s not in sessions]
            self.dismiss_offer(offer)
        for sid in sessions[:200]:
            self.link("session", sid, pid)
        return {"project": self._summary(self._get(pid))}

    # -- links (Speakeasy's) --------------------------------------------------------------------

    def link(self, kind: str, target: str, project_id: str | None) -> None:
        """File *target* into *project_id*; None takes it out; "" declines a suggestion."""
        with self.store._lock, self.store._db:
            if project_id is None:
                self.store._db.execute("DELETE FROM project_links WHERE kind=? AND target=?", (kind, target))
            else:
                self.store._db.execute("INSERT OR REPLACE INTO project_links (kind, target, project_id, at) "
                                       "VALUES (?,?,?,?)", (kind, target, project_id, self.clock()))

    def _links(self) -> dict[tuple[str, str], str]:
        with self.store._lock:
            rows = self.store._db.execute("SELECT kind, target, project_id FROM project_links").fetchall()
        return {(k, t): p for k, t, p in rows}

    def _job_key(self, key: str) -> str:
        """The key a job is filed under: its first task (a follow-up's job is its parent's)."""
        seen: set[str] = set()
        with self.store._lock:
            while key not in seen:
                seen.add(key)
                row = self.store._db.execute("SELECT job_id FROM job_links WHERE idem_key=?", (key,)).fetchone()
                if row is None or row[0] == key:
                    break
                key = row[0]
        return key

    def project_of_key(self, key: str, links: dict[tuple[str, str], str] | None = None) -> str | None:
        """The project a task's job is filed into (None: not filed, or declined)."""
        links = self._links() if links is None else links
        got = links.get(("job", self._job_key(key)))
        if got is None:
            session = self.store.session_for(key)
            got = links.get(("session", session)) if session else None
        return got or None

    def _cwd_projects(self) -> dict[str, str]:
        """{session id: project id} for sessions whose working folder is in a project's folder."""
        pdb = _hermes_projects()
        if pdb is None or not self.db_path.exists():
            return {}
        path = Path(self.state_db())
        try:
            stamp = (path.stat().st_mtime, self.db_path.stat().st_mtime)
        except OSError:
            return {}
        if self._cwd_cache and self._cwd_cache[0] == stamp:
            return self._cwd_cache[1]
        out: dict[str, str] = {}
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
            try:
                cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
                rows = (db.execute("SELECT id, cwd FROM sessions WHERE cwd IS NOT NULL AND cwd != ''").fetchall()
                        if "cwd" in cols else [])
            finally:
                db.close()
            by_cwd: dict[str, str | None] = {}
            with pdb.connect_closing(self.db_path) as conn:
                for sid, cwd in rows:
                    if cwd not in by_cwd:
                        p = pdb.project_for_path(conn, cwd)
                        by_cwd[cwd] = p.id if p else None
                    if by_cwd[cwd]:
                        out[sid] = by_cwd[cwd]  # type: ignore[assignment]
        except Exception as exc:  # noqa: BLE001 - membership by folder is a bonus, never a failure
            logger.debug("speakeasy: folder membership unreadable (%s)", type(exc).__name__)
            return {}
        self._cwd_cache = (stamp, out)
        return out

    def _job_rows(self) -> list[tuple[str, str | None, str | None, float]]:
        """(task key, session, title, created) of every task Speakeasy ran, newest first."""
        with self.store._lock:
            return self.store._db.execute(
                "SELECT idem_key, session_id, title, COALESCE(created, updated) FROM runs "
                "WHERE status != 'rejected' AND COALESCE(dismissed,0)=0 ORDER BY rowid DESC LIMIT 2000").fetchall()

    def members(self, project_id: str) -> dict[str, Any]:
        """{"sessions": set of Hermes session ids, "jobs": [task keys]} filed into or resolved to it."""
        links = self._links()
        sessions = {sid for sid, p in self.session_projects().items() if p == project_id}
        jobs: list[str] = []
        seen_jobs: set[str] = set()
        for key, session, _title, _at in self._job_rows():
            if self.project_of_key(key, links) != project_id:
                continue
            job = self._job_key(key)
            if job in seen_jobs:
                continue
            seen_jobs.add(job)
            jobs.append(key)
        return {"sessions": sessions, "jobs": jobs}

    def session_projects(self) -> dict[str, str]:
        """{session id: project id} for every chat in a project (direct links win over folders)."""
        out = dict(self._cwd_projects())
        links = self._links()
        for key, session, _t, _a in self._job_rows():
            if session and session not in out:
                p = self.project_of_key(key, links)
                if p:
                    out[session] = p
        for (k, t), p in links.items():
            if k == "session":
                if p:
                    out[t] = p
                else:
                    out.pop(t, None)
        return out

    # -- reads ----------------------------------------------------------------------------------

    def _summary(self, p: Any, counts: dict[str, int] | None = None) -> dict[str, Any]:
        out = self._public(p)
        if counts is None:
            m = self.members(p.id)
            counts = {"chats": len(m["sessions"]), "jobs": len(m["jobs"])}
        out.update(counts)
        notes = self.notes(p.id)
        if notes:
            out["notes"] = notes
        return out

    def list(self) -> dict[str, Any]:
        """``GET /voice/projects``: every project with its counts, plus at most one offer."""
        projects = self._read()
        chats: dict[str, set[str]] = {}
        for sid, pid in self.session_projects().items():
            chats.setdefault(pid, set()).add(sid)
        links = self._links()
        jobs: dict[str, set[str]] = {}
        for key, _s, _t, _a in self._job_rows():
            pid = self.project_of_key(key, links)
            if pid:
                jobs.setdefault(pid, set()).add(self._job_key(key))
        live, active = self._activity(links)
        rows = []
        for p in projects:
            row = self._summary(p, {"chats": len(chats.get(p.id, ())), "jobs": len(jobs.get(p.id, ()))})
            if live.get(p.id):
                row["running"] = live[p.id]
            if active.get(p.id):
                row["active_at"] = round(active[p.id], 3)
            rows.append(row)
        out: dict[str, Any] = {"projects": rows}
        selected = self.selected(projects)
        if selected:
            out["selected"] = selected
        offer = self.offer(projects)
        if offer:
            out["offer"] = offer
        return out

    def page(self, project_id: str, history: Callable[[set[str]], dict[str, Any]],
             work: Callable[[str], dict[str, Any] | None], boards: Callable[[str], dict[str, Any] | None]
             ) -> dict[str, Any]:
        """``GET /voice/projects/{id}``: the project, its chats (as history rows), its jobs (newest
        first) and its linked Kanban board, if any."""
        p = self._get(project_id)
        m = self.members(p.id)
        page = history(m["sessions"]) if m["sessions"] else {"sessions": []}
        jobs = [j for j in (work(k) for k in m["jobs"][:30]) if j]
        out: dict[str, Any] = {"project": self._summary(p, {"chats": len(m["sessions"]), "jobs": len(m["jobs"])}),
                               "chats": page.get("sessions") or [], "jobs": jobs}
        if p.board_slug:
            board = boards(p.board_slug)
            if board:
                out["board"] = board
        return out

    def _activity(self, links: dict[tuple[str, str], str]) -> tuple[dict[str, int], dict[str, float]]:
        """({project id: running jobs}, {project id: newest activity}) for the panel's quiet line
        ("2 running" or "3h ago"). Activity is a filed job's last update or a chat's filing time."""
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT idem_key, status, COALESCE(updated, created) FROM runs "
                "WHERE status != 'rejected' AND COALESCE(dismissed,0)=0 ORDER BY rowid DESC LIMIT 2000").fetchall()
        live: dict[str, set[str]] = {}
        active: dict[str, float] = {}
        for key, status, at in rows:
            pid = self.project_of_key(key, links)
            if not pid:
                continue
            if status in RS.LIVE:
                live.setdefault(pid, set()).add(self._job_key(key))
            if at and float(at) > active.get(pid, 0.0):
                active[pid] = float(at)
        with self.store._lock:
            for pid, at in self.store._db.execute(
                    "SELECT project_id, MAX(at) FROM project_links WHERE project_id != '' GROUP BY project_id"):
                if at and float(at) > active.get(pid, 0.0):
                    active[pid] = float(at)
        return {k: len(v) for k, v in live.items()}, active

    # -- where Home works: the composer's project chip ------------------------------------------

    def selected(self, projects: list[Any] | None = None) -> str | None:
        """The project Home works in (the composer's chip), or None for Home itself. Stored here so
        the phone and the Mac agree; a project that no longer exists reads as Home."""
        pid = self.store.get_meta(SELECTED_META) or ""
        if not pid:
            return None
        projects = self._read() if projects is None else projects
        return pid if any(p.id == pid for p in projects) else None

    def select(self, body: dict[str, Any]) -> dict[str, Any]:
        """``PUT /voice/projects/selected {project_id: id | null}``: where new work from Home (typed,
        voice memos, follow-ups, calls) is filed. null = Home, not in a project."""
        if not isinstance(body, dict) or set(body) != {"project_id"}:
            raise ProjectError(400, "Send {\"project_id\": a project's id, or null for Home}.")
        pid = body["project_id"]
        if pid is not None and (not isinstance(pid, str) or not pid):
            raise ProjectError(400, "project_id is a project's id, or null for Home.")
        pid = self._get(pid).id if pid else None
        self.store.set_meta(SELECTED_META, pid or "")
        return {"ok": True, "selected": pid}

    def auto_file(self, key: str) -> str | None:
        """File a job started from Home (or on a call) into the selected project, unless the job is
        already filed, declined or its chat is in a project. Returns the project id it was filed into."""
        pid = self.selected()
        if not pid:
            return None
        job = self._job_key(key)
        links = self._links()
        if ("job", job) in links:
            return None
        session = self.store.session_for(key)
        if session and (("session", session) in links or session in self._cwd_projects()):
            return None
        self.link("job", job, pid)
        self.mark_auto(key)
        return pid

    def mark_auto(self, key: str) -> None:
        """This job was filed by the Home chip, not by the person's words or a tap: if its words name
        another project, its card asks once ("Garden shed?") instead of moving it silently."""
        self.store.set_meta(AUTO_META + self._job_key(key), "1")

    def is_auto(self, job: str) -> bool:
        return bool(self.store.get_meta(AUTO_META + job)) and not self.store.get_meta(ASKED_META + job)

    def asked(self, key: str) -> None:
        """The once-only "file into X?" on an auto-filed job was answered (moved or declined)."""
        self.store.set_meta(ASKED_META + self._job_key(key), "1")

    # -- standing notes -------------------------------------------------------------------------

    def notes(self, project_id: str) -> str:
        with self.store._lock:
            row = self.store._db.execute("SELECT notes FROM project_notes WHERE project_id=?", (project_id,)).fetchone()
        return row[0] if row else ""

    def set_notes(self, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
        self._get(project_id)
        if not isinstance(body, dict) or set(body) != {"notes"} or not isinstance(body["notes"], str):
            raise ProjectError(400, "Send {\"notes\": \"...\"}.")
        notes = body["notes"].strip()
        if len(notes) > NOTES_MAX:
            raise ProjectError(400, f"Notes are at most {NOTES_MAX} characters.")
        with self.store._lock, self.store._db:
            if notes:
                self.store._db.execute("INSERT OR REPLACE INTO project_notes (project_id, notes, updated) VALUES (?,?,?)",
                                       (project_id, notes, self.clock()))
            else:
                self.store._db.execute("DELETE FROM project_notes WHERE project_id=?", (project_id,))
        return {"ok": True, "notes": notes}

    def brief_block(self, project_name: str, notes: str) -> str:
        notes = " ".join(notes.split())
        if len(notes) > NOTES_IN_BRIEF:
            notes = notes[: NOTES_IN_BRIEF - 1].rstrip() + "\u2026"
        return (f"This job is part of the user's project \u201c{project_name}\u201d. "
                f"Their standing notes for it: {notes}")

    # -- filing at start ------------------------------------------------------------------------

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join(_words(text))[:400]

    def expect(self, text: str, project_id: str) -> None:
        """A job about to start with *text* is filed into *project_id* (a "for X, ..." message):
        ``for_start`` picks it up when the job's brief is built, so its notes go in the first turn."""
        now = self.clock()
        with self._lock:
            for k, (_p, at) in list(self._pending.items()):
                if now - at > PENDING_S:
                    self._pending.pop(k, None)
            self._pending[self._norm(text)] = (project_id, now)

    def for_start(self, key: str, request: str) -> str:
        """Called as a task's brief is built: files a pending job and returns the block to add to its
        brief (the project's standing notes), once per job's session. "" when there's nothing."""
        with self._lock:
            pending = self._pending.pop(self._norm(request or ""), None)
        if pending and self.clock() - pending[1] <= PENDING_S:
            self.link("job", self._job_key(key), pending[0])
        pid = self.project_of_key(key)
        if not pid:
            return ""
        notes = self.notes(pid)
        if not notes:
            return ""
        session = self.store.session_for(key) or key
        mark = f"project_notes_sent:{session}:{pid}"
        if self.store.get_meta(mark):
            return ""   # this job's session already has them (a follow-up turn)
        try:
            name = self._get(pid).name
        except ProjectError:
            return ""
        self.store.set_meta(mark, "1")
        return self.brief_block(name, notes)

    def directive(self, text: str) -> tuple[str, str] | None:
        """ "for <project>, <ask>" / "<ask> for <project>" naming an existing project at Home:
        (project id, the ask without the project words). None when no project is named that way."""
        t = " ".join(str(text or "").split())
        if not t:
            return None
        projects = sorted(self._read(), key=lambda p: -len(p.name))
        for p in projects:
            name = re.escape(p.name)
            m = re.match(rf"(?i)^(?:for|in|on|re)\s+(?:the\s+|my\s+)?{name}(?:\s+project)?\s*[,:\u2014-]\s*(.+)$", t)
            if m and m.group(1).strip():
                return p.id, m.group(1).strip()
            m = re.match(rf"(?i)^(.+?)[,\s]+(?:for|in)\s+(?:the\s+|my\s+)?{name}\s+project[.!?]?$", t)
            if m and m.group(1).strip():
                return p.id, m.group(1).strip()
        return None

    # -- suggestions and offers -----------------------------------------------------------------

    def suggest(self, words: Iterable[str], projects: list[Any] | None = None) -> dict[str, str] | None:
        """The one existing project a new job's words name (whole-word match of the project's name),
        else None. Two or more matches: none (never a guess)."""
        text = " ".join(w for w in words if w)
        projects = self._read() if projects is None else projects
        hits = [p for p in projects if _phrase_in(p.name, text)]
        if len(hits) != 1:
            return None
        return {"id": hits[0].id, "name": hits[0].name}

    def card_fields(self, key: str, words: Iterable[str], projects: list[Any],
                    links: dict[tuple[str, str], str], selected: str | None = None) -> dict[str, Any]:
        """What a Home job card shows: ``project`` once filed, else ``project_suggestion`` (the chip),
        unless the person declined it for this job."""
        if not projects:
            return {}
        job = self._job_key(key)
        filed = links.get(("job", job))
        if filed is None:
            session = self.store.session_for(key)
            filed = links.get(("session", session)) if session else None
        if filed:
            p = next((p for p in projects if p.id == filed), None)
            if not p:
                return {}
            out: dict[str, Any] = {"project": {"id": p.id, "name": p.name}}
            if self.is_auto(job):
                # Filed by the Home chip, but its words name another project: ask once, never move it.
                got = self.suggest(words, projects)
                if got and got["id"] != p.id:
                    out["project_suggestion"] = got
            return out
        if filed == "":
            return {}   # declined
        if selected:
            return {}   # Home is working in a project: its chip files new work; no suggestions
        got = self.suggest(words, projects)
        return {"project_suggestion": got} if got else {}

    def _subjects(self) -> dict[str, dict[str, Any]]:
        """{subject (lowercase): {"name": as written, "sessions": [...]}} over recent unfiled jobs."""
        since = self.clock() - OFFER_WINDOW_S
        filed = self.session_projects()
        links = self._links()
        out: dict[str, dict[str, Any]] = {}
        for key, session, title, at in self._job_rows():
            if not session or not title or float(at or 0) < since or session in filed:
                continue
            if self.project_of_key(key, links) or links.get(("job", self._job_key(key))) == "":
                continue
            for word in set(re.findall(r"\b[A-Z][A-Za-z0-9]{2,}\b", title)):
                low = word.lower()
                if low in _NOT_SUBJECTS:
                    continue
                entry = out.setdefault(low, {"name": word, "sessions": []})
                if session not in entry["sessions"]:
                    entry["sessions"].append(session)
        return out

    def offer(self, projects: list[Any] | None = None) -> dict[str, Any] | None:
        """At most one "Make a project for X?": the subject with the most unfiled chats (3+) that no
        project is named for and that the person never dismissed."""
        projects = self._read() if projects is None else projects
        with self.store._lock:
            dismissed = {r[0] for r in self.store._db.execute("SELECT subject FROM project_offers")}
        names = {w for p in projects for w in _words(p.name)}
        best = None
        for low, entry in self._subjects().items():
            if low in dismissed or low in names or len(entry["sessions"]) < OFFER_AFTER:
                continue
            if best is None or len(entry["sessions"]) > len(best["sessions"]):
                best = entry
        if best is None:
            return None
        return {"subject": best["name"], "chats": len(best["sessions"])}

    def _offer_sessions(self, subject: str) -> list[str]:
        return list((self._subjects().get(subject.lower()) or {}).get("sessions") or [])

    def dismiss_offer(self, subject: str) -> dict[str, Any]:
        if not isinstance(subject, str) or not subject.strip() or len(subject) > NAME_MAX:
            raise ProjectError(400, "subject is the offer's subject.")
        with self.store._lock, self.store._db:
            self.store._db.execute("INSERT OR REPLACE INTO project_offers (subject, dismissed) VALUES (?,?)",
                                   (subject.strip().lower(), self.clock()))
        return {"ok": True}
