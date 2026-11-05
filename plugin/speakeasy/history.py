"""Every Hermes session and Speakeasy task, as a history the apps can browse and write to
(docs/API.md "History"; capability ``history``).

Read side: ``<HERMES_HOME>/state.db``, read-only, the same way continuity and the thread watcher read
it. The list mirrors Hermes Desktop's sidebar rules: no subagent/tool children, nothing hidden or
archived, one row per conversation (the newest segment of a compression chain), scheduled (cron)
sessions only when asked for. Search covers message text through Hermes' own full-text index
(``messages_fts``), plus titles; a plain LIKE scan when the index is missing.

Write side (``POST /voice/history/{id}/message``) always targets the session named, by id, and never
goes through the call router. Which door it uses depends on who owns the session (see service.py):

* a Speakeasy task's session: the task_messages machinery (steer, answer, or follow-up run);
* another API-server session: a new Speakeasy job in that same session (``/v1/runs`` + session_id);
* anything else (a Discord or Telegram thread, the terminal, Desktop): a Speakeasy task *linked*
  to that conversation, seeded with its recent messages. Hermes has no door that writes into such a
  session without the reply going back to the chat (``/v1/runs`` refuses a foreign session; the
  chat-stream door and the gateway socket's viewer submit both deliver the reply to the platform),
  and Speakeasy never posts into a chat. Later messages from that page go to the same linked task.
  The chat's page shows the linked task's messages after its own.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable

from . import activity as A

logger = logging.getLogger(__name__)

PAGE = 40                  # sessions per page
MAX_PAGE = 100
MESSAGES_PAGE = 300        # transcript rows per page (newest first, oldest shown first)
PREVIEW_CHARS = 140
TEXT_CHARS = 20000         # one message's text, as shown
SEARCH_HITS = 600          # most recent matching messages a search looks at
BUSY_WINDOW_S = 900        # a non-Speakeasy session whose last row is a pending turn this recent "runs"
MAX_TEXT = 4000
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_:@.+=-]{1,128}$")
SPEAKEASY_PREFIX = "speakeasy_task_"

# Internal sessions never listed (their work shows inside the session that started them).
HIDDEN_SOURCES = ("subagent", "tool", "oneshot", "acp", "recovered")
LABELS = {"discord": "Discord", "telegram": "Telegram", "slack": "Slack", "whatsapp": "WhatsApp",
          "signal": "Signal", "matrix": "Matrix", "email": "Email", "sms": "SMS",
          "cli": "Terminal", "tui": "Terminal", "gui": "Desktop", "local": "Desktop", "desktop": "Desktop",
          "api_server": "Hermes API", "cron": "Scheduled", "kanban": "Kanban", "webhook": "Webhook"}
# The source filter (``?source=``): id -> the Hermes sources it covers (None: Speakeasy's own).
FILTERS: dict[str, tuple[str, ...] | None] = {
    "speakeasy": None,
    "discord": ("discord",), "telegram": ("telegram",), "slack": ("slack",),
    "terminal": ("cli", "tui"), "desktop": ("gui", "local", "desktop"),
    "scheduled": ("cron",), "api": ("api_server",),
}
FILTER_LABELS = {"speakeasy": "Speakeasy", "discord": "Discord", "telegram": "Telegram", "slack": "Slack",
                 "terminal": "Terminal", "desktop": "Desktop", "scheduled": "Scheduled", "api": "Hermes API"}

# User rows that aren't the person talking: shown as a short note, never as their words.
_SYSTEM_RES = (
    (re.compile(r"\[ASYNC DELEGATION|\[IMPORTANT: Background"), "A helper reported back"),
    (re.compile(r"^\s*\[SYSTEM:"), "System note"),
    (re.compile(r"CONTEXT COMPACTION|_compressed_summary"), "Earlier messages were summarized"),
    (re.compile(r"^\s*\[Cron delivery"), "Scheduled delivery"),
)
# A Speakeasy task prompt: the person's words are the last "User:" line of its transcript.
_SPEAKEASY_BRIEF = re.compile(r"Recent timestamped voice transcript|handling one task .{0,80} gave you by voice", re.S)
_USER_LINE = re.compile(r"^(?:\[[^\]]{1,40}\]\s*)?(?:User|You)\s*:\s*(.+)$", re.M)
_ADDED = re.compile(r"^\S+ just added to this task (?:by voice|by typing)?:?\s*(.+?)(?: Fold it into the work.*)?$", re.S)


class HistoryError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# -- reading state.db ----------------------------------------------------------------------------

def _open(state_db: Path) -> sqlite3.Connection | None:
    if not Path(state_db).exists():
        return None
    try:
        db = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    db.row_factory = sqlite3.Row
    return db


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _flag(cols: set[str], name: str) -> str:
    return f"coalesce(s.{name},0)=0" if name in cols else "1=1"


def _last_expr(cols: set[str]) -> str:
    last_msg = "(SELECT max(m.timestamp) FROM messages m WHERE m.session_id = s.id)"
    if "last_activity_at" in cols:
        return f"coalesce(s.last_activity_at, {last_msg}, s.started_at, 0)"
    return f"coalesce({last_msg}, s.started_at, 0)"


def redact_text(text: Any) -> str:
    """A message as shown: credentials and tokens out (the person's words otherwise kept as written)."""
    if not isinstance(text, str):
        return ""
    for pattern in A._TOKEN_RES:
        text = pattern.sub("…", text)
    return text[:TEXT_CHARS]


def _one_line(text: str, limit: int = PREVIEW_CHARS) -> str:
    line = " ".join(text.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def user_words(content: str) -> tuple[str, str]:
    """(kind, text) for a user row: ``user`` with the person's words, or ``note`` for system rows
    (helper reports, summaries, deliveries) shown as a short label."""
    for pattern, label in _SYSTEM_RES:
        if pattern.search(content[:4000]):
            return "note", label
    if _SPEAKEASY_BRIEF.search(content[:2000]) or (len(content) > 3000 and _USER_LINE.search(content)):
        said = _USER_LINE.findall(content)
        return ("user", said[-1].strip()) if said else ("note", "Task brief from Speakeasy")
    added = _ADDED.match(content.strip())
    if added:
        return "user", added.group(1).strip()
    return "user", content


def source_label(source: str, display_name: str | None, chat_type: str | None, speakeasy: bool) -> str:
    if speakeasy:
        return "Speakeasy"
    base = LABELS.get(source, source.replace("_", " ").title() if source else "Hermes")
    name = (display_name or "").strip()
    if source in {"discord", "slack"} and name:
        channel = next((p.strip() for p in name.split(" / ") if p.strip().startswith("#")), None)
        if channel:
            return f"{base} · {channel}"
    return base


def _title_from(row: sqlite3.Row, first_user: str | None) -> str:
    title = (row["title"] or "").strip() if "title" in row.keys() else ""
    if title:
        return _one_line(title, 90)
    name = (row["display_name"] or "").strip() if "display_name" in row.keys() else ""
    if name and " / " in name:
        return _one_line(name.split(" / ")[-1], 90)
    if first_user:
        return _one_line(first_user, 60)
    return "Untitled conversation"


def _fts_query(q: str) -> str:
    words = [w for w in re.findall(r"[\w'’-]+", q) if w]
    return " ".join('"' + w.replace('"', "") + '"' for w in words[:8])


def _search_ids(db: sqlite3.Connection, q: str) -> tuple[dict[str, str], bool]:
    """Sessions whose messages or title match ``q``: {session_id: snippet}, and whether the full-text
    index answered (False: the LIKE fallback did)."""
    hits: dict[str, str] = {}
    query = _fts_query(q)
    used_fts = False
    if query:
        try:
            rows = db.execute(
                "SELECT m.session_id, snippet(messages_fts, 0, '', '', '…', 14) FROM messages_fts "
                "JOIN messages m ON m.id = messages_fts.rowid WHERE messages_fts MATCH ? "
                "AND m.role IN ('user','assistant') ORDER BY messages_fts.rowid DESC LIMIT ?",
                (query, SEARCH_HITS)).fetchall()
            used_fts = True
        except sqlite3.Error:
            like = f"%{q.strip()}%"
            try:
                rows = db.execute(
                    "SELECT session_id, content FROM messages WHERE role IN ('user','assistant') AND content LIKE ? "
                    "ORDER BY id DESC LIMIT ?", (like, SEARCH_HITS)).fetchall()
            except sqlite3.Error:
                rows = []
        for sid, snip in rows:
            if sid not in hits:
                kind, text = user_words(str(snip or ""))
                hits[sid] = _one_line(text if kind == "user" else "", 120) if not used_fts else _one_line(str(snip or ""), 120)
    try:
        for (sid,) in db.execute("SELECT id FROM sessions WHERE title LIKE ? OR display_name LIKE ? LIMIT 400",
                                 (f"%{q.strip()}%", f"%{q.strip()}%")):
            hits.setdefault(sid, "")
    except sqlite3.Error:
        try:
            for (sid,) in db.execute("SELECT id FROM sessions WHERE title LIKE ? LIMIT 400", (f"%{q.strip()}%",)):
                hits.setdefault(sid, "")
        except sqlite3.Error:
            pass
    return hits, used_fts


def _tips(db: sqlite3.Connection, ids: set[str]) -> dict[str, str]:
    """Each session's newest compression continuation (same source, child of it): {id: tip}."""
    out = {i: i for i in ids}
    frontier = set(ids)
    for _ in range(12):
        if not frontier:
            break
        marks = ",".join("?" * len(frontier))
        try:
            rows = db.execute(f"SELECT c.id, c.parent_session_id FROM sessions c JOIN sessions p ON p.id = c.parent_session_id "
                              f"WHERE c.parent_session_id IN ({marks}) AND c.source = p.source", tuple(frontier)).fetchall()
        except sqlite3.Error:
            break
        frontier = set()
        for child, parent in rows:
            for k, v in out.items():
                if v == parent:
                    out[k] = child
            frontier.add(child)
    return out


def _chain(db: sqlite3.Connection, session_id: str) -> list[str]:
    """Root..tip ids of one conversation (compression segments share a source)."""
    chain = [session_id]
    try:
        for _ in range(20):
            row = db.execute("SELECT p.id FROM sessions s JOIN sessions p ON p.id = s.parent_session_id "
                             "WHERE s.id=? AND p.source = s.source", (chain[0],)).fetchone()
            if not row or row[0] in chain:
                break
            chain.insert(0, row[0])
    except sqlite3.Error:
        pass
    return chain


def _pending_turn(db: sqlite3.Connection, session_id: str, now: float) -> bool:
    """A turn is under way: the newest row is the person's, a tool call or a tool result, and recent."""
    try:
        row = db.execute("SELECT role, tool_calls, timestamp FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 1",
                         (session_id,)).fetchone()
    except sqlite3.Error:
        return False
    if not row or not isinstance(row["timestamp"], (int, float)) or now - row["timestamp"] > BUSY_WINDOW_S:
        return False
    return row["role"] in {"user", "tool"} or (row["role"] == "assistant" and row["tool_calls"] not in (None, "", "[]"))


def _preview(db: sqlite3.Connection, session_id: str) -> tuple[str, str | None]:
    """(preview line, first user words) of a session."""
    preview, first = "", None
    try:
        rows = db.execute("SELECT role, content FROM messages WHERE session_id=? AND role IN ('user','assistant') "
                          "AND content IS NOT NULL AND content != '' ORDER BY id DESC LIMIT 6", (session_id,)).fetchall()
        for row in rows:
            kind, text = user_words(row["content"]) if row["role"] == "user" else ("assistant", row["content"])
            if kind != "note" and text.strip():
                preview = _one_line(redact_text(text))
                break
        head = db.execute("SELECT content FROM messages WHERE session_id=? AND role='user' AND content IS NOT NULL "
                          "ORDER BY id LIMIT 3", (session_id,)).fetchall()
        for (content,) in head:
            kind, text = user_words(content)
            if kind == "user" and text.strip():
                first = redact_text(text)
                break
    except sqlite3.Error:
        pass
    return preview, first


@dataclasses.dataclass
class Linked:
    """What Speakeasy knows of a session: its tasks (newest first) and their state."""
    keys: list[str]
    name: str | None
    running: bool
    needs_you: bool
    run_ids: list[str]


def parse_cursor(cursor: str | None) -> tuple[float, str] | None:
    """``next_cursor`` is "<last activity>|<session id>" (exact; ties broken by id)."""
    if not cursor:
        return None
    last, _, sid = cursor.partition("|")
    try:
        return float(last), sid
    except ValueError:
        raise HistoryError(400, "cursor is not one this server gave out") from None


def list_sessions(state_db: Path, *, cursor: str | None = None, q: str = "", source: str = "",
                  limit: int = PAGE, linked: Callable[[str], Linked | None] = lambda sid: None,
                  speakeasy_ids: Callable[[], set[str]] = set, live: Callable[[str], bool] = lambda sid: False,
                  now: float | None = None) -> dict[str, Any]:
    """``GET /voice/history``: conversations newest first, ``limit`` per page; pass ``next_cursor`` back
    as ``cursor`` for the next page."""
    after = parse_cursor(cursor)
    now = time.time() if now is None else now
    limit = max(1, min(int(limit or PAGE), MAX_PAGE))
    if source and source not in FILTERS:
        raise HistoryError(400, f"source must be one of: {', '.join(FILTERS)}")
    db = _open(state_db)
    if db is None:
        return {"sessions": [], "next_cursor": None, "filters": _filters(), "sources": [],
                "search": "none" if q else None}
    try:
        cols = _columns(db, "sessions")
        last = _last_expr(cols)
        where = [f"s.source NOT IN ({','.join('?' * len(HIDDEN_SOURCES))})", _flag(cols, "hidden"),
                 _flag(cols, "archived")]
        params: list[Any] = list(HIDDEN_SOURCES)
        if "parent_session_id" in cols:
            # The newest segment of a conversation only; children of another source are helpers.
            where.append("NOT EXISTS (SELECT 1 FROM sessions c WHERE c.parent_session_id = s.id AND c.source = s.source)")
            where.append("(s.parent_session_id IS NULL OR EXISTS (SELECT 1 FROM sessions p "
                         "WHERE p.id = s.parent_session_id AND p.source = s.source))")
        if "message_count" in cols:
            where.append("(coalesce(s.message_count,0) > 0 OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id))")
        mine = speakeasy_ids()
        sources = _source_counts(db, where, params, mine)
        if source == "speakeasy":
            ids = sorted(mine | set())
            where.append(f"(s.id LIKE '{SPEAKEASY_PREFIX}%'" + (f" OR s.id IN ({','.join('?' * len(ids))}))" if ids else ")"))
            params += ids
        elif source:
            covered = FILTERS[source] or ()
            where.append(f"s.source IN ({','.join('?' * len(covered))})")
            params += list(covered)
            if source == "api":
                where.append(f"s.id NOT LIKE '{SPEAKEASY_PREFIX}%'")
        else:
            where.append("s.source != 'cron'")
        snippets: dict[str, str] = {}
        search_kind = None
        if q.strip():
            hits, used_fts = _search_ids(db, q)
            search_kind = "fts" if used_fts else "like"
            tips = _tips(db, set(hits))
            snippets = {}
            for sid, tip in tips.items():
                if hits.get(sid) and not snippets.get(tip):
                    snippets[tip] = hits[sid]
                snippets.setdefault(tip, hits.get(sid, ""))
            if not snippets:
                return {"sessions": [], "next_cursor": None, "filters": _filters(), "sources": sources,
                        "search": search_kind}
            where.append(f"s.id IN ({','.join('?' * len(snippets))})")
            params += list(snippets)
        if after is not None:
            where.append(f"({last} < ? OR ({last} = ? AND s.id < ?))")
            params += [after[0], after[0], after[1]]
        display = "s.display_name" if "display_name" in cols else "NULL"
        chat_type = "s.chat_type" if "chat_type" in cols else "NULL"
        title = "s.title" if "title" in cols else "NULL"
        rows = db.execute(f"SELECT s.id, s.source, {title} AS title, {display} AS display_name, {chat_type} AS chat_type, "
                          f"s.started_at, {last} AS last FROM sessions s WHERE {' AND '.join(where)} "
                          f"ORDER BY last DESC, s.id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        out = []
        tail = rows[limit - 1] if more else None
        for row in rows[:limit]:
            sid = row["id"]
            link = linked(sid)
            # A chat continued from Speakeasy (linked tasks, no keys of its own) stays the chat's row.
            speakeasy = bool(link and link.keys) or sid.startswith(SPEAKEASY_PREFIX)
            preview, first = _preview(db, sid)
            running = bool(link and link.running) or live(sid) or (link is None and _pending_turn(db, sid, now))
            entry: dict[str, Any] = {
                "id": sid, "title": (link.name if link and link.name and not (row["title"] or "").strip() else None)
                or _title_from(row, first),
                "source": "speakeasy" if speakeasy else row["source"],
                "source_label": source_label(row["source"], row["display_name"], row["chat_type"], speakeasy),
                "last_activity": round(float(row["last"] or 0), 3), "preview": preview,
                "state": "needs_you" if link and link.needs_you else ("running" if running else "idle"),
            }
            if link and link.run_ids:
                entry["run_ids"] = link.run_ids[:5]
            if snippets.get(sid):
                entry["match"] = snippets[sid]
            out.append(entry)
        next_cursor = f"{float(tail['last'] or 0)!r}|{tail['id']}" if tail is not None else None
        return {"sessions": out, "next_cursor": next_cursor, "filters": _filters(), "sources": sources,
                "search": search_kind}
    except sqlite3.Error as exc:
        logger.warning("speakeasy: history list failed (%s)", type(exc).__name__)
        raise HistoryError(502, "I couldn't read the Hermes history.") from None
    finally:
        db.close()


def _source_counts(db: sqlite3.Connection, where: list[str], params: list[Any], mine: set[str]) -> list[dict[str, Any]]:
    """``sources``: each filter id the person has listable sessions under, with how many, over every
    visible session (not just this page, and ignoring the search and the chosen filter), so the apps
    only offer filters that would show something. Counted the same way ``?source=`` filters."""
    counts: dict[str, int] = {}
    by_source = {s: k for k, covered in FILTERS.items() for s in (covered or ())}
    for sid, src in db.execute(f"SELECT s.id, s.source FROM sessions s WHERE {' AND '.join(where)}", params):
        speakeasy = sid.startswith(SPEAKEASY_PREFIX) or sid in mine
        if speakeasy:
            counts["speakeasy"] = counts.get("speakeasy", 0) + 1
        key = by_source.get(src)
        # ``api`` leaves out Speakeasy's own task sessions; the rest count wherever ?source= lists them.
        if key and not (key == "api" and sid.startswith(SPEAKEASY_PREFIX)):
            counts[key] = counts.get(key, 0) + 1
    return [{"source": k, "count": counts[k]} for k in FILTERS if counts.get(k)]


def _filters() -> list[dict[str, str]]:
    return [{"id": k, "label": FILTER_LABELS[k]} for k in FILTERS]


def session_meta(state_db: Path, session_id: str) -> dict[str, Any] | None:
    """{id, source, title, display_name, chat_type, model} of a session (its newest segment), or None."""
    db = _open(state_db)
    if db is None:
        return None
    try:
        cols = _columns(db, "sessions")
        tip = _tips(db, {session_id}).get(session_id, session_id) if "parent_session_id" in cols else session_id
        display = "display_name" if "display_name" in cols else "NULL AS display_name"
        chat = "chat_type" if "chat_type" in cols else "NULL AS chat_type"
        model = "model" if "model" in cols else "NULL AS model"  # the model Hermes ran the session on
        row = db.execute(f"SELECT id, source, title, {display}, {chat}, {model} FROM sessions WHERE id=?",
                         (tip,)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


def session_messages(state_db: Path, session_id: str, *, before: int | None = None,
                     limit: int = MESSAGES_PAGE) -> dict[str, Any]:
    """``GET /voice/history/{id}``'s transcript: entries oldest first. ``user`` / ``assistant`` text,
    ``note`` for system rows, and ``tools`` groups: the calls made between two messages, each item in
    the job tool log's shape (``activity.public(detail=True)``: text, tool, args, result, status,
    started_at, ended_at), redacted the same way. ``before`` (a row id) pages back."""
    db = _open(state_db)
    if db is None:
        raise HistoryError(404, "I couldn't find that conversation.")
    try:
        if not db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
            raise HistoryError(404, "I couldn't find that conversation.")
        chain = _chain(db, session_id)
        marks = ",".join("?" * len(chain))
        cols = _columns(db, "messages")
        active = " AND coalesce(active,1)=1" if "active" in cols else ""
        hidden = " AND coalesce(display_kind,'') != 'hidden'" if "display_kind" in cols else ""
        rows = db.execute(
            f"SELECT id, role, content, tool_calls, tool_call_id, tool_name, timestamp FROM messages "
            f"WHERE session_id IN ({marks}){active}{hidden} AND id < ? ORDER BY id DESC LIMIT ?",
            (*chain, int(before) if before else 1 << 62, int(limit) + 1)).fetchall()
    except sqlite3.Error as exc:
        logger.warning("speakeasy: history read failed (%s)", type(exc).__name__)
        raise HistoryError(502, "I couldn't read that conversation.") from None
    finally:
        db.close()
    more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    return {"entries": build_entries(rows), "has_more": more,
            "before": rows[0]["id"] if more and rows else None}


def build_entries(rows: list[Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    tracker: A.JobActivity | None = None
    group_id: int | None = None
    names: dict[str, str] = {}

    def flush() -> None:
        nonlocal tracker, group_id
        if tracker is not None:
            items = A.public(tracker.state, detail=True).get("activity") or []
            if items:
                entries.append({"kind": "tools", "id": f"t{group_id}", "items": items})
        tracker, group_id = None, None

    for row in rows:
        role, content = row["role"], row["content"] or ""
        at = float(row["timestamp"]) if isinstance(row["timestamp"], (int, float)) else None
        calls = row["tool_calls"]
        if role == "assistant" and calls not in (None, "", "[]", "null"):
            if content.strip():
                flush()
                entries.append({"kind": "assistant", "id": f"m{row['id']}", "at": at, "text": redact_text(content)})
            try:
                parsed = json.loads(calls)
            except ValueError:
                parsed = []
            for call in parsed if isinstance(parsed, list) else []:
                if not isinstance(call, dict):
                    continue
                fn = call["function"] if isinstance(call.get("function"), dict) else call
                name = fn.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                raw = fn.get("arguments")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    args = raw
                from .threads import _arg_preview
                cid = call.get("id") or call.get("call_id")
                if tracker is None:
                    tracker, group_id = A.JobActivity(), row["id"]
                event: dict[str, Any] = {"event": "tool.started", "tool": name, "preview": _arg_preview(name, args)}
                if isinstance(cid, str) and cid:
                    event["tool_call_id"] = cid
                    names[cid] = name
                tracker.observe(event, now=at)
        elif role == "tool":
            name = row["tool_name"] or names.get(row["tool_call_id"] or "") or ""
            if tracker is None or not name:
                continue
            from .threads import TOOL_RESULT_CHARS, _tool_failed
            event = {"event": "tool.completed", "tool": name, "preview": content[:TOOL_RESULT_CHARS],
                     "error": _tool_failed(name, content)}
            if row["tool_call_id"]:
                event["tool_call_id"] = row["tool_call_id"]
            tracker.observe(event, now=at)
        elif role == "user":
            flush()
            kind, text = user_words(content)
            if text.strip():
                entries.append({"kind": kind, "id": f"m{row['id']}", "at": at,
                                "text": redact_text(text) if kind == "user" else text})
        elif role == "assistant" and content.strip():
            flush()
            entries.append({"kind": "assistant", "id": f"m{row['id']}", "at": at, "text": redact_text(content)})
    flush()
    return entries


# -- continuing a chat Speakeasy can't write into ------------------------------------------------

def linked_context(state_db: Path, session_id: str, title: str, label: str, text: str) -> str:
    """What a linked task sees: where the conversation lives, its recent messages, then the request."""
    from .continuity import recent_exchange
    recent = recent_exchange(state_db, session_id)
    head = (f"The user is continuing the conversation \"{title}\" ({label}, Hermes session {session_id}) from the "
            "Speakeasy app. Work here; your answer is shown in Speakeasy only, never posted to that chat. "
            "Look up more of that session's history if you need it.")
    return head + ("\n\nIts recent messages:\n" + recent if recent else "") + f"\n\nUser: {text}"


def valid_session_id(session_id: str) -> bool:
    return bool(SAFE_ID_RE.fullmatch(session_id or ""))
