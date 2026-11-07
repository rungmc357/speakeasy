"""Home is a chat (SPK-208): one ongoing conversation with a coordinator.

Typing on Home used to start a job for every message, so "yo" became a task card. Now each message
goes to the coordinator first: one fast model call (Hermes' auxiliary client, task
``speakeasy_home``) that sees the recent Home conversation and the jobs on Home, and answers with
strict JSON::

    {"reply": "...", "action": "none" | "start" | "follow_up" | "cancel", "job": "j2" | null,
     "request": "self-contained words for the job" | null}

- ``none``: chat. The reply is the whole answer ("yo", "thanks", a question it can answer from the
  conversation or a job's result). No job, no card.
- ``start``: real work. A short reply, then a job through the same machinery as ``POST /voice/work``;
  its card appears in the thread right after the reply and updates in place.
- ``follow_up``: words for an existing job (steer while it runs, the answer while it asks, a
  follow-up once it finished), through the job's own message route.
- ``cancel``: stop a running job.

The coordinator is not a Hermes session: it has no tools and never does work itself, which keeps
it fast. Its model: ``auxiliary.speakeasy_home`` when the user set one, else the Speakeasy routing
model (``auxiliary.speakeasy_router``, already picked to be fast), else Hermes' own auxiliary
choice. Live calls keep their own routing; this is only Home's composer.
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
Speakeasy app. Home is one ongoing conversation. Behind you, separate jobs (full agent sessions with \
tools, the user's computer, files, accounts and the web) do real work; you decide when to use them.

Reply with JSON only, no other text:
{{"reply": "...", "action": "none|start|follow_up|cancel", "job": "<job id or null>", "request": "<words for the job or null>"}}

Choose the action:
- "none": greetings, thanks, small talk, and questions you can answer from general knowledge, from this \
conversation, or from the jobs listed below (their status and results). The reply is the whole answer.
- "start": anything that needs doing or live information: searching, booking, buying, writing or \
changing files, code, email, calendar, messages, the user's machines or accounts, weather, prices, news, \
or anything about the user you don't already know. "request" is the task in self-contained words (resolve \
"it", "that", "the one you mentioned" from the conversation). The reply is one short line in your own \
words about what you'll do ("Looking for a quiet cafe near the river."). Never say "Starting task".
- "follow_up": the message changes, adds to, corrects, or answers an existing job, or asks that job to \
do more ("make it shorter", "actually Friday", "also check the price"). "job" is its id; "request" is \
the new instruction in self-contained words. Reply in one short line.
- "cancel": the user wants a running job stopped. "job" is its id. Reply in one short line.

Rules:
- Plain conversational text, usually one or two sentences. No headings, no lists unless asked.
- Never repeat or paraphrase the user's message back to them. Never narrate routing ("I'll send this \
to a job", "a task will handle it"): just say what you're doing.
- Never invent facts about the user, their files, accounts or anything live. If you'd have to guess, \
start a job instead.
- To answer "what did it find?" / "what's going on?", use the jobs below. Name a job by what it is about, \
never by its id.
- When the user refers to a job ("that", "the cafe one"), pick it from the list; if unclear, use the \
newest one the conversation was about.
{attachments}
Now: {now}.
Jobs on Home (newest first):
{jobs}"""


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def parse(raw: Any, aliases: dict[str, str]) -> dict[str, Any] | None:
    """The coordinator's JSON, checked: a known action, a known job for follow_up/cancel, a reply.
    None when it isn't usable (the caller falls back)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    action = str(data.get("action") or "none").strip().lower()
    if action not in ACTIONS:
        return None
    reply = data.get("reply")
    reply = reply.strip() if isinstance(reply, str) else ""
    job = data.get("job")
    job = job.strip() if isinstance(job, str) else None
    run_id = aliases.get(job or "")
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
        now = self.clock()
        ordered = sorted((i for i in items if i.get("run_id")), key=lambda i: -float(i.get("started_at") or 0))
        for n, item in enumerate(ordered[:JOBS], 1):
            alias = f"j{n}"
            aliases[alias] = item["run_id"]
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
        decided = parse(raw, aliases)
        if decided is None:
            logger.info("speakeasy: Home's coordinator reply wasn't usable")
        return decided, model

    def _model_call(self, messages: list[dict[str, str]]) -> tuple[str | None, dict | None]:
        if self._call is not None:
            got = self._call(messages)
            return (got if isinstance(got, str) or got is None else got[0]), (
                None if isinstance(got, str) or got is None else got[1])
        call_llm, extract = host_mod.aux_client()
        if extract is None:
            return None, None
        task = self.task()
        from .router import _profile_scope
        with _profile_scope():
            response = call_llm(task=task, messages=messages, temperature=0.3, max_tokens=MAX_TOKENS,
                                timeout=TIMEOUT_S)
        return extract(response), self._served(response, task)

    def task(self) -> str:
        """``speakeasy_home`` when the user configured it, else the routing model's task when that is
        set (the fast model they already picked for Speakeasy), else ``speakeasy_home`` (Hermes' auto)."""
        cfg = self._config()
        aux = cfg.get("auxiliary") if isinstance(cfg.get("auxiliary"), dict) else {}

        def configured(name: str) -> bool:
            block = aux.get(name)
            return isinstance(block, dict) and (str(block.get("provider") or "auto") != "auto" or bool(block.get("model")))
        if configured(AUX_TASK):
            return AUX_TASK
        if configured(FALLBACK_TASK):
            return FALLBACK_TASK
        return AUX_TASK

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

    def _served(self, response: Any, task: str) -> dict | None:
        """Which model wrote the reply, as the existing per-message attribution badge."""
        from . import reply_models as RM
        model = getattr(response, "model", None)
        if model is None and isinstance(response, dict):
            model = response.get("model")
        aux = self._config().get("auxiliary") or {}
        block = aux.get(task) if isinstance(aux, dict) else None
        block = block if isinstance(block, dict) else {}
        provider = block.get("provider")
        provider = provider if provider and provider != "auto" else None
        model = model or block.get("model")
        return RM.badge({"model": model, "provider": provider}, "message") if model else None
