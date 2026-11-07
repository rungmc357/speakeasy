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
keeps it fast. Its model: ``auxiliary.speakeasy_home`` when the user set one, else the user's main
model (their agent's own voice: fast routing models wrote noticeably worse chat and misrouted
requests in the real-model acceptance run), else the Speakeasy routing model. Live calls keep their
own routing; this is only Home's composer.
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
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_SMALL_TALK = re.compile(
    r"(?i)^\W*(?:yo+|hi+|hey+|hello|hiya|sup|what'?s up|good (?:morning|afternoon|evening|night)|"
    r"thanks?(?: you)?|thank you|thx|ty|ok(?:ay)?|cool|nice|great|perfect|got it|lol|haha|bye|"
    r"see you|cheers|👍|🙏)\W*$")


def small_talk(text: str) -> bool:
    return bool(_SMALL_TALK.match(text or ""))


_PROMPT = """You are {name}, the user's own AI assistant, chatting with them on the Home screen of the \
Speakeasy app. Home is one ongoing conversation. Behind you, jobs (full agent sessions with tools, the \
user's computer, files, accounts and the web) do real work.

Answer in plain text yourself for greetings, thanks, small talk, and anything you can answer from \
general knowledge, this conversation, or the jobs listed below (their status and results). Keep it \
conversational: usually one to three sentences, no headings, no lists unless asked.

Use a tool instead when the message needs doing or live or personal information: searching, booking, \
buying, files, code, email, calendar, messages, the user's machines or accounts, weather, prices, news, \
or anything about the user you don't already know.
- start_job for new work. "request" is the task in self-contained words (resolve "it", "that", "the one \
you mentioned" from the conversation); "reply" is one short line in your own words about what you're \
doing ("Looking for a quiet cafe near the river."). Never "Starting task".
- message_job when the message changes, corrects, answers or adds to an existing job, or asks it to do \
more with its result ("make it shorter", "actually Friday", "also check the price"). "job" is its id.
- cancel_job to stop a running job. "job" is its id.

Rules:
- Never repeat or paraphrase the user's message back to them. Never narrate routing ("I'll send this to \
a job", "a task will handle it"): just say what you're doing.
- Never say you can't access something live or can't do something: start a job, which can.
- Never invent facts about the user, their files, accounts or anything live. If you'd have to guess, \
start a job instead.
- Name a job by what it is about, never by its id. When the user refers to a job ("that", "the cafe \
one"), pick it from the list; if unclear, the newest one the conversation was about.
{attachments}
Now: {now}.
Jobs on Home (newest first):
{jobs}"""

_REPLY = {"type": "string", "description": "One short line to the user, in your own words, about what you're doing."}
_JOB = {"type": "string", "description": "The job's id from the list (j1, j2, ...)."}
TOOLS = [
    {"type": "function", "function": {
        "name": "start_job",
        "description": "Start a new job: real work, the user's computer or accounts, or live information.",
        "parameters": {"type": "object", "properties": {
            "request": {"type": "string", "description": "The task in self-contained words."},
            "reply": _REPLY}, "required": ["request", "reply"]}}},
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
            return {"reply": text[:REPLY_CHARS], "action": "none", "run_id": None, "request": None}
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
    return {"reply": reply[:REPLY_CHARS], "action": action, "run_id": run_id, "request": request}


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
                "text", "client_id", "attachments", "model", "reasoning", "agent", "source"}:
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
        decided, model = self._decide(text, history, (jobs, aliases), len(ids))
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
                words = job_text or (decided.get("request") if not voice else None) or text
                job_body = {"text": words, "source": "voice_message" if voice else "typed"}
                for k in ("model", "reasoning", "agent"):
                    if body.get(k) is not None:
                        job_body[k] = body[k]
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
        if reply_row:
            self._link(reply_row, run_id, action)
        self.svc._now_changed()
        user = self._rows("WHERE id=?", (user_id,), limit=1)[0]
        reply = self._rows("WHERE id=?", (reply_row,), limit=1)[0] if reply_row else None
        logger.info("speakeasy: Home message handled (%s)", action)
        return self._reply(user, reply, started=started)

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
                attachments: int) -> tuple[dict[str, Any] | None, dict | None]:
        job_lines, aliases = jobs if isinstance(jobs, tuple) else (jobs, {})
        name = (self.svc.settings.get() or {}).get("assistant_name") or "Hermes"
        note = (f"- The user attached {attachments} picture(s) or file(s) you can't see: use \"start\" (or "
                "\"follow_up\" for the job they're about) so a job looks at them.\n") if attachments else ""
        system = _PROMPT.format(name=name, now=time.strftime("%A %B %-d, %-I:%M %p", time.localtime(self.clock())),
                                jobs=job_lines, attachments=note)
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
            except Exception as exc:
                if route.get("task") == FALLBACK_TASK:
                    raise
                # The main model didn't answer (offline, a provider this client can't reach): the routing
                # model still gives the user a reply.
                logger.info("speakeasy: Home's coordinator model failed (%s); using the routing model",
                            type(exc).__name__)
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
            return {"tool": getattr(fn, "name", ""), "args": args if isinstance(args, dict) else {},
                    "text": text.strip() if isinstance(text, str) else ""}
        return extract(response)

    def route(self) -> dict[str, Any]:
        """Which model answers: ``{"task": "speakeasy_home"}`` when the user configured it, else the
        main model ``{"provider", "model"}``, else the routing model's task."""
        cfg = self._config()
        aux = cfg.get("auxiliary") if isinstance(cfg.get("auxiliary"), dict) else {}
        block = aux.get(AUX_TASK)
        if isinstance(block, dict) and (str(block.get("provider") or "auto") != "auto" or block.get("model")):
            return {"task": AUX_TASK}
        main = cfg.get("model")
        if isinstance(main, dict) and main.get("default") and main.get("provider"):
            return {"provider": str(main["provider"]), "model": str(main["default"])}
        return {"task": FALLBACK_TASK}

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
