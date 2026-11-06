"""One voice call: its live state, the SSE event feed, chat notices, and the sideband worker that
turns the voice model's handoffs into Hermes tasks and speaks their results back.

Provider-neutral: ``openai_live.OpenAISidebandWorker`` and ``codex_transport.CodexSidebandWorker``
only implement ``run()`` (read provider events) and ``_send()`` (write context to the call).
"""
from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
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

from . import channels, continuity, failures, liveness, router, threads
from . import messages as messages_mod
from .devices import client_label
from .duck import Ducker
from . import home_control as home_control_mod
from . import instant as instant_mod
from . import fence as fence_mod
from . import jev as jev_mod
from . import receipts
from . import skills as skills_mod
from . import session_titles
from . import quick as quick_mod
from . import views as views_mod
from . import activity as activity_mod
from . import subagents as subagents_mod
from . import attachments as A
from .attachments import Attachment, CallAttachments
from .hermes_api import HermesError, user_content
from .settings import DEFAULT_UPDATE_INTERVAL_S, valid_delivery_target
from .prompt import builder as P
from . import text as text_mod
from .text import (ID_RE, MAX_TRANSCRIPT, TERMINAL, clean_transcript, delivery_text, derive_tool_status,
                   interim_progress, live_images_in, notice_text, safe_user_text, short_title, home_title, split_result,
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
ECHO_WINDOW_S = 8.0    # a second handoff this soon that only repeats the last request's words is dropped
DUPLICATE_WINDOW_S = 15.0  # the very same words handed off again this soon start nothing new
CORRECTION_WINDOW_S = 30.0  # "guest room, sorry" this soon after a request replaces it
ACK_GRACE_S = 5.0      # the voice said "on it": a task must exist (or start) within this long
# The card's step for an approval answered from the call (the panel's buttons record the same words).
APPROVED_MILESTONE = {"once": "Approved", "session": "Approved for the rest of this task", "deny": "Approval denied"}
FOLLOW_UP_POLL_S = 2.0 # how often a queued follow-up is looked for in the task's transcript (receipts.py)
FOLLOW_UP_SETTLE_S = 3.0  # after the run ends, how long its transcript gets to show a follow-up it read
FENCE_POLL_S = 2.0     # how often a task held behind another (same project) checks on it
FENCE_MAX_WAIT_S = 4 * 3600  # never held longer than this: it then starts and says so


LOOP_GONE_POLL_S = 0.5
# Set when the interpreter starts shutting down, before concurrent.futures joins its workers (threading
# atexit hooks run newest first): a stream thread still waiting on a call's loop then lets go.
_EXITING = threading.Event()
try:
    threading._register_atexit(_EXITING.set)  # type: ignore[attr-defined]
except (AttributeError, RuntimeError):  # not CPython, or already shutting down
    pass


def on_loop(coro: Any, loop: asyncio.AbstractEventLoop) -> Any:
    """Run ``coro`` on the call's ``loop`` from a worker thread (a Hermes event stream) and wait for it.

    A bare ``run_coroutine_threadsafe(...).result()`` waits forever when the loop stops first (the typed
    worker's loop is stopped on service close) or never gets to the coroutine: the stream's thread, a
    non-daemon asyncio executor worker, then blocks interpreter exit (``concurrent.futures`` joins
    every executor worker at exit), which is how the test suite hung after its last test. This gives
    up with RuntimeError once the loop isn't running, which ends that stream like any other broken
    stream, and at once when Python is exiting, so a gateway shutdown never waits on a call's work."""
    if _EXITING.is_set() or loop.is_closed() or not loop.is_running():
        coro.close()
        raise RuntimeError("the call's event loop stopped")
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except RuntimeError:  # the loop closed just now
        coro.close()
        raise
    while True:
        try:
            return future.result(LOOP_GONE_POLL_S)  # short waits: _EXITING and a stopped loop are noticed
        except concurrent.futures.TimeoutError:
            if _EXITING.is_set() or loop.is_closed() or not loop.is_running():
                future.cancel()
                raise RuntimeError("the call's event loop stopped") from None


def _same_words(fragment: str, request: str) -> bool:
    """The transcript of words already in the request (it often lands just after the handoff)."""
    f = _words(fragment)
    return not f or f in _words(request)


def _log_follow_up_failure(task: "asyncio.Task[Any]") -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("speakeasy: an answered question's follow-up failed", exc_info=task.exception())


CLOSING_OFFER_RE = re.compile(r"(?i)^\W*(?:so\W+)?(?:want me to|do you want me to|should i|would you like me to|shall i)\b")


def closing_offer(said: str, question: str) -> bool:
    """The voice ended a substantial answer with an offer of what to do next ("Want me to hold until
    the tests finish, or walk through the fix?"). That is conversation, answered by talking; a card
    for it (and a "Decide on holding" task in Done) is noise. A real pick asked on its own still
    becomes a card."""
    before = said.rsplit(question.strip()[:40], 1)[0] if question.strip() else said
    return bool(CLOSING_OFFER_RE.match(question or "")) and len(before.strip()) >= 160


def _words(text: str) -> str:
    """Just the words, lowercased: to tell whether the same thing was said again."""
    return " ".join(re.findall(r"[a-z0-9']+", (text or "").lower()))


def _with_request(context: str, request: str) -> str:
    """The transcript with its last user turn(s) put as the request that actually goes (a correction
    applied, or words said just after the handoff), so the task reads one clean request."""
    lines = context.splitlines()
    while lines and lines[-1].startswith("User: "):
        lines.pop()
    return "\n".join(lines + [f"User: {request}"])


def typed_text(text: str) -> str:
    """A typed turn as one clean line: control characters and runs of whitespace become one space."""
    return " ".join(_CONTROL_CHARS_RE.sub(" ", text).split())


def is_greeting(request: str) -> bool:
    return bool(GREETING_RE.match(request or ""))


def whole_request(context: str, started: "set[str] | None" = None) -> str:
    """The user's last request, whole. Speech often lands as two transcript lines with nothing said
    in between ("Bedroom lamps at forty percent" / "purple"); handing on only the last scrap lost the
    rest. Consecutive user lines at the end are joined when the final one is short. A line that was
    already its own request (``started``: their words) is never joined on again: "Turn on the lamps"
    after a lamp command that already ran is a new request, not a replay of the old one."""
    lines = context.splitlines()
    picked: list[str] = []
    for line in reversed(lines):
        if line.startswith("User: "):
            said = line[6:].strip()
            if picked and started and _words(said) and any(_words(said) in s for s in started):
                break
            if picked and _words(said) and set(_words(said).split()) <= set(_words(" ".join(picked)).split()):
                continue  # the same words said twice ("what work... Yeah, what work...") count once
            picked.append(said)
            if len(picked) >= 3 or len(picked[0].split()) > 4:
                break
            continue
        if line.startswith("Assistant: ") or picked:
            break
    if not picked:
        return ""
    if len(picked[0].split()) > 4 or router.is_status_question(picked[0]):
        return picked[0]  # "how's that coming along" is its own question, never the end of the last ask
    return " ".join(reversed(picked)).strip()

TASK_SESSION_PREFIX = "speakeasy_task_"


def task_session_id(idem: str) -> str:
    """The Hermes session minted for one task (its own, so tasks run side by side)."""
    return TASK_SESSION_PREFIX + hashlib.sha256(idem.encode()).hexdigest()[:24]

LOCAL_RUN_PREFIX = "lr_"   # ids for tasks Hermes ran without a run id (thread tasks)
EARLY_PREFIX = "early_"    # handoffs for words said while the call was still connecting
EARLY_MAX_CHARS = 1000
# A turn typed on the call screen instead of spoken (POST /voice/interactions/{id}/input-text).
INPUT_TEXT_TYPE = "session.input_text"
MAX_INPUT_TEXT = 1000
# Codex echo guard: only a provider transcript arriving this soon after the voice took a typed turn
# can be that turn's echo. Short on purpose: a stale guard would swallow the user saying the same
# words out loud later, which loses a real turn; a missed echo only shows the turn twice.
TYPED_ECHO_S = 6.0
# What one typed turn's delivery came to (SidebandWorker.input_text / forward_typed).
STEER_RETRIES = 4                    # a just-admitted run refusing guidance: ask again (steer_follow_up)
STEER_RETRY_S = 1.5
TYPED_SENT = "sent"                   # the voice took it; it is in the transcript
TYPED_NOT_CONNECTED = "not_connected"  # the call (or its voice connection) is gone
TYPED_NOT_DELIVERED = "not_delivered"  # the call is up but this one send failed; nothing recorded
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
MAX_TASKS = 8
# Earlier calls' tasks kept on a call's board until cleared (running, waiting on the user, finished).
MAX_BOARD = 12
ACTIVE_RUN_STATES = {"admitting", "running", "working", "waiting_for_approval", "resolving_approval",
                     "cancel_requested", "waiting_on_task"}
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
# The call pulse: while a task runs and the call has been quiet this long since the assistant last
# spoke, it says one short line about what the task is doing (its real tool calls and findings),
# so a call never goes minutes without a word. After PULSE_IDLE_AFTER updates in a row with nothing
# new on a task, that task waits PULSE_IDLE_EVERY_S instead. Never while the user is muted, while
# anyone is talking (no transcript for PULSE_QUIET_S), or while a question or approval waits.
PULSE_EVERY_S = PROGRESS_EVERY_S
PULSE_IDLE_EVERY_S = 90.0
PULSE_IDLE_AFTER = 3
PULSE_TICK_S = 5.0
PULSE_QUIET_S = 6.0
# With the mic open (the app reported muted=false) the user is listening in, so a task's first line
# lands PULSE_FIRST_OPEN_S into it instead of PULSE_EVERY_S, and there is no PULSE_IDLE_EVERY_S
# back-off: where a muted call would go quiet, the open mic hears the running step tied to the task's
# age ("Still on reading the logs, about 3 minutes in"), never word for word a recent line, and every
# open-mic line is at most PULSE_MAX_WORDS long. Muted or never reported keeps the rule above exactly.
PULSE_FIRST_OPEN_S = 12.0
PULSE_MAX_WORDS = 22
# Openers for an open-mic line whose wording came out identical to one already said; once those are
# used up too, the anchored shapes (the step plus the task's age, which moves on between lines). An
# open-mic line is compared with the last PULSE_SAID_KEEP of them (BackendRun.pulse_said), so a
# wording model that keeps saying the same thing never gets the same words out twice in a long run.
PULSE_SAID_KEEP = 20
_PULSE_OPENERS = ("Meanwhile", "Right now", "Continuing")
_OPENER_LEAD = re.compile(r"(?i)^(?:(?:quick update|meanwhile|right now|continuing)\s*[:,]\s*)+")
_ANCHORED_SHAPES = ("Still on {step}, {age} in.", "{Age} in, still {step}.", "{Step}, still going, {age} in.")
_ANCHORED_STEP_WORDS = 12
# Never talk over a thought in progress: a result, a task's question or a pulse waits until the user has
# paused USER_PAUSE_S (UNFINISHED_PAUSE_S when their last words trailed off, "...because he"), or
# until a "wait, I wasn't done" hold (HOLD_S) ends; never longer than RESULT_HOLD_MAX_S in all.
USER_PAUSE_S = 2.5
UNFINISHED_PAUSE_S = 12.0
HOLD_S = 25.0
RESULT_HOLD_MAX_S = 45.0
UNDO_WINDOW_S = 60.0       # "no, don't add that" this soon after something went out takes it back
PENDING_THOUGHT_S = 90.0   # a trailing-off request held for "Want me to send that?" this long


def cap_words(line: str, limit: int = PULSE_MAX_WORDS) -> str:
    """At most `limit` words: end on the last whole sentence inside the cap when that keeps enough
    of it, otherwise cut and trail off."""
    words = line.split()
    if len(words) <= limit:
        return line
    head = " ".join(words[:limit])
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    if cut >= 0 and len(head[:cut + 1].split()) >= limit // 3:
        return head[:cut + 1]
    if head.endswith((".", "!", "?", "\u2026")):
        return head  # the kept words already end a sentence: no trailing "…" after it
    return head.rstrip(" ,;:-\u2014") + "\u2026"


def _said_key(line: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", line.lower()).split())


_I_WORD = re.compile(r"^I(?:\b|['\u2019])")
_FIRST_WORD = re.compile(r"[A-Za-z][\w.-]*")
# Names that keep their capital mid-sentence ("still on Stripe webhook", never "still on stripe
# webhook"), lowercased. Words the task's own request or title capitalizes mid-sentence are added
# per line (proper_nouns), so this only has to cover the common ones that may open a request.
_PROPER_NOUNS = frozenset("""
    airtable amazon android apple asana aws azure bluesky chatgpt chrome claude cloudflare codex discord
    docker dropbox facebook figma firebase firefox gemini github gitlab gmail google grok hermes
    heroku icloud instagram ios ipad iphone jira kubernetes linkedin linux mac macos microsoft
    netlify nginx notion openai outlook postgres python reddit safari salesforce shopify slack
    speakeasy spotify sqlite stripe supabase tailscale telegram tesla twitter vercel whatsapp
    windows xcode youtube monday tuesday wednesday thursday friday saturday sunday january february
    april june july august september october november december
""".split())


def proper_nouns(*texts: str | None) -> frozenset[str]:
    """Words these texts capitalize where a sentence doesn't start ("ask Dana about the Q3 deck"
    gives Dana, Q3): they are names, and keep their capital when a line opens with them."""
    out: set[str] = set()
    for text in texts:
        for sentence in re.split(r"(?<=[.!?:;\n])\s+", str(text or "")):
            for word in re.findall(r"[A-Za-z][\w-]*", sentence)[1:]:
                if word[:1].isupper():
                    out.add(word.lower())
    return frozenset(out)


def _lead_lower(text: str, keep: frozenset[str] = frozenset()) -> str:
    """`text` with its first letter lowercased for use mid-sentence, unless that first word is "I"
    ("I'm", "I've"), an acronym or a mixed-case name ("API", "GitHub"), or a known name (_PROPER_NOUNS,
    or `keep`, see proper_nouns), which keep their capital."""
    if not text or _I_WORD.match(text) or not text[:2].istitle():
        return text
    first = _FIRST_WORD.match(text)
    word = first.group(0).rstrip(".-") if first else ""
    if any(c.isupper() for c in word[1:]) or word.lower() in _PROPER_NOUNS or word.lower() in keep:
        return text
    return text[:1].lower() + text[1:]


def _age_phrase(age: float) -> str:
    if age < 45:
        return "under a minute"
    if age < 90:
        return "about a minute"
    return f"about {int(age / 60 + 0.5)} minutes"  # half up: 150 s is about 3, never banker's 2


def anchored_lines(step: str, age: float, reserve: int = 0, keep: frozenset[str] = frozenset(),
                   every: float | None = None) -> list[str]:
    """The running step tied to how long the task has run ("Still on reading the logs, about 3
    minutes in"), in every shape, starting from the one this pulse slot rotates to. `reserve` words
    are kept free for what goes in front of the line later (the job's name when several are running),
    so the step is shortened to fit and the age, the part that moves on between lines, is never the
    part the word cap cuts off. `keep`: names that keep their capital (see _lead_lower); `every`:
    the call's spoken-update interval, which sets how often the shape turns (PULSE_EVERY_S)."""
    step = re.sub(r"(?i)^quick update:\s*", "", " ".join(str(step or "").split())).rstrip(" .\u2026!?,;:")
    words = step.split() or ["it"]
    phrase = _age_phrase(age)
    lines = []
    for shape in _ANCHORED_SHAPES:
        fixed = len(shape.format(step="", Step="", age=phrase, Age=phrase).split())
        room = max(3, min(_ANCHORED_STEP_WORDS, PULSE_MAX_WORDS - reserve - fixed))
        short = " ".join(words[:room])
        lines.append(cap_words(shape.format(step=_lead_lower(short, keep), Step=short[:1].upper() + short[1:],
                                            age=phrase, Age=phrase[:1].upper() + phrase[1:])))
    turn = int(max(age, 0.0) // (every or PULSE_EVERY_S)) % len(lines)
    return lines[turn:] + lines[:turn]


def unrepeated(line: str | None, told: list[str] | list[tuple[float, str]], step: str, age: float,
               dress: Callable[[str], str] = cap_words, reserve: int = 0,
               keep: frozenset[str] = frozenset(), every: float | None = None) -> str:
    """An open-mic pulse line that is not word for word one already spoken on this task (`told`,
    oldest first, or ``(when, line)`` pairs in any order, which are put in time order):
    `line`, else `line` re-worded behind an opener, else the anchored lines on `step` (see
    anchored_lines). Every candidate goes through `dress` (naming the job, the word cap) before it is
    compared, since that is what was said; `reserve` is how many words `dress` may put in front (see
    anchored_lines). Never None: should all of them have been said, the one said
    longest ago comes back, because an old line beats silence."""
    if told and not isinstance(told[0], str):
        told = [t for _, t in sorted(told, key=lambda pair: pair[0])]
    said = [_said_key(t) for t in told]
    candidates: list[str] = []
    if line:
        candidates.append(line)
        body = _OPENER_LEAD.sub("", line.strip()) or line.strip()  # never "Meanwhile, meanwhile, …"
        body = _lead_lower(body, keep)
        if not body.endswith((".", "!", "?", "\u2026")):
            body += "."
        candidates += [f"{opener}, {body}" for opener in _PULSE_OPENERS]
    candidates = [dress(c) for c in candidates + anchored_lines(step, age, reserve, keep, every)]
    for candidate in candidates:
        if _said_key(candidate) not in said:
            return candidate
    # Everything was said within the last few lines: the one said longest ago.
    return min(candidates, key=lambda c: max(i for i, key in enumerate(said) if key == _said_key(c)))


class ServiceError(Exception):
    def __init__(self, status: int, message: str, reason: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.reason = reason  # a short code the app maps to its own line (attachment refusals)


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
    plan_call: Callable[[str], list[str] | None] | None = None
    shape_call: Callable[[str, str], dict[str, Any] | None] | None = None
    progress_call: Callable[..., str | None] | None = None
    # Instant home control (home_control.HomeControl); None or turned off = every request goes to Hermes.
    home: Any = None
    # Quick answers (quick.answer); None = the real one. Tests swap in a fake.
    quick_call: Callable[[str, str], str | None] | None = None
    # The local log of ended calls that "Tune from my calls" learns from (tune.CallLog); None = not kept.
    call_log: Any = None
    # Lowers the Mac's volume while the call is speaking (duck.Ducker); None = never.
    duck: Ducker | None = None
    # The device-wide history event feed (service.HistoryEvents); None = nobody listening.
    history_events: Any = None
    # Speakeasy's copies of shared files (attachments.SharedFolder), and the call that deletes copies
    # nothing uses any more (no task records them, no live call holds them). None = not kept.
    shared: Any = None
    discard_files: Callable[[list[str]], int] | None = None
    # Phone notifications while the app is closed (push.PushNotifier); None = never.
    push: Any = None
    # The model/reasoning a run starts with (task_id, idem key, session id, request) -> Hermes'
    # per-request overrides (models.run_fields), or None for its own config. None = never chosen.
    choose_model: Callable[[str, str, str | None, str | None], dict[str, Any] | None] | None = None

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
              replied_task_id: str | None = None, focus: router.Focus | None = None) -> router.Decision:
        settings = self.routable()
        topics = ([router.Topic(c.label, c.topic) for c in channels.opted_in(settings)]
                  if channels.mode(settings) == "topic" else [])
        provider = (self.settings().get("fast_routing") or {}).get("jev") or ""
        place = None
        if provider:
            from . import jev
            place = functools.partial(jev.placement, self.hermes_home, provider)
        return router.decide(request, tasks, marked, topics, self.route_call, chats=chats, call_so_far=call_so_far,
                             replied_task_id=replied_task_id, jev_place=place, focus=focus)

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
    pulse_told: set[int] = dataclasses.field(default_factory=set)  # activity.py items an update covered
    pulse_idle: int = 0             # pulse updates in a row with nothing new to say
    # Every update spoken on this task (pulse or progress) as (monotonic time, line), the open mic's
    # no-repeat memory: in time order, so "the one said longest ago" really is.
    pulse_said: list[tuple[float, str]] = dataclasses.field(default_factory=list)
    # Same-project fence (fence.py): the projects this task names or touched, whether it is known to
    # edit them, and, for a task held behind another, the blocker and whether the user said go anyway.
    projects: set[str] = dataclasses.field(default_factory=set)
    edits: bool = False
    blocked_by: str | None = None
    released: bool = False
    held_words: list[str] = dataclasses.field(default_factory=list)  # added while it waited to start

    @property
    def say_id(self) -> str:
        return self.voice_id or self.delegation_id


@dataclasses.dataclass
class Interaction:
    interaction_id: str
    live_session_id: str
    talking_only: bool = False  # "just talk to me": hold work until they ask for some
    # Vision Pro: the task the user is facing or holding, and what part of it ("day two of the draft").
    focus_task_id: str | None = None
    focus_detail: str = ""
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
    idle_paused_minutes: int | None = None  # paused for quiet after this many minutes (not by the user)
    mic_muted: bool | None = None  # the app's mic is off (POST .../mic), None = never said; only the pulse cares
    pause_bookkept: bool = False
    history: list[dict[str, str]] = dataclasses.field(default_factory=list)
    resumed_from: str | None = None
    device_id: str = ""
    # The app build that started this call (devices.parse_client fields; {} = an older app that
    # doesn't say), so a call in the log can be matched to the build that made it.
    client: dict[str, str] = dataclasses.field(default_factory=dict)
    # The call's app runs on the Mac that runs Hermes (a direct loopback request): only then is the
    # Mac's volume the call's own to duck.
    local: bool = False
    # Listening mode: what the Mac heard in the room before this call (cleaned "[HH:MM] text" lines).
    # Memory only, never in history, logs or reprs; it moves to a resumed call and is cleared when
    # the call ends (or a pause runs out). Empty = an ordinary call.
    room: str = dataclasses.field(default="", repr=False)
    # Listening mode: the voice already responded from the room on this call (any leg of it).
    room_nudged: bool = False
    last_activity: float = dataclasses.field(default_factory=time.monotonic)
    successor: "Interaction | None" = dataclasses.field(default=None, repr=False)
    # A later call (not a resume) that took on this ended or paused call's open tasks: their results,
    # approvals and follow-ups go there (head()), and carried_keys on it names the tasks it took on.
    carried_by: "Interaction | None" = dataclasses.field(default=None, repr=False)
    carried_keys: set[str] = dataclasses.field(default_factory=set, repr=False)
    worker: Any = dataclasses.field(default=None, repr=False)
    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)
    feed: EventFeed = dataclasses.field(default_factory=EventFeed, repr=False)
    # What the client can share, the screen toggle, pending pictures and files, capture requests.
    # A resumed call starts with a fresh one (sharing off, nothing pending); the app re-sends.
    attachments: CallAttachments = dataclasses.field(default_factory=CallAttachments, repr=False)
    # A request waiting for screen sharing to come on (one at a time), as the app shows it:
    # {"state": "waiting" | "not_sent", "text": its line, "since": epoch seconds}; "not_sent" clears
    # after HOLD_OUTCOME_S. Set by the worker (hold_request, end_hold), never mutated in place.
    hold: dict[str, Any] | None = None

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
                # Paused for quiet (idle_pause_minutes), not by the user: "Paused after N min of quiet".
                **({"idle_paused_minutes": self.idle_paused_minutes}
                   if self.paused and self.idle_paused_minutes else {}),
                "resumed_from": self.resumed_from,
                **self.attachments.snapshot(),  # screen, captures, attachments
                "hold": self.hold,
            }

    def head(self) -> "Interaction":
        """The newest call in this Pause/Resume chain, or the later call that took on its tasks
        (results are routed there)."""
        current, seen = self, {id(self)}
        while (nxt := current.successor or current.carried_by) is not None and id(nxt) not in seen:
            seen.add(id(nxt))
            current = nxt
        return current


def interaction_tasks(store: Any, interaction: Interaction, assistant_name: str = "Hermes") -> list[dict[str, Any]]:
    """Every voice task of this call (parallel runs, oldest first), as work objects + task_id, then
    earlier calls' tasks that stay on the board until cleared (the workspace outlives a call)."""
    with interaction.lock:
        runs = [(r.delegation_id, r.idem_key, r.run_id, r.status) for r in interaction.runs.values()]
        carried = set(interaction.carried_keys)
    # Its own tasks are capped at MAX_TASKS; tasks taken on from an earlier call count separately.
    own = [r for r in runs if r[1] not in carried][-MAX_TASKS:]
    runs = [r for r in runs if r[1] in carried][-MAX_BOARD:] + own
    tasks = []
    for delegation_id, key, run_id, status in runs:
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
        if key in carried:
            work["carried"] = True  # an earlier call's task: not an answer of this conversation
        tasks.append(work)
    exclude = {t["task_id"] for t in tasks} | {r[1] for r in runs} | {r[0] for r in runs}
    # Earlier calls' tasks this call doesn't hold (e.g. after a restart) stay on the board too.
    earlier = store.board_carry(exclude, MAX_BOARD) if hasattr(store, "board_carry") else []
    exclude |= {t["task_id"] for t in earlier}
    # A draft from an earlier call still waiting for Send / Deny / Revise stays on screen.
    return earlier + tasks + store.carried_draft_tasks(exclude)


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
                        "description": pending.get("description"),
                        "choices": [c for c in (pending.get("choices") or ["once", "deny"]) if c in {"once", "session", "deny"}]}
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


