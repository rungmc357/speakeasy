"""Continue an existing Hermes conversation from voice ("in the trip planning chat, also book ...").

Candidates are the user's recent live Hermes
sessions on any messaging platform (a Discord thread, a Telegram chat, a Slack channel, ...), read
read-only from ``<HERMES_HOME>/state.db``. Matching is deterministic (no third-party classifier):
the request must carry a continuation cue and clearly share words with exactly one recent
conversation's title/name.

The turn runs through ``/api/sessions/{id}/chat/stream`` with an ``X-Hermes-Session-Key`` alias,
the only API path that fans a completed turn back out to the native chat. The alias is added once
through Hermes' own config writer (comment-preserving) when running inside the gateway.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 40
RECENT_DAYS = 21
MIN_OVERLAP = 2
# Platforms whose sessions are the user's own chats. Internal sources never qualify.
EXCLUDED_SOURCES = {"cron", "subagent", "cli", "tool", "api_server", "webhook", "oneshot", "voice", "acp"}
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_:@.+=-]{1,128}$")

_CONTINUE_CUES = re.compile(
    r"\b(thread|channel|chat|conversation|project|session|over in|in the|pick up|continue|same place|there)\b", re.I)
_STOP = set("""a an the and or to of in on for with at by from is are was be this that it its
my our your me we you i he she they them do does did can could would should will just please
tell ask go let lets get have has had about into over up out so then than now new thing things
thread channel chat conversation session project build continue pick same place there also""".split())


@dataclasses.dataclass(frozen=True)
class Conversation:
    session_id: str
    platform: str
    chat_id: str
    chat_type: str
    thread_id: str
    user_id: str
    parent_chat_id: str
    name: str            # platform chat name, e.g. "My Server / #research / Topic"
    title: str           # Hermes session title
    last_active: float
    # False when Hermes recorded no chat type: ``chat_type`` then reads "dm" for ordinary text work,
    # but such a chat is never trusted with shared screens, pictures or files (see ``private``).
    chat_type_known: bool = True

    @property
    def private(self) -> bool:
        """A one-to-one chat with the user, as Hermes recorded it: the only kind of chat whose session
        a shared screen, picture or file may enter. Groups, channels, threads and unknowns never."""
        return self.chat_type_known and self.chat_type in {"dm", "private"}

    @property
    def label(self) -> str:
        """Short name to show and say: the Hermes session title, else the last chat-name segment."""
        topic = self.name.split(" / ")[-1].strip() if self.name else ""
        return (self.title or topic or self.platform)[:80]

    @property
    def where(self) -> str:
        return f"{self.platform.title()} \"{self.label}\""

    @property
    def target(self) -> str:
        """Where `hermes send` posts into this chat: `platform:chat_id`, plus the thread/topic when
        it is separate from the chat (Telegram topics; a Discord thread is its own chat id)."""
        out = f"{self.platform}:{self.chat_id}"
        if self.thread_id and self.thread_id != self.chat_id:
            out += f":{self.thread_id}"
        return out


def _open(state_db: Path) -> sqlite3.Connection | None:
    try:
        return sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None


def _from_row(session_id: str, source: str, chat_type: str | None, thread_id: str | None,
              origin_json: str | None, title: str, last: float) -> Conversation | None:
    try:
        origin = json.loads(origin_json or "{}")
    except ValueError:
        return None
    if not isinstance(origin, dict):
        return None
    chat_id = str(origin.get("chat_id") or "")
    if not SAFE_ID_RE.fullmatch(chat_id):
        return None
    fields = {k: str(origin.get(k) or "") for k in ("user_id", "parent_chat_id")}
    thread = str(thread_id or origin.get("thread_id") or "")
    if any(v and not SAFE_ID_RE.fullmatch(v) for v in (*fields.values(), thread)):
        return None
    stated = chat_type or origin.get("chat_type")
    return Conversation(session_id, source, chat_id, str(stated or "dm"), thread,
                        fields["user_id"], fields["parent_chat_id"], str(origin.get("chat_name") or "")[:200],
                        (title or "")[:200], float(last or 0), chat_type_known=bool(stated))


def recent_conversations(state_db: Path, *, days: int = RECENT_DAYS, limit: int = MAX_CANDIDATES,
                         now: float | None = None) -> list[Conversation]:
    """Live messaging-platform sessions active recently, newest first (read-only)."""
    since = (now or time.time()) - days * 86400
    db = _open(state_db)
    if db is None:
        return []
    marks = ",".join("?" * len(EXCLUDED_SOURCES))
    try:
        rows = db.execute(
            f"""SELECT s.id, s.source, s.chat_type, s.thread_id, s.origin_json, coalesce(s.title,''), max(m.timestamp)
               FROM sessions s JOIN messages m ON m.session_id = s.id
               WHERE s.ended_at IS NULL AND s.source NOT IN ({marks}) AND s.origin_json IS NOT NULL
                 AND m.timestamp > ?
               GROUP BY s.id ORDER BY max(m.timestamp) DESC LIMIT ?""",
            (*sorted(EXCLUDED_SOURCES), since, limit)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()
    out, seen = [], set()
    for row in rows:
        conv = _from_row(*row)
        if conv is None or conv.target in seen:  # one entry per chat, newest first
            continue
        seen.add(conv.target)
        out.append(conv)
    return out


def conversation_by_session(state_db: Path, session_id: str,
                            recent: list[Conversation] | None = None) -> Conversation | None:
    """The chat a session belongs to, backed by that chat's CURRENT live session. A Discord thread
    or Telegram chat keeps its id while Hermes swaps the session behind it (/new, a session switch,
    an auto-reset, compression); continuing the old session id would write into a dead session
    the chat no longer shows."""
    conv = _conversation_row(state_db, session_id)
    if conv is None:
        return None
    live = next((c for c in (recent if recent is not None else recent_conversations(state_db))
                 if c.target == conv.target), None)
    return live or conv


def _conversation_row(state_db: Path, session_id: str) -> Conversation | None:
    db = _open(state_db)
    if db is None:
        return None
    try:
        row = db.execute(
            """SELECT s.id, s.source, s.chat_type, s.thread_id, s.origin_json, coalesce(s.title,''),
                      coalesce((SELECT max(timestamp) FROM messages m WHERE m.session_id = s.id), s.started_at, 0)
               FROM sessions s WHERE s.id=?""", (session_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return _from_row(*row) if row else None


def session_busy(state_db: Path, session_id: str, *, window_s: float = 900, now: float | None = None) -> bool:
    """True while a conversation looks mid-turn (newest message is a user turn, a tool result, or a
    tool call, and recent). Voice work waits rather than colliding with a turn typed in the chat."""
    db = _open(state_db)
    if db is None:
        return False
    try:
        row = db.execute("SELECT role, tool_calls, timestamp FROM messages WHERE session_id=? "
                         "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    except sqlite3.Error:
        return False
    finally:
        db.close()
    if not row:
        return False
    role, tool_calls, ts = row
    open_turn = role in {"user", "tool"} or (role == "assistant" and bool(tool_calls))
    return open_turn and ((now or time.time()) - float(ts or 0)) < window_s


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP}


# -- finding the conversation by what was said in it --------------------------------------------
#
# A chat's title is set by its first turn ("Gateway tests exit code 1") and rarely says what the
# chat is about hours later. So candidates are found by what was actually said in each chat, and
# every chat is shown to the routing model with its recent user lines, not just its name.

MAX_SNIPPETS = 3
SNIPPET_CHARS = 160
POOL = 150          # recent chats searched by content (the model sees at most ``limit`` of them)
MIN_SCORE = 2.0     # about one rare shared word, or several less rare ones
SEARCH_BUDGET_S = 1.2  # the content search runs before routing, so it is on the user's wait (a hard stop)
# Gateway scaffolding around a user's words; stripped so the model sees what the person said.
_NOISE = re.compile(
    r"\[(?:Triggering message id|Image attached|Gateway message origin|IMPORTANT: Background)[^\]]*\]"
    r"|\[(?:Voice request from [^\]]*|Replying to: [^\]]*)\]"
    r"|\[OUT-OF-BAND USER MESSAGE[^\]]*\]|\[/OUT-OF-BAND USER MESSAGE\]"
    r"|Gateway message origin \(JSON[^\n]*\n\{[^\n]*\}\n?[^\n]*"
    r"|<memory-context>.*?</memory-context>|^\[[A-Za-z0-9_.-]{2,32}\]\s", re.S | re.M)
_MACHINE_LINE = re.compile(r"^\[?(?:ASYNC DELEGATION|IMPORTANT:|SYSTEM:|Cronjob Response|CONTEXT COMPACTION)", re.I)


@dataclasses.dataclass(frozen=True)
class Candidate:
    conv: Conversation
    snippets: tuple[str, ...]   # the user's latest lines in it, newest first
    hits: float = 0.0           # how strongly its recent talk matches the request (rare shared words)
    voice_request: str = ""     # the last spoken request sent into this chat, when there was one
    voice_at: float = 0.0


def with_placements(state_db: Path, candidates: list[Candidate], placements: list[dict[str, Any]],
                    limit: int = 8, recent: list[Conversation] | None = None) -> list[Candidate]:
    """Mark (or add) the chats voice work was recently sent into. Placements name the session at
    the time; the chat may be backed by a newer session now, so match by chat, not session."""
    by_chat = {_chat_key(c.conv): c for c in candidates}
    order = [_chat_key(c.conv) for c in candidates]
    for placed in placements:
        conv = conversation_by_session(state_db, placed["session_id"], recent)
        if conv is None:
            continue
        key = _chat_key(conv)
        current = by_chat.get(key)
        if current is not None and current.voice_at:
            continue  # newest placement already recorded
        if current is None:
            live = next((c for c in (recent if recent is not None else recent_conversations(state_db))
                         if _chat_key(c) == key), None)
            if live is None:
                continue  # that chat is gone or ended
            current = Candidate(live, ())
            order.insert(0, key)
        by_chat[key] = dataclasses.replace(current, voice_request=_clean_line(placed.get("request", "")),
                                           voice_at=float(placed.get("at") or 0))
    ranked = [by_chat[k] for k in dict.fromkeys(order)]
    voiced = sorted((c for c in ranked if c.voice_at), key=lambda c: -c.voice_at)
    return (voiced + [c for c in ranked if not c.voice_at])[:limit]


def _clean_line(text: str) -> str:
    text = _NOISE.sub(" ", text or "")
    return " ".join(text.split())[:SNIPPET_CHARS]


def _chat_key(conv: Conversation) -> str:
    """One chat, whatever Hermes session currently backs it (a thread keeps its id across
    resets and switches; the session id does not)."""
    return conv.target


# Only what the person typed or said: assistant replies and machine notices (subagent reports,
# background-process notices, cron output) mention everything and would make one busy thread
# match every request.
_HUMAN = ("AND m.content NOT LIKE '%[ASYNC DELEGATION%' AND m.content NOT LIKE '%[IMPORTANT: Background%' "
          "AND m.content NOT LIKE '[SYSTEM:%' AND m.content NOT LIKE '%CONTEXT COMPACTION%' "
          "AND m.content NOT LIKE '[Cron delivery%' AND length(m.content) < 4000")


def _recent_floor(db: sqlite3.Connection, since: float) -> int:
    """A message id at or before the start of the lookback window, found by bisecting ids (they
    grow with time) with primary-key lookups; a plain MIN(id) WHERE timestamp scanned the table.
    Errs low: an early floor only costs speed, never a missed match (timestamp is still checked)."""
    try:
        lo, hi = db.execute("SELECT MIN(id), MAX(id) FROM messages").fetchone()
        if lo is None:
            return 0
        lo, hi = int(lo), int(hi)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            row = db.execute("SELECT timestamp FROM messages WHERE id >= ? ORDER BY id LIMIT 1", (mid,)).fetchone()
            if row is None or row[0] is None or float(row[0]) > since:
                hi = mid
            else:
                lo = mid
        return max(0, lo - 1000)  # slack for out-of-order timestamps
    except (sqlite3.Error, TypeError, ValueError):
        return 0


def _term_sessions(db: sqlite3.Connection, term: str, since: float, sessions: list[str],
                   floor: int = 0) -> set[str]:
    """Sessions (of those given) where the person recently used the word.

    Drives the search from the word index, limited to recent message ids. Letting SQLite pick
    the plan walked every message of each candidate chat and re-checked the word against it:
    7-20 s per request on a large history, spent before the routing model even started."""
    marks = ",".join("?" * len(sessions))
    try:
        rows = db.execute(
            f"""SELECT DISTINCT m.session_id FROM messages_fts f CROSS JOIN messages m ON m.id = f.rowid
                WHERE messages_fts MATCH ? AND f.rowid >= ? AND m.timestamp > ? AND m.role = 'user' {_HUMAN}
                  AND m.session_id IN ({marks})""",
            ('"' + term.replace('"', "") + '"', floor, since, *sessions)).fetchall()
    except sqlite3.Error as exc:  # no full-text index (older Hermes): a plain scan
        if _interrupted(exc):
            raise _OverBudget from exc  # out of time, not out of an index: never fall back to a scan
        try:
            rows = db.execute(
                f"""SELECT DISTINCT m.session_id FROM messages m WHERE m.timestamp > ? AND m.role = 'user' {_HUMAN}
                    AND m.session_id IN ({marks}) AND m.content LIKE ?""", (since, *sessions, f"%{term}%")).fetchall()
        except sqlite3.Error as again:
            if _interrupted(again):
                raise _OverBudget from again
            raise
    return {r[0] for r in rows}


def _interrupted(exc: BaseException) -> bool:
    return "interrupt" in str(exc).lower()


class _OverBudget(Exception):
    """A word search ran past the search budget and was stopped mid-query."""


def _budgeted(db: sqlite3.Connection, deadline: float) -> None:
    """Stop any query on *db* that is still running at *deadline* (monotonic). The budget was only
    checked between words, so one word on cold pages of a large history ran 3-6 s by itself: the
    first hand-off of a call waited 4.4-7.2 s on this search (SPK-442)."""
    db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 2000)


# -- the part of the lookup that doesn't depend on the words (SPK-442) ---------------------------
#
# Which chats are live, where the lookback window starts, and each chat's latest user lines are
# the same for every request in a call. Read once when the call starts (on cold disk pages of a
# multi-GB history that is most of the first hand-off's wait) and reused by each request, so a
# request only pays for searching its own words.

BASE_TTL_S = 120.0       # a call's prepared chat list is reused this long, then read again
BASE_SNIPPETS = 8        # chats whose latest lines are read ahead (the always-offered recent ones)


@dataclasses.dataclass
class Base:
    convs: list[Conversation]
    since: float
    floor: int
    days: int
    made: float                                   # time.monotonic() when read
    snippets: dict[str, tuple[str, ...]] = dataclasses.field(default_factory=dict)

    def fresh(self, max_age: float = BASE_TTL_S) -> bool:
        return time.monotonic() - self.made < max_age


def prepare(state_db: Path, *, days: int = RECENT_DAYS, now: float | None = None,
            snippets: int = BASE_SNIPPETS) -> Base | None:
    """Read the request-independent part of ``conversations_with_context`` (read-only)."""
    convs = recent_conversations(state_db, days=days, now=now, limit=POOL)
    since = (now or time.time()) - days * 86400
    if not convs:
        return Base([], since, 0, days, time.monotonic())
    db = _open(state_db)
    if db is None:
        return None
    try:
        base = Base(convs, since, _recent_floor(db, since), days, time.monotonic())
        for c in convs[:snippets]:
            base.snippets[c.session_id] = _snippets(db, c.session_id)
        return base
    except sqlite3.Error:
        return None
    finally:
        db.close()


def conversations_with_context(state_db: Path, request: str, *, days: int = RECENT_DAYS,
                               limit: int = 8, now: float | None = None, base: Base | None = None) -> list[Candidate]:
    """The chats worth showing the routing model: the most recently active ones plus the ones
    whose recent messages talk about what was asked, each with the user's latest lines.

    Relevance is how many of the request's words a chat used, weighted by how rare each word is
    across chats (a word every chat uses, like "going", counts for little). Not a raw message
    count: a long-running thread would otherwise match everything.

    Read-only. Keyed by chat, not by session: a Discord thread whose session was reset, switched
    or compressed is still one chat, and its newest live session is the one to continue.
    """
    import math
    if base is not None and base.days == days:
        convs = base.convs
    else:
        base = None
        convs = recent_conversations(state_db, days=days, now=now, limit=POOL)
    if not convs:
        return []
    db = _open(state_db)
    if db is None:
        return [Candidate(c, base.snippets.get(c.session_id, ()) if base else ()) for c in convs[:limit]]
    try:
        scores: dict[str, float] = {}
        terms = sorted(_words(request), key=len, reverse=True)[:10]
        if terms:
            since = base.since if base else (now or time.time()) - days * 86400
            ids = [c.session_id for c in convs]
            deadline = time.monotonic() + SEARCH_BUDGET_S
            floor = base.floor if base else _recent_floor(db, since)
            _budgeted(db, deadline)   # a hard stop, mid-query too (SPK-442)
            for term in terms:
                if time.monotonic() > deadline:
                    break  # routing waits on this: the rarest (longest) words were searched first
                try:
                    found = _term_sessions(db, term, since, ids, floor)
                except _OverBudget:
                    logger.info("speakeasy: chat search stopped at its %.1f s budget", SEARCH_BUDGET_S)
                    break
                if not found or len(found) > len(ids) * 0.5:
                    continue  # absent, or so common it says nothing about which chat
                weight = math.log(len(ids) / len(found))
                for sid in found:
                    scores[sid] = scores.get(sid, 0.0) + weight
            db.set_progress_handler(None, 0)
        recent = convs[:max(1, limit - 3)]
        topical = sorted((c for c in convs if scores.get(c.session_id, 0) >= MIN_SCORE),
                         key=lambda c: (-scores[c.session_id], -c.last_active))
        picked: list[Conversation] = []
        for c in [*recent[:3], *topical[:limit - 3], *recent[3:]]:
            if c not in picked and len(picked) < limit:
                picked.append(c)
        known = base.snippets if base else {}
        return [Candidate(c, known[c.session_id] if c.session_id in known else _snippets(db, c.session_id),
                          round(scores.get(c.session_id, 0.0), 2)) for c in picked]
    except sqlite3.Error:
        return [Candidate(c, ()) for c in convs[:limit]]
    finally:
        db.close()


# Hermes stores a message with parts (text plus images) as this prefix and the parts' JSON.
_CONTENT_JSON_PREFIX = "\x00json:"


def message_text(content: Any) -> str:
    """The words of a stored message. A message with parts keeps only its text parts: image data
    never reaches a snippet (snippets go to the routing model). Unreadable parts count as no text."""
    if not isinstance(content, str):
        return ""
    if not content.startswith(_CONTENT_JSON_PREFIX):
        return content
    try:
        parts = json.loads(content[len(_CONTENT_JSON_PREFIX):])
    except ValueError:
        return ""
    if isinstance(parts, str):  # a literal text that happened to start with the prefix
        return parts
    if isinstance(parts, dict):
        parts = [parts]
    if not isinstance(parts, list):
        return ""
    texts = [p if isinstance(p, str) else p.get("text") for p in parts
             if isinstance(p, str) or (isinstance(p, dict) and p.get("type") in {"text", "input_text"})]
    return "\n".join(t for t in texts if isinstance(t, str) and t.strip())


def refused(error: Any) -> bool:
    """Hermes turned the turn away at the door: newer Hermes only lets API turns into sessions the
    API itself started, so a Discord/Telegram conversation can't be written into from outside."""
    return "permission_denied" in str(error or "")


