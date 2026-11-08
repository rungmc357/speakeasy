"""Home is a chat (SPK-208): one ongoing conversation with a coordinator.

Typing on Home used to start a job for every message, so "yo" became a task card. Now each message
goes to the coordinator first: one model call that sees the recent Home conversation and the jobs on
Home. It answers like a chat model does:

- plain text: chat. The text is the whole answer ("yo", "thanks", a question it can answer from the
  conversation, general knowledge or a job's result). No job, no card.
- a ``start_job`` tool call: real work. A short reply, then a job through the same machinery as
  ``POST /voice/work``; its card appears in the thread right after the reply and updates in place.
- ``message_job``: words for an existing job (steer while it runs, the answer while it asks, a
  follow-up once it finished), through the job's own message route.
- ``cancel_job``: stop a running job.

Tool calls instead of a JSON reply format: real fast models answered questions in prose about half
the time when asked for strict JSON, and prose is the right answer for chat anyway. A legacy JSON
reply (``{"reply", "action", "job", "request"}``) is still understood.

The coordinator is not a Hermes session: it has no real tools and never does work itself, which
keeps it fast. Its model (``route``), first that applies:

1. the user's pick on Home's model chip (``POST /voice/home/model``, kept in Speakeasy's store);
2. ``auxiliary.speakeasy_home`` when the user configured one in Hermes;
3. Auto: the first model in ``COORDINATOR_MODELS`` the user already has connected in Hermes (read
   from Hermes' own model list, never by calling a model);
4. the user's main model; then the Speakeasy routing model.

Home's model chip picks the WORK model (``work`` in ``GET /voice/home/model``, ``{"work_model": id}``
on POST): the model the jobs started from Home run on. Unset, jobs run on the user's Hermes default
model. The coordinator's own model above is a secondary choice in the same menu (``{"model": id}``).
Changing one never changes the other, and neither writes Hermes' default or auxiliary models.

Nothing here writes the user's Hermes config. Live calls keep their own routing; this is only Home's
composer.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from typing import Any, Callable

from . import host as host_mod

logger = logging.getLogger(__name__)

AUX_TASK = "speakeasy_home"
AUX_DISPLAY_NAME = "Speakeasy Home chat"
FALLBACK_TASK = "speakeasy_router"
TIMEOUT_S = 30.0
MAX_TOKENS = 700
HISTORY_TURNS = 20            # past messages the coordinator sees
SHOWN_S = 24 * 3600           # Home shows the last day of chat (after the last Clear)
SHOWN_MAX = 80
MAX_TEXT = 4000
REPLY_CHARS = 1500
RESULT_CHARS = 1600           # a finished job's answer, for "what did it find?"
JOBS = 8
ACTIONS = {"none", "start", "follow_up", "cancel"}
EXTRA_JOBS = 3                # "two asks in one message are two jobs": at most this many more per message
CHOICE_META = "home_model"    # the coordinator's model pick, a catalog id ("provider/model"); unset = Auto
WORK_META = "home_work_model"  # the Home chip's pick: the model jobs started from Home run on; unset = Hermes' default
WORK_OPTIONS = 6
FAILED_META = "home_model_failed"   # {model id: when it last failed}: listed in Hermes but not answering
FAILED_S = 24 * 3600          # Auto skips a model that failed in real use for this long (never probed)

# Approved coordinator models, best first. Home's coordinator answers chat and decides "reply, start
# a job, message a job"; it needs to be quick, good at plain conversation and reliable with tool
# calls. Each entry is (provider slug or None for any provider, model-id pattern); within one entry
# the newest version the user has wins. Auto picks the first entry the user already has connected in
# Hermes; the Home model chip overrides it. Keep this list short and current; swap entries here.
COORDINATOR_MODELS: list[tuple[str | None, str]] = [
    ("anthropic", r"^claude-haiku-\d"),      # fast, good plain chat, dependable tool calls
    (None, r"claude-haiku-\d"),              # the same model through another provider (OpenRouter, Bedrock...)
    (None, r"(^|/)gpt-\d+(\.\d+)*-mini$"),
    (None, r"gemini-\d+(\.\d+)*-flash$"),
    (None, r"grok-\d+(\.\d+)*-fast"),
]
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_SMALL_TALK = re.compile(
    r"(?i)^\W*(?:yo+|hi+|hey+|hello|hiya|sup|what'?s up|good (?:morning|afternoon|evening|night)|"
    r"thanks?(?: you)?|thank you|thx|ty|ok(?:ay)?|cool|nice|great|perfect|got it|lol|haha|bye|"
    r"see you|cheers|👍|🙏)\W*$")


def small_talk(text: str) -> bool:
    return bool(_SMALL_TALK.match(text or ""))


def _version(model: str) -> tuple[int, ...]:
    """Newest first within a family: the version digits in the id (dates count as one big number)."""
    return tuple(int(n) for n in re.findall(r"\d+", model)[:4])


def approved(entries: dict[str, dict[str, Any]]) -> list[str]:
    """The user's connected models that are approved coordinators, best first (COORDINATOR_MODELS
    order, newest version first within an entry). Pure: reads only *entries*."""
    out: list[str] = []
    for provider, pattern in COORDINATOR_MODELS:
        rx = re.compile(pattern, re.I)
        hits = [e for e in entries.values()
                if (provider is None or e.get("provider") == provider) and rx.search(str(e.get("model") or ""))]
        hits.sort(key=lambda e: _version(str(e["model"])), reverse=True)
        out += [e["id"] for e in hits if e["id"] not in out]
    return out


def pick_auto(entries: dict[str, dict[str, Any]]) -> str | None:
    got = approved(entries)
    return got[0] if got else None


_PROMPT = """You are {name}, the user's own AI assistant, chatting with them on the Home screen of the \
Speakeasy app. Home is one ongoing conversation. Behind you, jobs (full agent sessions with tools, the \
user's computer, files, accounts and the web) do real work.

