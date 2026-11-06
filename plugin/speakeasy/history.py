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
  The chat's page shows the linked task's messages merged in by time (``merge_linked``).
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
_ADDED = re.compile(r"^\S+ (?:just added to this task(?: by voice| by typing)?|typed to this task):?\s*(.+?)"
                    r"(?: Fold it into the work.*)?$", re.S)
# Hermes' wrapper around guidance sent into a running turn.
_OUT_OF_BAND = re.compile(r"^\s*\[OUT-OF-BAND USER MESSAGE[^\]]*\]\s*|\s*\[/OUT-OF-BAND USER MESSAGE\]\s*$")
# What Speakeasy adds after the words when something was shared: "Geo shared the picture x.jpg
# (saved at /path). Open the images with vision_analyze. Use them to understand what they mean."
_SHARED_TAIL = re.compile(r"\s*\S+ shared (?:the |a |an )?(?:picture|file|screenshot|photo)\b.*$", re.S)
# Hermes' context-compaction handoff, written for the model. It is stored as its own assistant row
# or appended after a user's words; either way it never belongs in a chat the user reads.
_COMPACTION = re.compile(r"\[CONTEXT COMPACTION\b|_compressed_summary|^\s*\[PRIOR CONTEXT\b")
# The routing header the gateway puts on a relayed chat message (Discord, Telegram...), wherever it sits.
_GATEWAY_ORIGIN = re.compile(r"\s*Gateway message origin \(JSON data[^\n]*\n\s*\{[^\n]*\}\s*"
                             r"(?:Do not guess a reply destination[^\n]*)?\s*")
_QUOTED = re.compile(r'^\s*"(.+)"\s*$', re.S)
# A /skill command: Hermes sends the whole skill file with the user's words at the end.
_SKILL_INVOKED = re.compile(r'^\s*\[IMPORTANT: The user has invoked the (?:"([^"\n]{1,80})" skill|([^,\]\n]{1,120}) skill bundle)')
_SKILL_WORDS = re.compile(r"(?:The user has provided the following instruction alongside the skill invocation: |"
                          r"\nUser instruction: )(.*?)(?:\n\n\[Runtime note:.*)?$", re.S)


def _skill_command(content: str) -> str | None:
    """What the user typed for a /skill command ("/name words"), from the message Hermes built."""
    m = _SKILL_INVOKED.match(content)
    if not m:
        return None
    name = (m.group(1) or m.group(2) or "").strip()
    words = _SKILL_WORDS.search(content[-20000:])
    said = words.group(1).strip() if words else ""
    if _CRON_PROMPT.match(said):
        return ""
    return f"/{name} {said}".strip() if name else said


# Prompts Hermes writes for runs nobody typed: a scheduled job, a Kanban card, a preloaded skill.
_CRON_PROMPT = re.compile(r"^\s*\[IMPORTANT: You are running as a scheduled cron job")
_KANBAN_PROMPT = re.compile(r"^\s*# Kanban task \S+: ([^\n]{1,160})")
_PRELOADED = re.compile(r'^\s*\[IMPORTANT: The user launched this \w+ session with the "([^"\n]{1,80})" skill')
# A chat reply: '[Replying to: "<quoted>"]\n\n<words>', sometimes with the surrounding chat after it
# and the person's words under "[New message]\n[name] ".
_REPLYING = re.compile(r'^\s*\[Replying to(?: your previous message)?: "')
_NEW_MESSAGE = re.compile(r"\n\[New message\]\n(?:\[[^\]\n]{1,60}\]\s*)?")
# Discord's per-turn reply pointer, then the sender tag: "[Triggering message id: `…` — …]\n\n[rungmc] words".
_TRIGGERING = re.compile(r"^\s*(?:\[Note: model was just switched [^\]\n]*\]\s*)?(?:\[Triggering message id:[^\]\n]*\]\s*"
                         r"(?:\[[\w.\- ]{1,40}\]\s+)?)?")
# A picture's saved path the gateway appends: "[Image attached at: /path]".
_ATTACHED_AT = re.compile(r"\s*\[(?:Image|File|Audio) attached at: [^\]\n]*\]")
_VOICE_FILE = re.compile(r"^\s*Listen to this voice message and respond to it\.\s*MEDIA:")


def _replied_words(content: str) -> str | None:
    if not _REPLYING.match(content):
        return None
    new = list(_NEW_MESSAGE.finditer(content))
    if new:
        return content[new[-1].end():].strip()
    end = content.find('"]\n\n')
    body = content[end + 4:] if end >= 0 else ""
    ctx = body.find("[Context around the replied-to message]")
    return (body[:ctx] if ctx >= 0 else body).strip()