# -- look at this: what a handoff carries besides its words ----------------------------------------

KEEP_CAPTURE_S = 60.0  # a request that asked a question first: its capture waits this long for the answer
KEPT_ANSWER_WORDS = 6  # ... which is a short reply that doesn't point at the screen itself
HOLD_S = 30.0          # a request that needs the screen waits this long for sharing to come on
RELEASED_REPEAT_S = 10.0  # once it went, asking to look again this soon is the same request
HOLD_OUTCOME_S = 8.0   # ... and its "Not sent" line stays this long once it ended unsent
STOP_HEARD_SETTLE_S = 0.6  # "stop looking at my screen" heard in the transcript: act once the words settle
STOP_WINDOW_S = 8.0    # the transcript words a stop phrase is looked for in
STOP_DEBOUNCE_S = 15.0 # heard and handed off for the same words: one confirmation


def _base(task_id: str) -> str:
    """The handoff a task belongs to: the parts of a split request are "<handoff>-p<n>"."""
    return re.sub(r"-p\d+$", "", task_id)


@dataclasses.dataclass
class Shared:
    """What one handoff carries to Hermes besides its words: the pictures and files dropped on the
    panel, the screen capture taken for it, and why a capture didn't come. Image bytes live only here
    and in the run's input; ``saved`` maps each image to Speakeasy's copy once saved (the parts of a
    split request share one Shared, so one copy)."""
    pictures: list[Attachment] = dataclasses.field(default_factory=list)
    files: list[Attachment] = dataclasses.field(default_factory=list)
    capture: Attachment | None = None
    missing: str | None = None  # why there's no capture: a CaptureResult reason, or "budget"
    at: float = dataclasses.field(default_factory=time.time)
    saved: dict[str, str] = dataclasses.field(default_factory=dict, repr=False)

    @property
    def images(self) -> list[Attachment]:
        return ([self.capture] if self.capture else []) + self.pictures

    @property
    def things(self) -> list[Attachment]:
        return self.images + self.files

    def __bool__(self) -> bool:
        return bool(self.capture or self.pictures or self.files)

    def forget_bytes(self) -> None:
        """The run has the images now: don't hold them in memory for as long as it works."""
        for image in self.images:
            image.data = None


@dataclasses.dataclass
class Held:
    """A request that needs the user's screen, waiting (HOLD_S) for sharing to come on; one per call.
    It holds nothing the user shared: the handoff gave everything back when it was held, and binds
    again (with a fresh capture) once it goes."""
    delegation_id: str
    revision: int
    context: str
    marked: Any
    request: str               # the whole request, settled
    since: float = dataclasses.field(default_factory=time.time)  # epoch seconds, as the snapshot shows it
    token: int = 0


def _fits(images: list[Attachment]) -> bool:
    """Inside one request's image budget (read at call time from attachments.py)."""
    sizes = [len(a.data or b"") for a in images]
    return (len(sizes) <= A.MAX_REQUEST_IMAGES and all(s <= A.MAX_IMAGE_BYTES for s in sizes)
            and sum(sizes) <= A.MAX_REQUEST_IMAGE_BYTES)


def _image_filename(image: Attachment) -> str:
    """A saved image's name: what it shows, with the extension of what it is (the Mac re-encodes
    every image, so a dropped picture's own name may carry another one)."""
    stem = (f"{image.app} window" if image.app else "Screen") if image.kind == "screen" else \
        (Path(image.name).stem if image.name else "Picture")
    return stem + (".png" if image.mime == "image/png" else ".jpg")


def _data_url(image: Attachment) -> str:
    return f"data:{image.mime};base64," + base64.b64encode(image.data or b"").decode("ascii")