def recent_exchange(state_db: Path, session_id: str, limit: int = 16, max_chars: int = 4000) -> str:
    """The conversation's last few user/assistant messages as plain lines, for a task that has to
    run beside the conversation instead of inside it."""
    try:
        db = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return ""
    try:
        rows = db.execute("SELECT role, content FROM messages WHERE session_id=? AND role IN ('user','assistant') "
                          "AND content IS NOT NULL AND content != '' ORDER BY id DESC LIMIT ?",
                          (session_id, limit)).fetchall()
    except sqlite3.Error:
        return ""
    finally:
        db.close()
    lines = []
    for role, text in reversed(rows):
        line = " ".join(message_text(text).split())
        if not line or _MACHINE_LINE.match(line):
            continue
        lines.append(("User: " if role == "user" else "Assistant: ") + (line[:600] + "…" if len(line) > 600 else line))
    out = "\n".join(lines)
    return out[-max_chars:]


def _snippets(db: sqlite3.Connection, session_id: str) -> tuple[str, ...]:
    rows = db.execute("SELECT content FROM messages WHERE session_id=? AND role='user' AND content IS NOT NULL "
                      "ORDER BY id DESC LIMIT 15", (session_id,)).fetchall()
    out: list[str] = []
    for (text,) in rows:
        line = _clean_line(message_text(text))
        if len(line) > 8 and not _MACHINE_LINE.match(line) and line not in out:
            out.append(line)
        if len(out) >= MAX_SNIPPETS:
            break
    return tuple(out)