def is_compaction(text: str) -> bool:
    """A context-compaction handoff (model-facing summary of earlier turns)."""
    return bool(_COMPACTION.search(text or ""))


def _before_compaction(text: str) -> str:
    """A user's words with any compaction handoff Hermes appended after them removed."""
    m = _COMPACTION.search(text or "")
    return text[:m.start()].rstrip() if m else text


# -- what a message shows: never Speakeasy's own plumbing ----------------------------------------
# A task's machine blocks (```speakeasy-question / -views ...): the app draws them as cards.
_MACHINE_BLOCK = re.compile(r"(?s)\n?```speakeasy-[\w-]+[ \t]*\n.*?(?:\n```|\Z)")
# A relayed voice request written into a chat: "[sender] [Voice request from Geo, relayed by Speakeasy] <words>".
_RELAYED = re.compile(r"^\s*(?:\[[^\]\n]{1,60}\]\s*)?\[Voice(?: request)?,? from [^\]\n]{1,60}\]\s*", re.I)
_RELAY_TAIL = re.compile(r"\n\s*\n\s*(?:Begin your reply with exactly one line|Recent voice conversation for context)", re.I)
# The note Speakeasy adds to a request: "(Geo is looking at: Find previous sessions)", "(following up: ...)".
_LOOKING_AT = re.compile(r"\s*\((?:[^():\n]{0,40}\bis looking at|following up)\s*:[^\n]*\)\s*$", re.I | re.M)
# The echo line a relayed request asks for: "Voice: <the request>" first.
_VOICE_LINE = re.compile(r"^\s*Voice:[^\n]*(?:\n+|$)")
_STATUS = re.compile(r"(?im)^[ \t]*STATUS:[ \t]*(.+?)[ \t]*$")
_DETAIL = re.compile(r"(?im)^[ \t]*DETAIL:[ \t]*(.+?)[ \t]*$")
# A voice report: "DONE: <what happened>" / "SPOKEN: <what the voice says>" lines. SPOKEN is the
# person-facing sentence (it may run on over several lines until the next label).
_REPORT_LABEL = re.compile(r"(?im)^[ \t]*(DONE|SPOKEN)[ \t]*:[ \t]*")
_SPOKEN = re.compile(r"(?is)^[ \t]*SPOKEN[ \t]*:[ \t]*(.+?)(?=^[ \t]*(?:DONE|SPOKEN|STATUS|DETAIL)[ \t]*:|\Z)", re.M)

# -- gateway plumbing on user rows (Discord, Telegram, ...) ------------------------------------
_TRIGGER_NOTE = re.compile(r"\[Triggering message id:[^\]\n]{0,300}\]\s*", re.I)
_MEMORY_CONTEXT = re.compile(r"(?s)<memory-context>.*?(?:</memory-context>|\Z)\s*")
_ORIGIN_HEADER = re.compile(r"(?s)^\s*Gateway message origin \(JSON data[^\n]*\n(?:\s*```[a-z]*\n)?\s*\{.*?\}\s*(?:```\s*)?(?:Do not guess a reply destination[^\n]*\s*)?")
_IMAGE_ATTACHED = re.compile(r"\[Image attached at:[^\]\n]{0,500}\]\s*", re.I)
_NO_TEXT = re.compile(r"\(The user sent a message with no text content\)\s*", re.I)
_SENDER_PREFIX = re.compile(r"^\s*\[[A-Za-z0-9_.\- ]{1,40}\]\s+(?=\S)")
_INTERNAL_RES = (
    (re.compile(r"\[IMPORTANT: Background process"), "Background job finished"),
    (re.compile(r"\[ASYNC DELEGATION"), "Helpers finished"),
    (re.compile(r"\[INTERNAL NOTIFICATION"), "Internal notification"),
)


def strip_gateway(content: str) -> str:
    """A gateway user row without Hermes' wrappers: no triggering-message note, memory context,
    origin header, image-path note, no-text placeholder or ``[sender]`` prefix."""
    text = _MEMORY_CONTEXT.sub("", content or "")
    text = _ORIGIN_HEADER.sub("", text)
    text = _TRIGGER_NOTE.sub("", text)
    text = _IMAGE_ATTACHED.sub("", text)
    text = _NO_TEXT.sub("", text)
    text = text.strip()
    if not text.startswith(("[Voice request", "[SYSTEM", "[Cron")):
        text = _SENDER_PREFIX.sub("", text, count=1)
    return text.strip()