def end_shared(rt: Runtime, interaction: Interaction, reason: str) -> None:
    """The call ended or paused: no more capture requests or uploads. Pictures and files that were
    only waiting on the panel are dropped (the app re-sends them to a resumed call), and so are
    Speakeasy's copies of those files unless a task or another call still uses them. A request
    waiting for the screen ends unsent."""
    dropped = interaction.attachments.end(reason)
    if interaction.worker is not None:
        interaction.worker.end_hold(reason)
    paths = [a.path for a in dropped if a.path]
    if paths and rt.discard_files is not None:
        try:
            rt.discard_files(paths)
        except Exception as exc:
            logger.warning("speakeasy: could not delete unsent shared files: %s", type(exc).__name__)


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
    # Codex's voice protocol delivers every appended note as something to say, so a "don't say
    # this" note still gets a spoken reply: "Mm-hmm." Quiet notes are held and ride along with the
    # next message the voice is meant to answer.
    HOLD_QUIET_NOTES = False
    """Base worker. Subclasses read provider events in ``run()`` and write in ``_send()``."""

    def __init__(self, rt: Runtime, interaction: Interaction):
        self.rt, self.store, self.hermes, self.interaction = rt, rt.store, rt.hermes, interaction
        self.notices = rt.notices
        self.fragments: deque[dict[str, Any]] = deque(maxlen=512)
        self.absorbed: list[tuple[str, float]] = []  # words folded into an earlier task by settle()
        self.recent_requests: list[tuple[str, float]] = []  # requests started this call, for echo checks
        self.recent_answers: dict[tuple[str, str], tuple[str, float]] = {}  # tapped answers, for double sends
        self.handoff_at: dict[str, float] = {}   # delegation id -> when the handoff arrived (monotonic)
        # Requests that went on as work this call: (words, when, delegation id, the request), newest last.
        self.started: list[tuple[str, float, str, str]] = []
        self.last_handoff_at = float("-inf")     # when the newest handoff (or tapped answer) arrived
        self.drops: list[tuple[str, float, str, str]] = []  # (reason, when, request, context) not started
        self.ack_checked = float("-inf")         # the user turn whose "on it" was last checked
        self.force_work: set[str] = set()        # handoffs started because the voice promised the work
        self.quiet_notes: dict[str, str] = {}    # held background notes, newest per kind
        self.show_pending: set[str] = set()      # task keys asked for a screenshot by "show me"
        # Home control: the question just asked (original request, question, when) and answers given
        # on this call, so "the thermostat" means the den until hang-up once they've said so.
        self.home_question: tuple[str, str, float] | None = None
        # Tasks whose question the user set aside on this call (never asked again).
        self.set_aside_tasks: set[str] = set()
        self.home_notes: list[str] = []
        self.home_tasks: set[str] = set()        # task ids that were instant home commands
        # Choices the voice offered out loud, shown as question cards (task id -> question asked).
        self.choice_tasks: dict[str, str] = {}
        self.choice_answered: set[str] = set()
        self.offered_choices: set[frozenset[str]] = set()
        self.replied_task: str | None = None     # the task the voice last spoke an answer from
        self.wants_show: set[str] = set()        # delegation ids the user wants to SEE (routing said show)
        self.show_seq = 0
        self.route_timings: dict[str, int] = {}  # task id -> routing latency (ms), stored with the task
        # Look at this, per handoff (a split request's parts share their handoff's): whether its words
        # point at the screen, the capture asked for it, and its one collection (collect_shared).
        self.share_refs: dict[str, bool] = {}
        self.capture_ids: dict[str, str] = {}
        self.share_jobs: dict[str, asyncio.Future[Shared]] = {}
        self.no_capture: set[str] = set()        # handoffs whose capture was given up (routed to a group)
        self.kept_capture: tuple[str, float] | None = None  # a question's capture, waiting for the answer
        self.capture_announced = False           # the call's first unasked-for capture was said aloud
        # A request waiting for the screen (interaction.hold is what the app sees), and "stop looking at
        # my screen" heard in the transcript: words up to stop_words_after are spent; stopped_at is when
        # sharing last went off (or a hold ended) by voice, so the same words act once.
        self.held: Held | None = None
        self.hold_token = 0
        # The held request that last went (its words, when), and handoffs a released hold is dispatching
        # now: the handoff that was held must not give back what the release just bound for it.
        self.released_hold: tuple[str, float] | None = None
        self.releasing: set[str] = set()
        self.stop_words_after = 0.0
        self.stopped_at = float("-inf")
        self.stop_check: asyncio.Task[Any] | None = None
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
        # Messages sent to a task on purpose (messages.py): handoffs the voice says nothing about while
        # they start, what was typed this call, and the spoken follow-up the router placed last (an
        # immediate "no, the X one" moves it).
        self.silent_ids: set[str] = set()
        self.typed: list[dict[str, Any]] = []
        self.last_routed: dict[str, Any] | None = None
        interaction.worker = self

    # -- plumbing -------------------------------------------------------------------------
    @property
    def names(self) -> P.Names:
        return self.rt.names

    def name_task(self, idem: str, request: str | None, parent_key: str | None = None) -> None:
        """Show the instant word-based label now, then swap in a proper name once the titler answers,
        unless something better (a thread's name) has replaced the label meanwhile. A request that
        names nothing itself ("Fix and ship", "Do both") shows the task it answers until then, and
        the titler sees that task and the call's last turns, so it can say what "that" was.

        *parent_key* is only ever the task this one really answers or joins (a decision's own task,
        the task a follow-up was routed to): never the chat thread it continues, nor whatever ran
        before it on the call. A confidently wrong name is worse than a vague one."""
        if parent_key:
            self.store.set_parent(idem, parent_key)
        provisional = self._no_names(short_title(request))
        parent = self.store.parent_name(parent_key)
        fragment = text_mod.is_fragment(request)
        if (fragment or provisional is None) and parent:
            # "Leave it", "Yeah, ship it": a confirmation names nothing itself; it carries on the task
            # it answers, so it takes that task's name rather than none at all.
            provisional = parent
        self.store.set_title(idem, provisional)
        if not request or not provisional:
            return
        context = self._title_context(request, parent_key, parent)
        titler = self.rt.title_call or router.smart_title
        polisher = self.rt.polish_call or router.polish_request
        statuser = self.rt.status_call or router.working_status
        planner = self.rt.plan_call or router.plan_steps

        async def upgrade() -> None:
            title, polished, status, plan = await asyncio.gather(asyncio.to_thread(self._titled, titler, context),
                                                                 asyncio.to_thread(polisher, request),
                                                                 asyncio.to_thread(statuser, request),
                                                                 asyncio.to_thread(planner, request))
            changed = False
            if plan:
                self.store.set_plan(idem, plan)
                changed = True
            if status:
                handed = polished or request
                changed = self.store.handoff_status(
                    idem, status, f"Handed to {self.names.assistant_name}: {handed}") or changed
            if title and title != provisional and self.store.title(idem) == provisional:
                self.store.set_title(idem, title)
                self.title_session(idem)
                changed = True
            if polished:
                self.store.set_title(idem, None, summary=polished)
                changed = True
            if changed:
                self.publish()

        task = asyncio.get_running_loop().create_task(upgrade())
        self.dispatch_tasks.add(task)
        task.add_done_callback(self.dispatch_tasks.discard)

    def title_session(self, idem: str) -> None:
        """Name the Hermes session behind this task after the task (Hermes would otherwise name it
        from the prompt's preamble). Only the session minted for this task: never a chat it continues.
        In the background; a session that keeps Hermes' own name costs nothing but looks."""
        session_id = task_session_id(idem)
        if self.store.session_for(idem) != session_id:
            return

        def name() -> str | None:
            return self._no_names(self.store.title(idem))

        # Not a dispatch task: the end of a call never waits on a session's name.
        task = asyncio.get_running_loop().create_task(
            asyncio.to_thread(session_titles.push, self.hermes, session_id, name))
        session_titles.PENDING.add(task)
        task.add_done_callback(session_titles.PENDING.discard)

    def shape_result(self, idem: str, report: str) -> None:
        """Sort a long finished report into its parts (checks, shipped, open, sections) in the background,
        so apps can lay it out; the answer itself is already delivered and never waits on this."""
        shaper = self.rt.shape_call or router.result_shape
        request = self.store.request_text(idem) or ""

        async def run() -> None:
            shape = await asyncio.to_thread(shaper, request, report)
            if not shape:
                return
            visual = shape.pop("_visual", None)
            changed = self.store.add_shape(idem, shape) if any(
                shape.get(k) for k in ("checks", "shipped", "open", "sections")) else False
            # A picture of the answer's data for agents that didn't draw one (thread tasks never can).
            if visual and self.store.add_view(idem, visual):
                changed = True
            if changed:
                self.publish()

        task = asyncio.get_running_loop().create_task(run())
        self.dispatch_tasks.add(task)
        task.add_done_callback(self.dispatch_tasks.discard)

    def _no_names(self, title: str | None) -> str | None:
        """A title without the user's or the assistant's name (the list is the user's own)."""
        names = self.names
        return text_mod.drop_names(title, names.user_name, names.assistant_name) if title else None

    def _titled(self, titler: Callable[[str], str | None], context: str) -> str | None:
        return self._no_names(titler(context))

    def _title_context(self, request: str, parent_key: str | None = None, parent: str | None = None,
                       earlier: str | None = None) -> str:
        """The titler's input: the request, the task it really answers or joins (if any), the call's
        last turns, and the names to leave out."""
        parent_request = earlier
        if parent_request is None and parent_key:
            try:
                parent_request = self.store.request_text(parent_key)
            except Exception:
                parent_request = None
        try:
            recent = [t for t in self.turns()[-7:] if t.get("text") and t["text"].strip() != request.strip()][-6:]
        except Exception:
            recent = []
        names = self.names
        return text_mod.title_input(request, parent=parent, parent_request=parent_request, recent=recent,
                                    names=(names.user_name, names.assistant_name))

    def retitle(self, idem: str, request: str | None, earlier: str | None = None) -> None:
        """Rename a task for what it has become: the titler sees the first ask and the newest one,
        and its name replaces the current one unless something else renamed it meanwhile."""
        current = self.store.title(idem)
        if not request:
            return
        titler = self.rt.title_call or router.smart_title
        parent_key = self.store.parent_of(idem)
        if earlier and earlier.strip() != request.strip():
            # A turn that joined a task: the task it joined (its own name, never its chat thread's).
            prompt = self._title_context(request, parent=self.store.parent_name(parent_key), earlier=earlier)
        else:
            prompt = self._title_context(request, parent_key, self.store.parent_name(parent_key))

        async def upgrade() -> None:
            title = await asyncio.to_thread(self._titled, titler, prompt)
            if title and title != current and self.store.title(idem) == current:
                self.store.set_title(idem, title)
                self.title_session(idem)
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
        self.ensure_pulse()

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
        out: list[dict[str, Any]] = []
        for fragment in self.fragments:
            role = fragment["speaker"]
            if out and out[-1]["role"] == role:
                out[-1]["text"] += fragment["text"]
            else:
                out.append({"role": role, "text": fragment["text"], "at": fragment["at"]})
        turns = [{"role": t["role"], "text": clean_transcript(t["text"]).strip(), "at": t["at"]}
                 for t in out if clean_transcript(t["text"]).strip()]
        typed = getattr(self, "typed", None)
        if typed:
            # Typed to a task during the call: a user turn of kind "text", never spoken.
            turns = sorted(turns + [{"role": "user", "kind": "text", "at": t["at"],
                                     "text": f"(typed to {t['task']}) {t['text']}"} for t in typed],
                           key=lambda t: t["at"])
        return [{k: v for k, v in t.items() if k != "at"} for t in turns]

    def call_closed(self) -> None:
        """Once per call: persist the end time, keep recent turns, notify about work in flight.

        A paused call keeps its transcript (and listening mode's room text) for Resume and defers the
        "Still working" notices. An ended call drops its room text: it never outlives the call.
        """
        if self.rt.duck is not None:  # also when the transport dropped without a session.closed
            self.rt.duck.restore(self.interaction.interaction_id)
        # A spoken choice's card is only answerable by this call's voice. A dropped connection (no
        # session.closed) used to leave it up as "needs you" for good, and a tap on it after a resume
        # became a follow-up Hermes job of a task that never ran. It goes with the call.
        self.close_choices()
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
                                        self.interaction.device_id, client=self.interaction.client)
            if not paused:
                self.store.mark_call_ended()
                self.still_working_notices()
        except Exception as exc:
            logger.warning("speakeasy: call-close bookkeeping failed: %s", type(exc).__name__)
        end_shared(self.rt, self.interaction, "paused" if paused else "ended")

    def call_tasks(self) -> list[dict[str, Any]]:
        """This call's tasks for the call log: what was asked, how it ended, the spoken answer."""
        with self.interaction.lock:
            keys = [(r.idem_key, r.status) for r in self.interaction.runs.values()
                    if r.idem_key not in self.interaction.carried_keys]
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

    def push(self, kind: str, key: str, title: str | None, body: str | None = None, run_id: str | None = None) -> None:
        """Wake a closed phone (push.py decides which, if any). Never raises into the task path."""
        notifier = getattr(self.rt, "push", None)
        if notifier is None:
            return
        try:
            notifier.notify(kind, key=key, title=title, body=body, run_id=run_id)
        except Exception as exc:
            logger.warning("speakeasy: push failed: %s", type(exc).__name__)

    def push_result(self, backend: Any, idem: str, status: str, result: dict[str, Any] | None, reason: str | None,
                    drafts: list[dict[str, Any]], event: dict[str, Any]) -> None:
        """A task ended: Done / Needs you (a question or an email draft) / Failed / New reply (an
        answer in a Hermes conversation). Stopped by the user, or still going elsewhere: nothing."""
        title = self.store.title(idem) or self.store.request_text(idem)
        run = backend.run_id or idem
        if status == "failed":
            self.push("failed", f"run:{run}", title, reason or "It stopped before finishing.", backend.run_id)
        elif status != "completed" or event.get("still_going"):
            return
        elif result and result.get("question"):
            question = result["question"].get("question") if isinstance(result["question"], dict) else None
            self.push("needs_you", f"question:{run}", title, question or result.get("spoken"), backend.run_id)
        elif drafts:
            subject = drafts[0].get("subject")
            self.push("needs_you", f"draft:{drafts[0].get('draft_id')}", title,
                      f"Email draft ready: {subject}" if subject else "An email draft is ready to check.", backend.run_id)
        else:
            self.push("reply" if event.get("continued") else "finished", f"run:{run}", title,
                      (result or {}).get("spoken") or "Done.", backend.run_id)

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
            if self.rt.duck is not None and speaker == "assistant" and self.interaction.local:
                # The assistant talking on this Mac: duck its volume until the voice goes quiet.
                self.rt.duck.note(speaker, self.interaction.interaction_id)
            if speaker == "user":
                self.listen_for_stop()
            else:
                self.watch_acknowledgement()
                if delta.rstrip().endswith("?"):
                    self.watch_choice()
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
            self.close_choices()
            self.call_closed()
            with self.interaction.lock:
                self.interaction.finalization = "confirmed"
                latest = self.interaction.runs.get(self.interaction.latest_delegation_id or "")
                if not latest or latest.status not in {"working", "running", "waiting_for_approval"}:
                    self.interaction.status = "closed"
            self.publish()

    def schedule_dispatch(self, delegation_id: str, context: str, marked: Any = None) -> None:
        self.close_choices()  # answered (or moved past) out loud: the card goes
        self.handoff_at[delegation_id] = self.last_handoff_at = time.monotonic()
        with self.interaction.lock:
            self.interaction.revision += 1
            revision = self.interaction.revision
            if self.interaction.room:
                # Asked during a room call: the work gets the room even if the call ends first.
                self.handoff_rooms[delegation_id] = self.interaction.room
        task = asyncio.get_running_loop().create_task(self.dispatch(delegation_id, revision, context, marked))
        self.dispatch_tasks.add(task)
        task.add_done_callback(self.dispatch_tasks.discard)

    # -- spoken choices become cards -----------------------------------------------------------------
    def watch_choice(self) -> None:
        """The voice just asked something. If it offered a pick ("the quick summary or the full
        report?"), put the options on screen as a question card the user can tap."""
        fragments = list(getattr(self, "fragments", []))
        turn = next((i for i in range(len(fragments) - 1, -1, -1) if fragments[i]["speaker"] == "user"), -1)
        said = "".join(f["text"] for f in fragments[turn + 1:] if f["speaker"] == "assistant")
        if said.lstrip().lower().startswith("quick question:"):
            return  # reading a task's own question card aloud: that card is already up
        found = router.spoken_choice(said)
        if found is None:
            return
        question, options = found
        if closing_offer(said, question):
            return  # "Want me to hold, or walk you through it?" closing a report: talk, not a card
        key = frozenset(_words(question).split())
        if key in self.offered_choices:
            return
        self.offered_choices.add(key)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.offer_choice(question, options))
        self.dispatch_tasks.add(task)
        task.add_done_callback(self.dispatch_tasks.discard)

    async def offer_choice(self, question: str, options: list[str]) -> str | None:
        """A lightweight task holding the question card (no Hermes run). A tap goes back to the voice
        as the user's answer (``answer``); answering out loud or hanging up sets it aside."""
        from .views import extract_question
        waiting = await asyncio.to_thread(self.waiting_on_user)
        if any(t not in self.choice_tasks for t in waiting):
            return None  # a task's own question is up; one decision on screen at a time
        _, view = extract_question("\n```speakeasy-question\n" + json.dumps({"question": question, "options": options})
                                   + "\n```")
        if view is None:
            return None
        self.close_choices()  # a newer choice replaces an older unanswered one
        delegation_id = "choice_" + secrets.token_hex(8)
        idem = self._idem(delegation_id, 0)
        backend = BackendRun(delegation_id, 0, idem, status="completed")
        with self.interaction.lock:
            self.interaction.runs[delegation_id] = backend
        self.choice_tasks[delegation_id] = question
        listed = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
        try:
            self.store.reserve_run(idem, self.interaction.interaction_id, delegation_id, 0)
            # A short question names its card as asked; a long one gets a clean cut, never an ellipsis.
            whole = notice_text(question, 200) or ""
            self.store.set_title(idem, (whole if len(whole) <= 60 else short_title(question)) or "A quick choice")
            self.store.update_run(idem, None, "completed")
            self.store.set_result(idem, {"spoken": question, "full": f"**{question}**\n{listed}",
                                         "question": view, "views": [view]})
        except Exception:
            logger.warning("speakeasy: could not record the spoken choice card")
            return None
        logger.info("speakeasy: spoken choice shown as a card (%d options)", len(options))
        self.publish()
        # Name the card for the decision, not the first words of the question.
        self.retitle(idem, f"Question put to the user: {question} Options: {'; '.join(options)}")
        return delegation_id

    def close_choices(self) -> None:
        """Set aside every open spoken-choice card on this call."""
        open_ids = [t for t in getattr(self, "choice_tasks", {}) if t not in self.choice_answered]
        if not open_ids:
            return
        with self.interaction.lock:
            keys = [self.interaction.runs[t].idem_key for t in open_ids if t in self.interaction.runs]
        for task_id in open_ids:
            self.choice_answered.add(task_id)
        for key in keys:
            try:
                self.store.set_aside(key)
            except Exception:
                logger.info("speakeasy: could not set a spoken choice aside")
        self.publish()

    async def pick_choice(self, task_id: str, text: str) -> str:
        """A tap on a spoken choice's card: the voice hears it as the user's answer."""
        if task_id in self.choice_answered:
            return task_id
        question = self.choice_tasks.get(task_id, "")
        with self.interaction.lock:
            run = self.interaction.runs.get(task_id)
        self.choice_answered.add(task_id)
        if run is not None:
            self.store.set_aside(run.idem_key)
            self.store.progress(run.idem_key, "milestone", f"You picked: {notice_text(text, 120)}")
        self.touch()
        self.fragments.append({"speaker": "user", "text": text, "at": time.monotonic(), "start_ms": 0, "end_ms": 0})
        await self.append("session.commentary.append", None, P.choice_picked_note(text, question))
        self.publish()
        return task_id

    # -- "on it" must mean a task -----------------------------------------------------------------
    def watch_acknowledgement(self) -> None:
        """The voice is speaking. If it is promising work ("On it", "let me pull that together"), check
        a beat later that a task really started for what the user just said; the call once said "On
        it" to "make the mobile logo match", started nothing, and the user found out much later."""
        fragments = list(self.fragments)
        turn = next((i for i in range(len(fragments) - 1, -1, -1) if fragments[i]["speaker"] == "user"), None)
        if turn is None:
            return
        turn_at = fragments[turn]["at"]
        if turn_at <= getattr(self, "ack_checked", float("-inf")):
            return
        said = "".join(f["text"] for f in fragments[turn + 1:] if f["speaker"] == "assistant")
        if not router.acknowledges_work(said):
            return
        self.ack_checked = turn_at
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._ack_guard(turn_at))
        self.dispatch_tasks.add(task)
        task.add_done_callback(self.dispatch_tasks.discard)

    async def _ack_guard(self, turn_at: float) -> None:
        await asyncio.sleep(ACK_GRACE_S)
        drops = [d for d in getattr(self, "drops", []) if d[1] >= turn_at - 1.0]
        talked = next((d for d in reversed(drops) if d[0] == "Talked it through"), None)
        if talked is not None:
            # The gate took it for conversation but the voice promised the work: do the work.
            logger.info("speakeasy: the voice said it was on it but nothing started; starting it now")
            self._force_dispatch(talked[3])
            return
        if getattr(self, "last_handoff_at", float("-inf")) >= turn_at - 2.0 and not drops:
            return  # a handoff came for this turn: it is a task (or was answered on the spot)
        if drops:
            if any(d[0] in {"Just a greeting", "Just a reaction", "Just talking", "Didn't catch it"} for d in drops):
                await self.append("session.thinking.append", None, P.nothing_started_note(self.names))
            return
        context = self.context(1 << 62)
        lines = context.splitlines()
        while lines and not lines[-1].startswith("User: "):
            lines.pop()
        context = "\n".join(lines)
        request = whole_request(context, self._started_words())
        if (not request or is_greeting(request) or router.is_fragment(request) or router.is_reaction(request)
                or router.starts_talk_mode(request)):
            await self.append("session.thinking.append", None, P.nothing_started_note(self.names))
            return
        logger.info("speakeasy: the voice said it was on it with no handoff; starting the request now")
        self._force_dispatch(context)

    def _force_dispatch(self, context: str) -> None:
        delegation_id = "ack_" + secrets.token_hex(8)
        self.delegations.add(delegation_id)
        if getattr(self, "force_work", None) is None:
            self.force_work = set()
        self.force_work.add(delegation_id)
        self.schedule_dispatch(delegation_id, context)

    def _started_words(self) -> set[str]:
        return {w for w, *_ in getattr(self, "started", [])}

    def _note_started(self, delegation_id: str, request: str) -> None:
        started = getattr(self, "started", None)
        if started is None:
            started = self.started = []
        started[:] = [s for s in started if s[2] != delegation_id][-19:]
        started.append((_words(request), time.monotonic(), delegation_id, request))

    def _duplicate(self, request: str) -> bool:
        """The very same words as a request started moments ago ("Fix it now" handed off twice)."""
        words, now = _words(request), time.monotonic()
        return bool(words) and any(w == words and now - at < DUPLICATE_WINDOW_S for w, at, *_ in getattr(self, "started", []))

    async def _apply_correction(self, last_request: str, context: str) -> str:
        """\"Our guest room, sorry\" / \"no, turn them on\": the corrected request, in place of the one it
        corrects. Joined onto words not yet started, it fixes them; said after a request already went,
        the corrected request goes instead, and the old one is stopped if it is still running."""
        line = next((l[6:].strip() for l in reversed(context.splitlines()) if l.startswith("User: ")), last_request)
        found = router.correction(line)
        if not found:
            return last_request
        how, words = found
        before = last_request[:-len(line)].strip() if last_request.endswith(line) and last_request != line else ""
        if before:
            fixed = words if how == "replace" else router.apply_correction(before, words)
            logger.info("speakeasy: a correction replaced part of the request before it started")
            return fixed
        now = time.monotonic()
        recent = [s for s in getattr(self, "started", []) if now - s[1] < CORRECTION_WINDOW_S]
        if not recent:
            return words if how == "replace" else last_request
        _, _, old_id, old_request = recent[-1]
        fixed = words if how == "replace" else router.apply_correction(old_request, words)
        await self._supersede(old_id)
        logger.info("speakeasy: a correction replaced the request just before it")
        return fixed

    async def _supersede(self, task_id: str) -> None:
        """A corrected request replaces this one: stop it if it is still working."""
        with self.interaction.lock:
            run = self.interaction.runs.get(task_id)
            if run is None or run.status not in ACTIVE_RUN_STATES or not run.run_id:
                return
            run.status = "cancel_requested"
            key, run_id = run.idem_key, run.run_id
        self.store.update_run(key, None, "cancel_requested")
        self.store.progress(key, "milestone", "Replaced by your correction")
        self.publish()
        try:
            await asyncio.to_thread(self.hermes.stop, run_id)
        except Exception as exc:
            logger.warning("speakeasy: could not stop the corrected task (%s)", type(exc).__name__)

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

    async def quiet(self, key: str, delegation_id: str | None, content: str) -> None:
        """Background the voice should know but never answer out loud (work started, where a task
        went, status, what's on screen). Held when the transport would make the voice reply to it."""
        if delegation_id and delegation_id in getattr(self, "silent_ids", ()):
            return  # a message sent to a task on purpose: the voice got its own note
        if not self.HOLD_QUIET_NOTES:
            await self.append("session.thinking.append", delegation_id, content)
            return
        self.quiet_notes.pop(key, None)
        self.quiet_notes[key] = content
        while len(self.quiet_notes) > 8:
            self.quiet_notes.pop(next(iter(self.quiet_notes)))

    def take_quiet_notes(self) -> str:
        notes, self.quiet_notes = list(self.quiet_notes.values()), {}
        if not notes:
            return ""
        return ("Background since you last spoke (context only, never read it out): "
                + " | ".join(notes))[:1400] + "\n\n"

    async def append(self, kind: str, delegation_id: str | None, content: str) -> None:
        """Send context to the live call. After Pause/Resume, results of runs started in an earlier
        call go to the resumed call as session-wide context."""
        if delegation_id and delegation_id in getattr(self, "silent_ids", ()):
            return  # a message sent to a task on purpose: the voice got its own note
        head = self.interaction.head()
        target = head.worker if head is not self.interaction else self
        if target is None or target is not self and not target.call_connected():
            return
        if delegation_id and (delegation_id.startswith(EARLY_PREFIX) or messages_mod.is_message_id(delegation_id)):
            delegation_id = None  # the provider never issued this handoff: say it session-wide
        if kind == "session.thinking.append" and self.quiet_notes:
            held = self.take_quiet_notes()
            if len(held) + len(content) <= 2000:
                content = held + content
            else:
                await self.append(kind, None, held.strip())
        if kind == "session.commentary.append":
            target.said_at = time.monotonic()  # the call pulse counts from here
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
        if task_id in getattr(self, "choice_tasks", {}):
            return await self.pick_choice(task_id, text)
        if task_id.startswith("choice_"):
            # Another leg's spoken choice (carried over by a resume): only the voice that asked it can
            # take the pick, and it has no Hermes run to follow up. Refused, so the app says "say it".
            return None
        # A double tap, or a tap plus the same answer said aloud, must reach the task once.
        key, now = (task_id, _words(text)), time.monotonic()
        answers = getattr(self, "recent_answers", None)
        if answers is None:
            answers = self.recent_answers = {}
        previous = answers.get(key)
        if previous:
            # The same pick for the same question, any time in this call (a retap after a slow reply,
            # or the tap plus saying it): one follow-up only. A different answer still goes.
            logger.info("speakeasy: same answer sent again for one task; passed on once")
            return previous[0]
        delegation_id = "answer_" + secrets.token_hex(8)
        answers[key] = (delegation_id, now)
        answered = getattr(self, "question_answered", None)
        if callable(answered):
            answered(task_id)
        self.delegations.add(delegation_id)
        self.last_handoff_at = time.monotonic()
        self.touch()
        self.fragments.append({"speaker": "user", "text": text, "at": time.monotonic(), "start_ms": 0, "end_ms": 0})
        if self.call_connected():
            await self.append("session.thinking.append", None, P.answered_note(text))
        # The tap is answered as soon as it's taken: following up can take a while (a task in a chat
        # thread waits for that thread, a continued session is looked up), and the app must not see a
        # slow reply as a failed tap and put the question back up while the answer is already working.
        picked = getattr(self, "picked_request", None)
        request = picked(task_id, text) if callable(picked) else text
        follow = asyncio.get_running_loop().create_task(
            self.follow_up(delegation_id, 0, f"User: {text}", router.Part("follow_up", request, task_id)))
        tasks = getattr(self, "dispatch_tasks", None)
        if tasks is None:
            tasks = self.dispatch_tasks = set()
        tasks.add(follow)
        follow.add_done_callback(tasks.discard)
        follow.add_done_callback(_log_follow_up_failure)
        return delegation_id

    def question_answered(self, task_id: str) -> None:
        """The task's question was answered: recorded in the store (the one truth every device's board,
        panel and session page read), and every client told at once (decision_resolved)."""
        with self.interaction.lock:
            run = self.interaction.runs.get(task_id)
            key, run_id = (run.idem_key, run.run_id) if run else (None, None)
        if key and self.store.mark_answered(key):
            events = getattr(self.rt, "history_events", None)
            if events is not None:
                events.publish("decision_resolved", {"run_id": run_id, "session_id": self.store.session_for(key)})
            self.publish()

    def picked_request(self, task_id: str, text: str) -> str:
        """A tapped answer carries the question it answers ("Fix and ship: Fix the onboarding gaps for
        ChatGPT-app users?"), so its task, title and spoken name say what it is about."""
        with self.interaction.lock:
            run = self.interaction.runs.get(task_id)
            key = run.idem_key if run else None
        work = self.store.work(idem_key=key) if key else None
        asked = ((work or {}).get("result") or {}).get("question")
        question = asked.get("question") if isinstance(asked, dict) else asked if isinstance(asked, str) else None
        question = notice_text(question, 200) if question else None
        return f"{text.rstrip('.')}: {question}" if question and set(_words(question).split()) - set(_words(text).split()) else text

    def waiting_on_user(self) -> list[str]:
        """This call's tasks waiting on the user, oldest first: an approval, or a finished task's
        question nobody answered or set aside yet (the newest task of its job, so an answered one,
        which went on as a follow-up, no longer counts)."""
        with self.interaction.lock:
            runs = [(r.delegation_id, r.idem_key, r.status) for r in self.interaction.runs.values()]
        newest: dict[str, str] = {}
        asking: list[tuple[str, str]] = []
        for task_id, key, status in runs:
            work = self.store.work(idem_key=key) or {}
            job = (work.get("job") or {}).get("id") or key
            newest[job] = task_id
            if work.get("set_aside") or task_id in self.set_aside_tasks:
                continue
            if status == "waiting_for_approval" or (
                    status == "completed" and (work.get("result") or {}).get("question")):
                asking.append((task_id, job))
        return [t for t, job in asking if newest.get(job) == t]

    async def set_aside(self, task_id: str, by_voice: bool = False) -> bool:
        """The user set a waiting question, decision or approval aside: already handled, or not
        needed. It settles as no longer needed (not failed); a run still waiting on the answer is
        told so and wraps up; the voice hears it quietly so it never asks again. A finished task's
        question starts no new work."""
        with self.interaction.lock:
            run = self.interaction.runs.get(task_id)
            if run is None:
                return False
            key, run_id, status, approval = run.idem_key, run.run_id, run.status, run.approval
            if status == "waiting_for_approval":
                run.approval, run.status = None, "resolving_approval"
        if not self.store.set_aside(key) and status != "waiting_for_approval":
            return False
        self.set_aside_tasks.add(task_id)
        self.store.progress(key, "milestone", "Set aside")
        if status == "waiting_for_approval" and run_id:
            answer = P.set_aside_answer(self.names)
            try:
                if approval and approval.get("request_id"):
                    await asyncio.to_thread(self.hermes.approve, run_id, approval["request_id"], "deny")
                await asyncio.to_thread(self.hermes.steer, run_id, P.steer_text(self.names, answer))
            except Exception as exc:  # the run may already be gone; it is closed here either way
                logger.warning("speakeasy: set-aside could not reach the run (%s)", type(exc).__name__)
            with self.interaction.lock:
                if run.status == "resolving_approval":
                    run.status = "working"
        self.publish()
        if self.call_connected():
            name = self.store.title(key) or self.store.request_text(key) or ""
            await self.append("session.thinking.append", None, P.set_aside_note(name, by_voice))
        return True

    def focus_job(self, task_id: str) -> str:
        """The name of the job a focused task belongs to (what the user sees on the job view)."""
        with self.interaction.lock:
            run = self.interaction.runs.get(task_id)
            key = run.idem_key if run else None
        work = self.store.work(idem_key=key) if key else None
        return ((work or {}).get("job") or {}).get("name") or ""

    def focus(self) -> tuple[str | None, str]:
        """The task the user is facing in the headset, if it's one of this call's open tasks."""
        with self.interaction.lock:
            task_id, detail = self.interaction.focus_task_id, self.interaction.focus_detail
        if not task_id or not any(t.task_id == task_id for t in self.open_tasks()):
            return None, ""
        return task_id, detail

    async def focus_changed(self) -> None:
        """Tell the voice what the user is looking at, so "what is this?" is answered on the spot."""
        task_id, detail = self.focus()
        if not self.call_connected():
            return
        if task_id is None:
            await self.quiet("focus", None, P.focus_cleared_note(self.names))
            return
        name = next((t.request for t in self.open_tasks() if t.task_id == task_id), "")
        await self.quiet("focus", None, P.focus_note(self.names, name, detail))

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

    # -- typed turns (session.input_text) ---------------------------------------------------
    # The user can type on the call screen instead of speaking. The turn goes to the voice model as
    # theirs, joins this call's transcript as a user turn (so the next handoff carries it), and goes out
    # on the live event stream as `transcript` so every client of the call can show it.

    async def input_text(self, text: Any, client_id: str | None = None) -> str:
        """One typed turn: TYPED_SENT once the voice took it and it is in the transcript, else
        TYPED_NOT_CONNECTED / TYPED_NOT_DELIVERED, and then nothing is recorded. A failed send is this
        send's outcome only: it never marks the call (interaction.error, the connection) as broken,
        so the next send on a healthy call goes through. ``client_id`` (the app's id for this turn)
        rides on the SSE copy so the sending app can tell it from its own local echo."""
        if not isinstance(text, str):
            return TYPED_NOT_DELIVERED
        text = typed_text(text)
        if not text or len(text) > MAX_INPUT_TEXT:
            return TYPED_NOT_DELIVERED
        if not self.call_connected():
            return TYPED_NOT_CONNECTED
        outcome = await self.forward_typed(text)
        if outcome != TYPED_SENT:
            return outcome
        self.touch()
        at = self.typed_offset()
        spaced = " " + text if self.fragments and self.fragments[-1]["speaker"] == "user" else text
        await self.handle_event({"type": "session.input_transcript.delta", "delta": spaced,
                                 "start_ms": at, "end_ms": at})
        event: dict[str, Any] = {"role": "user", "text": text, "source": "typed"}
        if client_id:
            event["client_id"] = client_id
        self.interaction.feed.publish("transcript", event)
        logger.info("speakeasy: typed turn on the call (%d chars)", len(text))
        await self.typed_recorded()
        return TYPED_SENT

    # GPT-Live's sideband takes no user text, only `session.thinking.append` and
    # `session.commentary.append`. A typed turn goes in as commentary, not thinking. From the code:
    # thinking is the background channel (`quiet()` exists precisely because it must never be answered
    # out loud; early_request's room branch above says "a thinking note isn't spoken"), while every "say this now /
    # answer this yourself" instruction rides commentary (DIDNT_CATCH, quick_note, and room_answer_note,
    # the one other turn the voice never heard and must answer aloud with no handoff waiting). A typed
    # turn is exactly that case: a thinking note would let the voice hold it silently while the route
    # reports sent. The note says "don't read this note out", as room_answer_note does. Not confirmed
    # on a live GPT-Live call; if the voice ever reads it verbatim, change this one constant.
    TYPED_TURN_KIND = "session.commentary.append"

    async def forward_typed(self, text: str) -> str:
        """Hand a typed turn to the voice model (GPT-Live: a session-wide note quoting the words, see
        TYPED_TURN_KIND). Providers that take real user text override this."""
        if not self.call_connected():
            return TYPED_NOT_CONNECTED
        if await self.send_typed_note(self.TYPED_TURN_KIND, P.typed_turn_note(self.names, text)[:2000]):
            return TYPED_SENT
        return TYPED_NOT_DELIVERED if self.call_connected() else TYPED_NOT_CONNECTED

    async def send_typed_note(self, kind: str, content: str) -> bool:
        """One typed-turn note to the voice; True when the transport took it. Default: the shared
        `_send` (which reports nothing), judged by whether the call is still up afterwards. A
        transport that can tell a failed send apart overrides this without touching call state."""
        await self._send(kind, None, content)
        return self.call_connected()

    def typed_offset(self) -> int:
        """Where a typed turn sits in this call's transcript clock: at the end of what was said, so a
        handoff made after it (context()) carries it."""
        return max(((f["end_ms"] or f["start_ms"]) for f in self.fragments), default=0)

    async def typed_recorded(self) -> None:
        """After a typed turn is in the transcript: providers that must be asked to reply do it here."""

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
            await self.quiet("where:" + delegation_id, delegation_id, P.where_note(place))

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
        await self.quiet("status:" + str(backend.say_id), backend.say_id, P.status_note(detail))

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
        actually doing, at most once per PROGRESS_EVERY_S (the user's interval, see update_every)
        across the call."""
        if not self.rt.settings()["speech"]["progress"]:
            return False
        now = time.monotonic()
        # A problem or heads-up ("the helper is broken, fixing it") is worth hearing now: it skips the
        # spacing between updates, which otherwise let "starting music" crowd it out.
        urgent = bool(milestone and text_mod.HEADS_UP_RE.search(milestone))
        if self.muted_now() and not urgent:
            return False  # muted: no "still working" lines (results, questions and approvals still speak)
        with self.interaction.lock:
            last_any = self.interaction.progress_spoken_at
            if (now - backend.started < PROGRESS_AFTER_S or backend.status not in {"running", "working"}
                    or (last_any and now - last_any < self.update_every(PROGRESS_EVERY_S) and not urgent)
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
        items, _ = activity_mod.speech_steps(self.store, backend.idem_key)
        with self.interaction.lock:
            backend.told_count = covered
            backend.told = (backend.told + [line])[-5:]
            backend.pulse_said = (backend.pulse_said + [(now, line)])[-PULSE_SAID_KEEP:]
            backend.pulse_told.update(i["n"] for i in items)
            backend.pulse_idle = 0
        await self.append("session.commentary.append", backend.say_id, line)
        return True

    # -- the call pulse: never minutes of silence while work runs ------------------------------
    def update_every(self, default: float | None = None) -> float:
        """Seconds between spoken updates on this call: the user's ``speech.interval_seconds``
        (30, 45, 60, 90 or 120). The default setting (45 s) means `default` (PULSE_EVERY_S), so
        the timing constants stay the one place the default rhythm is defined."""
        every = (self.rt.settings().get("speech") or {}).get("interval_seconds")
        if isinstance(every, bool) or not isinstance(every, (int, float)) or every == DEFAULT_UPDATE_INTERVAL_S:
            return PULSE_EVERY_S if default is None else default
        return float(every)

    def muted_now(self) -> bool:
        """The user's mic is off: the app said so (``POST .../mic``), or the relay's own mute."""
        head = self.interaction.head()
        return bool(getattr(self, "muted", False) or self.interaction.mic_muted or head.mic_muted)

    def mic_open(self) -> bool:
        """The app said the mic is on (``POST .../mic`` with ``muted: false``) and nothing mutes it.
        Never reported counts as not open: that call keeps the muted-era pulse timing."""
        head = self.interaction.head()
        return not self.muted_now() and (self.interaction.mic_muted is False or head.mic_muted is False)

    def pulse_step(self, backend: BackendRun, items: list[dict[str, Any]], current: dict[str, Any] | None) -> str:
        """What an anchored open-mic line says the task is on: the tool running now, else its latest
        step, as doing-words ("running the tests"); else the task's name; else "it"."""
        for item in ([current] if current else []) + list(reversed(items)):
            plain = P.speakable(item.get("text"))
            now = P.doing(plain) if plain else None
            if now:
                return now
        name = self.spoken_task_name(backend.idem_key)
        return _lead_lower(name, proper_nouns(self.store.request_text(backend.idem_key), name)) if name else "it"

    def someone_speaking(self, now: float) -> bool:
        """Either side is mid-turn: a transcript arrived in the last PULSE_QUIET_S, or the provider
        says a reply is playing / the user's words are still being transcribed."""
        if getattr(self, "response_active", False) or getattr(self, "user_pending", False):
            return True
        fragments = list(self.fragments)
        return bool(fragments) and now - fragments[-1].get("at", 0) < PULSE_QUIET_S

    def user_mid_thought(self, now: float) -> bool:
        """The user is still in the middle of a thought: a "wait, I wasn't done" hold, words still being
        transcribed, the last words under USER_PAUSE_S ago, or trailing off under UNFINISHED_PAUSE_S ago.
        Nothing is spoken over that: not a result, not a task's question, not a pulse."""
        if now < getattr(self, "speech_hold_until", 0.0) or getattr(self, "user_pending", False):
            return True
        fragments = list(self.fragments)
        if not fragments or fragments[-1]["speaker"] != "user":
            return False
        if fragments[-1].get("at", 0.0) <= getattr(self, "last_handoff_at", float("-inf")):
            return False  # what they said last was handed off: they finished that thought
        tail: list[str] = []
        for f in reversed(fragments):
            if f["speaker"] != "user":
                break
            tail.insert(0, f.get("text") or "")
        age = now - fragments[-1].get("at", 0.0)
        if age < USER_PAUSE_S:
            return True
        return age < UNFINISHED_PAUSE_S and router.trails_off("".join(tail))

    async def wait_for_pause(self, limit: float = RESULT_HOLD_MAX_S) -> None:
        """Wait (up to ``limit``) until the user is not mid-thought before speaking on our own."""
        start = time.monotonic()
        while (self.call_connected() and time.monotonic() - start < limit
               and self.user_mid_thought(time.monotonic())):
            await asyncio.sleep(0.25)

    def hold_speech(self, seconds: float = HOLD_S) -> None:
        self.speech_hold_until = max(getattr(self, "speech_hold_until", 0.0), time.monotonic() + seconds)

    def spoken_task_name(self, key: str | None) -> str:
        """A task's name to say aloud: its own title (never a job name cut to a few words)."""
        if not key:
            return ""
        return P.spoken_name(self.store.title(key) or self.store.request_text(key) or "")

    def pulse_needs_name(self, backend: BackendRun) -> bool:
        """Say which job a pulse is about when more than one could be meant, or it wasn't the
        last thing talked about."""
        with self.interaction.lock:
            live = [r for r in self.interaction.runs.values() if r.status in ACTIVE_RUN_STATES]
            latest = self.interaction.latest_delegation_id
        return len(live) > 1 or latest != backend.delegation_id

    def last_spoke_at(self) -> float:
        """When the assistant last said anything on this call (monotonic): its own speech, a spoken
        update, a handoff's acknowledgement, or the call coming up."""
        said = [f.get("at", 0.0) for f in list(self.fragments) if f["speaker"] == "assistant"]
        return max([self.connected_at, self.interaction.progress_spoken_at, getattr(self, "said_at", 0.0),
                    getattr(self, "last_handoff_at", float("-inf")), *said[-1:]])

    def ensure_pulse(self) -> None:
        """Start the pulse loop on this worker's own loop if work is running and it isn't already."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if loop is not self.loop or loop.is_closed():
            return
        task = getattr(self, "_pulse_task", None)
        if task is not None and not task.done():
            return
        with self.interaction.lock:
            active = any(r.status in ACTIVE_RUN_STATES for r in self.interaction.runs.values())
        if active and self.call_connected():
            self._pulse_task = loop.create_task(self._pulse_loop())

    async def _pulse_loop(self) -> None:
        while True:
            await asyncio.sleep(PULSE_TICK_S)
            if not self.call_connected():
                return
            with self.interaction.lock:
                if not any(r.status in ACTIVE_RUN_STATES for r in self.interaction.runs.values()):
                    return
            try:
                await self.pulse_tick()
            except Exception:
                logger.debug("speakeasy: call pulse failed", exc_info=True)

    def _pulse_candidates(self, now: float, after: float = PROGRESS_AFTER_S) -> list[BackendRun]:
        with self.interaction.lock:
            runs = list(self.interaction.runs.values())
        silent = getattr(self, "silent_ids", set())
        return [r for r in runs if r.status in {"running", "working"} and now - r.started >= after
                and not r.delegation_id.startswith("choice_") and r.delegation_id not in self.home_tasks
                and r.delegation_id not in silent]

    async def pulse_tick(self) -> bool:
        """One look: if the call has been quiet PULSE_EVERY_S (the user's interval, see update_every)
        since the assistant last spoke and work
        is running, say one short line about it. True when it spoke. With the mic open a task's first
        line comes after PULSE_FIRST_OPEN_S and there is no back-off (see PULSE_FIRST_OPEN_S)."""
        if not self.rt.settings()["speech"]["progress"] or not self.call_connected():
            return False
        if self.muted_now() or self.interaction.paused or self.interaction.talking_only:
            return False
        now = time.monotonic()
        with self.interaction.lock:
            asking = any(r.status in {"waiting_for_approval", "resolving_approval"} or r.approval
                         for r in self.interaction.runs.values())
        if asking or self.held is not None or self.someone_speaking(now) or self.user_mid_thought(now):
            return False
        last = self.last_spoke_at()
        open_mic = self.mic_open()  # the app said the mic is on: an early first line and no back-off
        every = self.update_every()
        if now - last < (PULSE_FIRST_OPEN_S if open_mic else every):
            return False
        running = self._pulse_candidates(now, PULSE_FIRST_OPEN_S if open_mic else PROGRESS_AFTER_S)
        if not running:
            return False
        if await asyncio.to_thread(self.waiting_on_user):
            return False  # a question or decision is up: that is what the call is waiting on
        picks = []
        for r in running:
            items, current = await asyncio.to_thread(activity_mod.speech_steps, self.store, r.idem_key)
            fresh = [i for i in items if i["n"] not in r.pulse_told and i is not current]
            notes = r.activity[-min(ACTIVITY_KEEP, r.activity_count - r.told_count):] \
                if r.activity_count > r.told_count else []
            new = bool(fresh or notes or (current is not None and current["n"] not in r.pulse_told))
            if open_mic:  # a task's first line early, then every `every` (the user's setting), news or not
                wait = every if r.spoken_at else PULSE_FIRST_OPEN_S
            else:
                wait = max(PULSE_IDLE_EVERY_S, every) if not new and r.pulse_idle >= PULSE_IDLE_AFTER else every
            if now - max(r.spoken_at, last) < wait:
                continue
            picks.append((not new, r.spoken_at, r, items, current, fresh, notes))
        if not picks:
            return False
        picks.sort(key=lambda p: (p[0], p[1]))  # something new first, then whoever was told least recently
        stale, _, backend, items, current, fresh, notes = picks[0]
        with self.interaction.lock:
            claimed = self.interaction.progress_spoken_at
            self.interaction.progress_spoken_at = now  # also holds back commentary-driven updates
            claimed_task = backend.spoken_at
            backend.spoken_at = now
            covered = backend.activity_count
            told = list(backend.told[-3:])
            told_all = list(backend.pulse_said) + [(float("-inf"), t) for t in backend.told
                                                   if t not in {said for _, said in backend.pulse_said}]
            idle = backend.pulse_idle
        running_s = time.time() - float(current.get("started_at") or current.get("at") or time.time()) if current else 0.0
        steps = P.pulse_steps(items, backend.pulse_told, notes, current, running_s)
        request = self.store.request_text(backend.idem_key) or ""
        writer = self.rt.progress_call or router.progress_update
        if open_mic and stale and idle >= PULSE_IDLE_AFTER:
            line = None  # where a muted call backs off, the open mic hears the step and the task's age
        else:
            try:
                line = await asyncio.to_thread(writer, request, steps, told)
            except Exception as exc:
                logger.info("speakeasy: pulse wording unavailable (%s)", type(exc).__name__)
                line = None
            line = line or P.pulse_fallback([i["text"] for i in fresh] + notes,
                                            current.get("text") if current else None,
                                            running_s, self.store.title(backend.idem_key), idle)
        name, needs = self.spoken_task_name(backend.idem_key), self.pulse_needs_name(backend)
        if open_mic:
            # Never word for word a recent line, never over the cap, and never nothing.
            line = unrepeated(line, told_all, self.pulse_step(backend, items, current), now - backend.started,
                              lambda c: cap_words(P.named_pulse(c, name, needs) or c),
                              reserve=len(name.split()) if needs and name else 0,
                              keep=proper_nouns(request, name, self.store.title(backend.idem_key)), every=every)
        else:
            line = P.named_pulse(line, name, needs)
        late = time.monotonic()
        if (not line or not self.call_connected() or self.muted_now() or self.someone_speaking(late)
                or self.last_spoke_at() > now or backend.status not in {"running", "working"}):
            with self.interaction.lock:
                if self.interaction.progress_spoken_at == now:
                    self.interaction.progress_spoken_at = claimed
                # Nothing was said, so on an open mic the task keeps its early first line (and its
                # spacing) instead of waiting PULSE_EVERY_S from a line that never went out. Muted and
                # never-reported calls keep main's behaviour here exactly.
                if open_mic and backend.spoken_at == now:
                    backend.spoken_at = claimed_task
            return False
        with self.interaction.lock:
            backend.told_count = covered
            backend.told = (backend.told + [line])[-5:]
            backend.pulse_told.update(i["n"] for i in items)
            backend.pulse_idle = idle + 1 if stale else 0
            backend.pulse_said = (backend.pulse_said + [(now, line)])[-PULSE_SAID_KEEP:]
        logger.info("speakeasy: call pulse (%s)", "nothing new" if stale else "new activity")
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
        content = set(_words(more).split()) - router._FRAGMENT_WORDS
        if not content or content <= set(_words(request).split()):
            return request  # "yeah, what work do we have" again: the same words count once
        fixed = router.correction(more)
        if fixed:
            # "...in the living room" + "guest room, sorry": the fix replaces, it isn't run on.
            logger.info("speakeasy: kept listening; a correction replaced part of the request")
            return fixed[1] if fixed[0] == "replace" else router.apply_correction(request, fixed[1])
        logger.info("speakeasy: kept listening, added %d more words to the request", len(more.split()))
        return f"{request.rstrip()} {more}"

    def _echoes_recent(self, request: str) -> bool:
        """The voice sometimes hands off one sentence twice: the whole thing, then a scrap of its tail
        a second or two later ("...task failing"), which whole_request() joins back onto the sentence.
        If every word of the new request was already in a request started moments ago, it's an echo."""
        now = time.monotonic()
        recent = getattr(self, "recent_requests", None)
        if recent is None:
            recent = self.recent_requests = []
        recent[:] = [(w, at) for w, at in recent if now - at < ECHO_WINDOW_S]
        want = _words(request).split()
        if len(want) < 3:
            return False  # "yes", "the lamp": short replies are real turns, never echoes
        return any(set(want) <= set(words.split()) for words, _ in recent)

    def _remember_request(self, request: str) -> None:
        if getattr(self, "recent_requests", None) is None:
            self.recent_requests = []
        self.recent_requests.append((_words(request), time.monotonic()))

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
            await self._drop(delegation_id, revision, "Talked it through", None, context=context)
            await self.append("session.thinking.append", delegation_id, P.talk_note(self.names))
            return True
        return False

    async def _pending_thought(self, delegation_id: str, revision: int, request: str) -> tuple[str, bool]:
        """A request held because it trailed off ("Want me to send that?"): a yes sends it, a no drops
        it, more words finish it. (request, True) when this handoff is handled here."""
        pending = getattr(self, "pending_thought", None)
        if pending is None:
            return request, False
        self.pending_thought = None
        earlier, _context, at = pending
        if time.monotonic() - at > PENDING_THOUGHT_S:
            return request, False
        words = " ".join(request.split())
        if router._AGREE.search(words) or re.fullmatch(r"(?i)\W*(?:yes|yeah|yep|sure)?\W*(?:send|send it|send that)\W*", words):
            logger.info("speakeasy: the held request was confirmed; sending it")
            self.confirmed_thought = delegation_id  # not a repeat of the words held back
            return earlier, False
        if router.meta_kind(words):
            return request, False  # "wait, I'm not done": still unsent, and answered as talk
        if re.fullmatch(r"(?i)\W*(?:no|nope|nah|not yet|never ?mind|forget it|don'?t)\W*", words):
            logger.info("speakeasy: the held request stays unsent")
            await self._drop(delegation_id, revision, "Not sent", None)
            await self.append("session.thinking.append", delegation_id, P.hold_note(self.names))
            return request, True
        if set(_words(earlier).split()) <= set(_words(words).split()):
            return request, False  # the voice handed off the whole thought again
        self.confirmed_thought = delegation_id
        return f"{earlier.rstrip()} {request.lstrip()}", False

    async def _meta_by_voice(self, delegation_id: str, revision: int, kind: str, marked: Any = None) -> None:
        """Talk about the conversation itself never starts or adds to work. "Wait, I wasn't done":
        stop and listen. "No, don't add that": take back what just went out. "For what?" / "what
        test run?": explain, from the job if one is running."""
        self.hold_speech()
        self.handoff_at.pop(delegation_id, None)
        if kind == router.META_UNDO:
            took = await self._undo_recent()
            logger.info("speakeasy: told not to send it (%s)", "took it back" if took else "nothing to take back")
            await self._drop(delegation_id, revision, "Held back", None)
            await self.append("session.thinking.append", delegation_id, P.undo_note(self.names, took))
            return
        if kind == router.META_CONFUSED:
            target = self._status_target(marked)
            if target is not None:
                logger.info("speakeasy: asked what the voice meant; answered from the job")
                await self.answer_status(delegation_id, target, confused=True)
                return
            logger.info("speakeasy: asked what the voice meant; answered by the voice (no task)")
            await self._drop(delegation_id, revision, "Talked it through", None)
            await self.append("session.thinking.append", delegation_id, P.confused_note(self.names))
            return
        logger.info("speakeasy: the user isn't done; listening (no task)")
        await self._drop(delegation_id, revision, "Listening", None)
        await self.append("session.thinking.append", delegation_id, P.hold_note(self.names))

    async def _undo_recent(self) -> str | None:
        """Take back the newest thing sent from what they just said (UNDO_WINDOW_S): words added to a
        task, or a task that started. Returns its name, or None when nothing recent went out."""
        routed = messages_mod._recent_route(self)
        if routed is not None:
            name = messages_mod.task_name(self, routed["task_id"] or "")
            await messages_mod._take_back(self, routed)
            self.last_routed = None
            return name
        now = time.monotonic()
        for _words_, at, task_id, _request in reversed(list(getattr(self, "started", []))):
            if now - at > UNDO_WINDOW_S:
                break
            with self.interaction.lock:
                run = self.interaction.runs.get(task_id)
            if run is None or run.status not in ACTIVE_RUN_STATES:
                continue
            name = self.spoken_task_name(run.idem_key) or "that"
            if run.run_id and run.status in {"running", "working"}:
                await self._supersede(task_id)
            with self.interaction.lock:
                run.status, run.error = "rejected", "Taken back"
            self.store.update_run(run.idem_key, None, "cancelled")
            self.store.progress(run.idem_key, "milestone", "Taken back: you weren't done")
            self.store.dismiss_key(run.idem_key)
            self.publish()
            return name
        return None

    async def _set_aside_by_voice(self, delegation_id: str, revision: int, kind: str, marked: Any = None) -> bool:
        """"It's already handled", "never mind that one": set aside the question waiting on the
        user instead of starting work. A plain "never mind" counts only when the waiting question
        is the newest thing on the call (otherwise it stops the request just made)."""
        pending = self.home_question
        if pending and time.monotonic() - pending[2] <= home_control_mod.ASK_WINDOW_S:
            self.home_question = None
            await self._drop(delegation_id, revision, "Set aside", None)
            await self.append("session.thinking.append", delegation_id, P.set_aside_note(pending[0], by_voice=True))
            return True
        waiting = self.waiting_on_user()
        if not waiting:
            return False
        focused, _ = self.focus()
        target = next((t for t in (marked, focused) if isinstance(t, str) and t in waiting), waiting[-1])
        if kind == "weak":
            with self.interaction.lock:
                live = [r.delegation_id for r in self.interaction.runs.values() if r.status != "rejected"]
            if not live or live[-1] != target:
                return False
        logger.info("speakeasy: question set aside by voice")
        await self._drop(delegation_id, revision, "Set aside", None)
        return await self.set_aside(target, by_voice=True)

    # -- answering an approval by voice --------------------------------------------------------
    def pending_approvals(self) -> list[tuple[str, BackendRun]]:
        """This call's tasks waiting on an approval Speakeasy can answer (oldest first)."""
        with self.interaction.lock:
            runs = [(t, r) for t, r in self.interaction.runs.items()
                    if r.status == "waiting_for_approval" and r.approval and r.run_id]
        return [(t, r) for t, r in runs if t not in self.set_aside_tasks]

    async def _approve_by_voice(self, delegation_id: str, revision: int, request: str, marked: Any = None) -> bool:
        """"Approve", "approved", "no", "for the rest of this task" while an approval waits: answer it.
        Never "always" (refused here and in hermes_api.approve); "session" only when Hermes offered it
        for that request. With several approvals waiting and nothing naming one, ask which."""
        pending = self.pending_approvals()
        if not pending:
            return False
        choice = router.approval_answer(request)
        if choice is None:
            return False
        focused, _ = self.focus()
        target = next(((t, r) for t, r in pending if t in {marked, focused}), None)
        if target is None and len(pending) > 1:
            await self._drop(delegation_id, revision, "Asked which approval", None)
            await self.append("session.commentary.append", delegation_id,
                              P.approval_which([(r.approval or {}).get("description") or "" for _, r in pending]))
            return True
        task_id, run = target or pending[0]
        offered = list((run.approval or {}).get("choices") or ["once", "deny"])
        await self._drop(delegation_id, revision, "Answered the approval", None)
        if choice == "always":
            await self.append("session.commentary.append", delegation_id,
                              P.approval_always_refused("session" in offered))
            return True
        if choice == "session" and "session" not in offered:
            await self.append("session.commentary.append", delegation_id, P.SESSION_NOT_OFFERED_SPOKEN)
            return True
        choice = "once" if choice == "yes" else choice
        logger.info("speakeasy: approval answered by voice (%s)", choice)
        confirmed = await self.resolve_approval(task_id, choice)
        if confirmed is None:
            await self.append("session.commentary.append", delegation_id, P.approval_failed_spoken(self.names))
            return True
        await self.append("session.commentary.append", delegation_id, P.approval_done_spoken(choice))
        return True

    async def resolve_approval(self, task_id: str, choice: str) -> str | None:
        """Send one answer to the approval a task is waiting on. Returns the choice once Hermes
        confirmed it (a 2xx reply), else None with the approval left waiting (or ambiguous when the
        reply never came)."""
        with self.interaction.lock:
            run = self.interaction.runs.get(task_id)
            approval = run.approval if run else None
            if run is None or run.status != "waiting_for_approval" or not approval or not run.run_id:
                return None
            run.approval, run.status = None, "resolving_approval"
        self.publish()
        try:
            await asyncio.to_thread(self.hermes.approve, run.run_id, approval["request_id"], choice,
                                    approval.get("choices") or ["once", "deny"])
        except HermesError as exc:
            logger.warning("speakeasy: approval by voice refused (%s)", exc.status)
            with self.interaction.lock:
                if run.status == "resolving_approval":
                    if 400 <= exc.status < 500:
                        run.approval, run.status = approval, "waiting_for_approval"  # nothing changed
                    else:
                        run.status, run.error = "ambiguous", "Backend approval outcome is ambiguous"
            self.publish()
            return None
        except Exception:
            with self.interaction.lock:
                if run.status == "resolving_approval":
                    run.status, run.error = "ambiguous", "Backend approval outcome is ambiguous"
            self.publish()
            return None
        with self.interaction.lock:
            if run.status == "resolving_approval":
                run.status = "working"
        self.store.update_run(run.idem_key, None, "working")
        self.store.progress(run.idem_key, "milestone", APPROVED_MILESTONE[choice])
        self.publish()
        return choice

    # -- same-project fence: what the user says about a task held behind another -------------
    async def _fence_by_voice(self, delegation_id: str, revision: int, request: str, marked: Any = None) -> bool:
        with self.interaction.lock:
            held = [(t, r) for t, r in self.interaction.runs.items()
                    if r.status == "waiting_on_task" and not r.released]
        if not held:
            return False
        choice = router.fence_choice(request)
        if choice is None:
            return False
        task_id, run = next(((t, r) for t, r in held if t == marked), held[-1])
        with self.interaction.lock:
            blocker = self.interaction.runs.get(run.blocked_by or "")
        await self._drop(delegation_id, revision, "Answered about the waiting task", None)
        if choice == "wait":
            await self.append("session.commentary.append", delegation_id, P.FENCE_WAITING_SPOKEN)
        elif choice == "anyway":
            with self.interaction.lock:
                run.released = True
            await self.append("session.commentary.append", delegation_id, P.FENCE_ANYWAY_SPOKEN)
        else:
            if blocker is None or not blocker.run_id or blocker.run_id.startswith(LOCAL_RUN_PREFIX) \
                    or blocker.status not in ACTIVE_RUN_STATES:
                await self.append("session.commentary.append", delegation_id, P.FENCE_CANT_STOP_SPOKEN)
                return True
            name = self.store.title(blocker.idem_key) or notice_text(self.store.request_text(blocker.idem_key), 80) or "that task"
            with self.interaction.lock:
                blocker.status = "cancel_requested"
            self.store.update_run(blocker.idem_key, None, "cancel_requested")
            self.store.progress(blocker.idem_key, "milestone", "Stop requested")
            self.publish()
            try:
                await asyncio.to_thread(self.hermes.stop, blocker.run_id)
            except Exception:
                with self.interaction.lock:
                    if blocker.status == "cancel_requested":
                        blocker.status, blocker.error = "ambiguous", "Backend stop outcome is ambiguous"
                self.publish()
                await self.append("session.commentary.append", delegation_id, P.FENCE_CANT_STOP_SPOKEN)
                return True
            await self.append("session.commentary.append", delegation_id, P.fence_stopping_spoken(name))
        return True

    def fence_blocker(self, task_id: str, request: str,
                      exclude: set[str | None] | None = None) -> tuple[BackendRun, str] | None:
        """A running task already editing the project this new request would edit, and that
        project's name; None when unsure (fence.py says when it is sure)."""
        if not fence_mod.is_edit_request(request):
            return None
        wanted = fence_mod.keys_in_text(request)
        if not wanted:
            return None
        with self.interaction.lock:
            running = [r for t, r in self.interaction.runs.items()
                       if t != task_id and t not in (exclude or set()) and r.status in ACTIVE_RUN_STATES]
        for other in reversed(running):
            asked = self.store.request_text(other.idem_key) or ""
            with self.interaction.lock:
                touched, edits = set(other.projects), other.edits
            keys = (fence_mod.keys_in_text(asked) | touched) if fence_mod.is_edit_request(asked) else \
                (touched if edits else set())
            project = fence_mod.overlap(wanted, keys)
            if project:
                return other, project
        return None

    async def wait_for_fence(self, backend: BackendRun, blocker: BackendRun, project: str, delegation_id: str) -> None:
        """Hold a new task until the task editing the same project ends (or the user says go)."""
        idem = backend.idem_key
        name = self.store.title(blocker.idem_key) or notice_text(self.store.request_text(blocker.idem_key), 80) or "the other task"
        with self.interaction.lock:
            backend.status, backend.blocked_by = "waiting_on_task", blocker.delegation_id
        self.store.update_run(idem, None, "waiting_on_task")
        self.store.set_waiting(idem, {"name": name, "project": project})
        self.store.progress(idem, "milestone", notice_text(f"Waiting for \"{name}\" ({project})", 200) or "Waiting")
        self.publish()
        logger.info("speakeasy: new task held behind a running task on the same project")
        await self.append("session.commentary.append", delegation_id, P.fence_spoken(name, project))
        waited = 0.0
        while True:
            with self.interaction.lock:
                released, ended = backend.released, blocker.status
            if released or ended not in ACTIVE_RUN_STATES or waited >= FENCE_MAX_WAIT_S:
                break
            self.store.touch(idem)
            await asyncio.sleep(FENCE_POLL_S)
            waited += FENCE_POLL_S
        with self.interaction.lock:
            backend.status = "admitting"
            released, ended = backend.released, blocker.status
        self.store.set_waiting(idem, None)
        self.store.update_run(idem, None, "admitting")
        self.store.progress(idem, "milestone", "Started")
        self.publish()
        if released:
            return
        if ended in ACTIVE_RUN_STATES:  # waited the longest it may: start, and say why
            await self.append("session.commentary.append", delegation_id, P.fence_timeout_spoken(name))
        elif ended == "completed":
            await self.quiet("fence:" + idem, delegation_id, P.fence_started_note(name, ended))
        else:
            await self.append("session.commentary.append", delegation_id, P.fence_started_note(name, ended))

    async def release_fenced(self, backend: BackendRun, status: str) -> None:
        """A task ended: tasks held behind it start on their own (wait_for_fence polls; this only
        republishes so the board updates at once)."""
        with self.interaction.lock:
            held = any(r.blocked_by == backend.delegation_id and r.status == "waiting_on_task"
                       for r in self.interaction.runs.values())
        if held:
            self.publish()

    async def _drop(self, delegation_id: str, revision: int, reason: str, spoken: str | None,
                    context: str = "") -> None:
        """A handoff that should never become a task. It ends visibly (no lingering "Waiting for")."""
        drops = getattr(self, "drops", None)
        if drops is None:
            drops = self.drops = []
        drops[:] = drops[-9:] + [(reason, time.monotonic(), "", context)]
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
        last_request = whole_request(context, self._started_words())
        if is_greeting(last_request):
            # "Hey" opens a conversation; it never starts work.
            logger.info("speakeasy: greeting, no task started")
            await self._drop(delegation_id, revision, "Just a greeting", P.GREETING_SPOKEN)
            return
        if self.typed and messages_mod.typed_echo(self, last_request):
            # The voice handed off what the user typed to a task (it only heard it in a note): sent already.
            await self._drop(delegation_id, revision, "Already sent to the task", None)
            return
        if self.last_routed is not None and await messages_mod.retarget(self, delegation_id, revision,
                                                                        last_request, marked):
            return  # "no, the dashboard one" right after a follow-up: moved there, not a new task
        if delegation_id not in getattr(self, "force_work", set()):
            last_request, held_back = await self._pending_thought(delegation_id, revision, last_request)
            if held_back:
                return
            meta = router.meta_kind(last_request)
            if meta is not None:
                await self._meta_by_voice(delegation_id, revision, meta, marked)
                return
        self.speech_hold_until = 0.0  # they finished the thought: what waited for a pause can go
        aside = router.set_aside_kind(last_request)
        if aside and await self._set_aside_by_voice(delegation_id, revision, aside, marked):
            return
        if await self._approve_by_voice(delegation_id, revision, last_request, marked):
            return
        if await self._fence_by_voice(delegation_id, revision, last_request, marked):
            return
        forced = delegation_id in getattr(self, "force_work", set())
        if not marked and not forced and await self._talk_not_work(delegation_id, revision, last_request, context):
            return
        offer = next((l[len("Assistant: "):] for l in reversed(context.splitlines()) if l.startswith("Assistant: ")), "")
        offered = offer.rstrip().endswith("?")
        if (not marked and not forced and not offered and router.is_fragment(last_request)
                and not self.waiting_on_user() and self.home_question is None):
            # "that", "there it is": a scrap with nothing to act on, and no question waiting for it.
            logger.info("speakeasy: a %d-word fragment with nothing waiting on it; no task started",
                        len(last_request.split()))
            await self._drop(delegation_id, revision, "Didn't catch it", None)
            await self.append("session.commentary.append", delegation_id, P.DIDNT_CATCH)
            return
        if not marked and router.is_reaction(last_request) and offered:
            # "Yeah" to "Want me to look up what they charge?": the yes carries the offer with it.
            last_request = f"{last_request.rstrip('.!')}: {offer.strip()}"
        if self._already_absorbed(last_request):
            logger.info("speakeasy: handoff already folded into the task before it; not started twice")
            await self._drop(delegation_id, revision, "Added to the previous request", None)
            return
        if not marked:
            # "Our guest room, sorry" / "no, turn them on": replaces what it corrects, never runs on.
            corrected = await self._apply_correction(last_request, context)
            if corrected != last_request:
                last_request, context = corrected, _with_request(context, corrected)
        # "Stop looking at my screen" and "share my screen" are about sharing itself, never work: answered
        # here, in calls whose app said it can share (others go on as words, as before).
        intent = router.screen_intent(last_request) if self.interaction.attachments.declared else None
        if intent is not None:
            rest = router.after_stop(last_request) if intent == "stop" else ""
            if not rest and not marked:
                # The voice sometimes hands the same words off twice: hint (or stop) once.
                if self._echoes_recent(last_request):
                    await self._drop(delegation_id, revision, "Added to the previous request", None)
                    return
                self._remember_request(last_request)
            await self.screen_words(delegation_id, intent, more=bool(rest))
            if not rest:
                return
            last_request = rest  # sharing is off now; the rest of what they asked still goes ahead
        held = self.held
        if held is not None and (router.acknowledges(last_request) or _same_words(last_request, held.request)):
            # "Ok, one sec" or "I'm turning on the eye" while they turn sharing on, or the same words
            # handed off again: the request waiting for the screen keeps waiting, and this one never
            # becomes a task.
            self.handoff_at.pop(delegation_id, None)
            await self._drop(delegation_id, revision, "Waiting for the screen", None)
            return
        if held is None and self.repeats_released(last_request):
            # "Now look at my screen" just as the request that waited for it went: that one has the screen.
            logger.info("speakeasy: asked to look again as the held request went; not started twice")
            self.handoff_at.pop(delegation_id, None)
            await self._drop(delegation_id, revision, "Already on its way with your screen", None)
            return
        if not marked and delegation_id != getattr(self, "confirmed_thought", None) and (
                self._echoes_recent(last_request) or (self._duplicate(last_request) and not offered)):
            logger.info("speakeasy: handoff repeats a request started seconds ago; not started twice")
            await self._drop(delegation_id, revision, "Added to the previous request", None)
            return
        if not marked:
            self._remember_request(last_request)
        self._note_started(delegation_id, last_request)
        self.end_hold("superseded")  # a newer request ends one waiting for the screen
        # Pointing at their own screen, or with pictures waiting on the panel, "show me this" is about
        # what they're showing; only "show me what you're looking at" still means a task's own view.
        # Only in a call that can share: elsewhere (an iPhone, an older Mac) "this page" is a task's, as before.
        refs = bool(self.interaction.attachments.declared) and router.refers_to_screen(last_request)
        showing = refs or bool(self.interaction.attachments.pending())
        if (router.is_show_me(last_request) and (not showing or router.about_task_view(last_request))
                and await self.show_me(delegation_id, last_request)):
            return
        if router.is_status_question(last_request) or router.asks_about_task(last_request):
            target = self._status_target(marked)
            if target is not None:
                await self.answer_status(delegation_id, target)
                return
        with self.interaction.lock:
            device, client = self.interaction.device_id, self.interaction.client
        logger.info("speakeasy: request on device %s%s (%d words)", (device or "unknown")[:8],
                    f" [{client_label(client)}]" if client else "", len(last_request.split()))
        home_follow = isinstance(marked, str) and marked in self.home_tasks
        if (not marked or home_follow) and await self.home_control(delegation_id, revision, last_request):
            return
        showing = self.bind_shared(delegation_id, last_request)
        # In a call that declared ``ready`` but isn't sharing, a request that needs the screen waits for
        # it once settled (``wait_for_screen``, in _dispatch_bound), instead of going on without it.
        try:
            await self._dispatch_bound(delegation_id, revision, context, marked, last_request, showing)
        finally:
            # Held, and sharing came on before the hold's own hint was out: the release dispatches this
            # handoff now and owns what it bound (dispatch_held finishes it), so it isn't given back here.
            if delegation_id not in self.releasing:
                self.finish_shared(delegation_id)

    async def _dispatch_bound(self, delegation_id: str, revision: int, context: str, marked: Any,
                              last_request: str, showing: bool, released: bool = False) -> None:
        """The rest of a handoff, once it holds what the user shared with it (``bind_shared``).
        ``showing``: it carries pictures or files or points at the screen, so the quick lanes and a
        task's own picture are never what it means. ``released``: a held request going now that
        sharing is on (already settled, never held again)."""
        # Home commands stay instant. Anything else waits a beat in case the user is still talking,
        # so a breath mid-thought doesn't send half a request.
        if not released and not marked and router.continues_newest(last_request, self.open_tasks()) is None:
            before_settle = last_request
            settled = await self.settle(last_request)
            if settled != last_request and self.interaction.attachments.declared and router.refers_to_screen(settled):
                self.share_refs[delegation_id] = showing = True  # "what's" ... "this error?"
            last_request = settled
            if settled != before_settle:
                context = _with_request(context, last_request)
            self._note_started(delegation_id, last_request)
            if (router.trails_off(last_request) and router.meta_kind(last_request) is None
                    and delegation_id != getattr(self, "confirmed_thought", None)):
                # "...because he": the thought stopped mid-sentence. Never send a fragment: ask.
                self.pending_thought = (last_request, context, time.monotonic())
                logger.info("speakeasy: request trails off (%d words); asked before sending", len(last_request.split()))
                await self._drop(delegation_id, revision, "Waiting for the rest", None)
                await self.append("session.thinking.append", delegation_id, P.unfinished_note(self.names))
                return
        if not released and await self.wait_for_screen(delegation_id, revision, context, marked, last_request):
            return
        if not marked and not showing and await self.quick_answer(delegation_id, revision, last_request):
            return
        focus = None
        if not marked:
            focused, detail = self.focus()
            if focused and not router.wants_new(last_request):
                # The task on screen (or in the headset) is a strong prior for the routing model, not
                # an order: an unrelated request still goes elsewhere.
                focus = router.Focus(focused, self.focus_job(focused), detail)
        candidates = await self.conversation_candidates(last_request)
        now = time.time()
        chats = [router.Chat(f"c{i + 1}", c.conv.where, c.snippets, c.voice_request,
                             max(0.0, now - c.conv.last_active) if c.conv.last_active else None)
                 for i, c in enumerate(candidates)]
        earlier = "\n".join(line for line in context.splitlines()[:-1] if line.startswith(("User: ", "Assistant: ")))
        try:
            decision = await asyncio.to_thread(self.rt.route, last_request, self.open_tasks(), marked, chats,
                                               earlier[-1200:], replied_task_id=self.replied_task,
                                               **({"focus": focus} if focus else {}))
        except Exception as exc:  # routing is a nicety: without it the request still becomes new work
            logger.warning("speakeasy: routing failed, starting as new work (%s)", type(exc).__name__)
            decision = router.fallback_decision(last_request)
        self.route_timings[delegation_id] = decision.latency_ms
        part = decision.parts[0]
        if focus is not None:
            went = "to the focused task" if part.task_id == focus.task_id else "elsewhere"
            logger.info("speakeasy: focused request routed %s (%s)", went, decision.source)
            if part.task_id == focus.task_id and focus.detail:
                part = router.Part(part.kind, f"{part.request}\n\n({self.names.user} is looking at: {focus.detail})",
                                   part.task_id)
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
        if decision.show and not showing:
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
            await messages_mod.announce_routed(self, delegation_id, revision, context, part)
            await self.follow_up(delegation_id, revision, context, part)
            return
        named = self.rt.explicit_channel(last_request)
        if named is None and len(decision.parts) == 1:
            conv = self.pick_conversation(last_request, decision, candidates, chats)
            if conv is not None:
                await self.start_continuity_task(delegation_id, revision, context, last_request, conv)
                return
        # What the user shared is answered home unless they named a channel: a topical channel is a
        # group chat they didn't pick for it.
        choice = named or self.rt.topical_channel(None if self.carries(delegation_id) else decision.channel)
        if choice.how == "topic" and choice.channel is not None and not choice.channel.default:
            self.forgo_capture(delegation_id)  # a riding-along capture never goes to a topical channel
        if len(decision.parts) > 1 and not choice.clarify:
            convs = {} if named is not None else {
                i: conv for i, p in enumerate(decision.parts)
                if (conv := self.pick_conversation(p.request, router.Decision([p], conversation=p.conversation),
                                                   candidates, chats)) is not None}
            await self.start_parts(delegation_id, revision, context, [p.request for p in decision.parts], choice,
                                   convs)
            return
        await self.start_routed(delegation_id, revision, context, last_request, choice)

    def _status_target(self, marked: Any = None) -> str | None:
        """The task a status question is about: the marked one if it's still open, else the newest
        one actually doing work (one queued behind another, or with nothing reported, comes last)."""
        running = [t for t in self.open_tasks() if t.status not in TERMINAL]
        if not running:
            return None
        if isinstance(marked, str) and any(t.task_id == marked for t in running):
            return marked
        with self.interaction.lock:
            runs = {t.task_id: self.interaction.runs.get(t.task_id) for t in running}
        def busy(t: router.OpenTask) -> tuple[int, int]:
            run = runs.get(t.task_id)
            key = run.idem_key if run else None
            items = len(activity_mod.speech_steps(self.store, key)[0]) if key else 0
            return (int(t.status in {"running", "working", "waiting_for_approval"}), int(items > 0))
        return max(enumerate(running), key=lambda p: (busy(p[1]), p[0]))[1].task_id

    async def answer_status(self, delegation_id: str, task_id: str, confused: bool = False) -> None:
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
        live: list[str] = []
        session = (work or {}).get("session_id") or ((work or {}).get("job") or {}).get("id")
        if status not in TERMINAL and isinstance(session, str) and session:
            live = await asyncio.to_thread(self.live_steps, session)
        self.handoff_at.pop(delegation_id, None)
        self.replied_task = task_id
        logger.info("speakeasy: status answered from %d steps (%s)", len(steps), status or "unknown")
        request = self.store.request_text(key) if key else ""
        tools = [i for i in ((work or {}).get("activity") or []) if isinstance(i, dict)]
        findings = [f.get("text", "") for f in ((work or {}).get("findings") or [])]
        queued = (work or {}).get("queued_behind")
        await self.append("session.commentary.append", delegation_id,
                          P.status_answer(name or "that task", status, steps, time.monotonic() - started, result,
                                          live, request=request, tools=[i.get("text", "") for i in tools if
                                                                        not str(i.get("text", "")).startswith("Found: ")],
                                          findings=findings, queued_behind=queued, confused=confused))

    def live_steps(self, session_id: str) -> list[str]:
        """What the task's Hermes session has done lately, in plain words, newest last: the voice's
        answer to "how's it going?" comes from the work itself, not only what the task chose to report."""
        try:
            rows = threads.recent_steps(self.rt.state_db, session_id)
        except Exception:
            return []
        out: list[str] = []
        for kind, text in rows:
            line = notice_text(text, 160) if kind == "said" else P.tool_step(text)
            if line and (not out or out[-1] != line):
                out.append(line)
        return out[-5:]

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
        cards: list[dict[str, Any]] = []
        if reply.ok and getattr(reply, "touched", ()) and hasattr(home, "device_views"):
            try:  # the devices it moved, with on/off buttons (POST .../card-action "toggle")
                cards = await asyncio.to_thread(home.device_views, reply.touched)
            except Exception as exc:
                logger.info("speakeasy: home cards unavailable (%s)", type(exc).__name__)
        with self.interaction.lock:
            self.interaction.runs[delegation_id] = backend
            self.interaction.latest_delegation_id = delegation_id
        try:
            self.store.reserve_run(idem, self.interaction.interaction_id, delegation_id, revision)
            # Named for what the house did ("Office lamp off · Bedroom lamps on"), else the ask itself.
            self.store.set_title(idem, (home_title(reply.spoken) if reply.ok else None) or short_title(request) or "Home")
            self.store.progress(idem, "request", request)
            self.store.update_run(idem, None, backend.status)
            result = split_result(reply.spoken, ()) or {}
            if cards:
                result["views"] = cards
            self.store.set_result(idem, result or None)
            self.store.progress(idem, "result", reply.spoken)
            if reply.ok:
                # "Lights off" -> "Done": heard on the call, nothing to read, so no new-answer badge.
                self.store.mark_seen(idem)
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
        can_share = bool(self.interaction.attachments.declared)  # "what's this error?" means their screen
        if not fast.get("quick_answers", True) or not quick_mod.eligible(request, can_share=can_share):
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
            self.retitle(idem, request)
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

    def request_capture(self, delegation_id: str) -> str | None:
        """Ask the app for a capture of the frontmost window for this handoff: only in a call that
        declared ``screen: ready``, with sharing on. Listed in the snapshot (polling clients) and
        published once as a ``capture`` event; asking again while it is open returns the same id.
        The app confirms with ``capturing`` before capturing and gets 410 once the request has
        closed, so a stale or replayed event never captures anything. None when it can't capture."""
        shared = self.interaction.attachments
        before = shared.capture_for(delegation_id)
        capture_id = shared.open_capture(delegation_id)
        if capture_id is None or capture_id == before:
            return capture_id
        self.publish()  # the snapshot first, so a polling app sees it too
        self.interaction.feed.publish("capture", {"capture_id": capture_id})
        return capture_id

    def take_attachments(self, delegation_id: str, idem: str) -> list[Attachment]:
        """Hand over the pictures and files bound to this handoff (bytes included) and mark them
        sent. Saved files are recorded on the task first, so clearing the task deletes Speakeasy's
        copies. Call once, when the task's run starts."""
        shared = self.interaction.attachments
        paths = [a.path for a in shared.bound(delegation_id) if a.path]
        if paths:
            self.store.add_shared_files(idem, paths)
        taken = shared.take_bound(delegation_id)
        self.publish()
        return taken

    # -- look at this: binding, collecting and attaching ------------------------------------------
    def bind_shared(self, delegation_id: str, request: str) -> bool:
        """Past the home lane, a handoff claims the pictures and files waiting on the panel and, in a
        call that is sharing its screen, asks the app for a capture now, so it overlaps the settle wait
        and routing. True when it carries pictures or files or its words point at the screen (only in a
        call that can share: elsewhere "this page" is never theirs). A short reply to a question just
        asked takes that question's capture (``keep_capture``); anything else gets a capture of its own."""
        attachments = self.interaction.attachments
        bound = attachments.bind_pending(delegation_id)
        refs = bool(attachments.declared) and router.refers_to_screen(request)
        self.share_refs[delegation_id] = refs
        answers = len(request.split()) <= KEPT_ANSWER_WORDS and not refs  # "Just keep it with me"
        capture_id = (self.adopt_kept_capture(delegation_id if answers else None)
                      or self.request_capture(delegation_id))
        if capture_id:
            self.capture_ids[delegation_id] = capture_id
        if bound:
            self.publish()  # the panel shows them as on their way
        return self.share_refs[delegation_id] or bool(bound)

    def finish_shared(self, delegation_id: str) -> None:
        """The handoff is done. What it still holds goes back to the panel (pictures and files no run
        took) or closes (a capture nobody took is discarded), so nothing taken for it lingers."""
        back = self.interaction.attachments.release(delegation_id)
        for held in (self.share_refs, self.capture_ids, self.share_jobs):
            held.pop(delegation_id, None)
        self.no_capture.discard(delegation_id)
        if back:
            self.publish()

    def drop_capture(self, task_id: str) -> None:
        """Words only after all (a plain follow-up into a running task): its capture is discarded."""
        base = _base(task_id)
        capture_id = self.capture_ids.pop(base, None)
        if capture_id:
            self.interaction.attachments.close_capture(capture_id)

    def keep_capture(self, delegation_id: str) -> None:
        """The handoff asked a question instead of starting work: its capture waits for the answer
        (KEEP_CAPTURE_S), which carries what was on screen when the question came up."""
        capture_id = self.capture_ids.pop(delegation_id, None)
        if capture_id and self.interaction.attachments.move_capture(capture_id, f"kept_{delegation_id}"):
            self.adopt_kept_capture(None)  # an older one is dropped
            self.kept_capture = (capture_id, time.monotonic())

    def adopt_kept_capture(self, delegation_id: str | None) -> str | None:
        """The next handoff takes the kept capture (the answer to that question); a stale one closes,
        and so does one the next handoff doesn't answer with (``delegation_id`` None)."""
        kept, self.kept_capture = self.kept_capture, None
        if kept is None:
            return None
        capture_id, at = kept
        attachments = self.interaction.attachments
        if delegation_id and time.monotonic() - at <= KEEP_CAPTURE_S and attachments.move_capture(capture_id, delegation_id):
            return capture_id
        attachments.close_capture(capture_id)
        return None

    def carries(self, task_id: str) -> bool:
        """The handoff must take something to Hermes besides its words: pictures or files bound to it,
        or a capture its words point at ("what's this error?"). Such a request never goes into a group
        chat, a chat thread or a channel the user didn't name. A capture that only rides along because
        sharing is on doesn't count: where the request goes is decided as usual, and the capture is
        given up (``forgo_capture``) wherever it may not go."""
        base = _base(task_id)
        if base not in self.share_refs:
            return False
        job = self.share_jobs.get(base)
        if job is not None and job.done() and not job.cancelled() and job.exception() is None:
            shared = job.result()
            return bool(shared.pictures or shared.files) or (self.share_refs[base] and shared.capture is not None)
        return self.shares_with_task(task_id)

    def forgo_capture(self, task_id: str) -> None:
        """This request goes somewhere a capture may not (a group chat, a thread, a topical channel) and
        its words don't need one: its capture is discarded and none is asked for at task start."""
        base = _base(task_id)
        if base in self.share_refs:
            self.no_capture.add(base)
            self.drop_capture(base)

    def shares_with_task(self, task_id: str) -> bool:
        """A follow-up into a running task brings what it shares only when it has pictures or files,
        or its words point at the screen while a capture is coming. Otherwise it's words only."""
        base = _base(task_id)
        if base not in self.share_refs:
            return False
        attachments = self.interaction.attachments
        return bool(attachments.bound(base)) or (self.share_refs[base] and (
            base in self.capture_ids or attachments.can_capture))

    async def collect_shared(self, task_id: str, idem: str) -> Shared:
        """Everything this task's handoff carries, collected once per handoff (a split request's parts
        share one capture and the same pictures and files). Nothing for a task no handoff bound: a
        question-card answer never waits for or carries a capture."""
        base = _base(task_id)
        if base not in self.share_refs:
            return Shared()
        job = self.share_jobs.get(base)
        if job is None:
            job = self.share_jobs[base] = asyncio.ensure_future(self._collect(base, idem))
        try:
            return await asyncio.shield(job)
        except Exception as exc:  # never break a task over what it carries: it goes with words only
            logger.warning("speakeasy: could not collect what was shared (%s)", type(exc).__name__)
            return Shared()

    async def _collect(self, base: str, idem: str) -> Shared:
        """Take the handoff's pictures and files (recorded on ``idem`` first, so their copies are never
        deleted in between) and wait for its capture: up to CAPTURE_WAIT_S, longer while the app
        reports it is uploading. Sharing turned on after the handoff: the capture is asked for now.
        A capture that would break the request's image budget is dropped, with a reason."""
        attachments = self.interaction.attachments
        capture_id = self.capture_ids.get(base)
        if capture_id is None and base not in self.no_capture:
            capture_id = self.request_capture(base)
            if capture_id:
                self.capture_ids[base] = capture_id
        taken = self.take_attachments(base, idem)
        shared = Shared(pictures=[a for a in taken if a.is_image], files=[a for a in taken if not a.is_image])
        if capture_id:
            result = await attachments.wait_capture(capture_id, A.CAPTURE_WAIT_S, A.CAPTURE_UPLOAD_WAIT_S)
            shared.capture, shared.missing = result.attachment, result.reason
        if shared.capture is not None and not _fits(shared.pictures + [shared.capture]):
            shared.capture, shared.missing = None, "budget"  # the capture goes first
        while shared.pictures and not _fits(shared.pictures):  # intake leaves room, so never in practice
            logger.warning("speakeasy: a picture over the request's image budget was left out")
            shared.pictures.pop()
        await self.say_shared(base, shared)
        return shared

    async def say_shared(self, base: str, shared: Shared) -> None:
        """What the voice hears: silently, what went with the request; aloud, once per call, the first
        capture that went with a request that wasn't about the screen (naming the app); and why a
        capture that was due didn't go. It never hears names, contents or bytes."""
        capture = shared.capture
        if shared:
            await self.append("session.thinking.append", base, P.shared_note(
                capture.app if capture else None, capture is not None, len(shared.pictures), len(shared.files)))
        if capture is not None and not self.share_refs.get(base) and not self.capture_announced:
            self.capture_announced = True
            await self.append("session.commentary.append", base, P.screen_sent_line(capture.app))
        elif capture is None and (line := P.capture_missing_line(shared.missing)):
            await self.append("session.commentary.append", base, line)

    def save_shared(self, shared: Shared, idem: str) -> list[dict[str, Any]]:
        """Speakeasy's copies of what this task carries, recorded on it (clearing the task deletes
        them): images are saved once, into the shared folder inside the image roots so the app can
        show them later; files already were. Returns the task's card entries, server-side paths
        included."""
        folder = self.rt.shared
        with folder.lock if folder is not None else contextlib.nullcontext():  # saved and recorded at once
            for image in shared.images:
                if folder is not None and image.id not in shared.saved and image.data:
                    try:
                        shared.saved[image.id] = str(folder.save(image.data, _image_filename(image)))
                    except OSError as exc:
                        logger.warning("speakeasy: could not keep a copy of a shared image (%s)", type(exc).__name__)
            items = [{"kind": a.kind, "app": a.app, "name": None if a.kind == "screen" else a.name,
                      "path": shared.saved.get(a.id) or a.path, "at": shared.at} for a in shared.things]
            paths = [i["path"] for i in items if i["path"]]
            if paths:
                self.store.add_shared_files(idem, paths)
        return items

    def image_urls(self, shared: Shared) -> list[str]:
        """The images as data URLs for the run's input, when the backend reads images; otherwise the
        prompt names their saved copies instead. Never logged."""
        if not shared.images:
            return []
        try:
            readable = bool(self.hermes.capabilities().images)
        except Exception:
            readable = False
        if not readable:
            logger.info("speakeasy: the backend doesn't take images; naming the saved copies instead")
            return []
        return [_data_url(a) for a in shared.images]

    def shared_prompt(self, items: list[dict[str, Any]], attached: bool, session_id: str | None,
                      screen: bool) -> str:
        """The prompt's block about what was shared, plus a note when this session had a screenshot
        before and this run has none (Hermes sends the old one again with every run there)."""
        last = None if screen else self.store.last_screen_at(session_id)
        return " ".join(p for p in (P.shared_block(self.names, items, attached),
                                    P.stale_screen_line(last) if last else "") if p)

    # -- look at this: requests that wait for the screen, and spoken screen intents ---------------
    async def wait_for_screen(self, delegation_id: str, revision: int, context: str, marked: Any,
                              request: str) -> bool:
        """A settled request that needs the user's screen (``router.needs_screen``) in a call that can
        share but isn't sharing, carrying nothing else (no pictures or files, no capture): in a
        ``ready`` call it waits for the button (``hold_request``); in a ``no_permission`` call the
        voice says where to allow Screen Recording and nothing runs. True when it went no further.
        Calls that never said they can share (an iPhone, an older Mac) go on as words, and so does a
        follow-up the voice tied to a task (``marked``) or that continues the newest one: "what does
        this error mean" right after a task reported one is about that task's result."""
        attachments = self.interaction.attachments
        if (attachments.declared not in A.DECLARATIONS or attachments.screen_on
                or delegation_id in self.capture_ids or attachments.bound(delegation_id)
                or not router.needs_screen(request)):
            return False
        if marked or router.continues_newest(request, self.open_tasks()) is not None:
            return False
        self.handoff_at.pop(delegation_id, None)
        if attachments.declared == "no_permission":
            logger.info("speakeasy: a request needs the screen but Screen Recording is off; nothing started")
            self.interaction.feed.publish("screen.hint", {"reason": "no_permission"})
            await self.append("session.commentary.append", delegation_id, P.SCREEN_PERMISSION_HINT)
            return True
        await self.hold_request(Held(delegation_id, revision, context, marked, request))
        return True

    async def hold_request(self, held: Held) -> None:
        """Hold a request until sharing comes on: the panel shows "Waiting for your screen" (the
        snapshot's ``hold``), the button pulses (``screen.hint``) and the voice says how to turn it on.
        Sharing on within HOLD_S sends it through the usual path with a capture (``sharing_turned_on``);
        otherwise it ends unsent (``end_hold``). One at a time: a newer hold ends the older one."""
        self.end_hold("superseded")
        with self.interaction.lock:
            self.hold_token += 1
            held.token = self.hold_token
            self.held = held
            self.interaction.hold = {"state": "waiting", "text": P.HOLD_WAITING, "since": held.since}
            self.stop_words_after = time.monotonic()  # words said before it can't call it off
        attachments = self.interaction.attachments
        if attachments.screen_on:
            self.sharing_turned_on()  # it came on while this request was settling
            return
        timer = threading.Timer(HOLD_S, self.end_hold, args=("expired", held.token))
        timer.daemon = True
        timer.start()
        logger.info("speakeasy: request held until screen sharing comes on (up to %d s)", int(HOLD_S))
        self.publish()
        self.interaction.feed.publish("screen.hint", {"reason": "screen_off"})
        await self.append("session.commentary.append", held.delegation_id, P.screen_off_hint())
        if attachments.ended:
            self.end_hold("ended", held.token)  # the call paused or ended meanwhile

    def end_hold(self, reason: str, token: int | None = None) -> bool:
        """The request waiting for the screen won't run: HOLD_S passed (``expired``), a newer request
        came (``superseded``), "stop looking" (``stopped``), or the call paused or ended. Its line
        reads "Not sent: screen sharing was off" for HOLD_OUTCOME_S, then clears. ``token``: only that
        hold. Thread-safe (the expiry timer, HTTP threads, the call's loop). True when one ended."""
        with self.interaction.lock:
            held = self.held
            if held is None or token is not None and held.token != token:
                return False
            self.held = None
            outcome = {"state": "not_sent", "text": P.HOLD_NOT_SENT, "since": time.time()}
            self.interaction.hold = outcome
        logger.info("speakeasy: held request not sent (%s)", reason)
        clear = threading.Timer(HOLD_OUTCOME_S, self._clear_hold_outcome, args=(outcome,))
        clear.daemon = True
        clear.start()
        self.publish()
        if reason in {"expired", "superseded"}:
            self.speak_from_thread("session.thinking.append", P.HOLD_DROPPED_NOTE)
        return True

    def _clear_hold_outcome(self, outcome: dict[str, Any]) -> None:
        with self.interaction.lock:
            if self.interaction.hold is not outcome:
                return  # a newer hold, or already cleared
            self.interaction.hold = None
        self.publish()

    def sharing_turned_on(self) -> None:
        """Sharing just came on (the panel's button, on an HTTP thread): a request waiting for the
        screen goes now, through the usual path, so it gets a capture of its own. Words said before
        now can't turn sharing off. Thread-safe."""
        loop = self.loop
        with self.interaction.lock:
            self.stop_words_after = time.monotonic()
            held = self.held
            if held is None or loop is None or loop.is_closed():
                return  # nothing waiting (a call whose loop is gone ends its hold with end_shared)
            self.held, self.interaction.hold = None, None
        logger.info("speakeasy: screen sharing came on; the held request goes now")
        self.publish()
        try:
            loop.call_soon_threadsafe(self._start_release, held)
        except RuntimeError:  # the call's loop closed just now
            with self.interaction.lock:
                self.held = held
            self.end_hold("ended", held.token)
            return
        self.released_hold = (held.request, time.monotonic())  # "now look at my screen" right after: the same

    def repeats_released(self, request: str) -> bool:
        """Said within RELEASED_REPEAT_S of a held request going: the same words, talk about turning
        sharing on ("it's on, take a look"), or only a request to look ("now look at my screen"). The
        request that went has the screen; this one adds nothing."""
        released = self.released_hold
        if released is None or not request.strip() or time.monotonic() - released[1] > RELEASED_REPEAT_S:
            return False
        return (_same_words(request, released[0]) or router.about_sharing_on(request)
                or router.needs_screen(request) and router.only_looks(request))

    def _start_release(self, held: Held) -> None:
        task = asyncio.get_running_loop().create_task(self.dispatch_held(held))
        self.dispatch_tasks.add(task)
        task.add_done_callback(self.dispatch_tasks.discard)

    async def dispatch_held(self, held: Held) -> None:
        """The held request, now that sharing is on: it claims what's waiting on the panel and asks for
        a capture like any handoff (``bind_shared``), then goes on from where it was held. Ends visibly
        like ``dispatch``. While it runs it owns the handoff's binding (``releasing``): the handoff that
        was held may still be finishing (its hint can be on its way when sharing comes on), and must
        not give back what was just bound here."""
        self.releasing.add(held.delegation_id)
        try:
            await self.append("session.thinking.append", held.delegation_id, P.HOLD_RELEASED_NOTE)
            showing = self.bind_shared(held.delegation_id, held.request)
            try:
                await self._dispatch_bound(held.delegation_id, held.revision, held.context, held.marked,
                                           held.request, showing, released=True)
            finally:
                self.finish_shared(held.delegation_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("speakeasy: held request failed before its task started (%s: %s)",
                           type(exc).__name__, str(exc)[:200])
            await self._handoff_failed(held.delegation_id, held.revision, exc)
        finally:
            self.releasing.discard(held.delegation_id)

    async def screen_words(self, delegation_id: str, intent: str, more: bool = False) -> None:
        """A handoff about screen sharing itself (``router.screen_intent``), answered here: ``stop``
        turns it off (``stop_looking``); ``share`` explains the button, since the voice can never turn
        sharing on. A request waiting for the screen keeps waiting on ``share``. No task starts for
        these words; ``more``: the handoff asked for something else too, which the caller dispatches."""
        if not more:
            self.handoff_at.pop(delegation_id, None)
        attachments = self.interaction.attachments
        logger.info("speakeasy: spoken screen intent (%s); no task started", intent)
        if intent == "stop":
            await self.stop_looking(delegation_id, handoff=True)
        elif attachments.declared == "no_permission":
            self.interaction.feed.publish("screen.hint", {"reason": "no_permission"})
            await self.append("session.commentary.append", delegation_id, P.SCREEN_PERMISSION_HINT)
        elif attachments.screen_on:
            await self.append("session.commentary.append", delegation_id, P.SCREEN_ALREADY_SHARED)
        else:
            self.interaction.feed.publish("screen.hint", {"reason": "share_request"})
            await self.append("session.commentary.append", delegation_id, P.share_hint())

    async def stop_looking(self, delegation_id: str | None, handoff: bool) -> bool:
        """"Stop looking at my screen": sharing goes off from the plugin's side (the app adopts it from
        ``screen.state``; open capture requests close), a request waiting for the screen ends unsent,
        and one short line confirms it. Heard in the transcript and handed off for the same words, it
        acts once. A handoff while sharing was off anyway gets a brief answer. Never a Hermes task.
        True when something changed."""
        attachments = self.interaction.attachments
        state = attachments.stop_sharing()
        cancelled = self.end_hold("stopped")
        now = time.monotonic()
        if state is not None:
            logger.info("speakeasy: screen sharing turned off by voice on %s", self.interaction.interaction_id[:10])
            self.publish()  # the snapshot first, so a polling app sees it too
            self.interaction.feed.publish("screen.state", state)
            for kind, value in attachments.take_notes():
                await self.append("session.thinking.append", None, P.sharing_note(kind, value))
            line: str | None = P.SCREEN_STOPPED
        elif cancelled:
            line = P.SCREEN_HOLD_CANCELLED
        elif handoff and now - self.stopped_at > STOP_DEBOUNCE_S:
            line = P.SCREEN_ALREADY_OFF
        else:
            line = None  # heard in the transcript and handed off too: confirmed once already
        if state is not None or cancelled:
            self.stopped_at = now
        if line:
            await self.append("session.commentary.append", delegation_id, line)
        return state is not None or cancelled

    def _recent_user_words(self) -> tuple[list[str], float]:
        """The user's words not spent yet from the last STOP_WINDOW_S, as two readings (pieces run
        together, for word pieces; and spaced, for whole turns), and when the newest piece arrived."""
        now = time.monotonic()
        recent = [f for f in list(self.fragments) if f["speaker"] == "user" and f["at"] > self.stop_words_after
                  and now - f["at"] <= STOP_WINDOW_S]
        if not recent:
            return [], 0.0
        return ["".join(f["text"] for f in recent)[-600:], " ".join(f["text"] for f in recent)[-600:]], recent[-1]["at"]

    def listen_for_stop(self) -> None:
        """Each piece of the user's transcript as it arrives (both voice backends): "stop looking at my
        screen" turns sharing off even when the voice answers without handing it off. Only while
        sharing is on or a request waits for it, and acted on STOP_HEARD_SETTLE_S later, so words that
        follow ("... in Zoom") can still rule it out."""
        attachments = self.interaction.attachments
        if attachments.declared != "ready" or not (attachments.screen_on or self.held is not None):
            return
        if self.stop_check is not None and not self.stop_check.done():
            return
        texts, _ = self._recent_user_words()
        if any(router.stops_looking(text) for text in texts):
            self.stop_check = asyncio.get_running_loop().create_task(self._stop_heard())

    async def _stop_heard(self) -> None:
        await asyncio.sleep(STOP_HEARD_SETTLE_S)
        texts, newest = self._recent_user_words()
        if not any(router.stops_looking(text) for text in texts):
            return  # the words that followed made it something else
        with self.interaction.lock:
            self.stop_words_after = max(self.stop_words_after, newest)  # these words are spent
        logger.info("speakeasy: heard a stop-looking phrase in the transcript")
        await self.stop_looking(None, handoff=False)

    async def start_parts(self, delegation_id: str, revision: int, context: str, parts: list[str],
                          choice: channels.Choice,
                          convs: dict[int, continuity.Conversation] | None = None) -> None:
        """A compound request: one parallel task per part, one spoken acknowledgement for all. A part
        that continues an existing conversation runs in that conversation; the rest are new tasks."""
        convs = convs or {}
        channel = choice.channel
        deliver_to = channel.target if channel is not None and not channel.default else None
        said = [f"{part} (continuing in {notice_text(convs[i].where, 80) or 'an earlier chat'})" if i in convs
                else part for i, part in enumerate(parts)]
        await self.append("session.thinking.append", delegation_id, P.split_note(self.names, said))
        self.handoff_at.pop(delegation_id, None)
        self.route_timings.update({f"{delegation_id}-p{i}": self.route_timings.get(delegation_id, 0)
                                   for i in range(len(parts))})
        await asyncio.gather(*(
            self.start_continuity_task(f"{delegation_id}-p{i}", revision, context, part, convs[i],
                                       voice_id=delegation_id) if i in convs else
            self.start_task(f"{delegation_id}-p{i}", revision, context, part, focus=part,
                            voice_id=delegation_id, deliver_to=deliver_to)
            for i, part in enumerate(parts)))

    async def start_routed(self, delegation_id: str, revision: int, context: str, request: str,
                           choice: channels.Choice) -> None:
        """A new task, placed by channel routing: ask when unclear, open a thread where the
        channel wants one (and Hermes can), else run here and post the answer to the channel."""
        if choice.clarify:
            self.handoff_at.pop(delegation_id, None)
            self.keep_capture(delegation_id)  # the answer carries what was on screen when this was asked
            await self.append("session.commentary.append", delegation_id, choice.clarify)
            return
        channel = choice.channel
        routed = channel is not None and not channel.default
        runner = self.rt.threads
        # A thread is a group chat's session: what the user shared never goes there.
        if channel is not None and channel.new_thread and runner is not None and not self.carries(delegation_id):
            available = await asyncio.to_thread(runner.available, channel.target)
            if available:
                self.forgo_capture(delegation_id)
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
        sharing = self.shares_with_task(delegation_id)
        self.store.link_job(self._idem(delegation_id, revision), key)  # whatever it starts is this job

        async def answered_home(why: str) -> None:
            # A new task that references the running one carries what was shared, answered home.
            logger.info("speakeasy: %s; starting a task that references it", why)
            await self.start_task(delegation_id, revision, context, f"{part.request} (following up: {earlier})",
                                  focus=P.follow_up_focus(part.request, earlier, None, status), parent=key)

        if status in ACTIVE_RUN_STATES and sharing and target.deliver_to:
            # A running task that posts its answer to a channel (one the user may never have named for
            # this) or works in a chat thread (a group chat's session) never gets what was shared.
            await answered_home("shared things never go into a task that posts to a channel or a thread")
            return
        if status == "waiting_on_task":
            # Not started yet (held behind another task): what they add goes in with it when it starts.
            with self.interaction.lock:
                target.held_words.append(part.request)
                self.interaction.latest_delegation_id = target.delegation_id
            self.drop_capture(delegation_id)
            self.store.progress(key, "milestone", f"You added: {notice_text(part.request, 200)}")
            self.publish()
            self.handoff_at.pop(delegation_id, None)
            await self.append("session.thinking.append", delegation_id, P.held_follow_up_note(earlier))
            return
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
                # Only the user's own DM takes what was shared (start_continuity_task decides).
                joins = status in ACTIVE_RUN_STATES
                await self.start_continuity_task(delegation_id, revision, context, part.request, conv,
                                                 joins=target if joins else None, parent=key)
                return
            if status in ACTIVE_RUN_STATES and sharing:
                # It continued a chat that can't be looked up now, so nobody can tell it's the user's own:
                # what was shared never goes into that chat's running turn.
                await answered_home("a running task continued a chat that can't be looked up")
                return
        if run_id and status in {"running", "working"}:
            items: list[dict[str, Any]] = []
            if sharing:
                # A running task can't take new image parts: the steer names Speakeasy's saved copies,
                # which it opens itself (vision_analyze, its file tools).
                shared = await self.collect_shared(delegation_id, key)
                items = self.save_shared(shared, key) if shared else []
            text = (P.shared_steer_text(self.names, part.request, P.shared_block(self.names, items, attached=False))
                    if items else P.typed_steer_text(self.names, part.request)
                    if delegation_id.startswith(messages_mod.TYPED_PREFIX) else P.steer_text(self.names, part.request))
            skill = await asyncio.to_thread(skills_mod.invocation, part.request, key)
            if skill:  # "/skill do this" typed to a running task: it loads that skill too
                text = f"{text}\n\n{skill}"
            nameable = all(i["path"] for i in items)  # an image with no saved copy can't be pointed at
            accepted = nameable and await self.steer_follow_up(target, key, part.request, text)
            if accepted:
                if items:
                    self.store.add_shared(key, items)
                else:
                    self.drop_capture(delegation_id)  # a plain follow-up: words only, the capture is dropped
                with self.interaction.lock:
                    self.interaction.latest_delegation_id = target.delegation_id
                self.publish()
                # Accepted only means queued: the voice may say it's lined up, not that the task has it.
                await self.append("session.thinking.append", delegation_id, P.follow_up_queued_note(earlier))
                self.handoff_at.pop(delegation_id, None)
                return
        work = self.store.work(idem_key=key) or {}
        result = notice_text((work.get("result") or {}).get("spoken"), 300)
        focus = P.follow_up_focus(part.request, earlier, result, status)
        # Carrying what the user shared: never into a chat's session (the task continued a
        # conversation), and answered home rather than in the task's channel.
        carrying = self.carries(delegation_id)
        if (placed or target.deliver_to) and not carrying:
            # It continues that chat's session or posts to the task's channel (topical or named, it can't
            # tell): no riding-along capture, as with a new request routed to a topical channel.
            self.forgo_capture(delegation_id)
        await self.start_task(delegation_id, revision, context, f"{part.request} (following up: {earlier})",
                              focus=focus, session_id=None if carrying and placed else self.store.session_for(key),
                              replaces=target.delegation_id, deliver_to=None if carrying else target.deliver_to,
                              parent=key)

    # -- follow-up receipts (receipts.py) ----------------------------------------------------
    async def steer_follow_up(self, target: BackendRun, key: str, words: str, text: str) -> bool:
        """Send what the user added to a running task. Accepted means Hermes queued it: the card
        shows it as queued, and it only turns to landed once its text is in the task's transcript."""
        after = await asyncio.to_thread(threads.last_message_id, self.rt.state_db)
        if not target.run_id:
            return False
        # A run Hermes admitted a moment ago refuses guidance (409) until its agent is up. Two things
        # said seconds apart then became two near-identical runs that both answered out loud; ask
        # again for a few seconds while the run is still going before starting another one.
        accepted = await asyncio.to_thread(self.hermes.steer, target.run_id, text)
        for _ in range(STEER_RETRIES):
            if accepted:
                break
            with self.interaction.lock:
                going = target.status in ACTIVE_RUN_STATES
            if not going:
                break
            await asyncio.sleep(STEER_RETRY_S)
            accepted = await asyncio.to_thread(self.hermes.steer, target.run_id, text)
        if not accepted:
            return False
        milestone = f"You added: {notice_text(words, 200)}"
        self.store.progress(key, "milestone", milestone)
        self.store.add_follow_up(key, words, text, milestone, after)
        loop = asyncio.get_running_loop()
        watch = loop.create_task(self.watch_follow_ups(target))
        self.dispatch_tasks.add(watch)
        watch.add_done_callback(self.dispatch_tasks.discard)
        return True

    def _landed_now(self, key: str) -> list[dict[str, Any]]:
        """Queued follow-ups of this task now seen in its transcript, marked landed."""
        session = self.store.session_for(key)
        found = []
        for item in self.store.open_follow_ups(key):
            if receipts.landed(self.rt.state_db, session, item["after_id"], item["body"]) and \
                    self.store.settle_follow_up(item["id"], receipts.LANDED):
                found.append(item)
        return found

    async def watch_follow_ups(self, backend: BackendRun) -> None:
        """While the run works, look for its queued follow-ups in its transcript (receipts.py)."""
        key = backend.idem_key
        while True:
            with self.interaction.lock:
                running = backend.status in ACTIVE_RUN_STATES
            if not running or not self.store.open_follow_ups(key):
                return
            try:
                landed = await asyncio.to_thread(self._landed_now, key)
            except RuntimeError:  # shutting down: the run's end settles what is left
                return
            if landed:
                self.publish()
                if self.call_connected():
                    earlier = notice_text(self.store.request_text(key), 200) or "the task"
                    for item in landed:
                        await self.quiet("landed:" + item["id"], None, P.follow_up_landed_note(earlier, item["words"]))
            await asyncio.sleep(FOLLOW_UP_POLL_S)

    async def settle_follow_ups(self, backend: BackendRun, pending_steer: Any, status: str | None = None,
                                modern: bool = True) -> list[str]:
        """The run ended: every follow-up still queued is now landed or not read. Returns the words of
        the ones it never read, so the voice can say so and offer them as new work.

        Landed: its text is in the transcript, or the run completed (or failed) and Hermes handed back
        no undelivered steer text (its finalizer returns whatever is still queued as ``pending_steer``,
        so nothing handed back means it was drained into the model's messages). A stop clears queued
        text without handing it back, so a stopped run needs the transcript to count it as read."""
        key = backend.idem_key
        if not self.store.open_follow_ups(key):
            return []
        status = status or backend.status
        waited = 0.0
        while True:
            await asyncio.to_thread(self._landed_now, key)
            open_items = self.store.open_follow_ups(key)
            if not open_items or waited >= FOLLOW_UP_SETTLE_S:
                break
            if any(receipts.still_pending(pending_steer, i["body"]) for i in open_items):
                break  # handed back undelivered: no transcript wait will change that
            await asyncio.sleep(0.5)
            waited += 0.5
        unread: list[str] = []
        for item in self.store.open_follow_ups(key):
            drained = modern and status in {"completed", "failed"} and not receipts.still_pending(pending_steer, item["body"])
            if self.store.settle_follow_up(item["id"], receipts.LANDED if drained else receipts.NOT_READ) and not drained:
                unread.append(item["words"])
        self.publish()
        return unread

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
        # In a call that can share, "look at my screen" is about theirs, never a request for the task's
        # picture; elsewhere (an iPhone, an older Mac) the words ask for the picture as before.
        wants_picture = router.wants_to_see(request) and not (
            self.interaction.attachments.declared and router.refers_to_screen(request))
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
                         ack_place: str | None = None, named: bool = False, parent: str | None = None) -> None:
        """*parent*: the task this one answers or follows up (its idem key), which names it."""
        idem = self._idem(task_id, revision)
        backend = BackendRun(task_id, revision, idem, voice_id=voice_id, deliver_to=deliver_to)
        delegation_id = backend.say_id
        replaced_key = self._register(task_id, backend, replaces)
        if replaced_key:
            self.store.dismiss_key(replaced_key)
        self.publish()
        continuing = bool(session_id)  # an existing session: it may hold an earlier screenshot
        if not session_id:
            session_id = task_session_id(idem)
        label = self.rt.channel_label(deliver_to) if deliver_to else self.rt.delivery_label()
        show = self._take_show(voice_id or task_id, idem)
        if show:
            focus = (focus or request) + P.SHOW_IT_FOCUS
        update_note = P.plugin_update_focus(request)
        if update_note:
            focus = (focus or request) + update_note
        # Read now, before any await: the call may end (and drop its room text) while what was shared is collected.
        room = self.room_handoff_text(voice_id or task_id)
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
            self.name_task(idem, request, parent)
        # Same-project fence: a new edit to a project a running task is already editing waits for it.
        fenced = self.fence_blocker(task_id, request, exclude={replaces}) if request else None
        if fenced is not None:
            self.handoff_at.pop(delegation_id, None)
            await self.wait_for_fence(backend, fenced[0], fenced[1], delegation_id)
            with self.interaction.lock:
                added, backend.held_words = list(backend.held_words), []
            if added:
                focus = (focus or request) + "\n\nThey added while it waited to start: " + " / ".join(added)
        with self.interaction.lock:
            others = [r.idem_key for r in self.interaction.runs.values()
                      if r.delegation_id != task_id and r.status in ACTIVE_RUN_STATES]
        parallel = [r for r in (notice_text(self.store.request_text(k), 80) for k in others[-4:]) if r]
        if voice_id is None and fenced is None:
            await self.quiet("started", delegation_id, P.work_started_note(parallel))
            if deliver_to and not replaces:
                await self.say_where(delegation_id, ack, place=ack_place, named=named)
            else:
                self.handoff_at.pop(delegation_id, None)
        # What the user shared goes with the run: images as image parts of its input, every saved
        # copy named in the prompt (files are opened with Hermes' own tools), shown on the task's card.
        shared = await self.collect_shared(task_id, idem)
        images = self.image_urls(shared)
        items = self.save_shared(shared, idem) if shared else []
        if items:
            self.store.add_shared(idem, items)
            self.publish()
        prompt = P.build_task_prompt(self.names, revision, context, focus, label, shared=self.shared_prompt(
            items, bool(images), session_id if continuing else None, shared.capture is not None), room=room,
            skill=await asyncio.to_thread(skills_mod.invocation, request, idem) if request else "")
        try:
            choice = await self.model_choice(task_id, idem, session_id, request)
            run_id = await asyncio.to_thread(self.hermes.start_run, prompt, idem, session_id,
                                             **({"images": images} if images else {}),
                                             **({"choice": choice} if choice else {}))
            images = []
            if voice_id is None:
                shared.forget_bytes()  # the run has them; a split request's parts still share theirs
            self.store.update_run(idem, run_id, "running")
            self.title_session(idem)
            with self.interaction.lock:
                backend.run_id, backend.status = run_id, "running"
            self.publish()
            loop = asyncio.get_running_loop()
            terminal_seen = await asyncio.to_thread(
                self.hermes.events, run_id,
                lambda event: on_loop(self.handle_hermes_event(backend, event), loop))
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
        self.store.set_failure(idem, failed_as, failures.failure_hint(str(exc)) or "start")
        self.store.progress(idem, "result", f"Work failed: {reason}" if reason else
                            f"Work failed: {failures.failure_reason('failed', failed_as, failures.failure_hint(str(exc)) or 'start')}")
        with self.interaction.lock:
            backend.status, backend.error = "failed", str(exc)[:240]
        self.publish()
        return failed_as

    async def start_continuity_task(self, task_id: str, revision: int, context: str, request: str,
                                    conv: continuity.Conversation, joins: "BackendRun | None" = None,
                                    voice_id: str | None = None, parent: str | None = None) -> None:
        """Run the request inside an existing Hermes conversation's own session: it sees that
        conversation's history, and the reply posts back to that chat.

        Only the user's own DM may take what they shared: a request that carries a screen, picture or
        file and would continue a group chat (or one Hermes recorded no type for) runs as a new task
        answered home instead. That covers follow-ups to finished thread tasks too."""
        if not conv.private and self.carries(task_id):
            logger.info("speakeasy: shared things never go into a group chat; starting a task answered home")
            await self.start_task(task_id, revision, context, request, voice_id=voice_id, parent=parent,
                                  focus=P.group_chat_focus(request, notice_text(conv.where, 100) or "that chat"))
            return
        if not conv.private:
            self.forgo_capture(task_id)  # a group chat's session never gets a riding-along capture
        idem = self._idem(task_id, revision)
        backend = BackendRun(task_id, revision, idem, deliver_to=joins.deliver_to if joins else None,
                             voice_id=voice_id)
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
        if joins is None and not self.store.has_job_link(idem) and not self.store.speakeasy_session(conv.session_id):
            # A new request placed into an outside chat (a thread other work also uses): its own job,
            # so its page shows its own plan, tool log and answer, never another request's.
            self.store.link_job(idem, None)
        self.store.progress(idem, "request", request)
        # The task it joins or follows up, never the conversation's own name (an unrelated older chat).
        self.name_task(idem, request, parent or (joins.idem_key if joins else None))
        self.store.progress(idem, "milestone", f"Continuing in {where}")
        if joins is not None:
            self.store.hide_key(idem)
        self.publish()
        if joins is None and voice_id is None:  # a split request's parts share one spoken note
            await self.quiet("continuing:" + delegation_id, delegation_id, P.continuing_in_note(where))
        if joins is None:
            self.handoff_at.pop(delegation_id, None)
        # Taken now, while it's what the user was looking at, even if this turn waits for another.
        shared = await self.collect_shared(task_id, idem) if conv.private else Shared()
        images = self.image_urls(shared)
        items = self.save_shared(shared, idem) if shared else []
        if items:
            self.store.add_shared(idem, items)
            self.publish()
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
                # The task grew: name it for what it is now, not just the first ask.
                self.retitle(idem, request, self.store.request_text(joins.idem_key))
                joins = None
                break
            self.store.touch(joins.idem_key)
            await asyncio.sleep(CONTINUITY_POLL_S)
            waited += CONTINUITY_POLL_S
        waited = 0
        while await asyncio.to_thread(continuity.session_busy, self.rt.state_db, conv.session_id):
            if waited == 0:
                self.store.user_progress(idem, "Queued", f"Waiting for current work in {where} to finish")
                self.publish()
            if waited >= CONTINUITY_WAIT_S:
                break
            self.store.touch(idem)  # verifiably queued: never "Status unconfirmed" while it waits its turn
            await asyncio.sleep(CONTINUITY_POLL_S)
            waited += CONTINUITY_POLL_S
        try:
            asked = request + (P.SHOW_IT_FOCUS if self._take_show(task_id, idem) else "")
            note = self.shared_prompt(items, bool(images), conv.session_id, shared.capture is not None)
            message = P.continuation_message(self.names, asked + (f"\n\n{note}" if note else ""),
                                             notice_text(request, 200) or "voice request",
                                             room=self.room_handoff_text(task_id))
            content = user_content(message, images) if images else message
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
                    on_loop(self.thread_activity(
                        backend, "commentary", text, {"event": "message.interim", "text": text}), loop)
                elif name == "tool.started":
                    event = {"event": "tool.started", "tool": payload.get("tool_name") or payload.get("tool"),
                             "preview": payload.get("preview")}
                    if isinstance(payload.get("tool_call_id"), str):
                        event["tool_call_id"] = payload["tool_call_id"]
                    on_loop(self.handle_hermes_event(backend, event), loop)
                elif name in {"tool.completed", "tool.failed"}:  # settles the step in the live activity
                    done = {"event": name, "tool": payload.get("tool_name") or payload.get("tool"),
                            "preview": payload.get("preview")}
                    for field in ("tool_call_id", "error"):
                        if field in payload:
                            done[field] = payload[field]
                    self.observe_activity(backend, done)
                elif name == "assistant.completed":
                    final["text"] = payload.get("content") or ""
                elif name.startswith("run.") and name != "run.started":
                    final["status"] = name.split(".", 1)[1]
                    final["error"] = payload.get("error")

            terminal = await asyncio.to_thread(continuity.stream_session_chat, self.hermes.base, self.rt.hermes_key(),
                                               conv, content, on_event)
            status = final.get("status") or ("completed" if terminal and final.get("text") else "ambiguous")
            output = P.without_voice_header(final.get("text", ""))
            if status == "ambiguous" and backend.run_id:
                # Real Hermes ends a refused turn's stream without a run.* event; the refusal is only
                # on the run itself. Read it so a refused chat still gets the request another way.
                try:
                    run = await asyncio.to_thread(self.hermes.get_run, backend.run_id)
                except Exception:
                    run = {}
                if run.get("status") == "failed" and continuity.refused(run.get("error")) and not str(run.get("output") or "").strip():
                    status, final["error"] = "failed", run.get("error")
            if status == "ambiguous":
                if backend.run_id:
                    await self.reconcile_stream_end(backend)
                    return
                raise ServiceError(502, "conversation stream ended without a result")
            if status == "failed" and continuity.refused(final.get("error")) and not output.strip():
                # Newer Hermes won't take an API turn into a chat's session. Say it in the thread
                # itself, through Hermes' webhook platform, exactly as if typed there; without a
                # thread (or the webhook platform), run it beside the chat with the chat's context.
                self.store.forget_run_id(idem)
                with self.interaction.lock:
                    backend.run_id = None
                if not await self.continue_in_thread(backend, conv, message, where):
                    await self.run_beside_conversation(backend, revision, context, asked, conv, where)
                return
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

    def vouch_for_thread(self, backend: "BackendRun", platform: str, thread_id: str) -> threading.Event:
        """While a thread task waits on its Hermes turn, ask the gateway every 20s whether that turn
        is really still running. If it is, keep the card live (no "Status unconfirmed" during one
        long step) and, when the turn has said nothing for a while, show what it is in the middle of.
        A turn the gateway no longer runs gets no vouching, so a dead one still goes stale. Set the
        returned event to stop."""
        stop = threading.Event()
        loop = asyncio.get_running_loop()
        idem, state_db = backend.idem_key, self.rt.state_db

        def session_of() -> str | None:
            return threads.thread_session(state_db, platform, str(thread_id))

        def on_alive(summary: dict[str, Any]) -> None:
            def apply() -> None:
                if backend.status in TERMINAL:
                    return
                work = self.store.work(idem_key=idem) or {}
                said_at = work.get("updated_at") or 0
                if time.time() - float(said_at) > liveness.QUIET_SAY_S:
                    self.store.thread_progress(idem, liveness.doing(summary))
                else:
                    self.store.touch(idem)
                self.publish()
            loop.call_soon_threadsafe(apply)

        threading.Thread(target=liveness.watch, args=(session_of, on_alive, stop),
                         name=f"speakeasy-vouch-{idem[:12]}", daemon=True).start()
        return stop

    async def continue_in_thread(self, backend: "BackendRun", conv: continuity.Conversation, message: str,
                                 where: str) -> bool:
        """Put the request into the chat thread as if typed there, so the agent works inside that
        thread with its whole history and Hermes replies there itself; the call hears the reply.
        False when it can't be delivered that way (no thread, webhook platform off)."""
        runner = self.rt.threads
        thread_id = conv.thread_id or (conv.chat_id if conv.chat_type == "thread" else "")
        if runner is None or not hasattr(runner, "post") or not hasattr(runner, "wait_turn") or not thread_id:
            return False
        idem = backend.idem_key
        after = await asyncio.to_thread(threads.last_message_id, self.rt.state_db)
        delivered = await asyncio.to_thread(runner.post, conv.platform, str(thread_id), message=message,
                                            delivery_id=f"{idem[:40]}-{secrets.token_hex(4)}")
        if not delivered:
            return False
        logger.info("speakeasy: continued a chat by messaging its thread")
        backend.deliver_to = None  # Hermes answers in the thread itself; posting it too would double it
        self.store.progress(idem, "milestone", f"Sent into {where}")
        self.publish()
        loop = asyncio.get_running_loop()

        def on_activity(kind: str, text: str) -> None:
            event = {"event": "message.interim" if kind == "commentary" else "tool.completed", "text": text}
            on_loop(self.thread_activity(backend, kind, text, event), loop)
        state_db, platform = self.rt.state_db, conv.platform

        def session_of() -> str | None:
            return threads.thread_session(state_db, platform, str(thread_id)) or conv.session_id
        # A webhook turn has no run event stream: its tool calls are read back from the session's
        # transcript (at most once a second) into the live activity, real names, args and durations.
        tools = self.watch_session_tools(backend, session_of, after)
        LIVE_THREAD_WAITS[idem] = time.monotonic()  # being waited on: the stuck-task sweep leaves it
        vouch = self.vouch_for_thread(backend, conv.platform, str(thread_id))
        try:
            answer = await asyncio.to_thread(runner.wait_turn, conv.platform, str(thread_id), after, on_activity)
        finally:
            vouch.set()
            await asyncio.to_thread(tools.stop)
            LIVE_THREAD_WAITS.pop(idem, None)
        if answer is None:
            self.store.progress(idem, "result", f"Still going in {where}; the answer will land there")
            await self.handle_hermes_event(backend, {"event": "run.completed",
                                                     "output": f"Still working on it in {where}. The answer will land there.",
                                                     "continued": True, "still_going": True})
            return True
        await self.handle_hermes_event(backend, {"event": "run.completed", "output": P.without_voice_header(answer),
                                                 "continued": True})
        return True

    async def run_beside_conversation(self, backend: "BackendRun", revision: int, context: str, request: str,
                                      conv: continuity.Conversation, where: str) -> None:
        """Hermes refused a turn inside the conversation's own session (newer Hermes only admits API
        turns into sessions the API started). Run the request as its own task, carrying the
        conversation's recent messages, and still post the answer into that chat."""
        idem = backend.idem_key
        logger.info("speakeasy: %s doesn't take outside turns; running the request as its own task", where)
        excerpt = await asyncio.to_thread(continuity.recent_exchange, self.rt.state_db, conv.session_id)
        focus = request + (f" (This picks up the conversation in {where}. Its most recent messages, for context:\n"
                           f"{excerpt}\n)" if excerpt else f" (This picks up the conversation in {where}.)")
        session_id = task_session_id(idem)
        if valid_delivery_target(conv.target):
            backend.deliver_to = conv.target
        prompt = P.build_task_prompt(self.names, revision, context, focus, where)
        self.store.set_session(idem, session_id)
        self.store.progress(idem, "milestone", f"Working on it here; the answer goes to {where}")
        with self.interaction.lock:
            backend.run_id, backend.status = None, "running"
        self.publish()
        try:
            choice = await self.model_choice(backend.delegation_id, idem, session_id, request)
            run_id = await asyncio.to_thread(self.hermes.start_run, prompt, idem, session_id,
                                             **({"choice": choice} if choice else {}))
            self.store.update_run(idem, run_id, "running")
            self.title_session(idem)
            with self.interaction.lock:
                backend.run_id = run_id
            self.publish()
            loop = asyncio.get_running_loop()
            terminal_seen = await asyncio.to_thread(
                self.hermes.events, run_id,
                lambda event: on_loop(self.handle_hermes_event(backend, event), loop))
            if not terminal_seen:
                await self.reconcile_stream_end(backend)
        except Exception as exc:
            if backend.run_id:
                try:
                    await self.reconcile_stream_end(backend, exc)
                    return
                except Exception as reconcile_exc:
                    exc = reconcile_exc
            failed_as = self._record_start_failure(backend, exc, "task beside a conversation")
            said = failures.reason_text(failed_as) and failures.voice_text(failed_as)
            await self.append("session.commentary.append", backend.say_id,
                              P.failed_because(said) if said else P.FAILED_SPOKEN)

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
            # Hermes names the thread after its first turn; show the same name in the panel (the job
            # keeps the name from the work itself).
            self.store.set_title(idem, title, thread=True)
            self.publish()

        loop = asyncio.get_running_loop()

        def on_activity(kind: str, text: str) -> None:
            # The thread's turn is visible to the call as it happens: what it writes while working
            # becomes the task's live status, and pictures it takes or looks at reach the panel.
            event = ({"event": "message.interim", "text": text} if kind == "commentary"
                     else {"event": "tool.completed", "tool": "thread", "preview": text})
            asyncio.run_coroutine_threadsafe(self.thread_activity(backend, kind, text, event), loop)

        state_db = self.rt.state_db
        # The new thread's turn has no run event stream: its tool calls come from its transcript.
        tools = self.watch_session_tools(
            backend, lambda: threads.thread_session(state_db, opened.platform, str(opened.thread_id)), 0)
        LIVE_THREAD_WAITS[idem] = time.monotonic()
        vouch = self.vouch_for_thread(backend, opened.platform, str(opened.thread_id))
        try:
            try:
                answer = await asyncio.to_thread(self.rt.threads.wait, opened, on_session, on_title, on_activity)
            except TypeError:  # an older runner without progress
                answer = await asyncio.to_thread(self.rt.threads.wait, opened, on_session, on_title)
        except Exception as exc:
            answer, backend.error = None, type(exc).__name__
        finally:
            vouch.set()
            await asyncio.to_thread(tools.stop)
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
            if kind == "commentary":
                self.observe_activity(backend, event)
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
            for field in ("output", "error", "pending_steer", "interrupted"):  # a failed run's status carries Hermes' error line too
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

    async def model_choice(self, task_id: str, idem: str, session_id: str | None,
                           request: str | None) -> dict[str, Any] | None:
        """The model/reasoning overrides for a run about to start (models.py), or None for Hermes' own
        configuration. Never fails a task: a choice that can't be worked out runs on the default."""
        choose = self.rt.choose_model
        if choose is None:
            return None
        try:
            return await asyncio.to_thread(choose, task_id, idem, session_id, request)
        except Exception:
            logger.warning("speakeasy: the model for a task could not be worked out; Hermes' default runs it",
                           exc_info=True)
            return None

    async def handle_hermes_event(self, backend: BackendRun, event: dict[str, Any]) -> None:
        self.observe_activity(backend, event)
        runtime = event.get("runtime")
        if isinstance(runtime, dict) and str(event.get("event") or "").startswith("run."):
            try:  # which model Hermes says served the run (it may have fallen back)
                self.store.served_model(backend.idem_key, runtime)
            except Exception:
                logger.debug("speakeasy: served model not recorded", exc_info=True)
        try:
            await self._handle_hermes_event(backend, event)
        finally:
            self.publish()

    def watch_session_tools(self, backend: BackendRun, session_of: Callable[[], str | None],
                            after_id: int) -> threads.SessionToolWatch:
        """Follow a thread task's session transcript into its live activity while it runs (tool
        calls only: its commentary already arrives through the thread wait). Stop it before the
        task's terminal event so the last calls land first."""
        loop = asyncio.get_running_loop()

        def on_event(event: dict[str, Any]) -> None:
            if backend.status in TERMINAL:
                return
            if subagents_mod.watches_helpers(event):
                self.follow_helpers(backend, session_of, loop)
            try:
                saved = activity_mod.observe(self.store, backend.idem_key, event, on_flush=self.publish)
            except Exception:
                logger.debug("speakeasy: activity update failed", exc_info=True)
                return
            if saved and not loop.is_closed():
                loop.call_soon_threadsafe(self.publish)
        return threads.SessionToolWatch(self.rt.state_db, session_of, after_id, on_event).start()

    def observe_activity(self, backend: BackendRun, event: dict[str, Any]) -> None:
        """Live job activity (activity.py) from the run's own events; saved at most once a second."""
        try:
            activity_mod.observe(self.store, backend.idem_key, event, on_flush=self.publish)
        except Exception:
            logger.debug("speakeasy: activity update failed", exc_info=True)
        if subagents_mod.watches_helpers(event):
            key = backend.idem_key
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            self.follow_helpers(backend, lambda: self.store.session_for(key), loop)

    def follow_helpers(self, backend: BackendRun, session_of: Callable[[], str | None],
                       loop: asyncio.AbstractEventLoop | None) -> None:
        """The task started helpers (delegate_task): follow each one's own transcript into its steps
        and settle the ones that never report (subagents.py). Display only: never routing or status."""
        def publish_soon() -> None:
            if loop is not None and not loop.is_closed():
                loop.call_soon_threadsafe(self.publish)

        titler = self.rt.title_call or router.smart_title
        names = self.names

        def title(goal: str) -> str | None:  # the same titler (and rules) as a task's own name
            return self._titled(titler, text_mod.title_input(goal, names=(names.user_name, names.assistant_name)))

        try:
            subagents_mod.ensure_watch(self.store, backend.idem_key, self.rt.state_db, session_of,
                                       on_flush=publish_soon, titler=title)
        except Exception:
            logger.debug("speakeasy: helper watch failed to start", exc_info=True)

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
                keys, edits = fence_mod.keys_from_tool(event.get("tool"), event.get("preview"))
                if keys:
                    with self.interaction.lock:
                        backend.projects |= keys
                        backend.edits = backend.edits or edits
                derived = derive_tool_status(event.get("tool"), event.get("preview"))
                if derived:
                    self.record_activity(backend, derived[1])
                    self.store.tool_progress(idem, *derived)
        elif kind == "approval.request":
            request_id = event.get("request_id")
            if not isinstance(request_id, str) or not ID_RE.fullmatch(request_id):
                return
            description = safe_user_text(event.get("description") or event.get("reason"), 500)
            # The answers Hermes offers for this request; Speakeasy never sends "always" (hermes_api.approve).
            offered = event.get("choices") if isinstance(event.get("choices"), list) else []
            choices = ["once"] + (["session"] if "session" in offered else []) + ["deny"]
            safe = {"request_id": request_id, "description": description or "Consequential action",
                    "choices": choices}
            with self.interaction.lock:
                if backend.status == "ambiguous":
                    return
                backend.approval, backend.status = safe, "waiting_for_approval"
            self.store.update_run(idem, None, "waiting_for_approval")
            self.store.progress(idem, "milestone", "Waiting for your approval")
            if not self.call_connected():
                self.notices.needs_you(request_id, safe["description"])
            self.push("needs_you", f"approval:{request_id}", self.store.title(idem) or self.store.request_text(idem),
                      f"Approve? {safe['description']}", backend.run_id)
            await self.append("session.thinking.append", delegation_id, P.approval_rules_note(self.names))
            await self.append("session.commentary.append", delegation_id,
                              P.approval_note(self.names, description, choices))
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
                self.store.set_failure(idem, failed_as, None if failed_as in failures.REASONS else failures.failure_hint(error))
                if failed_as in failures.REASONS:
                    logger.info("speakeasy: Hermes run %s failed (%s)", backend.run_id, failed_as)
                else:
                    # WARNING so it lands in errors.log, where the app sends the user for the details.
                    logger.warning("speakeasy: Hermes run %s failed, reason not recognized: %s", backend.run_id,
                                   str(error or "no error given")[:200])
            reason = failures.reason_text(failed_as)
            safe_output = safe_user_text(result["full"], 4000) if result else None
            self.store.progress(idem, "result", safe_output or (f"Work failed: {reason}" if reason else {
                "completed": "Work complete", "cancelled": "Work stopped",
                "failed": f"Work failed: {failures.failure_reason('failed', failed_as, failures.failure_hint(error))}",
                "interrupted": "Work interrupted"}.get(status, "Work ended")))
            with self.interaction.lock:
                backend.status, backend.approval = status, None
            # ``interrupted`` rides on every terminal event from a Hermes that also hands back unread steer
            # text (pending_steer); without it, "nothing handed back" proves nothing.
            unread = await self.settle_follow_ups(backend, event.get("pending_steer"), status,
                                                  modern="interrupted" in event or "pending_steer" in event)
            stored_drafts = [self.store.add_draft(idem, self.store.session_for(idem), d) for d in drafts]
            # Long reports get sorted into parts; any answer with numbers in it may also get a picture.
            if status == "completed" and result and (len(result.get("full") or "") >= 400 or (
                    not result.get("views") and len(re.findall(r"\d", result.get("full") or "")) >= 4)):
                self.shape_result(idem, result["full"])
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
            self.push_result(backend, idem, status, result, reason, stored_drafts, event)
            if self.call_connected():
                # Never over a thought in progress: the answer (or its question) waits for a pause.
                await self.wait_for_pause()
            if status == "completed":
                spoken = result["spoken"] if result else "Done."
                notes = list(P.result_notes(self.store.title(idem) or self.store.request_text(idem),
                                            result.get("full") if result else None, spoken))
                # The answer text says "here's the building" either way; tell the voice what the user
                # can actually see, so it never claims a picture is up when none reached the app.
                pictures = len(((self.store.work(idem_key=idem) or {}).get("review") or {}).get("images") or [])
                notes.append(P.pictures_note(pictures))
                if stored_drafts:
                    d = stored_drafts[-1]
                    notes.append(P.draft_waiting_note(self.names, d.get("subject"), d.get("to") or []))
                if self.HOLD_QUIET_NOTES:
                    # One message, not several: each appended note can draw its own spoken reply.
                    packed: list[str] = []
                    for note in notes:
                        if packed and len(packed[-1]) + 2 + len(note) <= 1900:
                            packed[-1] += "\n\n" + note
                        else:
                            packed.append(note)
                    for message in packed:
                        await self.append("session.thinking.append", delegation_id, message)
                else:
                    for note in notes:
                        await self.append("session.thinking.append", delegation_id, note)
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
            if unread:
                # What they added never reached it: say so (after its outcome) and offer it as new work.
                await self.append("session.thinking.append", delegation_id, P.follow_up_not_read_note(unread))
                await self.append("session.commentary.append", delegation_id,
                                  P.follow_up_not_read_spoken(unread, status == "completed"))
            await self.release_fenced(backend, status)


LATE_RESULT_S = 20.0  # the user spoke in the last 20 s of a task that took longer: say what it answers


def new_event_id() -> str:
    return "speakeasy_" + secrets.token_hex(12)