def best_by_content(request: str, candidates: list[Candidate]) -> Conversation | None:
    """No routing model: continue the chat whose recent talk clearly matches, only with a
    continuation cue ("the thread where we...", "continue the ...") and a clear winner."""
    if not candidates or not _CONTINUE_CUES.search(request or ""):
        return None
    if re.search(r"(?i)\b(how is|how's|where are we|what did we|status of|any update)\b", request):
        return None
    ranked = sorted(candidates, key=lambda c: (c.hits, c.conv.last_active), reverse=True)
    top = ranked[0]
    if top.hits < MIN_SCORE * 1.5 or (len(ranked) > 1 and ranked[1].hits * 1.5 > top.hits):
        return None
    return top.conv


def match(request: str, conversations: list[Conversation]) -> Conversation | None:
    """Deterministic: a continuation cue, then one clear best overlap of at least MIN_OVERLAP words.

    Status questions ("how is X going") are answered by reading, not by continuing a conversation.
    """
    request = (request or "").strip()
    if not request or not _CONTINUE_CUES.search(request):
        return None
    if re.search(r"(?i)\b(how is|how's|where are we|what did we|status of|any update)\b", request):
        return None
    asked = _words(request)
    if not asked:
        return None
    scored = sorted(((len(asked & (_words(c.name) | _words(c.title))), c.last_active, c) for c in conversations),
                    key=lambda item: (item[0], item[1]), reverse=True)
    if not scored or scored[0][0] < MIN_OVERLAP:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0]:
        return None  # ambiguous: two conversations match equally well
    return scored[0][2]