_MD_MARKS = re.compile(r"(\*\*|__|~~|`+)")
_MD_LEAD = re.compile(r"(?m)^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d{1,2}[.)]\s+)")
_MD_LINK = re.compile(r"!?\[([^\]\n]{1,200})\]\([^)\s]{1,500}\)")


def assistant_text(text: str) -> str:
    """An assistant message as shown: no machine blocks (question/views JSON), no "Voice: …" echo line,
    a progress note (STATUS:/DETAIL:) as its plain words."""
    if not isinstance(text, str) or is_compaction(text):
        return ""
    out = _MACHINE_BLOCK.sub("", text)
    status, detail = _STATUS.search(out), _DETAIL.search(out)
    if status or detail:
        rest = _DETAIL.sub("", _STATUS.sub("", out)).strip()
        said = " — ".join(m.group(1).strip().rstrip(".") for m in (status, detail) if m)
        out = (said + ("\n\n" + rest if rest else "")) if said else rest
    if _REPORT_LABEL.search(out):
        spoken = _SPOKEN.search(out)
        out = spoken.group(1).strip() if spoken and spoken.group(1).strip() else _REPORT_LABEL.sub("", out)
    out = _VOICE_LINE.sub("", out, count=1)
    return out.strip()


def relayed_words(text: str) -> str | None:
    """The person's own words in a request Speakeasy relayed into a chat, else None."""
    m = _RELAYED.match(text or "")
    if not m:
        return None
    body = text[m.end():]
    tail = _RELAY_TAIL.search(body)
    if tail:
        body = body[:tail.start()]
    return _LOOKING_AT.sub("", body).strip()


def preview_text(text: str, limit: int = PREVIEW_CHARS) -> str:
    """One plain line for a list row: no markdown marks, links as their words, no notes."""
    line = _MD_LINK.sub(r"\1", text or "")
    line = _MD_LEAD.sub("", line)
    line = _MD_MARKS.sub("", line)
    line = _LOOKING_AT.sub("", line)
    return _one_line(line, limit)


def tidy_title(title: str) -> str:
    """A title as shown: an all-lowercase one ("telegram general") gets a capital first letter."""
    t = " ".join((title or "").split())
    if t and t == t.lower() and t[0].isalpha():
        t = t[0].upper() + t[1:]
    return t


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
    if not isinstance(content, str):
        return "user", ""
    for pattern, label in _INTERNAL_RES:
        if pattern.search(content[:4000]):
            return "note", label
    content = strip_gateway(content)
    content = _TRIGGERING.sub("", content, count=1)
    skill = _skill_command(content)
    if skill is not None:
        return ("user", skill) if skill else ("note", "Scheduled run")
    if _CRON_PROMPT.match(content):
        return "note", "Scheduled run"
    kanban = _KANBAN_PROMPT.match(content)
    if kanban:
        return "note", f"Kanban task: {kanban.group(1).strip()}"
    preloaded = _PRELOADED.match(content)
    if preloaded:
        words = _SKILL_WORDS.search(content[-20000:])
        said = words.group(1).strip() if words else ""
        return ("user", said) if said else ("note", f"Started with the {preloaded.group(1)} skill")
    replied = _replied_words(content)
    if replied is not None:
        if not replied:
            return "note", "Replied to a message"
        content = replied
    content = _ATTACHED_AT.sub("", content).strip() or content
    if _VOICE_FILE.match(content):
        return "note", "Voice message"
    oob = re.search(r"\[OUT-OF-BAND USER MESSAGE", content)
    if oob and oob.start() > 0 and content[:oob.start()].strip():
        content = content[:oob.start()].rstrip()  # a request with a later follow-up merged into the same row
    if _OUT_OF_BAND.search(content[:200]) or _GATEWAY_ORIGIN.search(content):
        inner = _GATEWAY_ORIGIN.sub("\n", _OUT_OF_BAND.sub("", content)).strip()
        if not _ADDED.match(inner):
            quoted = _QUOTED.match(inner)
            content = quoted.group(1).strip() if quoted and '"' not in quoted.group(1) else inner
    words = _before_compaction(content)
    if words is not content and words.strip() and not any(p.search(words[:4000]) for p, _ in _SYSTEM_RES):
        content = words  # the user's words, then Hermes' handoff appended to the same row
    for pattern, label in _SYSTEM_RES:
        if pattern.search(content[:4000]):
            return "note", label
    relayed = relayed_words(content[:TEXT_CHARS])
    if relayed is not None:
        return ("user", relayed) if relayed else ("note", "Request from Speakeasy")
    if _SPEAKEASY_BRIEF.search(content[:2000]) or (len(content) > 3000 and _USER_LINE.search(content)):
        said = _USER_LINE.findall(content)
        return ("user", said[-1].strip()) if said else ("note", "Task brief from Speakeasy")
    added = _ADDED.match(_OUT_OF_BAND.sub("", content).strip())
    if added:
        words = _SHARED_TAIL.sub("", _LOOKING_AT.sub("", added.group(1))).strip()
        return ("user", words) if words else ("note", "You shared a picture")
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
        return tidy_title(_one_line(title, 90))
    name = (row["display_name"] or "").strip() if "display_name" in row.keys() else ""
    if name and " / " in name:
        return tidy_title(_one_line(name.split(" / ")[-1], 90))
    if first_user:
        return tidy_title(preview_text(first_user, 60))
    return "Untitled conversation"