Answer in plain text yourself for greetings, thanks, small talk, and anything you can answer from \
general knowledge, this conversation, or the jobs listed below (their status and results). Keep it \
conversational: usually one to three sentences, no headings, no lists unless asked.

Use a tool instead when the message needs doing or live or personal information: searching, booking, \
buying, files, code, email, calendar, messages, the user's machines or accounts, weather, prices, news, \
or anything about the user you don't already know. Anything the user wants shown or made (a card, a ticket, a \
pull request, a chart, a draft, a document, a picture, a list to keep) is a job too: jobs draw cards, \
your reply can't.
- start_job for new work. "request" is the task in self-contained words (resolve "it", "that", "the one \
you mentioned" from the conversation); "reply" is one short line in your own words about what you're \
doing ("Looking for a quiet cafe near the river."). Never "Starting task".
- message_job when the message changes, corrects, answers or adds to an existing job, or asks it to do \
more with its result ("make it shorter", "actually Friday", "also check the price"). "job" is its id.
- cancel_job to stop a running job. "job" is its id.
- One message that asks for two separate things gets two start_job calls, one per job ("Book the \
dentist and find a birthday gift" is two jobs). Never merge unrelated asks into one job, and never \
drop one: each ask is either answered in your reply or gets its own job. When you start a job and \
the same message also asks something you can answer yourself, answer it in that job's reply too \
("Looking for a hardware store open Sunday. A soft-boiled egg takes about 6 minutes.").

