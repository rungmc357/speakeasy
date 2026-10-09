"""One voice call: its live state, the SSE event feed, chat notices, and the sideband worker that
turns the voice model's handoffs into Hermes tasks and speaks their results back.

Provider-neutral: ``openai_live.OpenAISidebandWorker`` and ``codex_transport.CodexSidebandWorker``
only implement ``run()`` (read provider events) and ``_send()`` (write context to the call).
"""
from __future__ import annotations

import asyncio
import re
import dataclasses
import functools
import hashlib
import json
import logging
import queue
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from . import channels, continuity, failures, router
from . import home_control as home_control_mod
from . import instant as instant_mod
from . import jev as jev_mod
from . import quick as quick_mod
from . import views as views_mod
from .settings import valid_delivery_target
from .prompt import builder as P
from . import text as text_mod
from .text import (ID_RE, MAX_TRANSCRIPT, TERMINAL, clean_transcript, delivery_text, derive_tool_status,
                   interim_progress, live_images_in, notice_text, safe_user_text, short_title, split_result,
                   thread_commentary, vetted_live_ref)

logger = logging.getLogger(__name__)


# A greeting or a bare filler sound on its own is never work ("hey", "yo there", "um").
GREETING_RE = re.compile(
    r"(?i)^\s*(?:(?:hey|hi|hello|yo|sup|hiya|howdy|morning|evening|um+|uh+|hmm+|so|okay so|alright so)"
    r"(?:\s+(?:there|again|man|buddy|dude|you|yo))?"
    r"|what'?s up|whats up|wassup|how'?s it going|you there|are you there)\s*[.!?,]*\s*$")
SETTLE_S = 1.5         # wait this long after a handoff for the user to keep going
SETTLE_MAX_S = 6.0     # and never longer than this in total
ABSORB_WINDOW_S = 15.0 # a handoff of words already folded into an earlier task is dropped


def _same_words(fragment: str, request: str) -> bool:
    """The transcript of words already in the request (it often lands just after the handoff)."""
    f = _words(fragment)
    return not f or f in _words(request)


def _words(text: str) -> str:
    """Just the words, lowercased: to tell whether the same thing was said again."""
    return " ".join(re.findall(r"[a-z0-9']+", (text or "").lower()))


def is_greeting(request: str) -> bool:
    return bool(GREETING_RE.match(request or ""))


def whole_request(context: str) -> str:
    """The user's last request, whole. Speech often lands as two transcript lines with nothing said
    in between ("Bedroom lamps at forty percent" / "purple"); handing on only the last scrap lost the
    rest. Consecutive user lines at the end are joined when the final one is short."""
    lines = context.splitlines()
    picked: list[str] = []
    for line in reversed(lines):
        if line.startswith("User: "):
            picked.append(line[6:].strip())
            if len(picked) >= 3 or len(picked[0].split()) > 4:
                break
            continue
        if line.startswith("Assistant: ") or picked:
            break
    if not picked:
        return ""
    if len(picked[0].split()) > 4:
        return picked[0]
    return " ".join(reversed(picked)).strip()

TASK_SESSION_PREFIX = "speakeasy_task_"
LOCAL_RUN_PREFIX = "lr_"   # ids for tasks Hermes ran without a run id (thread tasks)
EARLY_PREFIX = "early_"    # handoffs for words said while the call was still connecting
EARLY_MAX_CHARS = 1000
MAX_TASKS = 8
ACTIVE_RUN_STATES = {"admitting", "running", "working", "waiting_for_approval", "resolving_approval",
                     "cancel_requested"}
CONTINUITY_WAIT_S = 600
THREAD_OPEN_WAIT_S = 20
# Thread tasks whose answer this process is waiting for right now, with when the wait began
# (monotonic). The recovery sweep skips them; a wait orphaned by a hang-up ages out.
LIVE_THREAD_WAITS: dict[str, float] = {}
CONTINUITY_POLL_S = 5
# Spoken progress: only for tasks running this long, at most once per PROGRESS_EVERY_S across the
# whole call (two tasks don't take turns talking), and only when the task has done something new
# since the last update. No forced silence: it's a conversation, just without filler updates.
PROGRESS_AFTER_S = 45.0
PROGRESS_EVERY_S = 45.0
ACTIVITY_KEEP = 10
# Silent status notes to the voice model (so it can answer "how's it going?"): at most this often.
# Every note used to go straight in, and the voice answered each one out loud ("Still on it")
# every few seconds on a long task.
STATUS_NOTE_EVERY_S = 60.0


class ServiceError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclasses.dataclass
class Runtime:
    """Everything a worker needs besides the call itself (built once by the server)."""
    store: Any
    hermes: Any
    settings: Callable[[], dict[str, Any]]
    hermes_home: Path
    image_roots: Callable[[], tuple[Path, ...]]
    notices: "Notices"
    hermes_key: Callable[[], str] = lambda: ""
    # Routing: the model call (None = Hermes' auxiliary client, task speakeasy_router) and the
    # new-thread runner (None = channels only get results posted, never a thread).
    route_call: Callable[[list[dict[str, str]]], str | None] | None = None
    threads: Any = None
    # Names a task by what it's about (None = Hermes' session titler; returns None when unavailable).
    title_call: Callable[[str], str | None] | None = None
    # The spoken request as clean written text (None = the title model; returns None when unavailable).
    polish_call: Callable[[str], str | None] | None = None
    status_call: Callable[[str], str | None] | None = None
    progress_call: Callable[..., str | None] | None = None
    # Instant home control (home_control.HomeControl); None or turned off = every request goes to Hermes.
    home: Any = None
    # Quick answers (quick.answer); None = the real one. Tests swap in a fake.
    quick_call: Callable[[str, str], str | None] | None = None
    # The local log of ended calls that "Tune from my calls" learns from (tune.CallLog); None = not kept.
    call_log: Any = None

    @property
    def names(self) -> P.Names:
        return P.Names.from_settings(self.settings())

    @property
    def state_db(self) -> Path:
        return self.hermes_home / "state.db"

    def routable(self) -> dict[str, Any]:
        """Settings minus any topic channel another Hermes profile answers (see
        delivery.other_profile_chats): a task sent there would finish where this profile can't see."""
        from .delivery import in_other_profile, other_profile_chats
        settings = self.settings()
        foreign = other_profile_chats(self.hermes_home)
        if not foreign:
            return settings
        delivery = dict(settings["delivery"])
        delivery["channels"] = [ch for ch in delivery.get("channels") or []
                                if not in_other_profile(ch.get("target", ""), foreign)]
        return {**settings, "delivery": delivery}

    def delivery_label(self) -> str:
        from .delivery import target_label
        return target_label(self.settings()["delivery"]["target"])

    def channel_label(self, target: str) -> str:
        found = next((c for c in channels.opted_in(self.routable()) if c.target == target), None)
        return found.label if found else self.delivery_label()

    def route(self, request: str, tasks: list[router.OpenTask], marked: Any,
              chats: list[router.Chat] | None = None, call_so_far: str = "",
              replied_task_id: str | None = None) -> router.Decision:
        settings = self.routable()
        topics = ([router.Topic(c.label, c.topic) for c in channels.opted_in(settings)]
                  if channels.mode(settings) == "topic" else [])
        provider = (self.settings().get("fast_routing") or {}).get("jev") or ""
        place = None
        if provider:
            from . import jev
            place = functools.partial(jev.placement, self.hermes_home, provider)
        return router.decide(request, tasks, marked, topics, self.route_call, chats=chats, call_so_far=call_so_far,
                             replied_task_id=replied_task_id, jev_place=place)

    def explicit_channel(self, request: str) -> channels.Choice | None:
        return channels.explicit(request, self.routable(), P.clarify_channel)

    def topical_channel(self, picked: str | None) -> channels.Choice:
        return channels.resolve(self.routable(), self.delivery_label(), picked)


# -- live event feed ------------------------------------------------------------------------

class EventFeed:
    """Bounded per-interaction event ring feeding the live SSE stream."""
    RING = 200

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.publish_lock = threading.Lock()
        self.ring: deque[tuple[int, str, Any]] = deque(maxlen=self.RING)
        self.seq = 0
        self.last: dict[str, Any] = {"interaction": None, "work": None, "approval": None, "tasks": None,
                                     "email_drafts": None}
        self.closed_id: int | None = None

    def publish(self, kind: str, payload: Any) -> None:
        with self.cond:
            if kind in self.last and self.last[kind] == payload:
                return  # dedupe identical consecutive payloads per type
            if kind == "closed" and self.closed_id is not None:
                return
            self.seq += 1
            self.last[kind] = payload
            self.ring.append((self.seq, kind, payload))
            if kind == "closed":
                self.closed_id = self.seq
            self.cond.notify_all()

    def since(self, last_id: int) -> list[tuple[int, str, Any]] | None:
        """Events after last_id, or None when the gap is outside the retained window."""
        if last_id == self.seq:
            return []
        if last_id > self.seq or not self.ring or self.ring[0][0] > last_id + 1:
            return None
        return [event for event in self.ring if event[0] > last_id]


# -- call state -------------------------------------------------------------------------------

@dataclasses.dataclass
class BackendRun:
    delegation_id: str
    revision: int
    idem_key: str
    run_id: str | None = None
    status: str = "admitting"
    approval: dict[str, Any] | None = None
    error: str | None = None
    # A part split from one spoken request, or a follow-up run: the voice delegation it answers.
    voice_id: str | None = None
    # Where the finished answer is posted when it is not the default target (a routed channel).
    deliver_to: str | None = None
    started: float = dataclasses.field(default_factory=time.monotonic)
    spoken_at: float = 0.0          # last spoken progress line for this task (monotonic)
    noted_at: float = 0.0           # last silent status note to the voice model (monotonic)
    activity: list[str] = dataclasses.field(default_factory=list)  # recent concrete steps, newest last
    activity_count: int = 0         # steps seen so far
    told_count: int = 0             # steps already covered by a spoken update
    told: list[str] = dataclasses.field(default_factory=list)      # updates already spoken

    @property
    def say_id(self) -> str:
        return self.voice_id or self.delegation_id