def snippet_text(snip: str) -> str:
    """A search snippet as shown: the same cleaning as a message (no gateway wrappers, no voice-report
    labels); an internal notification's snippet is left out."""
    # A cut-off wrapper the snippet window started inside of ("…message id: `123` — use as …]").
    snip = re.sub(r"^\s*…?[^\[\]\n]{0,200}(?:discord tools|reply/react/pin)[^\]\n]*\]\s*", "", snip or "")
    snip = re.sub(r"^\s*…?[^<\n]{0,200}</memory-context>\s*", "", snip)
    kind, text = user_words(snip)
    if kind == "note":
        return ""
    text = _REPORT_LABEL.sub("", _STATUS.sub(r"\1", _DETAIL.sub(r"\1", text)))
    return _one_line(preview_text(text, 200), 120)


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
                hits[sid] = snippet_text(str(snip or ""))
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
            kind, text = user_words(row["content"]) if row["role"] == "user" else ("assistant", assistant_text(row["content"]))
            if kind != "note" and text.strip():
                preview = preview_text(redact_text(text))
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
                  folded: Callable[[], dict[str, str]] = dict,
                  marks: Callable[[], dict[str, dict[str, Any]]] = dict, archived: bool = False,
                  include_archived: bool | None = None,
                  now: float | None = None) -> dict[str, Any]:
    """``GET /voice/history``: conversations newest first, ``limit`` per page; pass ``next_cursor`` back
    as ``cursor`` for the next page.

    *folded*: {session of a linked task: the chat it continues}. A chat continued from Speakeasy stays ONE
    row: its linked tasks' sessions are never listed on their own, and the chat sorts by its newest
    activity in either (so a reply from its page moves it to the top instead of adding a row).

    *marks*: {session: {pinned, read_at}} (the plugin's, shared by every device): rows carry ``pinned``
    and ``unread``; the first page lists pinned rows first. *archived*: list only archived conversations
    (Hermes' own ``archived`` flag, set by any Hermes app). *include_archived*: list archived ones too,
    marked ``archived`` (default: when searching, so an archived thread is still found)."""
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
        fold = {k: v for k, v in (folded() or {}).items() if k != v and SAFE_ID_RE.fullmatch(k) and SAFE_ID_RE.fullmatch(v)}
        last = _last_expr(cols)
        if fold:
            # The chat's own activity, or its linked tasks' (sessions named inline: ids are SAFE_ID_RE-checked).
            pairs = ",".join(f"('{src}','{dst}')" for dst, src in fold.items())
            linked_last = _last_expr(cols).replace("s.", "l.")
            last = (f"max({last}, coalesce((SELECT max({linked_last}) FROM (VALUES {pairs}) AS f "
                    f"JOIN sessions l ON l.id = f.column2 WHERE f.column1 = s.id), 0))")
        if include_archived is None:
            include_archived = bool(q.strip())
        where = [f"s.source NOT IN ({','.join('?' * len(HIDDEN_SOURCES))})", _flag(cols, "hidden")]
        if archived:
            where.append("coalesce(s.archived,0)!=0" if "archived" in cols else "1=0")
        elif not include_archived:
            where.append(_flag(cols, "archived"))
        params: list[Any] = list(HIDDEN_SOURCES)
        if "parent_session_id" in cols:
            # The newest segment of a conversation only; children of another source are helpers.
            where.append("NOT EXISTS (SELECT 1 FROM sessions c WHERE c.parent_session_id = s.id AND c.source = s.source)")
            where.append("(s.parent_session_id IS NULL OR EXISTS (SELECT 1 FROM sessions p "
                         "WHERE p.id = s.parent_session_id AND p.source = s.source))")
        if "message_count" in cols:
            where.append("(coalesce(s.message_count,0) > 0 OR EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id))")
        if fold:
            where.append(f"s.id NOT IN ({','.join('?' * len(fold))})")
            params += list(fold)
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
            tips = {sid: fold.get(tip, tip) for sid, tip in tips.items()}  # a linked task's hit is its chat's
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
        marked = marks() or {}
        pins = sorted((sid for sid, m in marked.items() if m.get("pinned") and SAFE_ID_RE.fullmatch(sid)),
                      key=lambda sid: -float(marked[sid]["pinned"]))
        pin_rank = "0"
        if pins and not archived:
            # Pinned first (newest pin on top), then everything else by last activity. The cursor
            # pages only the unpinned rest: every pinned row comes on the first page.
            pin_rank = "(CASE s.id " + " ".join(f"WHEN '{sid}' THEN {i + 1}" for i, sid in enumerate(pins[:50])) + " ELSE 0 END)"
            if after is not None:
                where.append(f"s.id NOT IN ({','.join('?' * len(pins[:50]))})")
                params += pins[:50]
        display = "s.display_name" if "display_name" in cols else "NULL"
        chat_type = "s.chat_type" if "chat_type" in cols else "NULL"
        title = "s.title" if "title" in cols else "NULL"
        arch = "coalesce(s.archived,0)" if "archived" in cols else "0"
        rows = db.execute(f"SELECT s.id, s.source, {title} AS title, {display} AS display_name, {chat_type} AS chat_type, "
                          f"s.started_at, {last} AS last, {pin_rank} AS pin, {arch} AS arch FROM sessions s WHERE {' AND '.join(where)} "
                          f"ORDER BY (pin = 0), pin, last DESC, s.id DESC LIMIT ?", (*params, limit + 1)).fetchall()
        more = len(rows) > limit
        out = []
        tail = rows[limit - 1] if more else None
        if tail is not None and tail["pin"]:
            # The page ended inside the pinned rows: the next page starts at the newest unpinned row.
            tail = {"last": 1e18, "id": "~"}
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
            mark = marked.get(sid) or {}
            if row["pin"]:
                entry["pinned"] = True
            if archived or row["arch"]:
                entry["archived"] = True
            read_at = mark.get("read_at")
            if read_at is not None and float(row["last"] or 0) > float(read_at) + 1 and entry["state"] != "running":
                entry["unread"] = True
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