STREAM_BUDGET = 4 * 1024 * 1024  # what one turn may stream back, not counting the echo of what was sent


def stream_session_chat(base: str, key: str, conv: Conversation, message: str | list[dict[str, Any]],
                        callback: Callable[[str, dict[str, Any]], None], timeout: float = 1800,
                        opener: Callable[..., Any] = urllib.request.urlopen) -> bool:
    """Run one turn in the conversation's own session; call back with (event, payload). True when a
    terminal run event arrived. Closing the stream interrupts the turn (Hermes' contract).

    ``message`` is plain text, or text and image parts (``hermes_api.user_content``); this route
    normalizes either. Hermes repeats the whole message, images included, in ``run.started``, so
    the stream budget starts after that event and the echo itself never reaches ``callback``."""
    data = json.dumps({"message": message}).encode()
    req = urllib.request.Request(f"{base.rstrip('/')}/api/sessions/{conv.session_id}/chat/stream",
                                 data=data, method="POST")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    name, terminal, total, started = None, False, 0, False
    with opener(req, timeout=timeout) as response:
        for raw in response:
            total += len(raw)
            # Until run.started the echo of the sent message is allowed on top of the budget.
            if total > STREAM_BUDGET + (0 if started else len(data)):
                raise ValueError("conversation stream too large")
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: "):
                try:
                    payload = json.loads(line[6:])
                except ValueError:
                    payload = {}
                event = name or (payload.get("event") if isinstance(payload, dict) else None) or ""
                if event == "run.started" and not started:
                    started, total = True, 0
                    if isinstance(payload, dict):
                        payload.pop("user_message", None)  # the echo: nobody needs it, and it can hold images
                if isinstance(payload, dict):
                    callback(event, payload)
                terminal = terminal or (event.startswith("run.") and event != "run.started")
                name = None
    return terminal