@dataclasses.dataclass
class Interaction:
    interaction_id: str
    live_session_id: str
    talking_only: bool = False  # "just talk to me": hold work until they ask for some
    status: str = "connecting"
    revision: int = 0
    finalization: str = "open"
    error: str | None = None
    runs: dict[str, BackendRun] = dataclasses.field(default_factory=dict)
    latest_delegation_id: str | None = None
    away: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    call_closed: bool = False
    progress_spoken_at: float = 0.0  # last spoken progress update on this call, any task (monotonic)
    paused: bool = False
    pause_bookkept: bool = False
    history: list[dict[str, str]] = dataclasses.field(default_factory=list)
    resumed_from: str | None = None
    device_id: str = ""
    # Listening mode: what the Mac heard in the room before this call (cleaned "[HH:MM] text" lines).
    # Memory only, never in history, logs or reprs; it moves to a resumed call and is cleared when
    # the call ends (or a pause runs out). Empty = an ordinary call.
    room: str = dataclasses.field(default="", repr=False)
    # Listening mode: the voice already responded from the room on this call (any leg of it).
    room_nudged: bool = False
    last_activity: float = dataclasses.field(default_factory=time.monotonic)
    successor: "Interaction | None" = dataclasses.field(default=None, repr=False)
    worker: Any = dataclasses.field(default=None, repr=False)
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)
    feed: EventFeed = dataclasses.field(default_factory=EventFeed, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            latest = self.runs.get(self.latest_delegation_id or "")
            approval = None
            pending = next((run for run in reversed(list(self.runs.values())) if run.approval), None)
            if pending and pending.approval:
                approval = dict(pending.approval)
                approval["run_id"] = pending.run_id
                approval["delegation_id"] = pending.delegation_id
            return {
                "interaction_id": self.interaction_id,
                "status": latest.status if latest else self.status,
                "run_id": latest.run_id if latest else None,
                "backend_run_id": latest.run_id if latest else None,
                "idem_key": latest.idem_key if latest else None,
                "delegation_id": latest.delegation_id if latest else None,
                "revision": self.revision,
                "approval": approval,
                "finalization": self.finalization,
                "error": (latest.error if latest else None) or self.error,
                "paused": self.paused,
                "resumed_from": self.resumed_from,
            }

    def head(self) -> "Interaction":
        """The newest call in this Pause/Resume chain (results are routed there)."""
        current = self
        while current.successor is not None:
            current = current.successor
        return current


def interaction_tasks(store: Any, interaction: Interaction, assistant_name: str = "Hermes") -> list[dict[str, Any]]:
    """Every voice task of this call (parallel runs, oldest first), as work objects + task_id."""
    with interaction.lock:
        runs = [(r.delegation_id, r.idem_key, r.run_id, r.status) for r in interaction.runs.values()]
    tasks = []
    for delegation_id, key, run_id, status in runs[-MAX_TASKS:]:
        if status == "rejected":
            continue
        work = store.work(idem_key=key, assistant_name=assistant_name)
        if work is not None and work.get("dismissed"):
            continue
        if work is None:
            work = {"run_id": run_id, "source_run_id": run_id, "status": status, "stale": False,
                    "updated": None, "events": [], "short_status": None, "detail": None,
                    "updated_at": None, "status_source": None, "result": None, "email_drafts": []}
        work["task_id"] = delegation_id
        tasks.append(work)
    # A draft from an earlier call still waiting for Send / Deny / Revise stays on screen.
    return tasks + store.carried_draft_tasks({t["task_id"] for t in tasks} | {r[1] for r in runs})


def publish_state(store: Any, interaction: Interaction, assistant_name: str = "Hermes") -> None:
    """Recompute interaction/work/approval/tasks/drafts and push changes to the live event feed.

    Never call while holding interaction.lock. Unchanged payloads are deduplicated.
    """
    feed = interaction.feed
    with feed.publish_lock:
        if feed.closed_id is not None:
            return
        snap = interaction.snapshot()
        with interaction.lock:
            active = any(run.status in ACTIVE_RUN_STATES for run in interaction.runs.values())
        # Thread tasks have no Hermes run id; find the row by its own key so the call's status line
        # shows the task's name and status instead of a bare wait.
        work = (store.work(snap["run_id"], assistant_name=assistant_name) if snap["run_id"] else
                store.work(idem_key=snap["idem_key"], assistant_name=assistant_name) if snap.get("idem_key") else None)
        pending = snap.get("approval")
        approval = None
        if pending:
            approval = {"run_id": pending.get("run_id"), "request_id": pending.get("request_id"),
                        "description": pending.get("description"), "choices": ["once", "deny"]}
        tasks = interaction_tasks(store, interaction, assistant_name)
        drafts = [dict(d, task_id=t["task_id"], run_id=t.get("run_id")) for t in tasks for d in t.get("email_drafts") or []]
        feed.publish("interaction", snap)
        feed.publish("work", work)
        feed.publish("tasks", tasks)
        feed.publish("approval", approval)
        feed.publish("email_drafts", drafts)
        if snap["finalization"] in {"confirmed", "incomplete"} and not active:
            feed.publish("closed", {
                "finalization": "complete" if snap["finalization"] == "confirmed" else "incomplete",
                "error": snap.get("error"),
            })


# -- chat notices (delivery target) ---------------------------------------------------------------

class Notices:
    """Deduplicated, fire-and-forget chat notices to the delivery target, from one background thread.

    `post` only claims a SQLite dedupe key and enqueues; it never blocks on delivery and never raises
    into call handling. With delivery.target = none nothing is posted.
    """

    def __init__(self, store: Any, notifier: Any | None, target: Callable[[], str], names: Callable[[], P.Names]):
        self.store, self.notifier, self.target, self.names = store, notifier, target, names
        self.queue: "queue.Queue[tuple[str, str, str]]" = queue.Queue()
        if notifier is not None:
            threading.Thread(target=self._worker, daemon=True, name="speakeasy-notices").start()

    def post(self, dedupe_key: str, text: str, limit: int = 600, target: str | None = None) -> bool:
        target = target or self.target()
        if self.notifier is None or not target or target == "none" or not text.strip():
            return False
        try:
            if not self.store.claim_notice(dedupe_key):
                return False
            self.queue.put((dedupe_key, target, text[:limit]))
            return True
        except Exception as exc:  # never break the voice path
            logger.warning("speakeasy: notice enqueue failed: %s", type(exc).__name__)
            return False

    def _worker(self) -> None:
        while True:
            key, target, text = self.queue.get()
            try:
                outcome = "sent" if self.notifier.send(target, text) else "failed"
            except Exception as exc:
                outcome = f"failed: {type(exc).__name__}"
            try:
                self.store.notice_outcome(key, outcome)
            except Exception:
                pass
            self.queue.task_done()

    def still_working(self, run_id: str, request: str | None, short_status: str | None) -> None:
        self.post(f"still:{run_id}", P.still_working_notice(request, short_status))

    def needs_you(self, request_id: str, summary: str | None) -> None:
        self.post(f"approval:{request_id}", P.needs_you_notice(self.names(), summary))

    def pointer(self, run_id: str, title: str | None, where: str | None) -> None:
        """One line in the voice channel for work whose answer landed somewhere else (another channel,
        a thread, an older conversation), so the voice channel is a complete log of every voice task."""
        home = self.target()
        if not where or not home or home == "none" or where == home:
            return
        self.post(f"pointer:{run_id}", P.pointer_notice(title, where), limit=400, target=home)

    def answered(self, run_id: str, full: str | None, target: str | None = None) -> None:
        """Post a finished answer in full, once per run (`hermes send` splits long text), to the
        task's routed channel when it has one, else the default target."""
        from .text import split_commands
        text = (full or "").strip()
        if not text:
            return
        pieces = split_commands(text)
        if not any(kind == "command" for kind, _ in pieces):
            self.post(f"answer:{run_id}", text, limit=20000, target=target)
            return
        # Each command goes out alone, bare, so long-press → Copy takes just the command.
        for i, (_, chunk) in enumerate(pieces):
            self.post(f"answer:{run_id}" if i == 0 else f"answer:{run_id}:{i}", chunk, limit=20000, target=target)

    def commands(self, run_id: str, full: str | None, target: str | None) -> None:
        """Thread replies are Hermes's own message: we can't reformat them, so any command in them is
        posted again underneath, alone, so it copies cleanly (only when the reply didn't fence it)."""
        from .text import split_commands
        text = full or ""
        for i, (kind, chunk) in enumerate(split_commands(text)):
            if kind == "command" and f"```" not in text:
                self.post(f"command:{run_id}:{i}", chunk, limit=4000, target=target)

    def stopped(self, run_id: str, status: str, request: str | None, why: str | None = None) -> None:
        self.post(f"stopped:{run_id}", P.stopped_notice(self.names(), status, request, why))

    def draft_waiting(self, draft_id: str, subject: str | None) -> None:
        self.post(f"draft:{draft_id}", P.draft_notice(subject))


# -- the sideband worker ------------------------------------------------------------------------

class SidebandWorker:
    """Base worker. Subclasses read provider events in ``run()`` and write in ``_send()``."""

    def __init__(self, rt: Runtime, interaction: Interaction):
        self.rt, self.store, self.hermes, self.interaction = rt, rt.store, rt.hermes, interaction
        self.notices = rt.notices
        self.fragments: deque[dict[str, Any]] = deque(maxlen=512)
        self.absorbed: list[tuple[str, float]] = []  # words folded into an earlier task by settle()
        self.handoff_at: dict[str, float] = {}   # delegation id -> when the handoff arrived (monotonic)
        self.show_pending: set[str] = set()      # task keys asked for a screenshot by "show me"
        # Home control: the question just asked (original request, question, when) and answers given
        # on this call, so "the thermostat" means the den until hang-up once they've said so.
        self.home_question: tuple[str, str, float] | None = None
        self.home_notes: list[str] = []
        self.home_tasks: set[str] = set()        # task ids that were instant home commands
        self.replied_task: str | None = None     # the task the voice last spoke an answer from
        self.wants_show: set[str] = set()        # delegation ids the user wants to SEE (routing said show)
        self.show_seq = 0
        self.route_timings: dict[str, int] = {}  # task id -> routing latency (ms), stored with the task
        self.delegations: set[str] = set()
        self.dispatch_tasks: set[asyncio.Task[Any]] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.connected = False
        # When this call's voice session came up (monotonic): the worker is made right after the
        # provider admitted the session, and the provider's worker updates it once its channel attaches.
        self.connected_at = time.monotonic()
        # Listening mode (a call with room text): room questions the voice was told to answer itself
        # (normalized words), so the same question handed off again goes on to Hermes; and the room
        # text each handoff was made with, so work asked for just before hang-up still gets it after
        # the call drops the room (removed when that handoff's work ends). Whether the voice already
        # responded from the room lives on the Interaction, so a resumed call never does it twice.
        self.room_answered: set[str] = set()
        self.handoff_rooms: dict[str, str] = {}
        interaction.worker = self

    # -- plumbing -------------------------------------------------------------------------
    @property
    def names(self) -> P.Names:
        return self.rt.names

    def name_task(self, idem: str, request: str | None) -> None:
        """Show the instant word-based label now, then swap in a proper name once Hermes' titler
        answers, unless something better (a thread's name) has replaced the label meanwhile."""
        provisional = short_title(request)
        self.store.set_title(idem, provisional)
        if not request or not provisional:
            return
        titler = self.rt.title_call or router.smart_title
        polisher = self.rt.polish_call or router.polish_request
        statuser = self.rt.status_call or router.working_status

        async def upgrade() -> None:
            title, polished, status = await asyncio.gather(asyncio.to_thread(titler, request),
                                                           asyncio.to_thread(polisher, request),
                                                           asyncio.to_thread(statuser, request))
            changed = False
            if status:
                handed = polished or request
                changed = self.store.handoff_status(
                    idem, status, f"Handed to {self.names.assistant_name}: {handed}") or changed
            if title and title != provisional and self.store.title(idem) == provisional:
                self.store.set_title(idem, title)
                changed = True
            if polished:
                self.store.set_title(idem, None, summary=polished)
                changed = True
            if changed:
                self.publish()

        task = asyncio.get_running_loop().create_task(upgrade())
        self.dispatch_tasks.add(task)
        task.add_done_callback(self.dispatch_tasks.discard)

    def publish(self) -> None:
        name = self.names.assistant_name
        publish_state(self.store, self.interaction, name)
        head = self.interaction.head()
        if head is not self.interaction:
            publish_state(self.store, head, name)

    async def _publish_async(self) -> None:
        self.publish()

    def start(self) -> None:
        threading.Thread(target=lambda: asyncio.run(self.run()), daemon=True,
                         name=f"speakeasy-call-{self.interaction.interaction_id[:12]}").start()

    async def run(self) -> None:  # pragma: no cover - provider-specific
        raise NotImplementedError

    async def _send(self, kind: str, delegation_id: str | None, content: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def stop_transport(self) -> None:
        """Close the provider session (idle pause, end). Provider-specific; default no-op."""

    def call_connected(self) -> bool:
        return self.connected and not self.interaction.call_closed

    def turns(self) -> list[dict[str, str]]:
        """This call's whole transcript as cleaned speaker turns."""
        out: list[dict[str, str]] = []
        for fragment in self.fragments:
            role = fragment["speaker"]
            if out and out[-1]["role"] == role:
                out[-1]["text"] += fragment["text"]
            else:
                out.append({"role": role, "text": fragment["text"]})
        return [{"role": t["role"], "text": clean_transcript(t["text"]).strip()}
                for t in out if clean_transcript(t["text"]).strip()]

    def call_closed(self) -> None:
        """Once per call: persist the end time, keep recent turns, notify about work in flight.

        A paused call keeps its transcript (and listening mode's room text) for Resume and defers the
        "Still working" notices. An ended call drops its room text: it never outlives the call.
        """
        with self.interaction.lock:
            if self.interaction.call_closed:
                return
            self.interaction.call_closed = True
            self.interaction.history = (self.interaction.history + self.turns())[-400:]
            paused = self.interaction.paused
            history = list(self.interaction.history)
            if not paused:
                self.interaction.room = ""
        try:
            if history:
                self.store.set_meta("recent_voice", json.dumps(history[-30:]))
            if not paused and self.rt.call_log is not None:
                self.rt.call_log.record(self.interaction.interaction_id, history, self.call_tasks(),
                                        self.interaction.device_id)
            if not paused:
                self.store.mark_call_ended()
                self.still_working_notices()
        except Exception as exc:
            logger.warning("speakeasy: call-close bookkeeping failed: %s", type(exc).__name__)

    def call_tasks(self) -> list[dict[str, Any]]:
        """This call's tasks for the call log: what was asked, how it ended, the spoken answer."""
        with self.interaction.lock:
            keys = [(r.idem_key, r.status) for r in self.interaction.runs.values()]
        out = []
        for key, status in keys:
            request = self.store.request_text(key)
            if not request:
                continue
            work = self.store.work(idem_key=key) or {}
            out.append({"request": request[:400], "title": self.store.title(key) or "", "status": status,
                        "result": str(((work.get("result") or {}).get("spoken")) or "")[:400]})
        return out

    def still_working_notices(self) -> None:
        with self.interaction.lock:
            runs = [(r.run_id, r.idem_key, r.status, dict(r.approval) if r.approval else None)
                    for r in self.interaction.runs.values()]
        try:
            for run_id, key, status, approval in runs:
                if not run_id or status not in ACTIVE_RUN_STATES:
                    continue
                if approval:
                    self.notices.needs_you(approval["request_id"], approval.get("description"))
                    continue
                work = self.store.work(run_id) or {}
                short = work.get("short_status") if work.get("status_source") != "system" else None
                self.notices.still_working(run_id, self.store.title(key) or self.store.request_text(key), short)
        except Exception as exc:
            logger.warning("speakeasy: still-working notices failed: %s", type(exc).__name__)

    def touch(self) -> None:
        self.interaction.last_activity = time.monotonic()

    # -- provider events --------------------------------------------------------------------
    async def handle_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            delta = event.get("delta")
            if not isinstance(delta, str) or not delta or len(delta.encode()) > 8192:
                return
            self.touch()
            speaker = "user" if kind.startswith("session.input") else "assistant"
            self.fragments.append({"speaker": speaker, "text": delta, "at": time.monotonic(),
                                   "start_ms": int(event.get("start_ms", 0)), "end_ms": int(event.get("end_ms", 0))})
            return
        if kind == "session.delegation.created":
            delegation = event.get("delegation") or {}
            delegation_id = delegation.get("id")
            if delegation.get("target") != "client" or not isinstance(delegation_id, str) or not ID_RE.fullmatch(delegation_id):
                return
            if delegation_id in self.delegations:
                return
            self.delegations.add(delegation_id)
            self.touch()
            # delegation.created is the documented utterance boundary: freeze the transcript now.
            context = self.context(int(event.get("offset_ms", 0)))
            marked = delegation.get("task_id") or delegation.get("follow_up_task_id")
            self.schedule_dispatch(delegation_id, context, marked)
        elif kind == "session.closed":
            self.call_closed()
            with self.interaction.lock:
                self.interaction.finalization = "confirmed"
                latest = self.interaction.runs.get(self.interaction.latest_delegation_id or "")
                if not latest or latest.status not in {"working", "running", "waiting_for_approval"}:
                    self.interaction.status = "closed"
            self.publish()

    def schedule_dispatch(self, delegation_id: str, context: str, marked: Any = None) -> None:
        self.handoff_at[delegation_id] = time.monotonic()
        with self.interaction.lock:
            self.interaction.revision += 1
            revision = self.interaction.revision
            if self.interaction.room:
                # Asked during a room call: the work gets the room even if the call ends first.
                self.handoff_rooms[delegation_id] = self.interaction.room
        task = asyncio.get_running_loop().create_task(self.dispatch(delegation_id, revision, context, marked))
        self.dispatch_tasks.add(task)
        task.add_done_callback(self.dispatch_tasks.discard)

    def context(self, offset_ms: int) -> str:
        selected = [f for f in self.fragments if (f["end_ms"] or f["start_ms"]) <= offset_ms]
        lines: list[str] = []
        current = None
        for fragment in selected:
            if fragment["speaker"] != current:
                current = fragment["speaker"]
                lines.append(f"\n{current.title()}: ")
            lines[-1] += fragment["text"]
        cleaned: list[str] = []
        for line in lines:
            label, _, body = line.partition(": ")
            body = clean_transcript(body)
            if body:
                cleaned.append(f"{label}: {body}")
        text = "".join(cleaned).strip()
        earlier = self.interaction.history if self.interaction.resumed_from else []
        if earlier and text:
            before = "\n".join(f"{t['role'].title()}: {t['text']}" for t in earlier[-40:])
            text = f"Earlier in this call, before it was paused:\n{before}\n\nAfter resuming:\n{text}"
        return text[-MAX_TRANSCRIPT:]

    async def append(self, kind: str, delegation_id: str | None, content: str) -> None:
        """Send context to the live call. After Pause/Resume, results of runs started in an earlier
        call go to the resumed call as session-wide context."""
        head = self.interaction.head()
        target = head.worker if head is not self.interaction else self
        if target is None or target is not self and not target.call_connected():
            return
        if delegation_id and delegation_id.startswith(EARLY_PREFIX):
            delegation_id = None  # the provider never issued this handoff: say it session-wide
        if target is not self:
            content = "(Update on a task from before the pause.) " + content
            await target.send_on_own_loop(kind, None, content)
            return
        await self._send(kind, delegation_id, content[:2000])

    async def answer(self, task_id: str, text: str) -> str | None:
        """An answer tapped on a task's question card: it goes back to that task as a follow-up, the
        same as saying it, and the voice hears what was picked so it doesn't ask again."""
        text = clean_transcript(text or "").strip()[:400]
        with self.interaction.lock:
            known = task_id in self.interaction.runs
        if not text or not known:
            return None
        delegation_id = "answer_" + secrets.token_hex(8)
        self.delegations.add(delegation_id)
        self.touch()
        self.fragments.append({"speaker": "user", "text": text, "at": time.monotonic(), "start_ms": 0, "end_ms": 0})
        if self.call_connected():
            await self.append("session.thinking.append", None, P.answered_note(text))
        await self.follow_up(delegation_id, 0, f"User: {text}", router.Part("follow_up", text, task_id))
        return delegation_id

    def in_room_call(self) -> bool:
        """This call was started by turning listening mode off and still has the room text."""
        with self.interaction.lock:
            return bool(self.interaction.room)

    def room_handoff_text(self, task_id: str) -> str:
        """The room text for work started by a handoff: what it was made with (kept even after the
        call ends), else the call's own while it lasts (a question-card answer on a live call)."""
        if task_id in self.handoff_rooms:
            return self.handoff_rooms[task_id]
        with self.interaction.lock:
            return self.interaction.room

    async def early_request(self, text: str) -> str | None:
        """Words the app heard while the call was still connecting (transcribed on the device).
        They become the call's first request, handled like any handoff, and the voice is told so it
        acknowledges them instead of greeting the user or asking them to repeat it.

        In a call started from listening mode, a question about the room is answered by the voice
        (no task), and no words at all mean the voice waits quietly with the room as context. Outside
        listening mode, no words start nothing, as before."""
        text = clean_transcript(text or "").strip()[:EARLY_MAX_CHARS]
        if not text:
            if self.in_room_call():
                await self.room_nudge()
            return None
        if self.in_room_call() and router.asks_about_room(text):
            self.touch()
            # The user's turn, like any early words; the voice never heard it, so the note quotes it.
            self.fragments.append({"speaker": "user", "text": text, "at": time.monotonic(), "start_ms": 0, "end_ms": 0})
            self.room_answered.add(_words(text))
            logger.info("speakeasy: question about the room heard while connecting (%d words); answered by the voice",
                        len(text.split()))
            # Spoken now, with no handoff waiting on it: a commentary append (a thinking note isn't spoken).
            await self.append("session.commentary.append", None, P.room_answer_note(self.names, text))
            return None
        delegation_id = EARLY_PREFIX + secrets.token_hex(8)
        self.delegations.add(delegation_id)
        self.touch()
        # Part of the call's transcript, so later requests ("make it warmer") see it as context.
        self.fragments.append({"speaker": "user", "text": text, "at": time.monotonic(), "start_ms": 0, "end_ms": 0})
        logger.info("speakeasy: request heard while connecting (%d words)", len(text.split()))
        await self.append("session.thinking.append", None, P.early_request_note(self.names, text))
        self.schedule_dispatch(delegation_id, f"User: {text}")
        return delegation_id

    async def room_nudge(self) -> None:
        """Listening mode was turned off and nothing was said. The voice stays quiet: it keeps what
        the room said and waits for a request. It can't know what the user wants, so it never offers
        something on its own. (Older Mac apps still send this after ~2 s of silence.)"""
        with self.interaction.lock:
            if self.interaction.room_nudged:
                return
            self.interaction.room_nudged = True
        logger.info("speakeasy: room call opened in silence; waiting for a request")

    async def send_on_own_loop(self, kind: str, delegation_id: str | None, content: str) -> None:
        loop, current = self.loop, asyncio.get_running_loop()
        if loop is None or loop is current:
            await self._send(kind, delegation_id, content[:2000])
            return
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(self._send(kind, delegation_id, content[:2000]), loop))

    def speak_from_thread(self, kind: str, content: str) -> None:
        """Called from HTTP threads (e.g. an email card decision) to tell the live call something."""
        head = self.interaction.head()
        worker = head.worker or self
        loop = worker.loop
        if loop is None or not worker.call_connected():
            return
        asyncio.run_coroutine_threadsafe(worker._send(kind, None, content[:2000]), loop)

    # -- spoken acknowledgement and progress -------------------------------------------------
    def assistant_spoke_since(self, since: float) -> bool:
        return any(f["speaker"] == "assistant" and f.get("at", 0) >= since and clean_transcript(f["text"]).strip()
                   for f in list(self.fragments))

    def user_spoke_since(self, since: float) -> bool:
        return any(f["speaker"] == "user" and f.get("at", 0) >= since and clean_transcript(f["text"]).strip()
                   for f in list(self.fragments))

    async def say_where(self, delegation_id: str, line: str | None, place: str | None = None,
                        named: bool = False) -> None:
        """Where a task went (a new thread, another channel). Spoken only when the user named the
        place ("put that in #work"): otherwise the voice model's own acknowledgement is enough and
        the place is a silent note it can use if asked. The app shows it either way."""
        self.handoff_at.pop(delegation_id, None)
        if line and named:
            await self.append("session.commentary.append", delegation_id, line)
        elif place:
            await self.append("session.thinking.append", delegation_id, P.where_note(place))

    def record_timing(self, task_id: str, idem: str) -> None:
        ms = self.route_timings.pop(task_id, None)
        if ms is not None:
            self.store.set_timing(idem, "routing_ms", ms)

    async def maybe_note_status(self, backend: BackendRun, detail: str) -> None:
        """Keep the voice model's picture of a running task current without prompting it to talk."""
        now = time.monotonic()
        with self.interaction.lock:
            last = backend.noted_at
            if last and now - last < STATUS_NOTE_EVERY_S:
                return
            backend.noted_at = now
        await self.append("session.thinking.append", backend.say_id, P.status_note(detail))

    def record_activity(self, backend: BackendRun, detail: str | None) -> None:
        """Remember what the task is concretely doing, so spoken updates can say it."""
        text = " ".join(str(detail or "").split())[:240]
        if not text:
            return
        with self.interaction.lock:
            if backend.activity and backend.activity[-1] == text:
                return
            backend.activity = (backend.activity + [text])[-ACTIVITY_KEEP:]
            backend.activity_count += 1

    async def maybe_speak_progress(self, backend: BackendRun, milestone: str) -> bool:
        """A long task that has done something new gets one brief spoken update naming what it is
        actually doing, at most once per PROGRESS_EVERY_S across the call."""
        if not self.rt.settings()["speech"]["progress"]:
            return False
        now = time.monotonic()
        # A problem or heads-up ("the helper is broken, fixing it") is worth hearing now: it skips the
        # spacing between updates, which otherwise let "starting music" crowd it out.
        urgent = bool(milestone and text_mod.HEADS_UP_RE.search(milestone))
        with self.interaction.lock:
            last_any = self.interaction.progress_spoken_at
            if (now - backend.started < PROGRESS_AFTER_S or backend.status not in {"running", "working"}
                    or (last_any and now - last_any < PROGRESS_EVERY_S and not urgent)
                    or backend.activity_count <= backend.told_count):
                return False
            self.interaction.progress_spoken_at = now  # claim the slot before the (slow) wording call
            backend.spoken_at = now
            new_steps = backend.activity[-min(ACTIVITY_KEEP, backend.activity_count - backend.told_count):]
            covered = backend.activity_count
            told = list(backend.told[-3:])
        request = self.store.request_text(backend.idem_key) or ""
        writer = self.rt.progress_call or router.progress_update
        try:
            line = await asyncio.to_thread(writer, request, new_steps, told)
        except Exception as exc:
            logger.info("speakeasy: progress wording unavailable (%s)", type(exc).__name__)
            line = None
        line = line or P.progress_fallback(new_steps, milestone)
        if not line or not self.call_connected():
            with self.interaction.lock:
                self.interaction.progress_spoken_at = last_any
            return False
        with self.interaction.lock:
            backend.told_count = covered
            backend.told = (backend.told + [line])[-5:]
        await self.append("session.commentary.append", backend.say_id, line)
        return True

    # -- dispatch ---------------------------------------------------------------------------
    def _idem(self, key: str, revision: int) -> str:
        return "se_" + hashlib.sha256(
            f"{self.interaction.interaction_id}\0{key}\0{revision}\0coordinator".encode()).hexdigest()

    def open_tasks(self) -> list[router.OpenTask]:
        with self.interaction.lock:
            runs = [(r.delegation_id, r.idem_key, r.status, r.started) for r in self.interaction.runs.values()
                    if r.status != "rejected"]
        tasks = []
        now = time.monotonic()
        for task_id, key, status, started in runs[-MAX_TASKS:]:
            request = self.store.request_text(key)
            if not request:
                continue
            work = self.store.work(idem_key=key) or {}
            spoken = ((work.get("result") or {}).get("spoken") or "") if status == "completed" else ""
            told = self.store.told(key)
            tasks.append(router.OpenTask(task_id, request, status, (spoken + " " + told).strip(),
                                         round(now - started, 1)))
        return tasks

    async def dispatch(self, delegation_id: str, revision: int, context: str, marked: Any = None) -> None:
        """Every handoff ends visibly. The voice already said "on it", and the app shows "Waiting for
        <name>" until a task row appears; an exception here used to vanish into the background task,
        leaving that waiting line up forever."""
        try:
            await self._dispatch(delegation_id, revision, context, marked)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("speakeasy: handoff failed before the task started (%s: %s)",
                           type(exc).__name__, str(exc)[:200])
            await self._handoff_failed(delegation_id, revision, exc)
        finally:
            self.handoff_rooms.pop(delegation_id, None)  # this handoff's work is over (or never started)

    async def settle(self, request: str) -> str:
        """Wait SETTLE_S after the handoff; every new user turn in that time restarts the wait (up to
        SETTLE_MAX_S) and is added to this request. Returns the whole request."""
        start = time.monotonic()
        quiet_until, seen = start + SETTLE_S, 0
        extra: list[str] = []
        while time.monotonic() < min(quiet_until, start + SETTLE_MAX_S):
            await asyncio.sleep(0.15)
            fresh = [f["text"] for f in list(self.fragments) if f["speaker"] == "user" and f["at"] > start
                     and not _same_words(f["text"], request)]
            if len(fresh) > seen:
                seen, extra = len(fresh), fresh
                quiet_until = time.monotonic() + SETTLE_S
        if not extra:
            return request
        more = " ".join(" ".join(extra).split())
        self.absorbed = [(t, at) for t, at in self.absorbed if time.monotonic() - at < ABSORB_WINDOW_S]
        self.absorbed.append((more, time.monotonic()))
        logger.info("speakeasy: kept listening, added %d more words to the request", len(more.split()))
        return f"{request.rstrip()} {more}"

    def _already_absorbed(self, request: str) -> bool:
        norm = _words
        want = norm(request)
        now = time.monotonic()
        return bool(want) and any(now - at < ABSORB_WINDOW_S and (want in norm(t) or norm(t) in want)
                                  for t, at in self.absorbed)

    async def _talk_not_work(self, delegation_id: str, revision: int, request: str, context: str) -> bool:
        """Ideas, opinions and reactions are conversation: the voice answers them, no task starts.
        "Just talk to me" holds all work until the user asks for something. In a call started from
        listening mode, a question about what was said in the room is the voice's to answer too."""
        if self.in_room_call() and router.asks_about_room(request):
            if _words(request) in self.room_answered:
                # The voice was already told to answer this from the room and handed it off anyway:
                # it needs real work. Hermes gets it, with the room as background.
                logger.info("speakeasy: room question handed off again; starting it as work")
            else:
                self.room_answered.add(_words(request))
                logger.info("speakeasy: question about the room, answered by the voice (no task)")
                await self._drop(delegation_id, revision, "Answered from the room", None)
                await self.append("session.thinking.append", delegation_id, P.room_answer_note(self.names))
                return True
        last_said = next((l for l in reversed(context.splitlines()) if l.startswith("Assistant: ")), "")
        offered = last_said.rstrip().endswith("?")  # "Want me to dig into that?" "Yeah" is a go-ahead
        if router.starts_talk_mode(request):
            with self.interaction.lock:
                self.interaction.talking_only = True
            logger.info("speakeasy: talk-only mode on; holding work until asked")
            await self._drop(delegation_id, revision, "Just talking", None)
            await self.append("session.thinking.append", delegation_id, P.talk_mode_note(self.names))
            return True
        with self.interaction.lock:
            talking_only = self.interaction.talking_only
        if talking_only and (router.asks_for_work(request) or offered):
            with self.interaction.lock:
                self.interaction.talking_only = False
            logger.info("speakeasy: talk-only mode off; asked for work")
            return False
        if router.is_reaction(request) and not offered:
            logger.info("speakeasy: reaction, no task started")
            await self._drop(delegation_id, revision, "Just a reaction", None)
            await self.append("session.thinking.append", delegation_id, P.reaction_note(self.names))
            return True
        if talking_only or (router.is_conversation(request) and not offered):
            logger.info("speakeasy: conversation, answered by the voice (no task)")
            await self._drop(delegation_id, revision, "Talked it through", None)
            await self.append("session.thinking.append", delegation_id, P.talk_note(self.names))
            return True
        return False

    async def _drop(self, delegation_id: str, revision: int, reason: str, spoken: str | None) -> None:
        """A handoff that should never become a task. It ends visibly (no lingering "Waiting for")."""
        backend = BackendRun(delegation_id, revision, self._idem(delegation_id, revision))
        with self.interaction.lock:
            self.interaction.runs[delegation_id] = backend
            backend.status, backend.error = "rejected", reason
        self.publish()
        if spoken:
            await self.append("session.commentary.append", delegation_id, spoken)

    async def _handoff_failed(self, delegation_id: str, revision: int, exc: Exception) -> None:
        with self.interaction.lock:
            backend = self.interaction.runs.get(delegation_id)
            started = backend is not None and backend.status not in {"pending", "queued"} and backend.run_id
        if started:
            return  # the task itself is running; its own error handling reports the outcome
        idem = self._idem(delegation_id, revision)
        with self.interaction.lock:
            if backend is None:
                backend = BackendRun(delegation_id, revision, idem)
                self.interaction.runs[delegation_id] = backend
                self.interaction.latest_delegation_id = delegation_id
            backend.status, backend.error = "failed", f"Couldn't start: {type(exc).__name__}"
        try:
            self.store.reserve_run(idem, self.interaction.interaction_id, delegation_id, revision)
            self.store.update_run(idem, None, "failed")
            self.store.progress(idem, "result", "Couldn't start this task")
        except Exception:
            logger.warning("speakeasy: could not record the failed handoff")
        self.publish()
        await self.append("session.commentary.append", delegation_id, P.FAILED_SPOKEN)

    async def _dispatch(self, delegation_id: str, revision: int, context: str, marked: Any = None) -> None:
        """One delegation from the live call: a new task (the usual case), a follow-up to an open
        task, or a request to continue one of the user's existing Hermes conversations."""
        if not context:
            backend = BackendRun(delegation_id, revision, self._idem(delegation_id, revision))
            with self.interaction.lock:
                self.interaction.runs[delegation_id] = backend
                self.interaction.latest_delegation_id = delegation_id
                backend.status = "rejected"
                backend.error = "No complete transcript was available at the delegation boundary"
            self.publish()
            await self.append("session.commentary.append", delegation_id, P.NO_TRANSCRIPT_SPOKEN)
            return
        last_request = whole_request(context)
        if is_greeting(last_request):
            # "Hey" opens a conversation; it never starts work.
            logger.info("speakeasy: greeting, no task started")
            await self._drop(delegation_id, revision, "Just a greeting", P.GREETING_SPOKEN)
            return
        if not marked and await self._talk_not_work(delegation_id, revision, last_request, context):
            return
        offer = next((l[len("Assistant: "):] for l in reversed(context.splitlines()) if l.startswith("Assistant: ")), "")
        if not marked and router.is_reaction(last_request) and offer.rstrip().endswith("?"):
            # "Yeah" to "Want me to look up what they charge?": the yes carries the offer with it.
            last_request = f"{last_request.rstrip('.!')}: {offer.strip()}"
        if self._already_absorbed(last_request):
            logger.info("speakeasy: handoff already folded into the task before it; not started twice")
            await self._drop(delegation_id, revision, "Added to the previous request", None)
            return
        if router.is_show_me(last_request) and await self.show_me(delegation_id, last_request):
            return
        if router.is_status_question(last_request):
            running = [t for t in self.open_tasks() if t.status not in TERMINAL]
            if running:
                target = next((t for t in running if marked and t.task_id == marked), running[-1])
                await self.answer_status(delegation_id, target.task_id)
                return
        with self.interaction.lock:
            device = self.interaction.device_id
        logger.info("speakeasy: request on device %s (%d words)", (device or "unknown")[:8], len(last_request.split()))
        home_follow = isinstance(marked, str) and marked in self.home_tasks
        if (not marked or home_follow) and await self.home_control(delegation_id, revision, last_request):
            return
        # Home commands stay instant. Anything else waits a beat in case the user is still talking,
        # so a breath mid-thought doesn't send half a request.
        if not marked and router.continues_newest(last_request, self.open_tasks()) is None:
            last_request = await self.settle(last_request)
        if not marked and await self.quick_answer(delegation_id, revision, last_request):
            return
        candidates = await self.conversation_candidates(last_request)
        now = time.time()
        chats = [router.Chat(f"c{i + 1}", c.conv.where, c.snippets, c.voice_request,
                             max(0.0, now - c.conv.last_active) if c.conv.last_active else None)
                 for i, c in enumerate(candidates)]
        earlier = "\n".join(line for line in context.splitlines()[:-1] if line.startswith(("User: ", "Assistant: ")))
        try:
            decision = await asyncio.to_thread(self.rt.route, last_request, self.open_tasks(), marked, chats,
                                               earlier[-1200:], replied_task_id=self.replied_task)
        except Exception as exc:  # routing is a nicety: without it the request still becomes new work
            logger.warning("speakeasy: routing failed, starting as new work (%s)", type(exc).__name__)
            decision = router.fallback_decision(last_request)
        self.route_timings[delegation_id] = decision.latency_ms
        part = decision.parts[0]
        if part.kind == router.IGNORE:
            self.handoff_at.pop(delegation_id, None)
            if delegation_id.startswith(EARLY_PREFIX):
                # "Hey" before the call connected: the voice already has the words and answers itself.
                logger.info("speakeasy: words heard while connecting were not a request")
                return
            logger.info("speakeasy: nothing to act on in a %d-word fragment; asked again", len(last_request.split()))
            await self.append("session.commentary.append", delegation_id, P.DIDNT_CATCH)
            return
        if part.kind == router.STATUS and part.task_id:
            await self.answer_status(delegation_id, part.task_id)
            return
        if decision.show:
            # "What do those speakers look like?": about an open task, show what it already has (or ask
            # it for a screenshot); otherwise the work that answers must come back with a picture.
            if part.kind == "follow_up" and part.task_id and await self.show_me(
                    delegation_id, last_request, task_id=part.task_id, only_if_visible=True):
                if router.only_looking(last_request):
                    return
                # "A picture of that page. OK, the 4:15 then": the picture opened, and the rest is
                # an instruction for the task, which must still reach it.
            self.wants_show.add(delegation_id)
        if part.kind == "follow_up" and part.task_id:
            await self.follow_up(delegation_id, revision, context, part)
            return
        named = self.rt.explicit_channel(last_request)
        if named is None and len(decision.parts) == 1:
            conv = self.pick_conversation(last_request, decision, candidates, chats)
            if conv is not None:
                await self.start_continuity_task(delegation_id, revision, context, last_request, conv)
                return
        choice = named or self.rt.topical_channel(decision.channel)
        if len(decision.parts) > 1 and not choice.clarify:
            await self.start_parts(delegation_id, revision, context, [p.request for p in decision.parts], choice)
            return
        await self.start_routed(delegation_id, revision, context, last_request, choice)

    async def answer_status(self, delegation_id: str, task_id: str) -> None:
        """"What's the status?": answered now from the steps the task has reported, never queued
        behind the task itself (that made status questions wait up to eight minutes)."""
        with self.interaction.lock:
            run = self.interaction.runs.get(task_id)
            steps = list(run.activity) if run else []
            status = run.status if run else None
            started = run.started if run else time.monotonic()
            key = run.idem_key if run else None
        name = (self.store.title(key) if key else None) or notice_text(self.store.request_text(key) if key else "", 80)
        work = self.store.work(idem_key=key) if key else None
        if not steps and work:
            steps = [e.get("text", "") for e in (work.get("events") or []) if e.get("kind") in {"milestone", "tool"}][-4:]
        result = notice_text(((work or {}).get("result") or {}).get("spoken"), 300) if status == "completed" else None
        self.handoff_at.pop(delegation_id, None)
        self.replied_task = task_id
        logger.info("speakeasy: status answered from %d steps (%s)", len(steps), status or "unknown")
        await self.append("session.commentary.append", delegation_id,
                          P.status_answer(name or "that task", status, steps, time.monotonic() - started, result))

    async def home_control(self, delegation_id: str, revision: int, request: str) -> bool:
        """A request that is only device commands, done straight through Home Assistant (opt-in).
        True when handled: the result is spoken and left as a finished task card. False sends the
        request on to Hermes unchanged, including whenever anything goes wrong before a device moved."""
        home = self.rt.home
        if home is None or not request or not getattr(home, "enabled", False):
            return False
        pending, self.home_question = self.home_question, None  # a question is answered at most once
        answering = None
        if pending and time.monotonic() - pending[2] <= home_control_mod.ASK_WINDOW_S:
            answering = (pending[0], pending[1])
        try:
            following = bool(self.home_notes) and self.replied_task in self.home_tasks
            try:
                reply = await asyncio.to_thread(home.respond, request, list(self.home_notes), answering, following)
            except TypeError:  # a test double without the newer argument
                reply = await asyncio.to_thread(home.respond, request, list(self.home_notes), answering)
        except Exception as exc:  # the fast path must never break a request
            logger.warning("speakeasy: home control failed, using Hermes (%s)", type(exc).__name__)
            return False
        if reply is None and answering:
            # Their answer didn't settle it: Hermes gets the original request plus the answer.
            return False
        if reply is None:
            return False
        if reply.ask:
            self.home_question = (request, reply.spoken, time.monotonic())
            self.handoff_at.pop(delegation_id, None)
            self.publish()
            await self.append("session.commentary.append", delegation_id, P.home_question(reply.spoken))
            return True
        if answering:
            self.home_notes = (self.home_notes + [
                f'For "{answering[0]}" I asked "{answering[1]}" and they said "{request}".'])[-home_control_mod.MAX_NOTES:]
            request = f"{answering[0]} ({request})"
        idem = self._idem(delegation_id, revision)
        backend = BackendRun(delegation_id, revision, idem, status="completed" if reply.ok else "failed")
        if not reply.ok:
            backend.error = reply.spoken
        with self.interaction.lock:
            self.interaction.runs[delegation_id] = backend
            self.interaction.latest_delegation_id = delegation_id
        try:
            self.store.reserve_run(idem, self.interaction.interaction_id, delegation_id, revision)
            self.store.set_title(idem, short_title(request) or "Home")
            self.store.progress(idem, "request", request)
            self.store.update_run(idem, None, backend.status)
            self.store.set_result(idem, split_result(reply.spoken, ()))
            self.store.progress(idem, "result", reply.spoken)
        except Exception:
            logger.warning("speakeasy: could not record the home-control card")
        self.handoff_at.pop(delegation_id, None)
        if reply.ok:
            # "make 'em dimmer" right after: the planner knows which lights were meant.
            self.home_tasks.add(delegation_id)
            self.home_notes = (self.home_notes + [f'Just now: "{request}" -> {reply.spoken}'])[-home_control_mod.MAX_NOTES:]
        self.replied_task = delegation_id
        self.publish()
        await self.append("session.commentary.append", delegation_id, reply.spoken)
        return True

    async def quick_answer(self, delegation_id: str, revision: int, request: str) -> bool:
        """A simple public question answered in seconds, without the full agent. Clock, date and arithmetic
        are computed here instantly; sports, weather and other facts come from one lookup (started while
        Jev, if on, is still deciding) plus one short sentence. Anything not clearly answered goes to
        Hermes as usual."""
        fast = self.rt.settings().get("fast_routing") or {}
        if not fast.get("quick_answers", True) or not quick_mod.eligible(request):
            return False
        tasks = self.open_tasks()
        if router.quick_intent(request, tasks, self.replied_task) is not None:
            return False  # a status question, a yes to an offer, a repeat: not a lookup
        started = time.monotonic()
        spoken = await asyncio.to_thread(instant_mod.answer, request)
        shown: list[dict[str, Any]] = []
        if spoken:
            card = await asyncio.to_thread(instant_mod.view, request)
            shown = views_mod.clean_all([card]) if card else []
        if not spoken:
            provider = fast.get("jev") or ""
            if not provider and (tasks or not quick_mod.QUESTION.search(request)):
                return False
            lookup = None
            if self.rt.quick_call is None:
                lookup = asyncio.ensure_future(asyncio.to_thread(quick_mod.facts, request, fast.get("home_place") or ""))
            if provider:
                lane, confidence, ms = await asyncio.to_thread(
                    jev_mod.route, self.rt.hermes_home, provider, request,
                    [t.request for t in tasks if t.status in ACTIVE_RUN_STATES])
                logger.info("speakeasy: jev routed to %s (%.2f) in %d ms", lane or "unsure", confidence, ms)
                if lane != jev_mod.QUICK:
                    if lookup is not None:
                        lookup.cancel()
                    return False
            today = time.strftime("%A, %B %d, %Y")
            if lookup is None:
                spoken = await asyncio.to_thread(self.rt.quick_call, request, today)
            else:
                try:
                    results = await lookup
                except Exception:
                    results = []
                spoken = await asyncio.to_thread(quick_mod.answer, request, today, None, None, results)
                if spoken:
                    shown = quick_mod.views_in(results)
        if not spoken:
            logger.info("speakeasy: quick answer passed to Hermes after %d ms", int((time.monotonic() - started) * 1000))
            return False
        logger.info("speakeasy: quick answer in %d ms", int((time.monotonic() - started) * 1000))
        idem = self._idem(delegation_id, revision)
        backend = BackendRun(delegation_id, revision, idem, status="completed")
        with self.interaction.lock:
            self.interaction.runs[delegation_id] = backend
            self.interaction.latest_delegation_id = delegation_id
        try:
            self.store.reserve_run(idem, self.interaction.interaction_id, delegation_id, revision)
            self.store.set_title(idem, short_title(request) or "Quick answer")
            self.store.progress(idem, "request", request)
            self.store.update_run(idem, None, "completed")
            result = split_result(spoken, ()) or {"spoken": spoken, "full": spoken}
            if shown:
                result["views"] = shown
            self.store.set_result(idem, result)
            self.store.progress(idem, "result", spoken)
        except Exception:
            logger.warning("speakeasy: could not record the quick-answer card")
        self.notices.post(f"quick:{idem}", P.quick_log(request, spoken))
        self.handoff_at.pop(delegation_id, None)
        self.replied_task = delegation_id
        self.publish()
        await self.append("session.commentary.append", delegation_id, P.quick_note(spoken))
        return True

    async def show_me(self, delegation_id: str, request: str, task_id: str | None = None,
                      only_if_visible: bool = False) -> bool:
        """'Show me' / 'what are you looking at' about existing work: open that task's image in the
        panel instead of starting new work. Returns False when there is nothing to point at (the
        request is then handled as ordinary work).

        The panel action carries only the task and run ids; the app fetches the bytes through the
        authenticated image routes. The voice says it's on screen only after the action went out."""
        tasks = self.open_tasks()
        with self.interaction.lock:
            runs = {t.task_id: self.interaction.runs.get(t.task_id) for t in tasks}
        keys = {task_id: run.idem_key for task_id, run in runs.items() if run is not None}
        has_image = {task_id for task_id, key in keys.items()
                     if self.store.live_image(key) or (self.store.work(idem_key=key) or {}).get("review")
                     or (((self.store.work(idem_key=key) or {}).get("result") or {}).get("cards"))}
        if task_id is not None:
            target = next((t for t in tasks if t.task_id == task_id), None)
            if target is None or (only_if_visible and target.task_id not in has_image
                                  and target.status not in {"admitting", "working", "running"}):
                return False  # a finished task with no picture: the caller asks for one as new work
        else:
            target = router.show_me_target(request, tasks, has_image)
        self.handoff_at.pop(delegation_id, None)
        if target is None:
            # Nothing in this call: an earlier call's finished images still waiting on a review card.
            carried = self.store.keys_with_pending_reviews()
            if not carried:
                return False
            work = self.store.work(idem_key=carried[-1]) or {}
            self.show_action(work.get("run_id"), self.store.task_id_for(carried[-1]), "review")
            await self.append("session.commentary.append", delegation_id, P.SHOW_ME_ON_SCREEN)
            return True
        run = runs.get(target.task_id)
        key = keys.get(target.task_id)
        if run is None or key is None:
            return False
        running = run.status in {"admitting", "working", "running", "waiting_for_approval"}
        if target.task_id in has_image:
            kind = "live" if running and self.store.live_image(key) else "review"
            self.show_action(run.run_id, target.task_id, kind)
            await self.append("session.commentary.append", delegation_id, P.SHOW_ME_ON_SCREEN)
            return True
        if running and not run.run_id:
            return False  # a thread task: the request goes into its thread (see message_thread_task)
        if running and run.run_id:
            accepted = await asyncio.to_thread(self.hermes.steer, run.run_id, P.SCREENSHOT_STEER)
            if accepted:
                with self.interaction.lock:
                    self.show_pending.add(key)
                self.store.progress(key, "milestone", "You asked to see what it's looking at")
                self.show_action(run.run_id, target.task_id, "detail")
                await self.append("session.commentary.append", delegation_id, P.SHOW_ME_REQUESTED)
                return True
        await self.append("session.commentary.append", delegation_id, P.SHOW_ME_NOTHING)
        return True

    def _take_show(self, delegation_id: str, idem: str) -> bool:
        """The user wants to see this task's result: tell the task, and open the first picture it
        shares as soon as it arrives."""
        base = delegation_id.split("-p")[0]
        with self.interaction.lock:
            if base not in self.wants_show:
                return False
            if base == delegation_id:
                self.wants_show.discard(base)
            self.show_pending.add(idem)
        return True

    def show_action(self, run_id: str | None, task_id: str, kind: str) -> None:
        """Ask the app to open a task's image: kind is live | review | detail (no image yet)."""
        self.show_seq += 1
        self.publish()  # the task list first, so the app already has the task it is told to open
        self.interaction.feed.publish("show", {"task_id": task_id, "run_id": run_id, "image": kind,
                                               "seq": self.show_seq})

    async def start_parts(self, delegation_id: str, revision: int, context: str, parts: list[str],
                          choice: channels.Choice) -> None:
        """A compound request: one parallel task per part, one spoken acknowledgement for all."""
        channel = choice.channel
        deliver_to = channel.target if channel is not None and not channel.default else None
        await self.append("session.thinking.append", delegation_id, P.split_note(self.names, parts))
        self.handoff_at.pop(delegation_id, None)
        self.route_timings.update({f"{delegation_id}-p{i}": self.route_timings.get(delegation_id, 0)
                                   for i in range(len(parts))})
        await asyncio.gather(*(self.start_task(f"{delegation_id}-p{i}", revision, context, part, focus=part,
                                               voice_id=delegation_id, deliver_to=deliver_to)
                               for i, part in enumerate(parts)))

    async def start_routed(self, delegation_id: str, revision: int, context: str, request: str,
                           choice: channels.Choice) -> None:
        """A new task, placed by channel routing: ask when unclear, open a thread where the
        channel wants one (and Hermes can), else run here and post the answer to the channel."""
        if choice.clarify:
            self.handoff_at.pop(delegation_id, None)
            await self.append("session.commentary.append", delegation_id, choice.clarify)
            return
        channel = choice.channel
        routed = channel is not None and not channel.default
        runner = self.rt.threads
        if channel is not None and channel.new_thread and runner is not None:
            available = await asyncio.to_thread(runner.available, channel.target)
            if available:
                await self.start_thread_task(delegation_id, revision, context, request, channel,
                                             named=choice.how == "named")
                return
        named = choice.how == "named"
        if routed:
            await self.start_task(delegation_id, revision, context, request, deliver_to=channel.target,
                                  ack=P.ack_channel_post(channel.label), ack_place=channel.label, named=named)
            return
        await self.start_task(delegation_id, revision, context, request)

    async def conversation_candidates(self, request: str) -> list[continuity.Candidate]:
        """Existing chats this request might continue: the ones voice work was last sent to, the
        most recently active, and the ones whose recent messages mention what was asked."""
        if not request or not self.rt.settings()["continuity"]["enabled"]:
            return []
        try:
            found = await asyncio.to_thread(continuity.conversations_with_context, self.rt.state_db, request)
            placements = self.store.recent_placements()
            ranked = await asyncio.to_thread(continuity.with_placements, self.rt.state_db, found, placements)
            settings = self.rt.routable()
            # Only chats in the home target or an approved channel; a thread elsewhere is never picked up.
            return [c for c in ranked if channels.allows_conversation(
                settings, c.conv.platform, c.conv.chat_id, c.conv.parent_chat_id, c.conv.chat_type)]
        except Exception as exc:  # never let matching break a request
            logger.warning("speakeasy: conversation candidates failed: %s", type(exc).__name__)
            return []

    @staticmethod
    def pick_conversation(request: str, decision: router.Decision, candidates: list[continuity.Candidate],
                          chats: list[router.Chat]) -> continuity.Conversation | None:
        """Only the routing model continues an existing chat. Without it (timed out, unavailable),
        word overlap is too weak a signal: a reminder about "new comments on the repo" landed in an
        unrelated thread that happened to mention the repo. A new task in the usual place is the
        safe miss; the wrong thread is not."""
        if not decision.conversation:
            return None
        index = next((i for i, c in enumerate(chats) if c.ref == decision.conversation), None)
        return candidates[index].conv if index is not None else None

    async def follow_up(self, delegation_id: str, revision: int, context: str, part: router.Part) -> None:
        """Add to an open task when its run still accepts guidance; otherwise continue in that
        task's own session with a run that knows the earlier request and result."""
        with self.interaction.lock:
            target = self.interaction.runs.get(part.task_id or "")
            run_id = target.run_id if target else None
            status = target.status if target else None
            key = target.idem_key if target else None
        if target is None or key is None:
            await self.start_task(delegation_id, revision, context, part.request)
            return
        earlier = notice_text(self.store.request_text(key), 200) or "an earlier request"
        placed = self.store.continued_for(key)
        if status in ACTIVE_RUN_STATES and not run_id and await self.message_thread_task(delegation_id, target, part.request):
            return
        opening_thread = placed is None and not run_id and status in ACTIVE_RUN_STATES and target.deliver_to
        waited = 0.0
        while opening_thread and placed is None and waited < THREAD_OPEN_WAIT_S:
            await asyncio.sleep(1.0)  # a thread task that just started: its session appears in seconds
            waited += 1.0
            placed = self.store.continued_for(key)
        if placed is not None:
            conv = await asyncio.to_thread(continuity.conversation_by_session, self.rt.state_db, placed["session_id"])
            if conv is not None:
                joins = status in ACTIVE_RUN_STATES
                await self.start_continuity_task(delegation_id, revision, context, part.request, conv,
                                                 joins=target if joins else None)
                return
        if run_id and status in {"running", "working"}:
            accepted = await asyncio.to_thread(self.hermes.steer, run_id, P.steer_text(self.names, part.request))
            if accepted:
                self.store.progress(key, "milestone", f"You added: {notice_text(part.request, 200)}")
                with self.interaction.lock:
                    self.interaction.latest_delegation_id = target.delegation_id
                self.publish()
                await self.append("session.thinking.append", delegation_id, P.added_to_task_note(earlier))
                self.handoff_at.pop(delegation_id, None)
                return
        work = self.store.work(idem_key=key) or {}
        result = notice_text((work.get("result") or {}).get("spoken"), 300)
        focus = P.follow_up_focus(part.request, earlier, result, status)
        await self.start_task(delegation_id, revision, context, f"{part.request} (following up: {earlier})",
                              focus=focus, session_id=self.store.session_for(key), replaces=target.delegation_id,
                              deliver_to=target.deliver_to)

    async def message_thread_task(self, delegation_id: str, target: BackendRun, request: str) -> bool:
        """A task running in a chat thread: put what was just said into that thread, exactly as if it
        were typed there, so the running turn takes it now (Hermes steers it in, or queues it right
        after). The voice is told it was added only once Hermes accepted the message; when it could
        not be delivered it is told that plainly. False: not a thread task (the caller carries on)."""
        key = target.idem_key
        placed = None
        waited = 0.0
        while waited <= THREAD_OPEN_WAIT_S:
            placed = self.store.continued_for(key) or {}
            if placed.get("thread_id") or not target.deliver_to:
                break
            await asyncio.sleep(1.0)  # a thread that is still being opened: its id lands in seconds
            waited += 1.0
        platform, thread_id = (placed or {}).get("platform"), (placed or {}).get("thread_id")
        runner = self.rt.threads
        if not platform or not thread_id or runner is None or not hasattr(runner, "post"):
            return False
        earlier = notice_text(self.store.request_text(key), 200) or "the task"
        wants_picture = router.wants_to_see(request)
        message = P.thread_follow_up(self.names, request, show=wants_picture)
        delivered = await asyncio.to_thread(runner.post, platform, str(thread_id), message=message,
                                            delivery_id=f"{key[:40]}-{secrets.token_hex(4)}")
        self.handoff_at.pop(delegation_id, None)
        if not delivered:
            logger.info("speakeasy: could not deliver a follow-up into a running task's thread")
            await self.append("session.commentary.append", delegation_id, P.not_delivered_note(earlier))
            return True
        if wants_picture:
            with self.interaction.lock:
                self.show_pending.add(key)
        self.store.progress(key, "milestone", f"You added: {notice_text(request, 200)}")
        with self.interaction.lock:
            self.interaction.latest_delegation_id = target.delegation_id
        self.publish()
        logger.info("speakeasy: follow-up delivered into a running task's thread")
        await self.append("session.thinking.append", delegation_id, P.added_to_task_note(earlier))
        return True

    def _register(self, task_id: str, backend: BackendRun, replaces: str | None = None) -> str | None:
        replaced_key = None
        with self.interaction.lock:
            old = self.interaction.runs.get(replaces or "")
            if old is not None and old.status not in ACTIVE_RUN_STATES and old.delegation_id != task_id:
                # A follow-up to a finished task replaces it: same slot in the list, one row.
                replaced_key = old.idem_key
                self.interaction.runs = {(task_id if key == replaces else key): (backend if key == replaces else run)
                                         for key, run in self.interaction.runs.items() if key != task_id}
            else:
                self.interaction.runs[task_id] = backend
            self.interaction.latest_delegation_id = task_id
        return replaced_key

    async def start_task(self, task_id: str, revision: int, context: str, request: str,
                         focus: str | None = None, voice_id: str | None = None,
                         session_id: str | None = None, replaces: str | None = None,
                         deliver_to: str | None = None, ack: str | None = None,
                         ack_place: str | None = None, named: bool = False) -> None:
        idem = self._idem(task_id, revision)
        backend = BackendRun(task_id, revision, idem, voice_id=voice_id, deliver_to=deliver_to)
        delegation_id = backend.say_id
        replaced_key = self._register(task_id, backend, replaces)
        if replaced_key:
            self.store.dismiss_key(replaced_key)
        self.publish()
        if not session_id:
            session_id = TASK_SESSION_PREFIX + hashlib.sha256(idem.encode()).hexdigest()[:24]
        label = self.rt.channel_label(deliver_to) if deliver_to else self.rt.delivery_label()
        show = self._take_show(voice_id or task_id, idem)
        if show:
            focus = (focus or request) + P.SHOW_IT_FOCUS
        prompt = P.build_task_prompt(self.names, revision, context, focus, label,
                                     room=self.room_handoff_text(voice_id or task_id))
        state, known_run = self.store.reserve_run(idem, self.interaction.interaction_id, task_id, revision)
        if state != "created":
            if known_run:
                with self.interaction.lock:
                    backend.run_id, backend.status = known_run, state
            self.publish()
            return
        self.store.set_session(idem, session_id)
        self.record_timing(task_id, idem)
        if request:
            self.store.progress(idem, "request", request)
            self.name_task(idem, request)
        with self.interaction.lock:
            others = [r.idem_key for r in self.interaction.runs.values()
                      if r.delegation_id != task_id and r.status in ACTIVE_RUN_STATES]
        parallel = [r for r in (notice_text(self.store.request_text(k), 80) for k in others[-4:]) if r]
        if voice_id is None:
            await self.append("session.thinking.append", delegation_id, P.work_started_note(parallel))
            if deliver_to and not replaces:
                await self.say_where(delegation_id, ack, place=ack_place, named=named)
            else:
                self.handoff_at.pop(delegation_id, None)
        try:
            run_id = await asyncio.to_thread(self.hermes.start_run, prompt, idem, session_id)
            self.store.update_run(idem, run_id, "running")
            with self.interaction.lock:
                backend.run_id, backend.status = run_id, "running"
            self.publish()
            loop = asyncio.get_running_loop()
            terminal_seen = await asyncio.to_thread(
                self.hermes.events, run_id,
                lambda event: asyncio.run_coroutine_threadsafe(self.handle_hermes_event(backend, event), loop).result())
            if not terminal_seen:
                await self.reconcile_stream_end(backend)
        except Exception as exc:
            if backend.run_id:
                try:
                    await self.reconcile_stream_end(backend, exc)
                    return
                except Exception as reconcile_exc:
                    exc = reconcile_exc
            failed_as = self._record_start_failure(backend, exc, "task")
            if backend.run_id:
                self.notices.stopped(backend.run_id, "failed", self.store.title(idem) or self.store.request_text(idem),
                                     failures.reason_text(failed_as))
            said = failures.reason_text(failed_as) and failures.voice_text(failed_as)
            await self.append("session.commentary.append", delegation_id,
                              P.failed_because(said) if said else P.FAILED_SPOKEN)

    def _record_start_failure(self, backend: BackendRun, exc: Exception, what: str) -> str:
        """A run that never got going on Hermes (or whose stream broke beyond repair): mark it failed
        with the reason Speakeasy can name, and return its failures kind (UNKNOWN when it can't name
        one). Callers pick the on-screen (reason_text) or spoken (voice_text) sentence from it."""
        # Logged so errors.log (which the app points to for an unknown reason) has the cause.
        logger.warning("speakeasy: %s failed to run on Hermes (%s: %s)", what, type(exc).__name__, str(exc)[:200])
        idem = backend.idem_key
        failed_as = failures.start_failure_kind(exc) or failures.UNKNOWN
        reason = failures.reason_text(failed_as)
        self.store.update_run(idem, None, "failed")
        self.store.set_failure(idem, failed_as)
        self.store.progress(idem, "result", f"Work failed: {reason}" if reason else "Work failed; details unavailable")
        with self.interaction.lock:
            backend.status, backend.error = "failed", str(exc)[:240]
        self.publish()
        return failed_as

    async def start_continuity_task(self, task_id: str, revision: int, context: str, request: str,
                                    conv: continuity.Conversation, joins: "BackendRun | None" = None) -> None:
        """Run the request inside an existing Hermes conversation's own session: it sees that
        conversation's history, and the reply posts back to that chat."""
        idem = self._idem(task_id, revision)
        backend = BackendRun(task_id, revision, idem, deliver_to=joins.deliver_to if joins else None)
        delegation_id = backend.say_id
        if joins is None:
            self._register(task_id, backend)
        else:
            # Part of a task still running: no new row. It goes into the same thread after that
            # turn and takes over the row then, so the panel shows one task with one final answer.
            earlier = notice_text(self.store.request_text(joins.idem_key), 200) or "the task"
            self.store.progress(joins.idem_key, "milestone", f"You added: {notice_text(request, 200)}")
            await self.append("session.thinking.append", delegation_id, P.added_to_task_note(earlier))
            self.handoff_at.pop(delegation_id, None)
        self.publish()
        state, known_run = self.store.reserve_run(idem, self.interaction.interaction_id, task_id, revision)
        if state != "created":
            if known_run:
                with self.interaction.lock:
                    backend.run_id, backend.status = known_run, state
            self.publish()
            return
        where = notice_text(conv.where, 100) or "that"
        self.record_timing(task_id, idem)
        self.store.set_continued(idem, conv.session_id, conv.where)
        self.store.progress(idem, "request", request)
        self.name_task(idem, request)
        self.store.progress(idem, "milestone", f"Continuing in {where}")
        if joins is not None:
            self.store.hide_key(idem)
        self.publish()
        if joins is None:
            await self.append("session.thinking.append", delegation_id, P.continuing_in_note(where))
            self.handoff_at.pop(delegation_id, None)
        waited = 0
        while joins is not None:  # hold until the first turn is done, then take over its row
            with self.interaction.lock:
                first_done = joins.status not in ACTIVE_RUN_STATES
            if first_done or waited >= CONTINUITY_WAIT_S:
                self.store.unhide_key(idem)
                replaced_key = self._register(task_id, backend, replaces=joins.delegation_id)
                if replaced_key:
                    self.store.dismiss_key(replaced_key)
                self.store.set_title(idem, self.store.title(joins.idem_key) or short_title(request))
                self.publish()
                joins = None
                break
            await asyncio.sleep(CONTINUITY_POLL_S)
            waited += CONTINUITY_POLL_S
        waited = 0
        while await asyncio.to_thread(continuity.session_busy, self.rt.state_db, conv.session_id):
            if waited == 0:
                self.store.user_progress(idem, "Queued", f"Waiting for current work in {where} to finish")
                self.publish()
            if waited >= CONTINUITY_WAIT_S:
                break
            await asyncio.sleep(CONTINUITY_POLL_S)
            waited += CONTINUITY_POLL_S
        try:
            asked = request + (P.SHOW_IT_FOCUS if self._take_show(task_id, idem) else "")
            message = P.continuation_message(self.names, asked, notice_text(request, 200) or "voice request",
                                             room=self.room_handoff_text(task_id))
            loop = asyncio.get_running_loop()
            final: dict[str, Any] = {}

            def on_event(name: str, payload: dict[str, Any]) -> None:
                if name == "run.started" and isinstance(payload.get("run_id"), str) and ID_RE.fullmatch(payload["run_id"]):
                    run_id = payload["run_id"]
                    self.store.update_run(idem, run_id, "running")
                    with self.interaction.lock:
                        backend.run_id, backend.status = run_id, "running"
                    asyncio.run_coroutine_threadsafe(self._publish_async(), loop)
                elif name == "assistant.commentary" and isinstance(payload.get("text"), str):
                    # What Hermes writes while it works ("Ratings are in, now checking fares") is the
                    # task's live status, read the same way as a thread task's. Only accepting strict
                    # STATUS: lines here left a continued task silent for its whole run.
                    text = payload["text"]
                    asyncio.run_coroutine_threadsafe(self.thread_activity(
                        backend, "commentary", text, {"event": "message.interim", "text": text}), loop).result()
                elif name == "tool.started":
                    event = {"event": "tool.started", "tool": payload.get("tool_name"), "preview": payload.get("preview")}
                    asyncio.run_coroutine_threadsafe(self.handle_hermes_event(backend, event), loop).result()
                elif name == "assistant.completed":
                    final["text"] = payload.get("content") or ""
                elif name.startswith("run.") and name != "run.started":
                    final["status"] = name.split(".", 1)[1]
                    final["error"] = payload.get("error")

            terminal = await asyncio.to_thread(continuity.stream_session_chat, self.hermes.base, self.rt.hermes_key(),
                                               conv, message, on_event)
            status = final.get("status") or ("completed" if terminal and final.get("text") else "ambiguous")
            output = P.without_voice_header(final.get("text", ""))
            if status == "ambiguous":
                if backend.run_id:
                    await self.reconcile_stream_end(backend)
                    return
                raise ServiceError(502, "conversation stream ended without a result")
            # The turn ran in the conversation's own session (its history), over the local API. Post
            # the answer into that chat ourselves, like any routed answer; Hermes config is untouched.
            if valid_delivery_target(conv.target):
                backend.deliver_to = conv.target
            # A failed turn's reply is Hermes' failure message; _handle_hermes_event keeps only its kind.
            await self.handle_hermes_event(backend, {"event": f"run.{status}", "output": output,
                                                     "error": final.get("error")})
        except Exception as exc:
            failed_as = self._record_start_failure(backend, exc, "conversation turn")
            said = failures.reason_text(failed_as) and failures.voice_text(failed_as)
            await self.append("session.commentary.append", delegation_id,
                              P.failed_because(said) if said else P.continuing_failed_note(where))

    async def start_thread_task(self, task_id: str, revision: int, context: str, request: str,
                                channel: channels.Channel, named: bool = False) -> None:
        """Open a new thread in the channel and run the task there, as if typed in it: the user can
        follow up in that thread, and the call hears its first answer. Falls back to running here
        and posting the answer to the channel when Hermes can't open the thread."""
        from .threads import ThreadError
        idem = self._idem(task_id, revision)
        backend = BackendRun(task_id, revision, idem, deliver_to=channel.target)
        delegation_id = backend.say_id
        self._register(task_id, backend)
        self.publish()
        summary = notice_text(request, 200) or "voice request"
        asked = request + (P.SHOW_IT_FOCUS if self._take_show(task_id, idem) else "")
        message = P.thread_task_message(self.names, asked, summary, context)
        try:
            opened = await asyncio.to_thread(self.rt.threads.open, channel.target, message=message,
                                             title=short_title(request) or "Voice task", delivery_id=idem)
        except (ThreadError, OSError) as exc:
            logger.info("speakeasy: new thread unavailable, posting to the channel instead (%s)", exc)
            await self.start_task(task_id, revision, context, request, deliver_to=channel.target,
                                  ack=P.ack_channel_post(channel.label), ack_place=channel.label, named=named)
            return
        state, _ = self.store.reserve_run(idem, self.interaction.interaction_id, task_id, revision)
        if state != "created":
            self.publish()
            return
        where = P.new_thread_in(channel.label)
        self.store.set_thread(idem, opened.platform, opened.thread_id, f"a thread in {channel.label}")
        self.record_timing(task_id, idem)
        self.store.progress(idem, "request", request)
        self.name_task(idem, request)
        self.store.update_run(idem, None, "running")
        self.store.progress(idem, "milestone", f"Started in {where}")
        with self.interaction.lock:
            backend.status = "running"
        self.publish()
        await self.say_where(delegation_id, P.ack_channel_thread(channel.label), place=where, named=named)

        def on_session(session_id: str) -> None:
            # Follow-ups to this task continue inside the thread (thread continuity).
            self.store.set_continued(idem, session_id, f"a thread in {channel.label}")

        def on_title(title: str) -> None:
            # Hermes names the thread after its first turn; show the same name in the panel.
            self.store.set_title(idem, title)
            self.publish()

        loop = asyncio.get_running_loop()

        def on_activity(kind: str, text: str) -> None:
            # The thread's turn is visible to the call as it happens: what it writes while working
            # becomes the task's live status, and pictures it takes or looks at reach the panel.
            event = ({"event": "message.interim", "text": text} if kind == "commentary"
                     else {"event": "tool.completed", "tool": "thread", "preview": text})
            asyncio.run_coroutine_threadsafe(self.thread_activity(backend, kind, text, event), loop)

        LIVE_THREAD_WAITS[idem] = time.monotonic()
        try:
            try:
                answer = await asyncio.to_thread(self.rt.threads.wait, opened, on_session, on_title, on_activity)
            except TypeError:  # an older runner without progress
                answer = await asyncio.to_thread(self.rt.threads.wait, opened, on_session, on_title)
        except Exception as exc:
            answer, backend.error = None, type(exc).__name__
        finally:
            LIVE_THREAD_WAITS.pop(idem, None)
        if answer is None:
            self.store.user_progress(idem, "In the thread", f"The answer will land in {where}")
            self.store.update_run(idem, None, "ambiguous")
            with self.interaction.lock:
                backend.status = "ambiguous"
            self.publish()
            return
        await self.handle_hermes_event(backend, {"event": "run.completed", "output": P.without_voice_header(answer),
                                                 "continued": True,
                                                 "thread_target": f"{channel.target}:{opened.thread_id}"
                                                 if channel.target.count(":") == 1 else None,
                                                 "pointer_target": None if channel.target == self.notices.target()
                                                 else f"{opened.platform}:{opened.thread_id}"})

    async def thread_activity(self, backend: BackendRun, kind: str, text: str, event: dict[str, Any]) -> None:
        """One step a thread task reported: keep its card live and the voice's picture of it current."""
        try:
            if backend.status in TERMINAL:
                return
            self._see_images(backend, event)
            if kind != "commentary":
                self.store.touch(backend.idem_key)  # alive: never "Status unconfirmed" while it works
                self.publish()
                return
            progress = interim_progress(event)
            detail = progress[1] if progress else thread_commentary(text)
            if not detail:
                return
            if progress:
                self.store.user_progress(backend.idem_key, *progress)
            self.store.thread_progress(backend.idem_key, detail)
            self.store.progress(backend.idem_key, "milestone", detail)
            self.record_activity(backend, detail)
            self.publish()
            if not await self.maybe_speak_progress(backend, detail):
                await self.maybe_note_status(backend, detail)
        except Exception:
            logger.debug("speakeasy: thread progress update failed", exc_info=True)

    async def reconcile_stream_end(self, backend: BackendRun, cause: Exception | None = None) -> None:
        if not backend.run_id:
            raise ServiceError(502, "Hermes stream ended before a run ID was assigned")
        try:
            result = await asyncio.to_thread(self.hermes.get_run, backend.run_id)
        except Exception as exc:
            result, cause = {}, cause or exc
        status = result.get("status")
        if status in TERMINAL:
            event = {"event": f"run.{status}"}
            for field in ("output", "error"):  # a failed run's status carries Hermes' error line too
                if field in result:
                    event[field] = result[field]
            await self.handle_hermes_event(backend, event)
            return
        detail = "Hermes event stream ended before a terminal event" + (f": {type(cause).__name__}" if cause else "")
        self.store.update_run(backend.idem_key, None, "ambiguous")
        with self.interaction.lock:
            backend.status, backend.approval, backend.error = "ambiguous", None, detail
        self.publish()
        self.notices.stopped(backend.run_id, "ambiguous", self.store.title(backend.idem_key) or self.store.request_text(backend.idem_key))
        await self.append("session.commentary.append", backend.say_id, P.lost_track_note(self.names))

    async def handle_hermes_event(self, backend: BackendRun, event: dict[str, Any]) -> None:
        try:
            await self._handle_hermes_event(backend, event)
        finally:
            self.publish()

    def _see_images(self, backend: BackendRun, event: dict[str, Any]) -> None:
        """Live view: remember the latest image the task produced or is looking at. Sources are the
        backend-neutral ``media.seen`` event and, for Hermes, ``MEDIA:`` tags (or a browser
        ``screenshot_path``) in interim text and tool result previews. Everything is vetted against the
        image roots / public HTTPS before it is stored; clients only get it through /voice/live-image."""
        if backend.status in TERMINAL or backend.status == "ambiguous":
            return
        kind = event.get("event")
        roots = self.rt.image_roots()
        seen: list[tuple[str, str, str]] = []
        source = "viewed"
        if kind == "media.seen":
            ref = vetted_live_ref(event.get("path") if event.get("path") is not None else event.get("url"), roots)
            seen = [ref] if ref else []
            source = event.get("source") if event.get("source") in {"screenshot", "viewed", "generated"} else "viewed"
            name = safe_user_text(event.get("name"), 120)
            if seen and name:
                seen = [(seen[0][0], seen[0][1], name)]
        elif kind == "message.interim":
            seen, source = live_images_in(event.get("text"), roots), "generated"
        elif kind in {"tool.completed", "tool.complete"}:
            seen = live_images_in(event.get("preview"), roots)
            source = "screenshot" if "screenshot" in str(event.get("tool") or "") or "vision" in str(event.get("tool") or "") else "viewed"
        if seen:
            ref_kind, ref, name = seen[-1]
            self.store.set_live_image(backend.idem_key, ref_kind, ref, name, source)
            with self.interaction.lock:
                asked = backend.idem_key in self.show_pending
                self.show_pending.discard(backend.idem_key)
            shared = source == "generated" and self.call_connected()  # the task chose to share it
            if asked or shared:  # open it as soon as it arrives
                self.show_action(backend.run_id, backend.delegation_id, "live")

    async def _handle_hermes_event(self, backend: BackendRun, event: dict[str, Any]) -> None:
        idem, delegation_id = backend.idem_key, backend.say_id
        kind = event.get("event")
        if kind in {"media.seen", "message.interim", "tool.completed", "tool.complete"}:
            self._see_images(backend, event)
        if kind in {"tool.started", "tool.start", "message.interim"}:
            with self.interaction.lock:
                if backend.status != "ambiguous":
                    backend.status = "working"
            if backend.status != "ambiguous":
                self.store.update_run(idem, None, "working")
            if kind == "message.interim":
                progress = interim_progress(event)
                if progress:
                    self.store.user_progress(idem, *progress)
                    self.store.progress(idem, "milestone", progress[1])
                    self.record_activity(backend, progress[1])
                    if not await self.maybe_speak_progress(backend, progress[0]):
                        await self.maybe_note_status(backend, progress[1])
            elif backend.status != "ambiguous":
                derived = derive_tool_status(event.get("tool"), event.get("preview"))
                if derived:
                    self.record_activity(backend, derived[1])
                    self.store.tool_progress(idem, *derived)
        elif kind == "approval.request":
            request_id = event.get("request_id")
            if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
                return
            description = safe_user_text(event.get("description") or event.get("reason"), 500)
            safe = {"request_id": request_id, "description": description or "Consequential action",
                    "choices": ["once", "deny"]}
            with self.interaction.lock:
                if backend.status == "ambiguous":
                    return
                backend.approval, backend.status = safe, "waiting_for_approval"
            self.store.update_run(idem, None, "waiting_for_approval")
            self.store.progress(idem, "milestone", "Waiting for your approval")
            if not self.call_connected():
                self.notices.needs_you(request_id, safe["description"])
            await self.append("session.commentary.append", delegation_id, P.approval_note(self.names))
        elif kind and kind.startswith("run."):
            status = kind.split(".", 1)[1]
            if status in TERMINAL and not backend.run_id:
                # A task that ran in a Discord/Telegram thread has no Hermes run id, and review cards,
                # image routes, dismissals and "show" are all addressed by one: give it a local id
                # so its pictures are not silently dropped.
                local = LOCAL_RUN_PREFIX + hashlib.sha256(idem.encode()).hexdigest()[:32]
                with self.interaction.lock:
                    backend.run_id = local
                self.store.update_run(idem, local, status)
            else:
                self.store.update_run(idem, None, status)
            if status not in TERMINAL:
                return
            output, error = event.get("output"), event.get("error")
            if status == "failed" and (line := failures.provider_line(output)):
                # The session chat stream (and its run status) carry no error field: a failed turn's
                # reply is Hermes' failure message quoting the provider. Keep only what it means.
                output, error = None, error or line
            result = split_result(output, self.rt.image_roots())
            drafts = (result or {}).pop("_email_drafts", None) or []
            self.store.set_result(idem, result)
            failed_as = None  # a failures.py kind
            if status == "failed":
                # Hermes' error line maps to Speakeasy's own sentence; with neither a known reason nor
                # an answer, the app points at Hermes' errors.log instead.
                failed_as = failures.failure_kind(error) or (None if result else failures.UNKNOWN)
                self.store.set_failure(idem, failed_as)
                if failed_as in failures.REASONS:
                    logger.info("speakeasy: Hermes run %s failed (%s)", backend.run_id, failed_as)
                else:
                    # WARNING so it lands in errors.log, where the app sends the user for the details.
                    logger.warning("speakeasy: Hermes run %s failed, reason not recognized: %s", backend.run_id,
                                   str(error or "no error given")[:200])
            reason = failures.reason_text(failed_as)
            safe_output = safe_user_text(result["full"], 4000) if result else None
            self.store.progress(idem, "result", safe_output or (f"Work failed: {reason}" if reason else {
                "completed": "Work complete", "cancelled": "Work stopped", "failed": "Work failed",
                "interrupted": "Work interrupted"}.get(status, "Work ended")))
            with self.interaction.lock:
                backend.status, backend.approval = status, None
            stored_drafts = [self.store.add_draft(idem, self.store.session_for(idem), d) for d in drafts]
            with self.interaction.lock:
                wanted = idem in self.show_pending
                self.show_pending.discard(idem)
            if (wanted or self.call_connected()) and (self.store.work(idem_key=idem) or {}).get("review"):
                # They asked to see it, or the answer came with a picture while they're on the call:
                # put it up instead of waiting to be asked.
                self.show_action(backend.run_id, backend.delegation_id, "review")
            head = self.interaction.head()
            with head.lock:
                latest = head.runs.get(head.latest_delegation_id or "")
                is_latest = latest is not None and latest.say_id == delegation_id
                siblings = [r for r in head.runs.values()
                            if r is not backend and r.say_id == delegation_id and r.status in ACTIVE_RUN_STATES]
            if status != "completed" and backend.run_id and not backend.run_id.startswith(LOCAL_RUN_PREFIX):
                self.notices.stopped(backend.run_id, status, self.store.title(idem) or self.store.request_text(idem),
                                     reason)
            if status == "completed" and backend.run_id and result and not event.get("continued"):
                self.notices.answered(backend.run_id, delivery_text(result), backend.deliver_to)
                self.notices.pointer(backend.run_id, self.store.title(idem), backend.deliver_to)
            elif status == "completed" and result and event.get("thread_target"):
                self.notices.commands(backend.run_id or idem, result.get("full"), event["thread_target"])
                self.notices.pointer(backend.run_id or idem, self.store.title(idem), event.get("pointer_target"))
            for draft in stored_drafts:
                if not self.call_connected():
                    self.notices.draft_waiting(draft["draft_id"], draft.get("subject"))
            if status == "completed":
                spoken = result["spoken"] if result else "Done."
                for note in P.result_notes(self.store.title(idem) or self.store.request_text(idem),
                                           result.get("full") if result else None, spoken):
                    await self.append("session.thinking.append", delegation_id, note)
                # The answer text says "here's the building" either way; tell the voice what the user
                # can actually see, so it never claims a picture is up when none reached the app.
                pictures = len(((self.store.work(idem_key=idem) or {}).get("review") or {}).get("images") or [])
                await self.append("session.thinking.append", delegation_id, P.pictures_note(pictures))
                if stored_drafts:
                    d = stored_drafts[-1]
                    await self.append("session.thinking.append", delegation_id,
                                      P.draft_waiting_note(self.names, d.get("subject"), d.get("to") or []))
                self.replied_task = backend.delegation_id
                asked = notice_text(self.store.request_text(idem), 140) or "an earlier request"
                moved_on = self.user_spoke_since(time.monotonic() - LATE_RESULT_S) and \
                    time.monotonic() - backend.started > LATE_RESULT_S
                if is_latest and backend.voice_id:
                    part = notice_text(self.store.request_text(idem), 140) or "one part"
                    await self.append("session.commentary.append", delegation_id,
                                      P.result_for_part(part, spoken, bool(siblings)))
                elif is_latest and moved_on:
                    await self.append("session.commentary.append", delegation_id, P.late_result(asked, spoken))
                elif is_latest:
                    await self.append("session.commentary.append", delegation_id, spoken)
                else:
                    request = notice_text(self.store.request_text(idem), 140) or "an earlier request"
                    await self.append("session.commentary.append", delegation_id,
                                      P.earlier_task_finished(self.names, request, spoken))
            elif status == "cancelled":
                await self.append("session.commentary.append", delegation_id, P.STOPPED_SPOKEN)
            elif reason:
                # Spoken: the same reason without the command (that's on screen and in chat).
                await self.append("session.commentary.append", delegation_id,
                                  P.failed_because(failures.voice_text(failed_as)))
            elif failed_as == failures.UNKNOWN:
                await self.append("session.commentary.append", delegation_id, P.FAILED_SPOKEN)
            else:
                await self.append("session.commentary.append", delegation_id, P.ended_without_result(status))


LATE_RESULT_S = 20.0  # the user spoke in the last 20 s of a task that took longer: say what it answers


def new_event_id() -> str:
    return "speakeasy_" + secrets.token_hex(12)