def fingerprint(state_db: Path, session_ids: list[str] | None = None) -> tuple[Any, ...] | None:
    """A cheap "has anything changed" stamp for cached history replies (indexed lookups only, a few ms
    on a multi-GB state.db): the newest message row of these sessions (or of all, for the list), and
    the sessions table's newest row and last activity. None when it can't be read (no caching)."""
    db = _open(state_db)
    if db is None:
        return None
    try:
        if session_ids:
            ids = list(dict.fromkeys(session_ids))[:40]
            marks = ",".join("?" * len(ids))
            msg = db.execute(f"SELECT max(id), count(*) FROM messages WHERE session_id IN ({marks})", ids).fetchone()
            cols = _columns(db, "sessions")
            extra = ", ".join(c for c in ("title", "archived", "hidden", "last_activity_at") if c in cols) or "id"
            meta = db.execute(f"SELECT {extra} FROM sessions WHERE id IN ({marks})", ids).fetchall()
            kids = db.execute(f"SELECT count(*) FROM sessions WHERE parent_session_id IN ({marks})", ids).fetchone() \
                if "parent_session_id" in cols else (0,)
            return (tuple(msg), tuple(tuple(r) for r in meta), tuple(kids))
        msg = db.execute("SELECT max(id) FROM messages").fetchone()
        sess = db.execute("SELECT max(rowid), count(*) FROM sessions").fetchone()
        return (tuple(msg), tuple(sess))
    except sqlite3.Error:
        return None
    finally:
        db.close()


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
        arch = "coalesce(archived,0) AS archived" if "archived" in cols else "0 AS archived"
        row = db.execute(f"SELECT id, source, title, {display}, {chat}, {model}, {arch} FROM sessions WHERE id=?",
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
            shown = assistant_text(content)
            if shown:
                flush()
                entries.append({"kind": "assistant", "id": f"m{row['id']}", "at": at, "text": redact_text(shown)})
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
        elif role == "assistant" and is_compaction(content):
            flush()
            entries.append({"kind": "note", "id": f"m{row['id']}", "at": at, "text": "Earlier messages were summarized"})
        elif role == "assistant" and assistant_text(content):
            flush()
            entries.append({"kind": "assistant", "id": f"m{row['id']}", "at": at, "text": redact_text(assistant_text(content))})
    flush()
    return dedupe_entries(entries)


def _norm(text: str) -> str:
    return " ".join(_MD_MARKS.sub("", text or "").lower().split())


def dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """No redundant bubbles: the same assistant text twice in a row (a progress note repeated as the
    answer, a retried final), a bare "Done." right before or after the answer, and a note repeated, show
    once. Tool groups between them don't count as a break (the answer is the same answer)."""
    out: list[dict[str, Any]] = []
    last_text: dict[str, str] = {}
    for e in entries:
        kind = e.get("kind")
        if kind in {"assistant", "note", "user"}:
            text = _norm(e.get("text") or "")
            prev = last_text.get(kind)
            if prev is not None and text and (text == prev or (kind == "assistant" and (
                    text in {"done", "done.", "all done", "finished"} or prev in {"done", "done.", "all done", "finished"}))):
                if kind == "assistant" and prev in {"done", "done.", "all done", "finished"}:
                    # Keep the real answer, not the bare "Done" before it.
                    for i in range(len(out) - 1, -1, -1):
                        if out[i].get("kind") == "assistant":
                            out[i] = e
                            break
                    last_text[kind] = text
                continue
            last_text[kind] = text
            if kind == "user":
                last_text.pop("assistant", None)   # a new turn: the same answer again is a real answer
        out.append(e)
    return out


def _units(entries: list[dict[str, Any]]) -> list[tuple[float, list[dict[str, Any]]]]:
    """Entries as (time, [message, the untimed entries after it]): a tool group stays with the
    message before it (one before any message takes the first message's time)."""
    units: list[tuple[float, list[dict[str, Any]]]] = []
    lead: list[dict[str, Any]] = []
    for e in entries:
        at = e.get("at")
        if isinstance(at, (int, float)):
            units.append((float(at), lead + [e]))
            lead = []
        elif units:
            units[-1][1].append(e)
        else:
            lead.append(e)
    if lead:
        units.insert(0, (float("-inf"), lead))
    return units


def merge_linked(own: list[dict[str, Any]], linked: list[dict[str, Any]], *, lower: float | None,
                 upper: float | None, note: str) -> list[dict[str, Any]]:
    """A chat's page with what was said to it from Speakeasy (its linked tasks' entries) merged in by
    time, so the page always ends with the newest activity. Only linked entries in this page's span
    come in: ``lower`` <= at < ``upper`` (None: open-ended; the newest page has no upper bound and the
    oldest no lower one), so each one shows on exactly one page. Each run of linked entries starts
    with a small ``note`` (ids ``linked``, ``linked-2``, …)."""
    lo = float("-inf") if lower is None else lower
    hi = float("inf") if upper is None else upper
    picked = [(at, items) for at, items in _units(linked) if lo <= at < hi]
    if not picked:
        return own
    picked.sort(key=lambda u: u[0])  # stable: a linked session's own order on ties
    # The chat's order is kept as written; each linked unit goes in before the first chat message
    # newer than it (ties: the chat's first).
    merged: list[tuple[int, list[dict[str, Any]]]] = []
    i = 0
    for at, items in _units(own):
        while i < len(picked) and picked[i][0] < at:
            merged.append((1, picked[i][1]))
            i += 1
        merged.append((0, items))
    merged += [(1, items) for _, items in picked[i:]]
    out: list[dict[str, Any]] = []
    runs, prev = 0, 0
    for origin, items in merged:
        if origin == 1 and prev != 1:
            runs += 1
            out.append({"kind": "note", "id": "linked" if runs == 1 else f"linked-{runs}", "at": None, "text": note})
        out += items
        prev = origin
    return out


def message_time(state_db: Path, row_id: int | None) -> float | None:
    """The timestamp of one message row (a page boundary), or None."""
    if not row_id:
        return None
    db = _open(state_db)
    if db is None:
        return None
    try:
        row = db.execute("SELECT timestamp FROM messages WHERE id=?", (int(row_id),)).fetchone()
        return float(row[0]) if row and isinstance(row[0], (int, float)) else None
    except sqlite3.Error:
        return None
    finally:
        db.close()


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