Rules:
- Plain prose only: no code blocks, tables, box drawing or mock-ups of cards in your reply.
- Use US units: miles, feet, °F, pounds, inches, gallons, mph. Lead with the US number; add metric \
only in parentheses after it, if at all (\"about 238,900 miles (384,400 km)\"), never metric first.
- Never repeat or paraphrase the user's message back to them. Never narrate routing ("I'll send this to \
a job", "a task will handle it"): just say what you're doing.
- Never say you can't access something live or can't do something: start a job, which can.
- Never invent facts about the user, their files, accounts or anything live. If you'd have to guess, \
start a job instead.
- Name a job by what it is about, never by its id. When the user refers to a job ("that", "the cafe \
one"), pick it from the list; if unclear, the newest one the conversation was about.
{attachments}{focus}
Now: {now}.
Jobs on Home (newest first):
{jobs}"""

_QUAL = r"(?:about |around |roughly |approximately |nearly |almost |~)"
_NUM = r"-?\d[\d,]*(?:\.\d+)?"
_METRIC = r"(?:km|kilometers?|kilometres?|meters?|metres?|m|cm|centimeters?|centimetres?|kg|kilograms?|°C|degrees Celsius|liters?|litres?|km/h|kph)"
_US = r"(?:miles?|mi|feet|ft|foot|inches?|in|°F|degrees Fahrenheit|pounds?|lbs?|gallons?|mph)"
_METRIC_FIRST = re.compile(
    rf"(?P<q>{_QUAL})?(?P<m>{_NUM}\s?{_METRIC})\b(?P<sep>\s*\(\s*|,\s*or\s+|\s+or\s+|\s*/\s*|\s*\u2014\s*)"
    rf"(?P<q2>{_QUAL})?(?P<u>{_NUM}\s?{_US})(?:\s*\)|,(?=\s))?", re.I)


def us_units_first(text: str) -> str:
    """A reply that leads with metric and gives the US figure second ("384,400 km (238,900 miles)")
    is turned around: the US number first, metric in parentheses. Everything else is unchanged."""
    def swap(m: re.Match) -> str:
        qual = m.group("q") or m.group("q2") or ""
        return f"{qual}{m.group('u').strip()} ({m.group('m').strip()})"
    return _METRIC_FIRST.sub(swap, text or "")

_REPLY = {"type": "string", "description": "One short line to the user, in your own words, about what you're doing."}
_START_REPLY = {"type": "string", "description": "One short line to the user, in your own words, about what you're "
                "doing, plus the answer to any other part of the message you can answer yourself."}
_JOB = {"type": "string", "description": "The job's id from the list (j1, j2, ...)."}
TOOLS = [
    {"type": "function", "function": {
        "name": "start_job",
        "description": "Start a new job: real work, the user's computer or accounts, or live information.",
        "parameters": {"type": "object", "properties": {
            "request": {"type": "string", "description": "The task in self-contained words."},
            "more_jobs": {"type": "array", "items": {"type": "string"}, "maxItems": EXTRA_JOBS,
                          "description": "Every OTHER separate ask in the same message that needs its own "
                                         "job, each in self-contained words; empty when there is only one. An ask "
                                         "you don't answer in the reply goes here."},
            "reply": _START_REPLY}, "required": ["request", "more_jobs", "reply"]}}},
    {"type": "function", "function": {
        "name": "message_job",
        "description": "Send words to an existing job: a change, a correction, the answer to its question, "
                       "or more work on its result.",
        "parameters": {"type": "object", "properties": {
            "job": _JOB, "request": {"type": "string", "description": "The new instruction in self-contained words."},
            "reply": _REPLY}, "required": ["job", "request", "reply"]}}},
    {"type": "function", "function": {
        "name": "cancel_job",
        "description": "Stop a running job.",
        "parameters": {"type": "object", "properties": {"job": _JOB, "reply": _REPLY},
                       "required": ["job", "reply"]}}},
]
_TOOL_ACTION = {"start_job": "start", "message_job": "follow_up", "cancel_job": "cancel"}


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _job_id(job: Any, aliases: dict[str, str], titles: dict[str, str]) -> str | None:
    """A job the model named: its alias (``j2``, even with stray characters), else its title."""
    if not isinstance(job, str) or not job.strip():
        return None
    job = job.strip()
    if job in aliases:
        return aliases[job]
    m = re.match(r"(?i)\W*j\s*(\d+)", job)
    if m and f"j{m.group(1)}" in aliases:
        return aliases[f"j{m.group(1)}"]
    want = " ".join(re.findall(r"[a-z0-9]+", job.lower()))
    for alias, title in titles.items():
        have = " ".join(re.findall(r"[a-z0-9]+", (title or "").lower()))
        if want and have and (want == have or want in have or have in want):
            return aliases.get(alias)
    return None


def _words(text: Any) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(text or "").lower())) - {"a", "an", "the", "and", "in", "on", "for", "to"}


def _distinct(main: Any, extra: list[dict[str, str]]) -> list[dict[str, str]]:
    """Each ask once: models sometimes list an ask in ``more_jobs`` and also call ``start_job`` for it
    again, which started the same search twice. A further ask that repeats (nearly word for word) the
    main ask or an earlier one is dropped."""
    kept: list[dict[str, str]] = []
    seen = [_words(main)] if _words(main) else []
    for e in extra:
        w = _words(e["request"])
        if not w or any(len(w & s) / len(w | s) >= 0.8 for s in seen):
            continue
        seen.append(w)
        kept.append(e)
    return kept


def parse(raw: Any, aliases: dict[str, str], titles: dict[str, str] | None = None) -> dict[str, Any] | None:
    """The coordinator's answer, checked: a tool call (``{"tool", "args", "text"}``), a legacy JSON reply,
    or plain text (chat). A follow-up/cancel must name a known job. None when it isn't usable (the caller
    falls back)."""
    titles = titles or {}
    if isinstance(raw, dict) and raw.get("tool"):
        name = str(raw["tool"]).rsplit("__", 1)[-1].rsplit(".", 1)[-1]
        action = _TOOL_ACTION.get(name)
        args = raw.get("args") if isinstance(raw.get("args"), dict) else {}
        if action is None:
            return None
        data = {"action": action, "reply": args.get("reply") or raw.get("text") or "",
                "job": args.get("job"), "request": args.get("request")}
        extra = []
        for words in (args.get("more_jobs") or []) if name == "start_job" and isinstance(args.get("more_jobs"), list) else []:
            if isinstance(words, str) and words.strip():
                extra.append({"request": " ".join(words.split()), "reply": ""})
        for more in raw.get("more") or []:
            mname = str(more.get("tool") or "").rsplit("__", 1)[-1].rsplit(".", 1)[-1]
            margs = more.get("args") if isinstance(more.get("args"), dict) else {}
            words = margs.get("request")
            if mname == "start_job" and isinstance(words, str) and words.strip():
                extra.append({"request": " ".join(words.split()), "reply": str(margs.get("reply") or "").strip()})
        data["extra"] = _distinct(args.get("request"), extra)[:EXTRA_JOBS]
    else:
        text = raw.get("text") if isinstance(raw, dict) else raw
        if not isinstance(text, str) or not text.strip():
            return None
        text = text.strip()
        bare = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        data = None
        if bare.startswith("{") and bare.endswith("}"):
            try:
                data = json.loads(bare)
            except ValueError:
                data = None
            if not isinstance(data, dict) or "action" not in data and "reply" not in data:
                data = None
        if data is None:
            return {"reply": us_units_first(text)[:REPLY_CHARS], "action": "none", "run_id": None, "request": None}
    action = str(data.get("action") or "none").strip().lower()
    if action not in ACTIONS:
        return None
    reply = data.get("reply")
    reply = reply.strip() if isinstance(reply, str) else ""
    run_id = _job_id(data.get("job"), aliases, titles)
    request = data.get("request")
    request = " ".join(request.split()) if isinstance(request, str) and request.strip() else None
    if action in {"follow_up", "cancel"} and not run_id:
        return None
    if action == "none" and not reply:
        return None
    extra = data.get("extra") if isinstance(data.get("extra"), list) else []
    if extra and action == "none":
        # Prose plus a start_job: the first ask is the main action.
        first = extra.pop(0)
        action, request, reply = "start", first["request"], (reply or first["reply"])
    if extra:
        # One reply line for the whole message: the extra jobs' own lines join the first.
        lines = [reply] + [e["reply"] for e in extra if e["reply"] and e["reply"] not in reply]
        reply = " ".join(x for x in lines if x)
    return {"reply": us_units_first(reply)[:REPLY_CHARS], "action": action, "run_id": run_id, "request": request,
            "extra": [e["request"] for e in extra]}


class HomeChat:
    """Home's conversation: the messages (store table ``home_messages``) and the coordinator turn."""

    def __init__(self, service: Any, call: Callable[..., Any] | None = None, clock: Callable[[], float] = time.time):
        self.svc = service
        self.store = service.store
        self._call = call
        self.clock = clock
        self._lock = threading.Lock()
        self._busy: dict[str, threading.Event] = {}
        self._titles: dict[str, str] = {}
        with self.store._lock, self.store._db:
            self.store._db.execute("""CREATE TABLE IF NOT EXISTS home_messages (
                id TEXT PRIMARY KEY, client_id TEXT, role TEXT NOT NULL, text TEXT NOT NULL, at REAL NOT NULL,
                model TEXT, run_id TEXT, action TEXT, voice TEXT, reply_to TEXT)""")
            self.store._db.execute("CREATE UNIQUE INDEX IF NOT EXISTS home_messages_client "
                                   "ON home_messages(client_id) WHERE client_id IS NOT NULL")
            # Further jobs one message started ("two asks, two jobs"); the first is home_messages.run_id.
            self.store._db.execute("CREATE TABLE IF NOT EXISTS home_jobs (message_id TEXT NOT NULL, "
                                   "run_id TEXT NOT NULL, at REAL NOT NULL)")

    # -- the messages ----------------------------------------------------------------------------
    def _rows(self, where: str = "", args: tuple = (), limit: int = SHOWN_MAX) -> list[dict[str, Any]]:
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT id, client_id, role, text, at, model, run_id, action, voice, reply_to FROM home_messages "
                f"{where} ORDER BY at DESC, rowid DESC LIMIT ?", (*args, limit)).fetchall()
        names = ("id", "client_id", "role", "text", "at", "model", "run_id", "action", "voice", "reply_to")
        out: list[dict[str, Any]] = [dict(zip(names, r)) for r in rows]
        for r in out:
            for k in ("model", "voice"):
                try:
                    r[k] = json.loads(r[k]) if r[k] else None
                except ValueError:
                    r[k] = None
        return list(reversed(out))

    def _add(self, role: str, text: str, at: float, client_id: str | None = None, model: dict | None = None,
             voice: dict | None = None, reply_to: str | None = None) -> str:
        mid = ("hu_" if role == "user" else "ha_") + secrets.token_hex(8)
        with self.store._lock, self.store._db:
            self.store._db.execute(
                "INSERT INTO home_messages (id, client_id, role, text, at, model, voice, reply_to) VALUES (?,?,?,?,?,?,?,?)",
                (mid, client_id, role, text, at, json.dumps(model) if model else None,
                 json.dumps(voice) if voice else None, reply_to))
        return mid

    def _extra_run(self, mid: str, run_id: str) -> None:
        with self.store._lock, self.store._db:
            self.store._db.execute("INSERT INTO home_jobs (message_id, run_id, at) VALUES (?,?,?)",
                                   (mid, run_id, self.clock()))

    def _link(self, mid: str, run_id: str | None, action: str) -> None:
        with self.store._lock, self.store._db:
            self.store._db.execute("UPDATE home_messages SET run_id=?, action=? WHERE id=?", (run_id, action, mid))

    def _cleared(self) -> float:
        try:
            return float(self.store.get_meta("now_cleared_at") or 0)
        except ValueError:
            return 0.0

    def shown(self, now: float | None = None) -> list[dict[str, Any]]:
        """The messages Home shows: the last day's, after the last Clear, oldest first."""
        now = self.clock() if now is None else now
        return self._rows("WHERE at >= ?", (max(now - SHOWN_S, self._cleared()),))

    def started_runs(self) -> set[str]:
        """Runs a Home message started or followed up: their card needs no "you asked" bubble (the
        message already shows)."""
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT run_id FROM home_messages WHERE run_id IS NOT NULL AND at >= ?",
                (self.clock() - 2 * SHOWN_S,)).fetchall()
            rows += self.store._db.execute("SELECT run_id FROM home_jobs WHERE at >= ?",
                                           (self.clock() - 2 * SHOWN_S,)).fetchall()
        return {r[0] for r in rows if r[0]}

    @staticmethod
    def entry(row: dict[str, Any]) -> dict[str, Any]:
        """One stream entry: ``{"kind": "message", "role": "user"|"assistant", "text", "at", "model"?}``."""
        out: dict[str, Any] = {"id": "m:" + row["id"], "kind": "message", "role": row["role"],
                               "text": row["text"], "at": row["at"]}
        if row.get("model"):
            out["model"] = row["model"]
        if row.get("voice"):
            out["voice"] = row["voice"]
        if row.get("run_id"):
            out["run_id"] = row["run_id"]
        if row.get("client_id") and row["role"] == "user":
            out["client_id"] = row["client_id"]   # the app swaps its own "sending" bubble for this one
        return out

    # -- the coordinator turn --------------------------------------------------------------------
    def message(self, body: dict[str, Any], device_id: str | None = None, *, voice: dict | None = None,
                job_text: str | None = None) -> dict[str, Any]:
        """``POST /voice/home/message {text, client_id?, attachments?, model?, reasoning?, agent?}``.
        Idempotent on client_id: a retry gets the first reply and never starts a second job."""
        if not isinstance(body, dict) or "text" not in body or not set(body) <= {
                "text", "client_id", "attachments", "model", "reasoning", "agent", "source", "focus"}:
            raise self._error(400, "Send the message as text.")
        text, client_id = body.get("text"), body.get("client_id")
        ids = body.get("attachments") or []
        if not isinstance(ids, list):
            raise self._error(400, "attachments must be a list of attachment ids.")
        if not isinstance(text, str) or (not text.strip() and not ids):
            raise self._error(400, "The message is empty.")
        text = text.strip()
        if len(text) > MAX_TEXT:
            raise self._error(400, f"The message is too long (at most {MAX_TEXT} characters).")
        if client_id is not None and (not isinstance(client_id, str) or not ID_RE.fullmatch(client_id)):
            raise self._error(400, "client_id must be an id.")
        if client_id:
            with self._lock:
                event = self._busy.get(client_id)
                mine = event is None
                if mine:
                    self._busy[client_id] = threading.Event()
            if not mine:
                event.wait(TIMEOUT_S + 30)
            done = self._replay(client_id)
            if done is not None:
                if mine:
                    self._release(client_id)
                return done
            if not mine:
                raise self._error(409, "That message is still being sent. Try again in a moment.")
        try:
            return self._turn(text, client_id, ids, body, device_id, voice, job_text)
        finally:
            if client_id:
                self._release(client_id)

    def _release(self, client_id: str) -> None:
        with self._lock:
            event = self._busy.pop(client_id, None)
        if event is not None:
            event.set()

    def _replay(self, client_id: str) -> dict[str, Any] | None:
        mine = self._rows("WHERE client_id=?", (client_id,), limit=1)
        if not mine:
            return None
        user = mine[0]
        reply = self._rows("WHERE reply_to=?", (user["id"],), limit=1)
        return self._reply(user, reply[0] if reply else None, replayed=True)

    def _reply(self, user: dict[str, Any], reply: dict[str, Any] | None, replayed: bool = False,
               started: dict[str, Any] | None = None) -> dict[str, Any]:
        source = reply or user
        out: dict[str, Any] = {"ok": True, "message": self.entry(user),
                               "reply": self.entry(reply) if reply else None,
                               "action": source.get("action") or "none", "run_id": source.get("run_id")}
        if started:
            out["session_id"] = started.get("session_id")
            out["task_id"] = started.get("task_id")
            if started.get("attachments"):
                out["attachments"] = started["attachments"]
        if replayed:
            out["replayed"] = True
        return out

    def _turn(self, text: str, client_id: str | None, ids: list[str], body: dict[str, Any],
              device_id: str | None, voice: dict | None, job_text: str | None) -> dict[str, Any]:
        history = self.shown()[-HISTORY_TURNS:]
        jobs, aliases = self.jobs()
        shown_text = text or ("(a picture)" if len(ids) == 1 else f"({len(ids)} pictures and files)")
        user_id = self._add("user", shown_text, self.clock(), client_id=client_id, voice=voice)
        self.svc._now_changed()   # every device shows the message while the coordinator thinks
        focus = body.get("focus") if isinstance(body.get("focus"), str) else None
        decided, model = self._decide(text, history, (jobs, aliases), len(ids), focus=focus)
        if decided is None:
            # No coordinator answer (no model, a timeout, unreadable JSON): never invent a reply. Small
            # talk gets a plain line; anything else runs as a job, as Home always did.
            decided = ({"reply": "I'm here. What do you need?", "action": "none", "run_id": None, "request": None}
                       if small_talk(text) and not ids else
                       {"reply": "", "action": "start", "run_id": None, "request": None})
            model = None
        if ids and decided["action"] in {"none", "cancel"}:
            decided = {**decided, "action": "start", "run_id": None}  # pictures always reach a job
        reply_row = None
        if decided["reply"]:
            reply_id = self._add("assistant", decided["reply"], self.clock(), model=model, reply_to=user_id)
            reply_row = reply_id
        started = None
        run_id = None
        action = decided["action"]
        try:
            if action == "start":
                # A voice memo's job gets the ask the coordinator wrote for it, like a typed message:
                # the memo itself stays in the thread. Handing the job the whole memo made it do the
                # part the reply had already handled (or asked about) and drop the ask it was for.
                # Without a written ask, the job hears the memo itself.
                words = decided.get("request") or job_text or text
                job_body = {"text": words, "source": "voice_message" if voice else "typed"}
                self._job_choice(job_body, body)
                if ids:
                    job_body["attachments"] = ids
                if client_id:
                    job_body["client_id"] = ("hj" + client_id)[:128]
                started = self.svc.start_job(job_body, device_id)
                run_id = started.get("run_id")
            elif action == "follow_up":
                run_id = decided["run_id"]
                msg = {"text": job_text or decided.get("request") or text,
                       "source": "voice_message" if voice else "typed"}
                if ids:
                    msg["attachments"] = ids
                if client_id:
                    msg["client_id"] = ("hf" + client_id)[:128]
                got = self.svc.task_message(run_id, msg, device_id)
                run_id = got.get("run_id") or run_id
                started = got
            elif action == "cancel":
                run_id = decided["run_id"]
                try:
                    self.svc.stop_task(run_id)
                except Exception as exc:  # already finished: the reply still stands
                    logger.info("speakeasy: Home's cancel found nothing running (%s)", type(exc).__name__)
        except Exception:
            # The job didn't go: say so in the thread instead of leaving a promise hanging.
            if reply_row:
                self._delete(reply_row)
            self._add("assistant", "That didn't go through. Try again in a moment.", self.clock(),
                      reply_to=user_id)
            self._link(user_id, None, "failed")
            self.svc._now_changed()
            raise
        self._link(user_id, run_id, action)
        for n, words in enumerate(decided.get("extra") or []):
            # "Those are two different tasks": each further ask in the message is its own job.
            job_body = {"text": words, "source": "voice_message" if voice else "typed"}
            self._job_choice(job_body, body)
            if client_id:
                job_body["client_id"] = (f"hx{n}" + client_id)[:128]
            try:
                more = self.svc.start_job(job_body, device_id)
            except Exception as exc:
                logger.info("speakeasy: Home's second job didn't start (%s)", type(exc).__name__)
                self._add("assistant", "One of those didn't go through. Ask me again for it.", self.clock(),
                          reply_to=user_id)
                continue
            if more.get("run_id"):
                self._extra_run(user_id, more["run_id"])
        if reply_row:
            self._link(reply_row, run_id, action)
        self.svc._now_changed()
        user = self._rows("WHERE id=?", (user_id,), limit=1)[0]
        reply = self._rows("WHERE id=?", (reply_row,), limit=1)[0] if reply_row else None
        logger.info("speakeasy: Home message handled (%s)", action)
        return self._reply(user, reply, started=started)

    def _job_choice(self, job_body: dict[str, Any], body: dict[str, Any]) -> None:
        """A job started from Home runs on the work model: what the message itself names, else the
        Home chip's pick, else nothing (the user's Hermes default). Never the coordinator's model."""
        for k in ("model", "reasoning", "agent"):
            if body.get(k) is not None:
                job_body[k] = body[k]
        if "model" not in job_body and "agent" not in job_body:
            picked = self.work_route()
            if picked.get("source") == "chosen":
                job_body["model"] = picked["model"]

    def _hermes_default(self) -> str | None:
        """The user's Hermes default model id, from the cached model list or config (no model call)."""
        catalog = getattr(self.svc, "catalog", None)
        raw = catalog.peek() if catalog is not None else None
        if raw is not None:
            try:
                got = catalog.hermes_default(raw)
            except Exception:
                got = None
            if got:
                return got
        main = self._config().get("model")
        if isinstance(main, dict) and main.get("default") and main.get("provider"):
            return f"{main['provider']}/{main['default']}"
        return None

    def work_route(self) -> dict[str, Any]:
        """The model Home's jobs run on: ``{"model": id | None, "source": "chosen" | "default"}``.
        ``default`` = the user's Hermes default model (nothing extra is sent; Hermes decides)."""
        entries = self._entries()
        chosen = self.store.get_meta(WORK_META) or ""
        if chosen and (entries is None or chosen in entries):
            return {"model": chosen, "source": "chosen"}
        return {"model": self._hermes_default(), "source": "default"}

    def _work_status(self, entries: dict[str, dict[str, Any]], row: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
        """The chip's half of ``GET /voice/home/model``: the work model and a few choices (the
        Hermes default first, then each connected provider's featured models, one row per model)."""
        picked = self.work_route()
        default = self._hermes_default()
        ids: list[str] = []
        for mid in (default, picked["model"] if picked["source"] == "chosen" else None):
            if mid and mid not in ids:
                ids.append(mid)
        catalog = getattr(self.svc, "catalog", None)
        raw = catalog.peek() if catalog is not None else None
        featured: list[str] = []
        for prow in (raw or {}).get("providers") or []:
            if isinstance(prow, dict):
                featured += [f"{prow.get('slug')}/{m}" for m in (prow.get("featured_models") or [])[:3]
                             if isinstance(m, str)]
        names = {row(m)["name"] for m in ids}
        for mid in featured + list(entries):
            if len(ids) >= WORK_OPTIONS:
                break
            if mid in entries and mid not in ids and row(mid)["name"] not in names:
                ids.append(mid)
                names.add(row(mid)["name"])
        return {"model": row(picked["model"]) if picked["model"] else None, "source": picked["source"],
                "default": row(default) if default else None, "options": [row(m) for m in ids]}

    def _delete(self, mid: str) -> None:
        with self.store._lock, self.store._db:
            self.store._db.execute("DELETE FROM home_messages WHERE id=?", (mid,))

    @staticmethod
    def _error(status: int, message: str) -> Exception:
        from .service import ServiceError
        return ServiceError(status, message)

    # -- context ---------------------------------------------------------------------------------
    def jobs(self) -> tuple[str, dict[str, str]]:
        """The jobs on Home as the coordinator's list (``j1`` newest), and alias -> run id."""
        try:
            items = self.svc.now_model.build(peek=True)["items"]
        except Exception:
            logger.debug("speakeasy: Home's jobs for the coordinator failed", exc_info=True)
            items = []
        lines: list[str] = []
        aliases: dict[str, str] = {}
        titles: dict[str, str] = {}
        now = self.clock()
        ordered = sorted((i for i in items if i.get("run_id")), key=lambda i: -float(i.get("started_at") or 0))
        for n, item in enumerate(ordered[:JOBS], 1):
            alias = f"j{n}"
            aliases[alias] = item["run_id"]
            titles[alias] = str(item.get("title") or "")
            state = {"working": "running", "waiting": "waiting on another job", "question": "waiting on the user",
                     "new_answer": "finished", "seen_answer": "finished", "stopped": "stopped"}.get(
                         str(item.get("kind")), str(item.get("kind")))
            age = max(0, int(now - float(item.get("started_at") or now)))
            when = f"{age // 60} min ago" if age >= 60 else "just now"
            line = f"- {alias}: {_clip(item.get('title'), 120)} ({state}, started {when})"
            if item.get("line"):
                line += f"\n  Now: {_clip(item['line'], 200)}"
            q = item.get("question") or {}
            if item.get("kind") == "question" and q.get("prompt"):
                line += f"\n  Asking the user: {_clip(q['prompt'], 300)}"
            if state == "finished":
                answer = self._answer(item["run_id"])
                if answer:
                    line += f"\n  Result: {answer}"
            lines.append(line)
        self._titles = titles
        return ("\n".join(lines) or "(none)"), aliases

    def _answer(self, run_id: str) -> str:
        try:
            work = self.store.work(run_id=run_id) or {}
        except Exception:
            return ""
        result = work.get("result") or {}
        text = result.get("full") or result.get("spoken") or result.get("headline") or work.get("summary") or ""
        return _clip(text, RESULT_CHARS)

    def _decide(self, text: str, history: list[dict[str, Any]], jobs: tuple[str, dict[str, str]] | str,
                attachments: int, focus: str | None = None) -> tuple[dict[str, Any] | None, dict | None]:
        job_lines, aliases = jobs if isinstance(jobs, tuple) else (jobs, {})
        name = (self.svc.settings.get() or {}).get("assistant_name") or "Hermes"
        note = (f"- The user attached {attachments} picture(s) or file(s) you can't see: use \"start\" (or "
                "\"follow_up\" for the job they're about) so a job looks at them.\n") if attachments else ""
        system = _PROMPT.format(name=name, now=time.strftime("%A %B %-d, %-I:%M %p", time.localtime(self.clock())),
                                jobs=job_lines, attachments=note, focus=self._focus_line(focus, aliases))
        messages: list[dict[str, str]] = [{"role": "system", "content": system}]
        for row in history:
            messages.append({"role": "user" if row["role"] == "user" else "assistant", "content": row["text"]})
        messages.append({"role": "user", "content": text or "(pictures, no words)"})
        try:
            raw, model = self._model_call(messages)
        except Exception as exc:
            logger.info("speakeasy: Home's coordinator didn't answer (%s)", type(exc).__name__)
            return None, None
        decided = parse(raw, aliases, self._titles)
        if decided is None:
            logger.info("speakeasy: Home's coordinator reply wasn't usable")
        return decided, model

    def _focus_line(self, focus: str | None, aliases: dict[str, str]) -> str:
        """The job the user has selected (Home's work strip, an iPad board): "this"/"it" means it,
        the same as a job on screen during a call."""
        if not focus:
            return ""
        for alias, run in aliases.items():
            if run == focus:
                title = _clip(self._titles.get(alias), 120)
                return (f"- The user has job {alias} ({title}) selected right now: \"this\", \"it\" and short "
                        "instructions are about that job unless they clearly start something new.\n")
        return ""

    def _model_call(self, messages: list[dict[str, str]]) -> tuple[Any, dict | None]:
        """One coordinator turn. Returns the answer (plain text, or ``{"tool", "args", "text"}`` for a
        tool call) and the model that wrote it, as the reply's attribution badge."""
        if self._call is not None:
            got = self._call(messages)
            if isinstance(got, tuple):
                return got[0], got[1]
            return got, None
        call_llm, extract = host_mod.aux_client()
        if extract is None:
            return None, None
        from .router import _profile_scope
        route = self.route()
        kwargs: dict[str, Any] = dict(messages=messages, temperature=0.3, max_tokens=MAX_TOKENS,
                                      timeout=TIMEOUT_S, tools=TOOLS)
        with _profile_scope():
            try:
                if route.get("task"):
                    response = call_llm(task=route["task"], **kwargs)
                else:
                    response = call_llm(provider=route["provider"], model=route["model"], **kwargs)
                    if route.get("source") == "chosen":
                        self._mark_failed(f"{route['provider']}/{route['model']}", failed=False)
            except Exception as exc:
                if route.get("task") == FALLBACK_TASK:
                    raise
                # The chosen/auto model didn't answer (offline, a provider this client can't reach):
                # the user's main model, then the routing model, still give the user a reply.
                logger.info("speakeasy: Home's coordinator model failed (%s); falling back",
                            type(exc).__name__)
                if route.get("model"):
                    self._mark_failed(f"{route['provider']}/{route['model']}")
                main = self._config().get("model")
                tried = (route.get("provider"), route.get("model"))
                response = None
                if (isinstance(main, dict) and main.get("default") and main.get("provider")
                        and (str(main["provider"]), str(main["default"])) != tried):
                    try:
                        route = {"provider": str(main["provider"]), "model": str(main["default"]), "source": "main"}
                        response = call_llm(provider=route["provider"], model=route["model"], **kwargs)
                    except Exception:
                        response = None
                if response is None:
                    route = {"task": FALLBACK_TASK}
                    response = call_llm(task=FALLBACK_TASK, **kwargs)
        return self._answer_of(response, extract), self._served(response, route)

    @staticmethod
    def _answer_of(response: Any, extract: Callable[[Any], str | None]) -> Any:
        try:
            message = response.choices[0].message
            calls = list(getattr(message, "tool_calls", None) or [])
        except Exception:
            message, calls = None, []
        text = (getattr(message, "content", None) or "") if message is not None else ""
        if calls:
            fn = getattr(calls[0], "function", None)
            raw = getattr(fn, "arguments", None) or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (ValueError, TypeError):
                args = {}
            more = []
            for extra in calls[1:1 + EXTRA_JOBS]:
                efn = getattr(extra, "function", None)
                eraw = getattr(efn, "arguments", None) or "{}"
                try:
                    eargs = json.loads(eraw) if isinstance(eraw, str) else dict(eraw)
                except (ValueError, TypeError):
                    continue
                if isinstance(eargs, dict):
                    more.append({"tool": getattr(efn, "name", ""), "args": eargs})
            return {"tool": getattr(fn, "name", ""), "args": args if isinstance(args, dict) else {},
                    "text": text.strip() if isinstance(text, str) else "", "more": more}
        return extract(response)

    def route(self) -> dict[str, Any]:
        """Which model answers (see the module notes): ``{"provider", "model", "source"}`` or
        ``{"task", "source"}``. Reads only Speakeasy's store, Hermes' config and the cached model
        list; it never calls a model or the network."""
        entries = self._entries()
        chosen = self.store.get_meta(CHOICE_META) or ""
        if chosen and (entries is None or chosen in entries):
            provider, _, model = chosen.partition("/")
            if provider and model:
                return {"provider": provider, "model": model, "source": "chosen"}
        cfg = self._config()
        aux = cfg.get("auxiliary") if isinstance(cfg.get("auxiliary"), dict) else {}
        block = aux.get(AUX_TASK)
        if isinstance(block, dict) and (str(block.get("provider") or "auto") != "auto" or block.get("model")):
            return {"task": AUX_TASK, "source": "configured"}
        auto = pick_auto({k: v for k, v in (entries or {}).items() if k not in self._failed()})
        if auto:
            provider, _, model = auto.partition("/")
            return {"provider": provider, "model": model, "source": "auto"}
        main = cfg.get("model")
        if isinstance(main, dict) and main.get("default") and main.get("provider"):
            return {"provider": str(main["provider"]), "model": str(main["default"]), "source": "main"}
        return {"task": FALLBACK_TASK, "source": "routing"}

    def _failed(self) -> dict[str, float]:
        """Models that failed to answer a real Home message lately (a sign-in Hermes lists but that
        doesn't work). Learned from real use only; nothing is ever test-called."""
        try:
            got = json.loads(self.store.get_meta(FAILED_META) or "{}")
        except ValueError:
            return {}
        now = self.clock()
        return {k: float(v) for k, v in got.items() if isinstance(v, (int, float)) and now - float(v) < FAILED_S}

    def _mark_failed(self, mid: str | None, failed: bool = True) -> None:
        if not mid:
            return
        got = self._failed()
        if failed:
            got[mid] = self.clock()
        elif mid not in got:
            return
        else:
            got.pop(mid)
        self.store.set_meta(FAILED_META, json.dumps(got))

    def _entries(self) -> dict[str, dict[str, Any]] | None:
        """The user's connected models from the cached Hermes list (None until it has been read; a
        background read is started then, so the next message can use Auto)."""
        catalog = getattr(self.svc, "catalog", None)
        if catalog is None:
            return None
        raw = catalog.peek()
        if raw is None:
            warm = getattr(self.svc, "_warm_catalog", None)
            if callable(warm):
                try:
                    warm()
                except Exception:
                    pass
            return None
        return catalog.entries(raw)

    def model_status(self) -> dict[str, Any]:
        """``GET /voice/home/model``: the model Home's chat uses, why, and the chip's choices (the
        approved coordinator models the user has, then their main model and the current pick)."""
        from . import models as MD
        catalog = getattr(self.svc, "catalog", None)
        if catalog is not None and catalog.peek() is None:
            try:   # the app asked: read Hermes' local model list now (a list, never a model call)
                catalog.raw()
            except Exception:
                pass
        route = self.route()
        entries = self._entries() or {}
        current = (f"{route['provider']}/{route['model']}" if route.get("model") else None)
        if current is None and route.get("task"):
            aux = (self._config().get("auxiliary") or {}).get(route["task"]) or {}
            if isinstance(aux, dict) and aux.get("model"):
                current = f"{aux.get('provider') or 'auto'}/{aux['model']}"
        options = approved(entries)[:16]
        main = self._config().get("model")
        if isinstance(main, dict) and main.get("default") and main.get("provider"):
            mid = f"{main['provider']}/{main['default']}"
            if mid in entries and mid not in options:
                options.append(mid)
        if current and current in entries and current not in options:
            options.insert(0, current)

        def row(mid: str) -> dict[str, Any]:
            e = entries.get(mid) or {}
            return {"id": mid, "name": e.get("name") or MD.friendly_name(mid.partition("/")[2] or mid),
                    "provider_name": e.get("provider_name") or mid.partition("/")[0]}
        failed = self._failed()
        auto = pick_auto({k: v for k, v in entries.items() if k not in failed})
        note = None
        if current and current in failed:
            note = f"{row(current)['name']} didn't answer last time, so another model replied."
        rows: list[dict[str, Any]] = []
        for mid in options:
            r = row(mid)
            if mid == current or r["name"] not in {x["name"] for x in rows}:   # one row per model, not per provider
                rows.append(r)
        rows = rows[:4] + [r for r in rows[4:] if r["id"] in {current, f"{(self._config().get('model') or {}).get('provider')}/{(self._config().get('model') or {}).get('default')}"}]
        coordinator = {"model": row(current) if current else None, "source": route.get("source"),
                       "auto": row(auto) if auto else None, "options": rows, "note": note}
        # Top level = the coordinator (kept for older apps); ``work`` = what Home's chip picks.
        return {**coordinator, "coordinator": dict(coordinator), "work": self._work_status(entries, row)}

    def choose_model(self, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /voice/home/model``: ``{"work_model": id | null}`` sets the model Home's jobs run on
        (null = the Hermes default); ``{"model": id | null}`` sets the coordinator's (null = Auto).
        One at a time; the other stays as it was."""
        if not isinstance(body, dict) or len(body) != 1 or set(body) - {"model", "work_model"}:
            raise self._error(400, "Send {\"work_model\": id or null} or {\"model\": id or null}.")
        work = "work_model" in body
        meta = WORK_META if work else CHOICE_META
        mid = body.get("work_model" if work else "model")
        if mid in (None, "", "auto", "default"):
            self.store.set_meta(meta, "")
            return self.model_status()
        if not isinstance(mid, str) or "/" not in mid or len(mid) > 200:
            raise self._error(400, "model must be a model id from the list.")
        entries = self._entries()
        if entries is None:
            catalog = getattr(self.svc, "catalog", None)
            entries = catalog.entries(catalog.raw()) if catalog is not None else {}
        if mid not in (entries or {}):
            raise self._error(400, "That model isn't one your Hermes has.")
        self.store.set_meta(meta, mid)
        return self.model_status()

    def task(self) -> str:
        """The aux task name in use (kept for older callers): the routing model's task unless the user
        configured ``speakeasy_home``."""
        return self.route().get("task") or AUX_TASK

    def _config(self) -> dict[str, Any]:
        try:
            host = getattr(self.svc, "host", None)
            cfg = host.config() if host is not None else {}
        except Exception:
            cfg = {}
        if not cfg:
            try:
                from hermes_cli.config import load_config_readonly  # type: ignore
                cfg = load_config_readonly() or {}
            except Exception:
                cfg = {}
        return cfg if isinstance(cfg, dict) else {}

    def _served(self, response: Any, route: dict[str, Any] | str) -> dict | None:
        """Which model wrote the reply, as the existing per-message attribution badge."""
        from . import reply_models as RM
        route = {"task": route} if isinstance(route, str) else route
        model = getattr(response, "model", None)
        if model is None and isinstance(response, dict):
            model = response.get("model")
        provider = route.get("provider")
        if route.get("task"):
            aux = self._config().get("auxiliary") or {}
            block = aux.get(route["task"]) if isinstance(aux, dict) else None
            block = block if isinstance(block, dict) else {}
            provider = block.get("provider")
            model = model or block.get("model")
        model = model or route.get("model")
        provider = provider if provider and provider != "auto" else None
        return RM.badge({"model": model, "provider": provider}, "message") if model else None
