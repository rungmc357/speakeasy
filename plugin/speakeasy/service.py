"""The Speakeasy server core: calls, tasks, approvals, email drafts, settings, brief, status.

Transport-agnostic (the HTTP handler in ``server.py`` is a thin layer over this). Constructed once
inside the gateway with the profile's HERMES_HOME resolved up front: HTTP threads do not carry the
gateway's profile scope.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import copy
import datetime as _dt
import hashlib
import json
import logging
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__
from . import attachments as A
from . import transcribe as T
from . import delivery as D
from . import duck as duck_mod
from . import settings as S
from . import history as H
from . import messages as M
from . import models as MD
from .brief import BriefInvalid, BriefManager
from .calls import (ACTIVE_RUN_STATES, APPROVED_MILESTONE, INPUT_TEXT_TYPE, MAX_BOARD, MAX_INPUT_TEXT, MAX_TASKS, TYPED_NOT_CONNECTED,
                    TYPED_SENT, BackendRun, ServiceError,
                    Interaction, Notices, Runtime, SidebandWorker, end_shared, interaction_tasks, publish_state,
                    typed_text)
from .cards import ImageRejected, _sniff, default_image_roots, fetch_image, read_local_image
from .devices import CLIENT_HEADER, DeviceStore, parse_client
from .emails import canonical_json, extract_email_drafts
from .hermes_api import HermesAPI, HermesError

RESTART_NEEDED = ("Speakeasy was updated while Hermes was running. Restart Hermes to finish the update: send "
                  "/restart in your chat with Hermes (or run hermes gateway restart in a terminal).")
from .prompt import builder as P
from .router import home_plan_call as router_home_plan_call, routing_model
from .threads import THREAD_PLATFORMS
from .settings import (GROK_BETA_OFF, max_call_seconds, OPENAI_KEY_NAME, Settings, SettingsError, default_voice, find_codex, hermes_api_base,
                       grok_beta_on, hermes_secret, openai_api_key, valid_delivery_target)
from .secure_service import SecureInputMixin
from .store import StateStore
from .suggest import SuggestError, suggest
from .threads import ThreadRunner, capability, sync_routes
from .text import ID_RE, MAX_CARDS, MAX_ROOM_CHARS, TERMINAL, notice_text, safe_room_text, split_result, vague_title

logger = logging.getLogger(__name__)
# How long POST .../input-text waits for the call to take a typed turn before answering 504.
INPUT_TEXT_WAIT_S = 15.0

ROUTING_EXPLAINER = ("When you ask for something on a call, a quick model first decides where it goes: a new task, an addition to a task that's already running, several separate tasks, or a Hermes chat or thread you were already working in. It also decides whether you want to see something on screen. It doesn't do the work; your Hermes agent does. Every request waits on this step, so pick a fast model; a wrong call can put a task in the wrong place.")
HOME_EXPLAINER = ("Home control lets your voice run your Home Assistant devices directly, in about a second. "
                  "Say \"lights off in the kitchen\", \"set the den to 72 and turn on the fan\" or \"is the bedroom "
                  "light on?\" and it's done without starting a Hermes task. Anything with a time, a condition or real "
                  "thinking (\"close the blinds at 11\") still goes to Hermes, which keeps its own Home Assistant access. "
                  "It can only use the devices you tick here. Locks, alarms, garage doors and gates are never on the list; "
                  "those stay with Hermes. It uses the Home Assistant connection Hermes already has; nothing new is stored.")
ROUTING_HINT = ("Also: `hermes voice routing` on the machine that runs Hermes. "
                "Stored in your Hermes config under auxiliary → speakeasy_router.")

MAX_SDP = 96 * 1024
# UX contract flags in GET /voice/status ("capabilities"); clients hide what a server lacks.
CAPABILITIES = {"card_actions": True, "job_progress": True, "spoken_choices": True, "summary_points": True,
                "task_messages": True}
PAUSE_NOTICE_AFTER_S = 15 * 60
IDLE_WORK_STRETCH = 4  # a quiet call with a task still running waits this many times idle_pause_minutes
LOOK_TEXT = "Take a look at what I attached."  # a typed message or job that is only attachments
IDLE_CHECK_S = 15
TITLE_SWEEP_TICKS = 40  # idle ticks between stand-in title sweeps (about ten minutes)
TITLE_SWEEP_BATCH = 5   # tasks a sweep may name (one model call each)
IMAGE_SUPPORT_EVERY_S = 3600  # re-check whether Hermes reads images (its model setup rarely changes)
EARLY_CONNECT_WAIT_S = 8  # words heard while connecting wait this long for the call's event channel
CAPABILITIES["job_subagents"] = True  # helpers a task started (delegate_task) on its work object (subagents.py)
CAPABILITIES["job_activity"] = True  # live activity / findings / touched / hero on tasks (activity.py)
CAPABILITIES["task_stop"] = True  # POST /voice/work/{run_id}/stop: stop one task by id, with or without a call
CAPABILITIES["start_jobs"] = True  # POST /voice/work: a new job from typed text, no call needed (the app's Update button)
CAPABILITIES["endpoints"] = True  # GET /voice/endpoints (+ address_drop in the pair response): every address of this Hermes
# POST /voice/interactions/{id}/input-text: a turn typed to the call itself (same as the top-level
# "input_text"; listed here too so both typing features sit together). task_messages types to one job.
CAPABILITIES["input_text"] = True
# GET /voice/history (+ /{session_id}, POST /{session_id}/message): every Hermes session and task,
# readable and writable by id from the app, with or without a call (history.py).
CAPABILITIES["history"] = True
# POST /voice/attachments (+ DELETE /voice/attachments/{id}): pictures and files staged on a device,
# then sent with a typed message or new job by id (``attachments`` on /voice/work[/{id}/message]).
CAPABILITIES["attachments"] = True
# GET /voice/secrets + POST /voice/secrets/{id}: a key a skill needs, or the Mac password for one sudo
# command, typed into a secure card and handed to Hermes, never to the model (secure_input.py).
CAPABILITIES["secret_input"] = True
CAPABILITIES["tool_log"] = True  # activity items carry tool/started_at/ended_at; GET /voice/work/{id} adds args/result
# voice.max_call_minutes 0 = no limit (older plugins refuse 0: clients offer "No limit" only with this).
CAPABILITIES["call_no_limit"] = True
# POST /voice/transcribe: a recorded voice message's audio, transcribed by Hermes the way a Telegram
# voice note is (transcribe.py). Clients without it fall back to on-device transcription.
CAPABILITIES["transcribe"] = True
# POST /voice/push + GET /voice/push/events/{id}: notifications while the app is closed (push.py).
CAPABILITIES["push"] = True
# GET /voice/models, model/reasoning on POST /voice/work + history messages, PUT /voice/history/{id}/model,
# settings jobs.*, and "model" on tasks and session pages: which model works a chat (models.py).
CAPABILITIES["model_choice"] = True
# GET /voice/schedules (+ /{id}, preview, create, pause/resume/run, PATCH, DELETE): the Hermes scheduled
# (cron) jobs of this profile, seen and managed from the apps through Hermes' own cron API (schedules.py).
CAPABILITIES["schedules"] = True
# GET /voice/boards (+ /{slug}): the Hermes Kanban boards, read-only, as Ready / Running / Blocked / Done
# columns whose cards open the session they ran in (kanban_board.py).
CAPABILITIES["boards"] = True
# POST/GET/DELETE /voice/codex/login: sign Codex in to ChatGPT from the app with Codex's own
# device-code login (codex_login.py). The plugin first looks for a working sign-in in every Codex
# copy on the machine, and installs Codex when there is none; the app asks only when nothing works.
CAPABILITIES["codex_login"] = True
# Decision cards: POST /voice/tasks/{run_id}/question/{dismiss|undo}, history messages may name the
# question they answer ("answers": run_id), and work carries question_closed (dismissed | moved_on).
CAPABILITIES["question_actions"] = True
# mode "steer" | "queue" on POST /voice/work/{id}/message and /voice/history/{id}/message: a message to a
# running task goes into it now (steer, the default) or waits and is sent as the next turn once it is done
# (queue). Session pages and tasks list held messages (``queued``); DELETE cancels one, .../steer sends it now.
CAPABILITIES["message_modes"] = True
# A task can take a queued message while it is in one of these (else the message is simply sent).
QUEUE_BEHIND = ACTIVE_RUN_STATES - {"cancel_requested"}
QUEUE_SENDS_AFTER = {"completed", "failed"}  # a stopped task keeps its queue (shown; Steer now or Cancel)
STEER_WAITING_AFTER_S = 60  # a steered message not read by then: "Waiting for the current step to finish"
STEER_UNREAD_GRACE_S = 8    # a finished task with no receipt for it this long after: it was never read
# GET /voice/setup-check (+ POST /voice/setup-check/{id}): every requirement a first call needs as
# ready / not-ready rows with one fix each, which the apps show instead of dialing into a failure.
CAPABILITIES["setup_check"] = True



_SHORTCUT_RE = re.compile(r"^[^\x00-\x1f<>{}]{1,24}$")


def _install_codex_quietly() -> bool:
    """Install or update Codex the way `hermes voice setup --yes` does, with its output in the log."""
    import functools
    import subprocess as _sp
    from .setup_flow import Env, install_codex
    return install_codex(Env(run=functools.partial(_sp.run, capture_output=True, text=True),
                             out=lambda line: logger.info("speakeasy: codex install: %s", line.strip()),
                             interactive=False))


def _tour(value: Any) -> dict[str, str]:
    """``tour`` on a session request: ``{}`` or shortcut labels ``{call, mute, pause}`` shown in the app."""
    if value is True:
        return {}
    if not isinstance(value, dict) or not set(value) <= {"call", "mute", "pause"}:
        raise ServiceError(400, "tour must be an object with optional call, mute and pause shortcut labels")
    out = {}
    for key, label in value.items():
        if label in (None, ""):
            continue
        if not isinstance(label, str) or not _SHORTCUT_RE.fullmatch(label):
            raise ServiceError(400, f"tour.{key} must be a short shortcut label")
        out[key] = label
    return out


def _room(value: Any) -> str:
    """``room`` on a session request: what listening mode heard before the call, as "[HH:MM] text"
    lines. At most MAX_ROOM_CHARS code points as sent; secret-looking lines are redacted here, before
    the text reaches the voice or Hermes. An empty string means no room text."""
    if not isinstance(value, str):
        raise ServiceError(400, "room must be a string")
    if len(value) > MAX_ROOM_CHARS:
        raise ServiceError(400, f"room must be at most {MAX_ROOM_CHARS} characters")
    return safe_room_text(value)


def _merge_rooms(kept: str, heard: str) -> str:
    """A call paused for listening mode resumes with what was just heard added to any room text it
    already had (oldest lines dropped first to stay within MAX_ROOM_CHARS)."""
    lines = [line for line in (kept + "\n" + heard).split("\n") if line.strip()]
    while lines and len("\n".join(lines)) > MAX_ROOM_CHARS:
        lines.pop(0)
    return "\n".join(lines)

def _submodule(name: str) -> Any:
    """One of this plugin's modules, imported lazily. Retried briefly: while Hermes re-imports the
    plugin's files underneath the running server (reloader.py), the import machinery can raise
    KeyError for a sibling module mid-swap. Seen live 16:26-16:28 as GET /voice/status 500s, which
    the apps then showed as "can't reach your Hermes" while Tailscale was up."""
    import importlib
    for attempt in range(4):
        try:
            return importlib.import_module(f".{name}", __package__)
        except KeyError:
            if attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise ImportError(name)


class HistoryEvents:
    """One device-wide event feed for the history: ``session_updated`` / ``session_archived`` /
    ``decision_resolved`` from the actions that cause them, and ``history_changed`` when the shared
    watcher sees anything else move (a reply landing, a task finishing, a title set in another Hermes
    app). The watcher runs only while a stream is open, and once for all of them."""

    POLL_S = 1.5
    KEEP = 200

    def __init__(self, stamp: Callable[[], Any]) -> None:
        self._stamp = stamp
        self.cond = threading.Condition()
        self.seq = 0
        self.events: collections.deque[tuple[int, str, dict[str, Any]]] = collections.deque(maxlen=self.KEEP)
        self.listeners = 0
        self._watcher: threading.Thread | None = None
        self._last: Any = None

    def publish(self, kind: str, payload: dict[str, Any]) -> None:
        with self.cond:
            self.seq += 1
            self.events.append((self.seq, kind, {**payload, "at": round(time.time(), 3)}))
            self.cond.notify_all()

    def since(self, cursor: int) -> list[tuple[int, str, dict[str, Any]]] | None:
        """Events after *cursor*, or None when some were dropped (the client reloads)."""
        if self.events and cursor < self.events[0][0] - 1:
            return None
        return [e for e in self.events if e[0] > cursor]

    def opened(self) -> None:
        with self.cond:
            self.listeners += 1
            if self._watcher is None or not self._watcher.is_alive():
                self._watcher = threading.Thread(target=self._watch, daemon=True, name="speakeasy-history-watch")
                self._watcher.start()

    def closed(self) -> None:
        with self.cond:
            self.listeners = max(0, self.listeners - 1)

    def _watch(self) -> None:
        try:
            self._last = self._stamp()
        except Exception:
            self._last = None
        while True:
            time.sleep(self.POLL_S)
            with self.cond:
                if self.listeners <= 0:
                    self._watcher = None
                    return
            try:
                now = self._stamp()
            except Exception:
                continue
            if now != self._last:
                self._last = now
                self.publish("history_changed", {})


class VoiceService(SecureInputMixin):
    def __init__(self, hermes_home: Path, *, hermes: Any = None, notifier: Any = None,
                 codex_factory: Callable[..., Any] | None = None, openai_negotiate: Callable[..., Any] | None = None,
                 openai_worker: Callable[..., Any] | None = None, codex_login: Callable[..., Any] | None = None,
                 brief_run: Callable[[str, str], tuple[str, str]] | None = None, start_threads: bool = True,
                 route_call: Callable[..., Any] | None = None, thread_runner: Any = None,
                 suggest_run: Callable[[str, str], tuple[str, str]] | None = None,
                 title_call: Callable[[str], str | None] | None = None,
                 polish_call: Callable[[str], str | None] | None = None,
                 status_call: Callable[[str], str | None] | None = None,
                 home_plan_call: Callable[..., Any] | None = None, home_client: Callable[..., Any] | None = None,
                 tune_run: Callable[[str, str], tuple[str, str]] | None = None,
                 volume_backend: duck_mod.VolumeBackend | None = None,
                 grok_credential: Callable[[], tuple[str, str]] | None = None,
                 grok_connect: Callable[..., Any] | None = None,
                 grok_signin: Callable[[], tuple[bool, bool, str]] | None = None,
                 codex_install: Callable[[], bool] | None = None, codex_login_popen: Callable[..., Any] | None = None,
                 codex_candidates: Callable[[], list[Path]] | None = None):
        self.home = Path(hermes_home)
        self.dir = self.home / "speakeasy"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(self.home)
        self.devices = DeviceStore(self.home)
        self.store = StateStore(self.dir / "state.sqlite3", hermes_home=self.home)
        self.hermes = hermes or HermesAPI(hermes_api_base(self.home), self.hermes_key,
                                          self.settings.get()["hermes_profile"])
        self.notifier = notifier if notifier is not None else D.HermesSendNotifier(
            self.home, self.settings.get()["hermes_profile"])
        self.notices = Notices(self.store, self.notifier, lambda: self.settings.get()["delivery"]["target"],
                               lambda: P.Names.from_settings(self.settings.get()))
        self.rt = Runtime(store=self.store, hermes=self.hermes, settings=self.settings.get, hermes_home=self.home,
                          image_roots=self.image_roots, notices=self.notices, hermes_key=self.hermes_key,
                          route_call=route_call, title_call=title_call, polish_call=polish_call, status_call=status_call,
                          threads=thread_runner or ThreadRunner(self.home, D.threads_supported))
        from .home_control import HomeAssistantClient, HomeControl, credentials
        self.home_control = HomeControl(self.settings.get, lambda: credentials(self.home),
                                        plan_call=home_plan_call or router_home_plan_call,
                                        client_factory=home_client or HomeAssistantClient)
        self.rt.home = self.home_control
        from .schedules import Schedules
        self.schedules = Schedules(self.home, self.rt.state_db)
        from .kanban_board import KanbanBoards, kanban_root
        self.boards = KanbanBoards(kanban_root(self.home), self.rt.state_db)
        from .push import PushNotifier
        # Phone notifications while the app is closed. Only the gateway's long-lived service sends them
        # from a background thread; a short-lived one (the CLI, tests) sends inline when asked.
        self.push = PushNotifier(self.devices, self.settings.get, hermes_home=self.home,
                                 claim=self.store.claim_notice, background=start_threads)
        self.rt.push = self.push
        self.rt.pause_call = lambda interaction_id: self.pause(interaction_id, stop_transport=True, limit=True)
        self.shared = A.SharedFolder(self.home)
        self.rt.shared = self.shared
        # Pictures and files a device uploaded for a message it hasn't sent yet (POST /voice/attachments).
        self.staged = A.StagedPool()
        self.rt.discard_files = self.discard_shared_files
        # Hermes' model catalog (GET /api/model/options), read lazily and cached; and the per-run choice.
        self.catalog = MD.Catalog(self._fetch_model_options, self._hermes_reasoning)
        self._catalog_warming = threading.Lock()
        self.rt.choose_model = self.choose_model
        self.store.describe_model = self.describe_model
        # A running task's live line (live_status.py): the side model words it at most every 40 s
        # per task; a deterministic line from its steps otherwise. A fresh summary republishes.
        from . import live_status as LS
        self.store.live_summarizer = LS.Summarizer(self._live_summary_call)
        self.store.live_publish = lambda key: self.publish_all(key)
        # Messages sent to a task by id (POST /voice/work/{id}/message): replies by client_id for
        # MESSAGE_REPLAY_S, and the voiceless worker for tasks whose call is gone.
        self._message_replies: dict[str, tuple[float, threading.Event, dict[str, Any] | None]] = {}
        self._message_lock = threading.Lock()
        self._text_worker: Any = None
        # Messages sent "when it's done" (mode queue): held in the store, sent when the task they wait
        # on completes or fails (docs/API.md "Steer or queue"). One drain at a time.
        self._drain_lock = threading.Lock()
        self.store.settle_listeners.append(self._queue_settled)
        self.store.follow_up_listeners.append(self._follow_up_settled)
        # Device-wide live changes to the history (GET /voice/history/events): every client's list,
        # session page, board, widgets and Live Activity refresh within ~1-2 s of a change anywhere.
        self.history_events = HistoryEvents(self._history_stamp)
        self.rt.history_events = self.history_events
        self._suggest_run = suggest_run or self._brief_run
        self.brief = BriefManager(self.home, brief_run or self._brief_run, self.settings.get,
                                  error_fn=lambda: getattr(self.hermes, "last_error", "") or "")
        from .tune import CallLog, TuneManager, merge_calls
        self.call_log = CallLog(self.dir)
        self.rt.call_log = self.call_log
        # Only the gateway's long-lived service (start_threads) drives the volume: a short-lived CLI
        # service would otherwise restore, at its start, a duck the gateway is holding mid-call.
        backend = volume_backend or (duck_mod.default_backend() if start_threads else None)
        self.duck = duck_mod.Ducker(self.home, self.settings.get, backend)
        self.rt.duck = self.duck
        self.tune = TuneManager(self.dir, tune_run or brief_run or self._tune_run, self.brief,
                                lambda: merge_calls(self.call_log.recent(), self.store.calls_from_tasks()),
                                names=self._tune_names)
        self.interactions: dict[str, Interaction] = {}
        self.lock = threading.Lock()
        # Typed turns sent with a client_id, per call chain: (root interaction id, client_id) ->
        # (text, the send's future). A retry of the same id waits on, or reports, the first send.
        self.typed_sends: collections.OrderedDict[tuple[str, str], tuple[str, concurrent.futures.Future]] = \
            collections.OrderedDict()
        self.typed_lock = threading.Lock()
        self._codex_factory = codex_factory
        self._openai_negotiate = openai_negotiate
        self._openai_worker = openai_worker
        self._codex_login = codex_login
        self._codex_install = codex_install or _install_codex_quietly
        self._codex_candidates = codex_candidates
        from .codex_login import CodexLogin
        import subprocess as _sp
        self.codex_signin = CodexLogin(self.dir / "codex-login", popen=codex_login_popen or _sp.Popen,
                                       on_signed_in=self._codex_signed_in)
        self._install_lock = threading.Lock()
        self._install: dict[str, Any] = {"state": "", "message": "", "switch": False}
        self._grok_credential = grok_credential
        self._grok_connect = grok_connect
        self._grok_signin = grok_signin
        self._stop = threading.Event()
        # Secure input (secure_input.py): only the gateway's long-lived service answers Hermes' secret
        # and sudo prompts for Speakeasy's runs; a CLI or test service never takes them over.
        self._init_secure(wire=start_threads)
        if start_threads:
            threading.Thread(target=self._idle_loop, daemon=True, name="speakeasy-idle").start()
            self._warm_catalog()
            self.brief.start_scheduler(ready=self.hermes.health)

    # -- secrets (read per use, never stored or returned) ------------------------------------
    def hermes_key(self) -> str:
        return hermes_secret(self.home, "API_SERVER_KEY")

    def openai_key(self) -> str:
        return openai_api_key(self.home)

    def image_roots(self) -> tuple[Path, ...]:
        return default_image_roots(self.home, self.settings.get()["image_roots"])

    def _tune_run(self, prompt: str, idem: str) -> tuple[str, str]:
        self.hermes.last_error = ""
        return self.hermes.run_to_completion(prompt, idem, "speakeasy_tune")

    def _tune_names(self) -> tuple[str, str]:
        names = P.Names.from_settings(self.settings.get())
        return names.assistant_name, names.user_cap or "User"

    def _brief_run(self, prompt: str, idem: str) -> tuple[str, str]:
        self.hermes.last_error = ""
        return self.hermes.run_to_completion(prompt, idem, "speakeasy_brief")

    def busy(self) -> bool:
        """A call is live or a task is still running: an automatic reload would cut it off."""
        with self.lock:
            live = list(self.interactions.values())
        for interaction in live:
            # A paused call counts until its pause is bookkept as ended (PAUSE_NOTICE_AFTER_S): a call
            # paused and never resumed (or ended on the device while paused) must not hold back an
            # update forever. Resuming it after a reload starts a fresh call on the app's side.
            if not interaction.call_closed or (interaction.paused and not interaction.pause_bookkept):
                return True
            if any(r.status in ACTIVE_RUN_STATES for r in interaction.runs.values()):
                return True
        # Jobs with no call (typed follow-ups, POST /voice/work, the app's plugin Update) run on the
        # voiceless worker: a reload while one runs would drop its tracking, so they count too.
        text = self._text_worker
        if text is not None:
            with text.interaction.lock:
                if any(r.status in ACTIVE_RUN_STATES for r in text.interaction.runs.values()):
                    return True
        return False

    def close(self) -> None:
        self._stop.set()
        self._close_secure()
        self.brief.stop()
        with self.lock:
            live = list(self.interactions.values())
        for interaction in live:
            worker = interaction.worker
            if worker is not None:
                try:
                    worker.stop_transport()
                except Exception:
                    pass
        self.duck.close()  # put the volume back before a reload swaps this code out
        self.codex_signin.close()  # a waiting sign-in's Codex child must not outlive the service
        if self._text_worker is not None:
            self._text_worker.stop()

    # -- auth ---------------------------------------------------------------------------------
    def authenticate(self, headers: Any, presence: bool = True) -> str:
        """The device behind a request's bearer token. ``presence``: the request means the app is open
        on that device (push.py sends nothing then); the notification extension's own fetch doesn't."""
        supplied = headers.get("Authorization", "")
        token = supplied[7:].strip() if supplied.startswith("Bearer ") else None
        device_id = self.devices.authenticate(token, client=parse_client(headers.get(CLIENT_HEADER)))
        if device_id is None:
            raise ServiceError(401, "unauthorized")
        if presence:
            self.push.seen(device_id)
        return device_id

    # -- push notifications (push.py) ----------------------------------------------------------
    def register_push(self, device_id: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.push.register(device_id, body)

    def push_event(self, device_id: str, event_id: str) -> dict[str, Any]:
        return self.push.event(device_id, event_id)

    def pair(self, body: dict[str, Any], headers: Any = None) -> dict[str, Any]:
        code, name = body.get("code"), body.get("device_name", "Mac")
        if not isinstance(code, str) or not re.fullmatch(r"\s*\d{6}\s*", code) or not isinstance(name, str):
            raise ServiceError(400, "code (6 digits) and device_name are required")
        client = parse_client(headers.get(CLIENT_HEADER)) if headers is not None else None
        result = self.devices.redeem(code, name, client=client)
        if result is None:
            raise ServiceError(403, "pairing code is wrong or expired")
        device_id, token = result
        out: dict[str, Any] = {"device_id": device_id, "token": token, "assistant_name": self.settings.get()["assistant_name"]}
        out.update(self.endpoints())  # endpoints + the address-drop key: only a paired device sees them
        return out

    def prove(self, body: dict[str, Any], host: str) -> dict[str, Any]:
        device_id, nonce = body.get("device_id"), body.get("nonce")
        if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-f]{1,32}", device_id) \
                or not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{22,128}", nonce):
            raise ServiceError(400, "device_id and nonce are required")
        return {"proof": self.devices.proof(device_id, nonce, host)}

    def endpoints(self) -> dict[str, Any]:
        """``GET /voice/endpoints``: every address of this Hermes the app may keep and try, and how
        to read the encrypted address note (address_drop.py) when they all stop answering."""
        from . import address_drop
        s = self.settings.get()["server"]
        tailnet = s["advertised_url"] if ".ts.net" in s["advertised_url"] else ""
        if not tailnet and s["tailscale_name"]:
            tailnet = f"https://{s['tailscale_name']}"
        cloudflare = (f"https://{s['tunnel_hostname']}" if s["tunnel_hostname"] else s["tunnel_url"]) if s["tunnel"] else ""
        return {"endpoints": {"tailnet": tailnet or None, "cloudflare": cloudflare or None,
                              "cloudflare_permanent": bool(s["tunnel"] and s["tunnel_hostname"])},
                "address_drop": address_drop.client_info(self.home, self.settings.get())
                if s["tunnel"] and not s["tunnel_hostname"] else None}

    # -- voice sessions -------------------------------------------------------------------------
    def instructions(self, *, away: list[dict[str, Any]], resume: str,
                     tour: dict[str, str] | None = None, room: str = "", open_work: str = "") -> str:
        """The voice's instructions for a new or resumed call. ``room`` is listening mode's room
        text (the builder fences it and leaves recent voice out when it is there)."""
        s = self.settings.get()
        names = P.Names.from_settings(s)
        recent = ""
        label = D.target_label(s["delivery"]["target"], self.home)
        if s["brief"]["include_recent_voice"] and not resume and tour is None:
            try:
                turns = json.loads(self.store.get_meta("recent_voice") or "[]")
                recent = P.format_recent([{"role": t.get("role"), "content": t.get("text")} for t in turns
                                          if isinstance(t, dict)], names)
            except ValueError:
                recent = ""
        now = _dt.datetime.now().astimezone().strftime("%A %d %B %Y, %H:%M %Z")
        return P.build_live_instructions(names, brief=self.brief.text(), away=away, recent_voice=recent,
                                         resume=resume, extra=s["instructions_extra"], now=now,
                                         delivery_label=label, channels=s["delivery"]["channels"],
                                         tour=P.tour_block(names, tour, label, s["delivery"]["channels"])
                                         if tour is not None else "", room=room, open_work=open_work)

    def create_session(self, body: dict[str, Any], request_id: str, device_id: str = "",
                       local: bool = False) -> dict[str, Any]:
        if self.updated_underneath():
            raise ServiceError(503, RESTART_NEEDED)
        resume_from = body.get("resume_from")
        relay = self.settings.get()["voice"]["provider"] == "grok"
        if relay:
            body = {"sdp": "", **body}  # Grok calls relay audio through the plugin: no SDP needed
        if (not set(body) <= {"sdp", "resume_from", "tour", "screen", "room"} or not isinstance(body.get("sdp"), str)
                or resume_from is not None and not (isinstance(resume_from, str) and ID_RE.fullmatch(resume_from))):
            raise ServiceError(400, "body must contain only an SDP offer, an optional resume_from, tour, screen and room")
        # What the app can share this call: ready (it can capture the screen) or no_permission. Apps
        # that predate screen sharing never send it, and their calls behave as before.
        screen = body.get("screen")
        if screen is not None and screen not in A.DECLARATIONS:
            raise ServiceError(400, "screen must be ready or no_permission")
        # Listening mode: what was heard rides on a new call, or on the resume of a call paused for
        # listening mode (it's added to any room text that call already had; see adopt).
        room = _room(body["room"]) if "room" in body else ""
        heard = room
        tour = _tour(body.get("tour")) if "tour" in body and not resume_from else None
        if tour is not None and self.store.get_meta("last_call_end") is not None:
            # The app asks per Mac (a new Mac, a reinstall or an update all look like a first call
            # there); the server knows whether this person has called before, and decides.
            tour = None
        if tour is not None and room:
            # A call about what was just said in the room: the first-call tour would talk over it.
            tour = None
        source = self.resumable(resume_from) if resume_from else None
        sdp = body["sdp"]
        # SDP is a wire format: its final CRLF is significant. Never strip it.
        if not relay and (not sdp.strip() or len(sdp.encode()) > MAX_SDP or not sdp.startswith("v=0")):
            raise ServiceError(400, "invalid SDP offer")
        if not ID_RE.fullmatch(request_id):
            raise ServiceError(400, "invalid Idempotency-Key")
        fingerprint = hashlib.sha256((sdp + "\0" + (resume_from or "") + ("\0tour" if tour is not None else "")
                                      + ("\0room\0" + room if room else "")).encode()).hexdigest()
        interaction_id = "vi_" + secrets.token_hex(16)
        outcome, replay = self.store.reserve_session(request_id, fingerprint, interaction_id)
        if outcome == "conflict":
            raise ServiceError(409, "Idempotency-Key reused with a different SDP or room")
        if outcome == "pending":
            raise ServiceError(409, "identical session admission is still pending")
        if replay is not None:
            with self.lock:
                live = replay.get("interaction_id") in self.interactions
            if not live:
                raise ServiceError(409, "session replay unavailable after server restart; create a new offer")
            return replay
        s = self.settings.get()
        names = P.Names.from_settings(s)
        history: list[dict[str, str]] = []
        if source is not None:
            away: list[dict[str, Any]] = []
            history = self.pause_history(source)
            resume = P.resume_block(interaction_tasks(self.store, source, names.assistant_name), names)
            with source.lock:
                # A call started from listening mode keeps its room across Pause/Resume; one paused
                # for listening mode comes back with what was just heard added.
                room = _merge_rooms(source.room, heard) if heard else source.room
        else:
            away, resume = self.store.away(), ""
        # A new call (not a resume) picks up the workspace: earlier calls' tasks that are running,
        # waiting on the user or finished but not cleared stay on its board, and its voice knows them.
        donors = self.carry_donors() if source is None else []
        carried = self.carry_runs(donors)
        open_work = ""
        if source is None:
            earlier = self.open_work_tasks(carried, names.assistant_name)
            open_work = P.open_work_block(earlier, names)
            shown = {t.get("run_id") for t in earlier}
            away = [a for a in away if a.get("run_id") not in shown]
        if room:
            logger.info("speakeasy: call has the room transcript from listening mode (%d words)", len(room.split()))
        instructions = self.instructions(away=away, resume=resume, tour=tour, room=room, open_work=open_work)
        seed = P.resume_input(history)
        provider = s["voice"]["provider"]
        voice = default_voice(s)
        transport = None
        try:
            if provider == "codex":
                transport = self._start_codex(sdp, instructions, seed, voice, s)
                live_id, answer = transport.thread_id, transport_answer(transport)
            elif provider == "grok":
                if not grok_beta_on(s):
                    raise ServiceError(409, GROK_BETA_OFF)
                token, base = self.grok_credential()
                if not token:
                    from .grok_live import SIGN_IN_HINT
                    raise ServiceError(409, SIGN_IN_HINT)
                from .grok_live import realtime_url
                # The voice socket opens in the worker; the app connects its relay to this call.
                transport = {"token": token, "url": realtime_url(base), "instructions": instructions,
                             "seed": seed, "voice": voice, "max_call_s": max_call_seconds(s)}
                live_id, answer = "grok_" + interaction_id[3:], "relay"
            else:
                key = self.openai_key()
                if not key:
                    raise ServiceError(409, f"voice.provider is openai but {OPENAI_KEY_NAME} is not set in this Hermes profile")
                from .openai_live import negotiate, session_payload
                result = (self._openai_negotiate or negotiate)(key, session_payload(instructions, voice, sdp, seed))
                live_id = (result.get("session") or {}).get("id")
                answer = (result.get("transport") or {}).get("sdp")
        except ServiceError:
            self.store.fail_session(request_id)
            raise
        except Exception as exc:
            if transport is not None and hasattr(transport, "stop"):
                transport.stop()
            self.store.fail_session(request_id)
            from .codex_transport import CodexStartError
            if isinstance(exc, CodexStartError):
                logger.warning("speakeasy: voice start failed: %s", exc)
                raise ServiceError(502, str(exc), reason=getattr(exc, "reason", None)) from None
            logger.warning("speakeasy: voice start failed (%s)", type(exc).__name__)
            raise ServiceError(502, f"Voice session creation failed: {type(exc).__name__}") from None
        if not isinstance(live_id, str) or not live_id or not isinstance(answer, str) or not answer:
            if transport is not None and hasattr(transport, "stop"):
                transport.stop()
            self.store.fail_session(request_id)
            raise ServiceError(502, "voice provider returned an invalid session response")
        wire: dict[str, Any] = {"type": "webrtc", "sdp": answer}
        if provider == "grok":
            from .grok_live import RELAY_FORMAT
            wire = {"type": "relay", "path": f"/voice/interactions/{interaction_id}/audio", "format": dict(RELAY_FORMAT)}
        client_result: dict[str, Any] = {
            "interaction_id": interaction_id, "session": {"id": live_id},
            "transport": wire, "voice_provider": provider,
        }
        if screen is None and source is not None:
            screen = source.attachments.declared  # a resume that doesn't say keeps the call's declaration
        interaction = Interaction(interaction_id=interaction_id, live_session_id=live_id, away=away,
                                  device_id=device_id, client=self.devices.client(device_id) if device_id else {},
                                  attachments=A.CallAttachments(screen),
                                  room=room if source is None else "", local=local)
        self.take_on(donors, carried, interaction)
        if source is not None:  # a resume: adopt moves the room text over from the paused call
            self.adopt(source, interaction, history)
            client_result["resumed_from"] = source.interaction_id
            if heard:
                # Listening mode was just turned off mid-conversation: a new take-off, so the voice
                # may respond from the room once more if nothing is said.
                with interaction.lock:
                    interaction.room = room
                    interaction.room_nudged = False
        with self.lock:
            self.interactions[interaction_id] = interaction
        self.store.complete_session(request_id, client_result)
        self._start_worker(interaction, provider, transport).start()
        # Weather is the most common quick question: have the home forecast ready before it's asked.
        home = (s.get("fast_routing") or {}).get("home_place") or ""
        if home and s.get("fast_routing", {}).get("quick_answers", True):
            from . import weather
            threading.Thread(target=weather.warm, args=(home,), daemon=True, name="speakeasy-weather-warm").start()
        return client_result

    def _start_codex(self, sdp: str, instructions: str, seed: list, voice: str, s: dict[str, Any]) -> Any:
        if self._codex_factory is not None:
            transport = self._codex_factory()
        else:
            from .codex_transport import CodexTransport
            # The copy signed in with ChatGPT, wherever it is on the machine (codex_discovery).
            binary = self.codex_discovery()["binary"]
            if binary is None:
                raise ServiceError(409, "Codex not found: install the ChatGPT desktop app (it includes Codex) or the Codex CLI, then run `hermes voice setup`")
            transport = CodexTransport(binary, self.dir / "codex", max_call_seconds(s))
        transport._answer = transport.start(sdp, instructions, seed, voice)
        return transport

    def _start_worker(self, interaction: Interaction, provider: str, transport: Any) -> SidebandWorker:
        if provider == "codex":
            from .codex_transport import CodexSidebandWorker
            return CodexSidebandWorker(self.rt, interaction, transport)
        if provider == "grok":
            from .grok_live import GrokRelayWorker
            return GrokRelayWorker(self.rt, interaction, connector=self._grok_connect, **transport)
        if self._openai_worker is not None:
            return self._openai_worker(self.rt, interaction)
        from .openai_live import OpenAISidebandWorker
        return OpenAISidebandWorker(self.rt, interaction, self.openai_key)

    def grok_credential(self) -> tuple[str, str]:
        """(token, base_url) for xAI realtime; read per call, never stored or returned."""
        if self._grok_credential is not None:
            return self._grok_credential()
        from .grok_live import resolve_credential
        return resolve_credential(self.home)

    def grok_state(self) -> tuple[bool, bool, str]:
        """(signed in to xAI, XAI_API_KEY set, message) for status and onboarding. No network."""
        if self._grok_signin is not None:
            return self._grok_signin()
        from .grok_live import signin_state
        return signin_state(self.home)

    def relay_worker(self, interaction_id: str) -> Any:
        """The call's relay-owning worker, for the app's audio relay connection."""
        interaction = self.interaction(interaction_id)
        worker = interaction.worker
        if worker is None or not hasattr(worker, "serve_client"):
            raise ServiceError(409, "this call has no audio relay")
        if interaction.call_closed:
            raise ServiceError(409, "call ended")
        return worker

    def finish_transport(self, interaction_id: str) -> dict[str, Any]:
        """End: close the provider session once the client saw it close (Codex needs an explicit stop)."""
        interaction = self.interaction(interaction_id)
        worker = interaction.worker
        with interaction.lock:
            if interaction.finalization == "confirmed":
                return {"finalization": "confirmed"}
        if hasattr(worker, "end_call"):  # relayed call (Grok): the plugin owns the voice socket
            worker.end_call("ended")
            for _ in range(40):
                with interaction.lock:
                    if interaction.finalization == "confirmed":
                        return {"finalization": "confirmed"}
                time.sleep(.05)
            return {"finalization": interaction.finalization}
        transport = getattr(worker, "transport", None)
        if transport is None:
            return {"finalization": interaction.finalization}
        if transport.thread_id != interaction.live_session_id:
            raise ServiceError(409, "voice session mismatch")
        try:
            transport.request("thread/realtime/stop", {"threadId": transport.thread_id}, timeout=4)
        except (RuntimeError, TimeoutError, OSError):
            raise ServiceError(502, "voice stop unconfirmed") from None
        for _ in range(20):
            with interaction.lock:
                if interaction.finalization != "open":
                    return {"finalization": interaction.finalization}
            time.sleep(.1)
        return {"finalization": "pending"}

    # -- pause / resume / idle ---------------------------------------------------------------
    def pause(self, interaction_id: str, *, stop_transport: bool = False,
              idle_minutes: int | None = None, limit: bool = False) -> dict[str, Any]:
        """Pause closes the paid voice session but keeps the call's transcript and tasks.
        ``idle_minutes``: paused for that long a quiet spell (the idle check), not by the user.
        ``limit``: paused at the call's length cap (voice.max_call_minutes), not by the user."""
        interaction = self.interaction(interaction_id)
        with interaction.lock:
            if interaction.successor is not None:
                raise ServiceError(409, "call was already resumed")
            already_closed = interaction.call_closed
            interaction.paused = True
            interaction.idle_paused_minutes = idle_minutes
            interaction.limit_paused = limit
        worker = interaction.worker
        if already_closed and worker is not None:
            with interaction.lock:
                if not interaction.history:
                    interaction.history = worker.turns()[-400:]
        # A relayed call (Grok) has no app-side voice session to close: the plugin closes it.
        if (stop_transport or hasattr(worker, "serve_client")) and worker is not None:
            try:
                worker.stop_transport()
            except Exception:
                pass
        # Captures and uploads stop with the pause; the app re-sends its pending things on resume.
        end_shared(self.rt, interaction, "paused")
        timer = threading.Timer(PAUSE_NOTICE_AFTER_S, self.pause_expired, args=(interaction,))
        timer.daemon = True
        timer.start()
        publish_state(self.store, interaction, self.settings.get()["assistant_name"])
        return {"interaction_id": interaction_id, "paused": True,
                **({"idle_paused_minutes": idle_minutes} if idle_minutes else {}),
                **({"limit_paused": True} if limit else {})}

    def pause_expired(self, interaction: Interaction) -> None:
        with interaction.lock:
            if interaction.successor is not None or not interaction.paused or interaction.pause_bookkept:
                return
            interaction.pause_bookkept = True
            # Paused this long is bookkept as ended: listening mode's room text goes with it.
            interaction.room = ""
        worker = interaction.worker
        try:
            self.store.mark_call_ended()
            if worker is not None:
                worker.still_working_notices()
        except Exception as exc:
            logger.warning("speakeasy: pause-expiry bookkeeping failed: %s", type(exc).__name__)

    def resumable(self, interaction_id: str) -> Interaction:
        source = self.interaction(interaction_id)
        with source.lock:
            if not source.paused:
                raise ServiceError(409, "call is not paused")
            if source.successor is not None:
                raise ServiceError(409, "call was already resumed")
        return source

    def pause_history(self, source: Interaction) -> list[dict[str, str]]:
        with source.lock:
            closed, history = source.call_closed, list(source.history)
        worker = source.worker
        if not closed and worker is not None:
            history = history + worker.turns()
        return history[-400:]

    def adopt(self, source: Interaction, interaction: Interaction, history: list[dict[str, str]]) -> None:
        with source.lock:
            if source.successor is not None:
                raise ServiceError(409, "call was already resumed")
            source.successor = interaction
            source.paused = False
            runs = dict(source.runs)
            # Listening mode's room text moves to the resumed call; the paused one no longer holds it.
            room, source.room = source.room, ""
            room_nudged = source.room_nudged
        interaction.runs.update(runs)
        with source.lock:
            interaction.carried_keys |= source.carried_keys
        interaction.room = room
        interaction.room_nudged = room_nudged   # an empty request after listening is handled once per call (the voice waits)
        interaction.history = history
        interaction.resumed_from = source.interaction_id
        interaction.revision = source.revision
        interaction.latest_delegation_id = source.latest_delegation_id

    # -- the workspace across calls --------------------------------------------------------------
    def carry_donors(self) -> list[Interaction]:
        """Ended or paused calls whose tasks no later call holds yet (oldest first)."""
        with self.lock:
            live = list(self.interactions.values())
        out = []
        for interaction in live:
            with interaction.lock:
                closed = interaction.call_closed or interaction.paused
                free = interaction.successor is None and interaction.carried_by is None
            if closed and free:
                out.append(interaction)
        return out

    def carry_runs(self, donors: list[Interaction]) -> dict[str, BackendRun]:
        """The donors' tasks that stay on the board: running, waiting on the user, or finished but
        not cleared (never a rejected one, nor one that settled before carrying existed)."""
        runs: dict[str, BackendRun] = {}
        for donor in donors:
            with donor.lock:
                runs.update((t, r) for t, r in donor.runs.items() if r.status != "rejected")
        keep = {key for key, _ in self.store.board_carry_keys(set(), limit=MAX_BOARD * 4)}
        picked = {t: r for t, r in runs.items() if r.idem_key in keep or r.status in ACTIVE_RUN_STATES}
        return dict(list(picked.items())[-MAX_BOARD:])

    def open_work_tasks(self, carried: dict[str, BackendRun], assistant_name: str) -> list[dict[str, Any]]:
        """Work rows (with task_id) of the earlier calls' tasks a new call starts with."""
        keys = {r.idem_key for r in carried.values()}
        rows = []
        for task_id, run in carried.items():
            work = self.store.work(idem_key=run.idem_key, assistant_name=assistant_name)
            if work is not None and not work.get("dismissed"):
                rows.append(dict(work, task_id=task_id))
        # Ones no call in memory holds (after a restart) are on the board too.
        rows += self.store.board_carry(keys | set(carried), MAX_BOARD)
        return rows[-MAX_BOARD:]

    def take_on(self, donors: list[Interaction], carried: dict[str, BackendRun], interaction: Interaction) -> None:
        """The new call holds the donors' open tasks: their results, approvals and follow-ups go to
        it (Interaction.head), and the router sees them as this call's open tasks."""
        if not carried:
            return
        for donor in donors:
            with donor.lock:
                if donor.successor is None and donor.carried_by is None:
                    donor.carried_by = interaction
        with interaction.lock:
            interaction.runs.update(carried)
            interaction.carried_keys |= {r.idem_key for r in carried.values()}
        logger.info("speakeasy: new call took on %d task(s) from earlier calls", len(carried))

    def idle_check(self, now: float | None = None) -> list[str]:
        """Auto-pause connected calls idle longer than idle_pause_minutes (0 = off)."""
        minutes = self.settings.get()["idle_pause_minutes"]
        if not minutes:
            return []
        now = time.monotonic() if now is None else now
        with self.lock:
            live = list(self.interactions.values())
        paused = []
        for interaction in live:
            worker = interaction.worker
            with interaction.lock:
                quiet = now - interaction.last_activity
                eligible = (worker is not None and not interaction.paused and not interaction.call_closed
                            and interaction.successor is None and quiet > minutes * 60)
                # A task still running is the call waiting on an answer, not a quiet call: it stays up so
                # the answer is spoken when it lands (up to IDLE_WORK_STRETCH x the idle setting).
                working = any(r.status in ACTIVE_RUN_STATES for r in interaction.runs.values())
            if eligible and working and quiet <= minutes * 60 * IDLE_WORK_STRETCH:
                continue
            if not eligible or not worker.call_connected():
                continue
            try:
                self.pause(interaction.interaction_id, stop_transport=True, idle_minutes=minutes)
                paused.append(interaction.interaction_id)
            except ServiceError:
                continue
        return paused

    def _idle_loop(self) -> None:
        ticks = 0
        self.settle_stuck_tasks()
        self._drain_quietly()  # held messages from before a reload
        self._check_image_support_soon()
        self.prune_shared()
        next_image_check = time.monotonic() + IMAGE_SUPPORT_EVERY_S
        while not self._stop.wait(IDLE_CHECK_S):
            try:
                self.idle_check()
            except Exception as exc:
                logger.warning("speakeasy: idle check failed: %s", type(exc).__name__)
            ticks += 1
            self._drain_quietly()  # held messages whose task settled while nothing was listening
            if ticks % 4 == 0:  # about once a minute
                self.settle_stuck_tasks()
                self.expire_staged()
            if ticks % TITLE_SWEEP_TICKS == 1:  # shortly after start, then about every ten minutes
                self._sweep_titles_soon()
            if time.monotonic() >= next_image_check:  # about once an hour
                self._check_image_support_soon()
                self.prune_shared()
                next_image_check = time.monotonic() + IMAGE_SUPPORT_EVERY_S

    def _sweep_titles_soon(self) -> None:
        """Rename finished tasks still under a stand-in label (the titler failed or never ran) on
        their own thread: each name is a model call. One sweep at a time."""
        running = getattr(self, "_title_sweep", None)
        if running is not None and running.is_alive():
            return
        self._title_sweep = threading.Thread(target=self.sweep_titles, name="speakeasy-title-sweep", daemon=True)
        self._title_sweep.start()

    def sweep_titles(self, limit: int = TITLE_SWEEP_BATCH) -> int:
        """Give stand-in-named finished tasks a real name, with the task each one really answers or
        joined as context (a "Fix and ship" is named for what it ships), by the same rule as a live
        call: never the task that merely ran before it, never a chat thread's name; with none, from
        its own words. A task the titler can't name is not retried this run. Returns how many were
        renamed."""
        from . import router
        from .text import drop_names, title_input
        settings = self.settings.get()
        user, assistant = settings.get("user_name") or "", settings.get("assistant_name") or ""
        tried = self.__dict__.setdefault("_title_tried", set())
        titler = getattr(self.rt, "title_call", None) or router.smart_title
        renamed = attempts = 0
        for task in self.store.stand_in_titles(limit=200):
            if attempts >= limit or self._stop.is_set():
                break  # a bounded batch per sweep; the rest wait for the next one
            key = self.store.key_for_task(task["task_id"])
            if not key or key in tried:
                continue
            tried.add(key)
            attempts += 1
            parent, parent_request = self.store.title_context(key)
            try:
                title = drop_names(titler(title_input(task["request"], parent=parent, parent_request=parent_request,
                                                      names=(user, assistant))), user, assistant)
            except Exception as exc:
                logger.info("speakeasy: title sweep skipped a task (%s)", type(exc).__name__)
                title = None
            if title and self.store.rename_stand_in(key, title):
                renamed += 1
        if renamed:
            logger.info("speakeasy: renamed %d stand-in task titles", renamed)
        return renamed

    def _check_image_support_soon(self) -> None:
        """Run the image support check on its own thread: it can wait tens of seconds on the network
        (offline models.dev lookups), which must not hold up auto-pause. One check at a time."""
        running = getattr(self, "_image_check", None)
        if running is not None and running.is_alive():
            return
        self._image_check = threading.Thread(target=self.refresh_image_support, name="speakeasy-image-check",
                                             daemon=True)
        self._image_check.start()

    def refresh_image_support(self) -> None:
        """Check again whether Hermes reads images; status reports the cached answer. Runs on the
        idle loop (at startup, then hourly), never on a request: the check can wait on the network."""
        refresh = getattr(self.hermes, "refresh_image_support", None)
        if refresh is None:
            return
        before = dict(getattr(self.hermes, "image_support", None) or {})
        try:
            support = refresh()
        except Exception as exc:
            logger.warning("speakeasy: image support check failed: %s", type(exc).__name__)
            return
        if support != before:
            logger.info("speakeasy: Hermes images: %s, vision: %s", support.get("images"), support.get("vision"))

    def settle_stuck_tasks(self) -> list[tuple[str, str]]:
        from .calls import LIVE_THREAD_WAITS
        from .recovery import sweep
        try:
            return sweep(self.store, self.home / "state.db", self.image_roots, LIVE_THREAD_WAITS)
        except Exception as exc:
            logger.warning("speakeasy: stuck-task sweep failed: %s", type(exc).__name__)
            return []

    # -- lookups -------------------------------------------------------------------------------
    def interaction(self, interaction_id: str) -> Interaction:
        if not ID_RE.fullmatch(interaction_id or ""):
            raise ServiceError(404, "interaction not found")
        with self.lock:
            value = self.interactions.get(interaction_id)
        if not value:
            raise ServiceError(404, "interaction not found")
        return value

    @staticmethod
    def _run(interaction: Interaction, run_id: str) -> Any:
        for backend in interaction.runs.values():
            if backend.run_id == run_id:
                return backend
        backend = interaction.runs.get(run_id)
        if backend is not None and backend.run_id:
            return backend
        raise ServiceError(409, "run does not belong to this interaction")

    @staticmethod
    def _live_summary_call(messages: list[dict[str, str]]) -> str | None:
        from .router import aux_call
        return aux_call(messages, 8.0, 120)

    def publish_all(self, key: str | None = None) -> None:
        events = getattr(self, "history_events", None)
        if events is not None and key:
            events.publish("session_updated", {"session_id": self.store.session_for(key)})
        with self.lock:
            live = list(self.interactions.values())
        name = self.settings.get()["assistant_name"]
        for interaction in live:
            with interaction.lock:
                touched = key is None or any(r.idem_key == key for r in interaction.runs.values())
            if touched:
                publish_state(self.store, interaction, name)

    def interaction_for_key(self, key: str) -> Interaction | None:
        with self.lock:
            live = list(self.interactions.values())
        for interaction in live:
            with interaction.lock:
                if any(r.idem_key == key for r in interaction.runs.values()):
                    return interaction.head()
        return None

    # -- task controls -------------------------------------------------------------------------
    def early_request(self, interaction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Words the app heard (and transcribed on the device) while the call was connecting.
        Waits briefly for the call's event channel, then hands them over as the first request."""
        text = body.get("text") if isinstance(body, dict) else None
        if set(body or {}) != {"text"} or not isinstance(text, str) or len(text) > 4000:
            raise ServiceError(400, "body must contain only text")
        interaction = self.interaction(interaction_id)
        deadline = time.monotonic() + EARLY_CONNECT_WAIT_S
        worker = interaction.worker
        while time.monotonic() < deadline:
            worker = interaction.worker
            if worker is not None and worker.loop is not None and worker.call_connected():
                break
            time.sleep(0.1)
        else:
            raise ServiceError(409, "the call isn't connected")
        future = asyncio.run_coroutine_threadsafe(worker.early_request(text), worker.loop)
        task_id = future.result(10)
        return {"interaction_id": interaction_id, "task_id": task_id}

    def answer(self, interaction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """A tap on a question card's option (or a typed answer): back to that task as a follow-up."""
        if not isinstance(body, dict) or set(body) != {"task_id", "text"}:
            raise ServiceError(400, "body must contain only task_id and text")
        task_id, text = body["task_id"], body["text"]
        if not isinstance(task_id, str) or not isinstance(text, str) or not text.strip() or len(text) > 400:
            raise ServiceError(400, "task_id and text must be short strings")
        worker = self.interaction(interaction_id).worker
        if worker is None or worker.loop is None or worker.loop.is_closed():
            raise ServiceError(409, "this call has ended; answer in the task's chat thread")
        future = asyncio.run_coroutine_threadsafe(worker.answer(task_id, text), worker.loop)
        sent = future.result(15)
        if not sent:
            raise ServiceError(404, "that task isn't in this call")
        return {"interaction_id": interaction_id, "task_id": task_id, "sent": True}

    def input_text(self, interaction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """A turn typed on the call screen instead of spoken (``session.input_text``): into the live
        call as the user's turn. After Pause/Resume the newest call in the chain takes it.

        An optional ``client_id`` makes a retry safe: the same id again on this call (a resend after
        a 504 or a dropped connection) never reaches the voice twice; it waits on, or reports, the
        first send. Without one the route behaves as it always has."""
        text = body.get("text") if isinstance(body, dict) else None
        client_id = body.get("client_id") if isinstance(body, dict) else None
        if (not isinstance(body, dict) or set(body) - {"client_id"} != {"type", "text"}
                or body.get("type") != INPUT_TEXT_TYPE or not isinstance(text, str)
                or ("client_id" in body and (not isinstance(client_id, str) or not ID_RE.fullmatch(client_id)))):
            raise ServiceError(400, f"body must be exactly type={INPUT_TEXT_TYPE} and text, plus an optional "
                                    "client_id", reason="invalid_body")
        text = typed_text(text)
        if not text:
            raise ServiceError(400, "text is empty", reason="empty_text")
        if len(text) > MAX_INPUT_TEXT:
            raise ServiceError(400, f"text is longer than {MAX_INPUT_TEXT} characters", reason="text_too_long")
        try:
            asked = self.interaction(interaction_id)
        except ServiceError:
            raise ServiceError(404, "interaction not found", reason="unknown_interaction") from None
        interaction = asked.head()
        key = (self._chain_root(asked), client_id) if client_id else None
        with self.typed_lock:
            prior = self.typed_sends.get(key) if key else None
            if prior is not None:
                if prior[0] != text:
                    raise ServiceError(409, "client_id was already used for a different message",
                                       reason="client_id_reused")
                if self._typed_failed(prior[1]):
                    del self.typed_sends[key]  # the first try didn't get through: this one is a new send
                    prior = None
            if prior is None:
                future = self._typed_send(interaction, text, client_id)
                if key:
                    self.typed_sends[key] = (text, future)
                    while len(self.typed_sends) > 256:
                        self.typed_sends.popitem(last=False)
            else:
                future = prior[1]
        outcome, took = self._typed_wait(future, retry_safe=key is not None)
        if outcome == TYPED_NOT_CONNECTED:
            raise ServiceError(409, "the call isn't connected", reason="not_connected")
        if outcome != TYPED_SENT:
            # The call is up; only this send failed. Nothing was recorded, so sending it again is safe.
            raise ServiceError(502, "the call didn't take the message; nothing was recorded", reason="not_delivered")
        reply: dict[str, Any] = {"interaction_id": took or interaction.interaction_id, "type": INPUT_TEXT_TYPE,
                                 "sent": True}
        if client_id:
            reply["client_id"] = client_id
            if prior is not None:
                reply["duplicate"] = True  # an earlier send with this id already took it
        return reply

    def _chain_root(self, interaction: Interaction) -> str:
        """The first call of a Pause/Resume chain: a typed turn's client_id is one turn per chain."""
        current, seen = interaction, set()
        while current.resumed_from and current.resumed_from not in seen:
            seen.add(current.interaction_id)
            with self.lock:
                earlier = self.interactions.get(current.resumed_from)
            if earlier is None:
                break
            current = earlier
        return current.interaction_id

    def _typed_send(self, interaction: Interaction, text: str, client_id: str | None) -> concurrent.futures.Future:
        """Start one typed turn on the call's worker loop (409 when there's no live call to take it)."""
        with interaction.lock:
            closed, paused = interaction.call_closed, interaction.paused
        if closed or paused:
            raise ServiceError(409, "the call isn't connected",
                               reason="call_paused" if paused else "call_ended")
        worker = interaction.worker
        loop = getattr(worker, "loop", None)
        if worker is None or loop is None or loop.is_closed() or not worker.call_connected():
            raise ServiceError(409, "the call isn't connected", reason="not_connected")

        async def send() -> tuple[str, str]:
            return await worker.input_text(text, client_id), interaction.interaction_id
        try:
            return asyncio.run_coroutine_threadsafe(send(), loop)
        except RuntimeError:  # the loop closed in between
            raise ServiceError(409, "the call isn't connected", reason="not_connected") from None

    @staticmethod
    def _typed_failed(future: concurrent.futures.Future) -> bool:
        """A finished send that didn't take the turn (a retry with its id may send it afresh)."""
        if not future.done():
            return False
        if future.cancelled() or future.exception() is not None:
            return True
        return future.result()[0] != TYPED_SENT

    @staticmethod
    def _typed_wait(future: concurrent.futures.Future, retry_safe: bool) -> tuple[str, str | None]:
        """Wait for a typed send. A 504 says what is true: the outcome isn't known, and the voice may
        already have the words. With a client_id the send carries on, so a retry with the same id
        learns how it ended without sending twice; without one it is cancelled, as before, and its
        echo guard goes with it (CodexSidebandWorker.forward_typed)."""
        try:
            return future.result(INPUT_TEXT_WAIT_S)
        except concurrent.futures.CancelledError:
            raise ServiceError(504, "the call didn't confirm the message in time; the voice may already "
                                    "have it", reason="timeout") from None
        except TimeoutError:  # concurrent.futures.TimeoutError is TimeoutError on 3.11+
            if not future.done():
                if retry_safe:
                    raise ServiceError(504, "the call hasn't confirmed the message yet and it may still go "
                                            "through; send it again with the same client_id to learn how it "
                                            "ended (it won't reach the call twice)", reason="timeout") from None
                future.cancel()
                raise ServiceError(504, "the call didn't confirm the message in time; the voice may already "
                                        "have it, so check before sending it again", reason="timeout") from None
            # It finished between the timeout and this check: report what the send actually did. A send
            # that went through is `sent`, never a 502 (which tells the app nothing was recorded).
            try:
                return future.result()
            except Exception as caught:
                exc = caught
        except Exception as caught:
            exc = caught
        logger.warning("speakeasy: typed turn failed (%s)", type(exc).__name__)
        raise ServiceError(502, f"typed message failed ({type(exc).__name__})", reason="transport_error") from None

    def focus(self, interaction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Vision Pro: the task the user is facing or holding (or none). Unnamed requests go there."""
        if not isinstance(body, dict) or not set(body) <= {"task_id", "detail"} or "task_id" not in body:
            raise ServiceError(400, "body must contain task_id and optionally detail")
        task_id, detail = body["task_id"], body.get("detail") or ""
        if task_id is not None and (not isinstance(task_id, str) or not ID_RE.fullmatch(task_id)):
            raise ServiceError(400, "task_id must be a task id or null")
        if not isinstance(detail, str) or len(detail) > 200:
            raise ServiceError(400, "detail must be a short string")
        interaction = self.interaction(interaction_id)
        detail = re.sub(r"[\x00-\x1f\x7f]", " ", detail).strip()
        with interaction.lock:
            changed = (interaction.focus_task_id, interaction.focus_detail) != (task_id, detail)
            interaction.focus_task_id, interaction.focus_detail = task_id, detail
        worker = interaction.worker
        if changed and worker is not None and worker.loop is not None and not worker.loop.is_closed():
            asyncio.run_coroutine_threadsafe(worker.focus_changed(), worker.loop)
        return {"interaction_id": interaction_id, "task_id": task_id}

    def set_mic(self, interaction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """The app's mic went on or off: exactly ``{muted}``. Only the call pulse reads it (no
        "still working" lines while muted); results, questions and approvals are spoken either way."""
        muted = body.get("muted") if isinstance(body, dict) else None
        if set(body or {}) != {"muted"} or not isinstance(muted, bool):
            raise ServiceError(400, "body must be exactly {\"muted\": true|false}")
        interaction = self.interaction(interaction_id)
        with interaction.lock:
            interaction.mic_muted = muted
        return {"interaction_id": interaction_id, "muted": muted}

    def mic_check(self, interaction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """The app found the call's mic wasn't getting through and reopened the connection.
        Logged (no audio, no words) so dead-mic calls show up in the logs with a reason."""
        note = body.get("note") if isinstance(body, dict) else None
        if set(body or {}) != {"note"} or not isinstance(note, str) or len(note) > 400:
            raise ServiceError(400, "body must contain only note")
        self.interaction(interaction_id)
        logger.warning("speakeasy: mic check on %s: %s", interaction_id[:10], re.sub(r"[^\w :.,()/;+-]", "", note))
        return {"interaction_id": interaction_id, "logged": True}

    def skip_tour(self, interaction_id: str) -> dict[str, Any]:
        """Skip button in the panel: tell the live call to drop the first-call tour."""
        interaction = self.interaction(interaction_id)
        if interaction.worker is not None:
            interaction.worker.speak_from_thread("session.thinking.append", P.TOUR_SKIPPED_NOTE)
        return {"interaction_id": interaction_id, "tour": "skipped"}

    def cancel_backend(self, interaction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        interaction = self.interaction(interaction_id)
        run_id = body.get("run_id")
        if set(body) != {"run_id"} or not isinstance(run_id, str) or not ID_RE.fullmatch(run_id):
            raise ServiceError(400, "exact run_id is required")
        with interaction.lock:
            backend = self._run(interaction, run_id)
        return self._cancel(interaction, backend, interaction_id)

    def stop_task(self, run_id: str) -> dict[str, Any]:
        """``POST /voice/work/{run_id}/stop``: stop one running task by id, with or without a call (a
        panel row's Stop). Finds the call or voiceless worker holding it and stops it as a call's Stop does."""
        if not ID_RE.fullmatch(run_id or ""):
            raise ServiceError(404, "I couldn't find that task.")
        key = self.store.key_for_run(run_id) or self.store.key_for_task(run_id)
        with self.lock:
            holders = list(self.interactions.values())
        text = self._text_worker
        if text is not None and getattr(text, "interaction", None) is not None:
            holders.append(text.interaction)
        for interaction in holders:
            with interaction.lock:
                found = next((r for r in interaction.runs.values()
                              if r.run_id and (r.run_id == run_id or (key is not None and r.idem_key == key))), None)
            if found is not None:
                return self._cancel(interaction, found, interaction.interaction_id)
        raise ServiceError(409, "That task isn't running here, so there's nothing to stop.")

    def _cancel(self, interaction: Interaction, backend: Any, interaction_id: str | None) -> dict[str, Any]:
        with interaction.lock:
            if backend.status in TERMINAL or backend.status in {"ambiguous", "cancel_requested"}:
                raise ServiceError(409, "no active backend run")
            backend.status = "cancel_requested"
        self.store.update_run(backend.idem_key, None, "cancel_requested")
        self.store.progress(backend.idem_key, "milestone", "Stop requested")
        name = self.settings.get()["assistant_name"]
        publish_state(self.store, interaction, name)
        try:
            result = self.hermes.stop(backend.run_id)
        except Exception:
            with interaction.lock:
                if backend.status == "cancel_requested":
                    backend.status, backend.error = "ambiguous", "Backend stop outcome is ambiguous"
            publish_state(self.store, interaction, name)
            raise ServiceError(502, "Hermes stop failed") from None
        return {"interaction_id": interaction_id, "backend_cancel": result.get("status", "unknown"),
                "run_id": backend.run_id, "voice_session": "open"}

    def approve(self, interaction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        interaction = self.interaction(interaction_id)
        request_id, run_id, choice = body.get("request_id"), body.get("run_id"), body.get("choice")
        if (set(body) != {"request_id", "run_id", "choice"}
                or not isinstance(request_id, str) or not ID_RE.fullmatch(request_id)
                or not isinstance(run_id, str) or not ID_RE.fullmatch(run_id) or choice not in {"once", "session", "deny"}):
            # Never "always": Speakeasy grants at most the rest of one task (hermes_api.approve).
            raise ServiceError(400, "exact run_id, request_id, and once|session|deny are required")
        with interaction.lock:
            backend = self._run(interaction, run_id)
            approval = backend.approval
            if backend.status != "waiting_for_approval" or not approval or request_id != approval.get("request_id"):
                raise ServiceError(409, "approval is stale or not pending for that run")
            offered = list(approval.get("choices") or ["once", "deny"])
            if choice not in offered:
                raise ServiceError(400, "that approval can't be answered that way")
            backend.approval, backend.status = None, "resolving_approval"
        name = self.settings.get()["assistant_name"]
        try:
            result = self.hermes.approve(backend.run_id, request_id, choice, offered)
        except Exception:
            with interaction.lock:
                if backend.status == "resolving_approval":
                    backend.status, backend.error = "ambiguous", "Backend approval outcome is ambiguous"
            publish_state(self.store, interaction, name)
            raise ServiceError(502, "Hermes approval failed") from None
        with interaction.lock:
            if backend.status == "resolving_approval":
                backend.status = "working"
        self.store.update_run(backend.idem_key, None, "working")
        self.store.progress(backend.idem_key, "milestone", APPROVED_MILESTONE[choice])
        publish_state(self.store, interaction, name)
        return {"interaction_id": interaction_id, "choice": choice, "run_id": backend.run_id,
                "resolved": result.get("resolved", 0)}

    def dismiss_tasks(self, body: dict[str, Any]) -> dict[str, Any]:
        run_ids = body.get("run_ids")
        if set(body) != {"run_ids"} or not isinstance(run_ids, list) or not run_ids or len(run_ids) > 50 \
                or not all(isinstance(r, str) and ID_RE.fullmatch(r) for r in run_ids):
            raise ServiceError(400, "run_ids must be a list of run ids")
        dismissed = self.store.dismiss(run_ids)
        # Clearing a task deletes Speakeasy's copies of what was shared with it.
        self.forget_task_files([k for k in (self.store.key_for_run(r) for r in dismissed) if k])
        with self.lock:
            live = list(self.interactions.values())
        name = self.settings.get()["assistant_name"]
        for interaction in live:
            with interaction.lock:
                touched = any(r.run_id in dismissed for r in interaction.runs.values())
            if touched:
                publish_state(self.store, interaction, name)
        return {"dismissed": dismissed}

    def answer_seen(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /voice/tasks/{run_id}/seen``: the user opened (or dismissed) this answer in an app. It
        leaves New answers on every device, and the voice never reads it out afterwards (a late result,
        or a resumed call's results that arrived while it was paused)."""
        if not ID_RE.fullmatch(run_id) or not isinstance(body, dict) or body:
            raise ServiceError(400, "body must be empty")
        key = self.store.key_for_run(run_id) or (run_id if self.store.run_row(run_id) else None)
        if key is None:
            raise ServiceError(404, "task not found")
        self.store.mark_seen(key)
        self._republish_all()
        return {"run_id": run_id, "seen": True}

    def set_aside(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """"Not needed": set aside a question, decision or approval waiting on the user without
        answering it (already handled, or not needed). The task settles as set aside (not failed),
        a run still waiting on the answer is told to wrap up, and a live call hears it quietly so
        the voice never asks again. A finished task's question starts no new work."""
        if not ID_RE.fullmatch(run_id) or not isinstance(body, dict) or body:
            raise ServiceError(400, "body must be empty")
        key = self.store.key_for_run(run_id)
        if key is None:
            raise ServiceError(404, "task not found")
        with self.lock:
            live = list(self.interactions.values())
        for interaction in live:
            with interaction.lock:
                task_id = next((t for t, r in interaction.runs.items() if r.idem_key == key), None)
            worker = interaction.worker
            if task_id is None or worker is None or worker.loop is None or worker.loop.is_closed():
                continue
            future = asyncio.run_coroutine_threadsafe(worker.set_aside(task_id), worker.loop)
            if not future.result(20):
                raise ServiceError(409, "nothing is waiting on you there")
            return {"run_id": run_id, "set_aside": True}
        # No live call holds it: record it; the next call and every app see it closed.
        if not self.store.set_aside(key):
            raise ServiceError(409, "nothing is waiting on you there")
        for interaction in live:  # an approval left waiting after its call ended: decline it
            with interaction.lock:
                backend = next((r for r in interaction.runs.values() if r.idem_key == key), None)
                approval = backend.approval if backend and backend.status == "waiting_for_approval" else None
                if approval and backend.run_id:
                    backend.approval, backend.status = None, "working"
            if approval and backend.run_id:
                try:
                    self.hermes.approve(backend.run_id, approval.get("request_id", ""), "deny")
                except Exception:
                    logger.warning("speakeasy: set-aside could not decline the approval")
        self.store.progress(key, "milestone", "Set aside")
        self._republish_all()
        return {"run_id": run_id, "set_aside": True}

    def dismiss_review(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Dismiss a finished task's image review card (all its images, or the listed card numbers).
        Stored per run and card, so it stays dismissed across calls; the images stay in the task."""
        cards = body.get("cards")
        if not ID_RE.fullmatch(run_id) or not set(body) <= {"cards"} or (cards is not None and (
                not isinstance(cards, list) or not cards or len(cards) > MAX_CARDS
                or not all(isinstance(c, int) and not isinstance(c, bool) and 1 <= c <= MAX_CARDS for c in cards))):
            raise ServiceError(400, "cards must be a list of card numbers")
        if self.store.key_for_run(run_id) is None:
            raise ServiceError(404, "task not found")
        dismissed = self.store.dismiss_review(run_id, cards)
        self._republish_all()
        return {"run_id": run_id, "dismissed": dismissed}

    def _republish_all(self) -> None:
        with self.lock:
            live = list(self.interactions.values())
        name = self.settings.get()["assistant_name"]
        for interaction in live:
            publish_state(self.store, interaction, name)

    def work_latest(self) -> dict[str, Any]:
        self.sweep_questions()
        name = self.settings.get()["assistant_name"]
        tasks = self.store.latest_tasks(MAX_TASKS)
        if any(t.get("status") in TERMINAL and vague_title(t.get("title")) for t in tasks):
            # A finished task on the list still has a name that says nothing ("Check work status"):
            # rename it now (in the background), not at the next ten-minute sweep.
            self._sweep_titles_soon()
        return {"work": self.store.work(assistant_name=name), "away": self.store.away(), "tasks": tasks}

    # -- card actions (docs/API.md "Card actions") -------------------------------------------------
    def card_action(self, run_id: str | None, body: dict[str, Any], interaction_id: str | None = None) -> dict[str, Any]:
        """A button on a task's card: ``{"view": i, "action": id, "value": optional}`` (plus ``run_id``
        on the call route). The server decides what an action id does; unknown ones are refused."""
        if not isinstance(body, dict):
            raise ServiceError(400, "body must be an object")
        allowed = {"view", "action", "value"} | ({"run_id"} if interaction_id is not None else set())
        if not set(body) <= allowed or "view" not in body or "action" not in body:
            raise ServiceError(400, "body must contain view and action" + (" and run_id" if interaction_id else ""))
        if interaction_id is not None:
            self.interaction(interaction_id)  # 404 for an unknown call
            run_id = body.get("run_id")
        index, action, value = body["view"], body["action"], body.get("value")
        if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id):
            raise ServiceError(400, "run_id must be a run id")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ServiceError(400, "view must be a card index")
        if not isinstance(action, str) or not action:
            raise ServiceError(400, "action must be an action id")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float, str))):
            raise ServiceError(400, "value must be a number or a short string")
        found = self.store.run_result(run_id)
        if found is None:
            raise ServiceError(404, "not found")
        key, result = found
        views = result.get("views") if isinstance(result.get("views"), list) else []
        if index >= len(views) or not isinstance(views[index], dict):
            raise ServiceError(404, "that card isn't on this task")
        view = views[index]
        if action not in {a.get("id") for a in view.get("actions") or [] if isinstance(a, dict)}:
            raise ServiceError(400, "that card has no such button")
        if view.get("kind") == "home" and action == "toggle":
            devices = view.get("devices") or []
            entity_id = devices[0].get("entity_id") if devices and isinstance(devices[0], dict) else None
            if not isinstance(entity_id, str):
                raise ServiceError(400, "that card has no device")
            if value is not None and not isinstance(value, (int, float)):
                raise ServiceError(400, "value must be a brightness from 0 to 100")
            # The button does what its label said ("Turn off" on a card showing it on), not a blind flip.
            shown = devices[0].get("on")
            reply, card = self.home_control.toggle(entity_id, value,
                                                   want_on=(not shown) if isinstance(shown, bool) else None)
            if not reply.ok:
                raise ServiceError(409, reply.spoken)
            if card is not None:
                views[index] = card
                result["views"] = views
                self.store.set_result(key, result)
                self.store.progress(key, "milestone", reply.spoken)
            self.publish_all(key)
            return {"ok": True, "message": reply.spoken}
        raise ServiceError(400, "this server doesn't do that action yet")

    def work(self, run_id: str) -> dict[str, Any]:
        if not ID_RE.fullmatch(run_id):
            raise ServiceError(404, "not found")
        # One task by id: its whole tool-call log (args and result previews), not the stream's summary.
        work = self.store.work(run_id, assistant_name=self.settings.get()["assistant_name"], activity_detail=True)
        key = self.store.key_for_run(run_id) or self.store.key_for_task(run_id)
        if work is not None and key:
            work["queued"] = self.queued_for([key, *([work["idem_key"]] if work.get("idem_key") else [])])
            work["steered"] = self.steered_for([key])
        return {"work": work}

    # -- messages sent to one task on purpose (docs/API.md "Messages to a task") -------------------
    MESSAGE_REPLAY_S = 600

    def task_message(self, run_id: str, body: dict[str, Any], device_id: str | None = None) -> dict[str, Any]:
        """``POST /voice/work/{run_id}/message``: text typed to one task. Goes to that task by id, never
        through routing: a steer while it runs, the answer while it waits on the user, a follow-up job
        once it has finished. Idempotent on client_id. ``attachments``: ids this device staged
        (POST /voice/attachments), carried the way the Mac's panel attachments are."""
        if not isinstance(body, dict) or "text" not in body or not set(body) <= {
                "text", "source", "client_id", "interaction_id", "choice_task_id", "attachments", "mode"}:
            raise ServiceError(400, "Send the message as text.")
        mode = self._message_mode(body)
        text, source = body.get("text"), body.get("source") or "typed"
        client_id, interaction_id = body.get("client_id"), body.get("interaction_id")
        choice_task_id = body.get("choice_task_id")
        ids = self._attachment_ids(body)
        if isinstance(text, str) and not text.strip() and ids:
            text = LOOK_TEXT
        if not isinstance(text, str) or not text.strip():
            raise ServiceError(400, "The message is empty.")
        text = text.strip()
        if len(text) > M.MAX_TEXT:
            raise ServiceError(400, f"The message is too long (at most {M.MAX_TEXT} characters).")
        if source != "typed":
            raise ServiceError(400, "source must be typed.")
        for name, value in (("client_id", client_id), ("interaction_id", interaction_id),
                            ("choice_task_id", choice_task_id)):
            if value is not None and (not isinstance(value, str) or not ID_RE.fullmatch(value)):
                raise ServiceError(400, f"{name} must be an id.")
        if not ID_RE.fullmatch(run_id or ""):
            raise ServiceError(404, "I couldn't find that task.")
        if mode == "queue" and not choice_task_id:
            held = self._queued_reply(client_id) if client_id else None
            if held is not None:
                return held  # the same message again (a retry, or after a reload): never held twice
            key, row = self._message_target(run_id)
            if row["status"] in QUEUE_BEHIND:
                if ids:
                    raise ServiceError(409, "Pictures and files can't wait for the task to finish. Send them "
                                            "now, or add them when it's done.", reason="not_now")
                return self._queue(client_id, "task", run_id, self.store.session_for(key), key, text)
        got = self._once(client_id, lambda: self._with_staged(
            device_id, ids, lambda staged: self._task_message(run_id, text, interaction_id, choice_task_id, staged)))
        self._note_steered(client_id, got, None, text)
        return got

    # -- steer or queue (docs/API.md "Steer or queue") ----------------------------------------------

    @staticmethod
    def _message_mode(body: dict[str, Any]) -> str:
        mode = body.get("mode") or "steer"
        if mode not in {"steer", "queue"}:
            raise ServiceError(400, "mode must be steer or queue.")
        return mode

    def _queue(self, client_id: str | None, kind: str, target: str, page_session: str | None,
               watch_key: str, text: str) -> dict[str, Any]:
        cid = client_id or ("q_" + secrets.token_hex(8))
        self.store.queue_message(cid, kind, target, page_session, watch_key, text)
        logger.info("speakeasy: a typed message waits for its task to finish")
        self.publish_all(watch_key)
        reply = self._queued_reply(cid)
        assert reply is not None
        return reply

    def _queued_reply(self, client_id: str) -> dict[str, Any] | None:
        """The reply for a held message (the first send's reply, again, on a retry)."""
        row = self.store.queued_message(client_id)
        if row is None:
            return None
        run_row = self.store.run_row(row["watch_key"]) or {}
        run_id = self.store.openable_run_id(row["watch_key"]) or run_row.get("run_id")
        out: dict[str, Any] = {"ok": True, "run_id": run_id, "delivered_as": "queued", "client_id": client_id,
                               "queue_position": self.store.queue_position(client_id), "queue_state": row["state"]}
        if row["kind"] == "session":
            out["session_id"] = row["target"]
            out["via"] = self._send_via(row["target"])
        if row["state"] == "sent" and row["reply"]:
            out["sent_as"] = row["reply"].get("delivered_as")
            out["sent_run_id"] = row["reply"].get("run_id")
        return out

    @staticmethod
    def _queued_public(row: dict[str, Any], position: int) -> dict[str, Any]:
        return {"client_id": row["client_id"], "text": row["text"], "at": row["created"], "position": position,
                **({"error": "That didn't go through yet; it will be tried again."} if row.get("error") else {})}

    def queued_for(self, keys: list[str], page_session: str | None = None) -> list[dict[str, Any]]:
        """Held messages for a page: waiting on any of *keys* or sent from *page_session*, oldest first."""
        rows = self.store.queued_messages(keys, page_session)
        counts: dict[str, int] = {}
        out = []
        for row in rows:
            counts[row["watch_key"]] = counts.get(row["watch_key"], 0) + 1
            out.append(self._queued_public(row, counts[row["watch_key"]]))
        return out

    def cancel_queued(self, client_id: str) -> dict[str, Any]:
        """``DELETE /voice/{work/{run_id}|history/{session_id}}/queued/{client_id}``."""
        if not ID_RE.fullmatch(client_id or ""):
            raise ServiceError(404, "That message isn't waiting any more.")
        row = self.store.queued_message(client_id)
        state = self.store.cancel_queued(client_id)
        if state is None:
            raise ServiceError(404, "That message isn't waiting any more.")
        if state != "cancelled":
            raise ServiceError(409, "That message was already sent.", reason="sent")
        if row:
            self.publish_all(row["watch_key"])
        return {"ok": True, "client_id": client_id, "cancelled": True}

    def steer_queued(self, client_id: str) -> dict[str, Any]:
        """``POST /voice/{work/{run_id}|history/{session_id}}/queued/{client_id}/steer``: send a held
        message now, the way a steer is sent (into the running task, or as the next turn if it's done)."""
        if not ID_RE.fullmatch(client_id or ""):
            raise ServiceError(404, "That message isn't waiting any more.")
        row = self.store.queued_message(client_id)
        if row is None or row["state"] == "cancelled":
            raise ServiceError(404, "That message isn't waiting any more.")
        if row["state"] == "sent" and row["reply"]:
            return {**row["reply"], "client_id": client_id}  # already sent (a retry)
        if not self.store.claim_queued(client_id):
            raise ServiceError(409, "That message is being sent now.", reason="sending")
        return self._send_held(row, steered=True)

    def _send_held(self, row: dict[str, Any], steered: bool = False) -> dict[str, Any]:
        """Send one claimed held message (state sending) through the normal path; sent or back in the queue."""
        cid = row["client_id"]
        try:
            if row["kind"] == "session":
                meta = H.session_meta(self.rt.state_db, row["target"])
                if meta is None:
                    raise ServiceError(404, "I couldn't find that conversation.")
                got = self._history_message(meta, row["text"])
            else:
                got = self._task_message(row["target"], row["text"], None, None)
        except Exception as exc:
            self.store.release_queued(cid, type(exc).__name__)
            self.publish_all(row["watch_key"])
            raise
        reply = {k: v for k, v in got.items() if k != "task"}
        reply["client_id"] = cid
        if steered:
            self._note_steered(cid, got, row["page_session"], row["text"])
        self.store.finish_queued(cid, reply)
        new_key = self.store.key_for_run(got["run_id"]) if got.get("run_id") else None
        if new_key and new_key != row["watch_key"]:
            # The rest of the queue waits for the turn this one started, then goes next.
            self.store.rewatch_queued(row["watch_key"], new_key)
        self.publish_all(row["watch_key"])
        if new_key:
            self.publish_all(new_key)
        logger.info("speakeasy: a queued message was sent (%s, %s)", "steer now" if steered else "after its task",
                    got.get("delivered_as"))
        return {**got, "client_id": cid}

    def _queue_settled(self, key: str, status: str) -> None:
        """store.update_run listener: a task with held messages finished; send the next one shortly
        (off the worker's loop: sending waits on it)."""
        if status in QUEUE_SENDS_AFTER and self.store.queued_messages([key]):
            timer = threading.Timer(0.3, self._drain_quietly)
            timer.daemon = True
            timer.start()
        elif self.store.queued_messages([key]):
            self.publish_all(key)  # stopped: the queue stays, shown on the page

    def _drain_quietly(self) -> None:
        try:
            self.drain_queued()
        except Exception as exc:
            logger.warning("speakeasy: sending a queued message failed (%s)", type(exc).__name__)
        try:
            self.sweep_steered()
        except Exception as exc:
            logger.warning("speakeasy: checking steered messages failed (%s)", type(exc).__name__)

    # -- what became of a steered message (docs/API.md "Steer or queue": receipts) --------------------

    def _note_steered(self, client_id: str | None, got: dict[str, Any], page_session: str | None, text: Any) -> None:
        """A typed message went into a running task: follow it (sent -> seen | follow_up | unused)."""
        if not client_id or not isinstance(got, dict) or got.get("delivered_as") != "steer" or not isinstance(text, str):
            return
        key = self.store.key_for_run(got["run_id"]) if got.get("run_id") else None
        if key is None:
            return
        self.store.add_steered(client_id, key, page_session or got.get("session_id"), text.strip())

    @staticmethod
    def _paraphrase(text: str, words: int = 6) -> str:
        parts = " ".join(str(text or "").split()).split(" ")
        short = " ".join(parts[:words]).rstrip(".,;:!?")
        return short + ("…" if len(parts) > words else "")

    def _follow_up_settled(self, key: str, follow_up_id: str, state: str) -> None:
        """store.settle_follow_up listener: a steered message's receipt settled."""
        for row in self.store.steered_messages([key], states=("sent",)):
            receipt = self.store.follow_up_receipt(key, row["text"], row["sent_at"])
            if receipt is None or receipt["id"] != follow_up_id:
                continue
            if state == "landed":
                self._steer_seen(row, receipt.get("settled"))
            else:
                timer = threading.Timer(0.2, self._sweep_quietly)
                timer.daemon = True
                timer.start()

    def _sweep_quietly(self) -> None:
        try:
            self.sweep_steered()
        except Exception as exc:
            logger.warning("speakeasy: checking steered messages failed (%s)", type(exc).__name__)

    def _steer_seen(self, row: dict[str, Any], at: float | None) -> None:
        if not self.store.settle_steered(row["client_id"], "seen", seen_at=at or time.time()):
            return
        self.store.adjusting(row["idem_key"], "Adjusting: " + self._paraphrase(row["text"]))
        self._steer_changed(row, "seen")

    def _steer_changed(self, row: dict[str, Any], state: str) -> None:
        events = getattr(self, "history_events", None)
        if events is not None:
            events.publish("message_updated", {"session_id": row.get("page_session") or self.store.session_for(row["idem_key"]),
                                               "client_id": row["client_id"], "state": state})
        self.publish_all(row["idem_key"])

    def sweep_steered(self) -> int:
        """Settle steered messages whose task read them, or ended without reading them (then sent again
        as the next turn, or kept as unused when the task was stopped). Returns how many changed."""
        changed = 0
        now = time.time()
        for row in self.store.steered_messages(states=("sent",), limit=200):
            key = row["idem_key"]
            receipt = self.store.follow_up_receipt(key, row["text"], row["sent_at"])
            if receipt and receipt["state"] == "landed":
                self._steer_seen(row, receipt.get("settled"))
                changed += 1
                continue
            run = self.store.run_row(key) or {}
            ended = run.get("status") in TERMINAL
            unread = (receipt and receipt["state"] == "not_read") or (
                ended and receipt is None and now - row["sent_at"] > STEER_UNREAD_GRACE_S)
            if not unread or not ended:
                continue
            if run.get("status") not in QUEUE_SENDS_AFTER:
                if self.store.settle_steered(row["client_id"], "unused"):
                    self._steer_changed(row, "unused")
                    changed += 1
                continue
            target = self.store.openable_run_id(key) or run.get("run_id")
            try:
                got = self._task_message(target, row["text"], None, None)
            except Exception as exc:
                logger.warning("speakeasy: a steered message the task never read could not be resent (%s)",
                               type(exc).__name__)
                continue
            if self.store.settle_steered(row["client_id"], "follow_up", follow_up_run=got.get("run_id")):
                logger.info("speakeasy: a steered message the task never read went as a follow-up")
                self._steer_changed(row, "follow_up")
                changed += 1
        return changed

    def steered_for(self, keys: list[str], page_session: str | None = None) -> list[dict[str, Any]]:
        """What became of messages steered into this page's tasks, oldest first."""
        name = self.settings.get()["assistant_name"]
        now = time.time()
        out = []
        for row in self.store.steered_messages(keys, page_session):
            state = row["state"]
            if state == "sent":
                run = self.store.run_row(row["idem_key"]) or {}
                if run.get("status") in QUEUE_BEHIND and now - row["sent_at"] >= STEER_WAITING_AFTER_S:
                    state = "waiting"
            label = {"sent": "Sent to the running task",
                     "waiting": "Waiting for the current step to finish",
                     "seen": f"Seen by {name}",
                     "follow_up": "It finished first · sent as a follow-up",
                     "unused": "The task stopped before reading this"}[state]
            item: dict[str, Any] = {"client_id": row["client_id"], "text": row["text"], "at": row["sent_at"],
                                    "state": state, "label": label}
            if row["seen_at"]:
                item["seen_at"] = row["seen_at"]
            if row["follow_up_run"]:
                item["run_id"] = row["follow_up_run"]
            out.append(item)
        return out

    def drain_queued(self) -> int:
        """Send the next held message of every task that has completed or failed (one per task; the
        rest wait for the turn it starts). Run on each settle and on the idle check. Returns how many went."""
        if not self._drain_lock.acquire(blocking=False):
            return 0
        sent = 0
        try:
            done: set[str] = set()
            for row in self.store.queued_messages():
                watch = row["watch_key"]
                if watch in done:
                    continue
                done.add(watch)
                run = self.store.run_row(watch)
                if run is None:
                    continue
                if run["status"] not in QUEUE_SENDS_AFTER:
                    continue  # still running, or stopped (kept, shown)
                if not self.store.claim_queued(row["client_id"]):
                    continue
                try:
                    self._send_held(row)
                    sent += 1
                except Exception as exc:
                    logger.warning("speakeasy: a queued message could not be sent yet (%s)", type(exc).__name__)
        finally:
            self._drain_lock.release()
        return sent

    def _once(self, client_id: str | None, send: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Run ``send`` once per client_id (MESSAGE_REPLAY_S); a retry gets the first reply."""
        if client_id is None:
            return send()
        mine, event = self._claim_message(client_id)
        if not mine:
            event.wait(M.START_WAIT_S + 15)
            with self._message_lock:
                done = self._message_replies.get(client_id)
            if done is None or done[2] is None:
                raise ServiceError(409, "That message is still being sent. Try again in a moment.")
            return done[2]
        try:
            reply = send()
        except Exception:
            with self._message_lock:
                self._message_replies.pop(client_id, None)  # a failed send may be retried with the same id
            event.set()
            raise
        with self._message_lock:
            self._message_replies[client_id] = (time.monotonic(), event, reply)
        event.set()
        return reply

    def start_job(self, body: dict[str, Any], device_id: str | None = None) -> dict[str, Any]:
        """``POST /voice/work``: a new job from text typed in the app, with no call (the plugin-update
        notice's Update button). It runs like a job started on a call, on the voiceless worker, never
        through routing, and shows on the board. Idempotent on client_id."""
        if not isinstance(body, dict) or "text" not in body or not set(body) <= {
                "text", "source", "client_id", "attachments", "model", "reasoning"}:
            raise ServiceError(400, "Send the job as text.")
        choice = self._body_choice(body)
        text, source, client_id = body.get("text"), body.get("source") or "typed", body.get("client_id")
        ids = self._attachment_ids(body)
        if isinstance(text, str) and not text.strip() and ids:
            text = LOOK_TEXT
        if not isinstance(text, str) or not text.strip():
            raise ServiceError(400, "The job is empty.")
        text = " ".join(text.split())
        if len(text) > M.MAX_TEXT:
            raise ServiceError(400, f"The job is too long (at most {M.MAX_TEXT} characters).")
        if source != "typed":
            raise ServiceError(400, "source must be typed.")
        if client_id is not None and (not isinstance(client_id, str) or not ID_RE.fullmatch(client_id)):
            raise ServiceError(400, "client_id must be an id.")
        return self._once(client_id, lambda: self._with_staged(
            device_id, ids, lambda staged: self._start_job(text, staged, choice=choice, new_chat=True)))

    @staticmethod
    def _attachment_ids(body: dict[str, Any]) -> list[str]:
        ids = body.get("attachments")
        if ids is None:
            return []
        if (not isinstance(ids, list) or not all(isinstance(i, str) and ID_RE.fullmatch(i) for i in ids)
                or len(ids) > A.MAX_ATTACHMENTS):
            raise ServiceError(400, f"attachments must be up to {A.MAX_ATTACHMENTS} attachment ids.")
        return ids

    def _with_staged(self, device_id: str | None, ids: list[str],
                     send: Callable[[list[A.Attachment]], dict[str, Any]]) -> dict[str, Any]:
        """Claim this device's staged attachments for one message, all or nothing, and give them
        back if the message never went (a retry with the same ids may send them)."""
        if not ids:
            return send([])
        if device_id is None:
            raise ServiceError(404, "an attachment is gone; add it again", reason="unknown")
        try:
            staged = self.staged.claim(device_id, ids)
        except A.Refused as exc:
            raise self._refused(exc) from None
        try:
            reply = send(staged)
        except Exception:
            self.staged.restore(device_id, staged)
            raise
        reply["attachments"] = [{"id": a.id, "kind": a.kind, "name": a.name} for a in staged]
        logger.info("speakeasy: a typed message carried %d attachment(s)", len(staged))
        return reply

    def _start_job(self, text: str, staged: list[A.Attachment] | None = None, session_id: str | None = None,
                   context: str | None = None, link_to: str | None = None, choice: dict[str, Any] | None = None,
                   new_chat: bool = False) -> dict[str, Any]:
        """*link_to*: the chat session this job continues from a history page (store.history_links).
        *staged*: attachments claimed for this job (carried with its handoff). *choice*: the model /
        reasoning picked for it (models.parse_choice); a new chat (*new_chat*) keeps it as its own."""
        if choice is None and link_to:
            choice = self.store.model_choice("session:" + link_to)  # the chat's choice works its linked task
        worker = self.text_worker()
        future = asyncio.run_coroutine_threadsafe(
            M.start_job(worker, text, session_id=session_id, context=context, staged=staged or None,
                        choice=choice), worker.loop)
        try:
            got = future.result(M.START_WAIT_S + 10)
        except Exception as exc:
            logger.warning("speakeasy: a typed job could not start (%s)", type(exc).__name__)
            raise ServiceError(502, "I couldn't start that job. Try again.") from None
        with worker.interaction.lock:
            run = worker.interaction.runs.get(got["task_id"])
            key, run_id = (run.idem_key, run.run_id) if run else (None, None)
        run_id = (self.store.openable_run_id(key) if key else None) or run_id
        if key and link_to:
            self.store.link_history(key, link_to)
        if key and new_chat and choice and (choice.get("model") or choice.get("reasoning")):
            own = self.store.session_for(key)
            if own:  # "Use for this chat" on New chat: the chat it became keeps it
                self.store.set_model_choice("session:" + own, choice.get("model"), choice.get("reasoning"))
        task = self.store.work(idem_key=key, assistant_name=self.settings.get()["assistant_name"]) if key else None
        if task is not None:
            task["task_id"] = got["task_id"]
        if key:
            self.publish_all(key)
        logger.info("speakeasy: a typed job started with no call")
        # Its Hermes session id, known the moment it started (the history page it continues on): a New
        # chat page becomes that session at once, so its next message continues it instead of starting another.
        session = self.store.session_for(key) if key else None
        return {"ok": True, "run_id": run_id, "task_id": got["task_id"], "task": task, "session_id": session}

    def _claim_message(self, client_id: str) -> tuple[bool, threading.Event]:
        now = time.monotonic()
        with self._message_lock:
            for cid, (at, _, reply) in list(self._message_replies.items()):
                if reply is not None and now - at > self.MESSAGE_REPLAY_S:
                    del self._message_replies[cid]
            found = self._message_replies.get(client_id)
            if found is not None:
                return False, found[1]
            event = threading.Event()
            self._message_replies[client_id] = (now, event, None)
            return True, event

    def _message_target(self, run_id: str) -> tuple[str, dict[str, Any]]:
        """The task a message by run id goes to: (idem key, its row). A follow-up replaces its parent's
        row, so a message to the parent goes to the newest task of the same job."""
        key = self.store.key_for_run(run_id) or self.store.key_for_task(run_id)
        row = self.store.run_row(key) if key else None
        if row is None:
            raise ServiceError(404, "I couldn't find that task.")
        if row["dismissed"]:
            newer = self.store.newest_in_job(key)
            row = self.store.run_row(newer) if newer and newer != key else None
            if row is None:
                raise ServiceError(409, "That task was deleted, so it can't take messages.")
            key = row["idem_key"]
        if row["status"] in {"cancelled", "cancel_requested"}:
            raise ServiceError(409, "That task was cancelled, so it can't take messages. Start a new one instead.")
        if row["status"] == "rejected":
            raise ServiceError(409, "That task never started, so it can't take messages.")
        return key, row

    def _live_worker(self, key: str | None = None, interaction_id: str | None = None) -> Any:
        """The open call's worker: the one holding this task (key), else the call named, else any open
        call. None when no call is open."""
        with self.lock:
            live = list(self.interactions.values())
        def usable(i: Interaction) -> bool:
            w = i.worker
            return (w is not None and w.loop is not None and not w.loop.is_closed() and not i.call_closed
                    and not i.paused and i.successor is None)
        if key is not None:
            for interaction in live:
                with interaction.lock:
                    has = any(r.idem_key == key for r in interaction.runs.values())
                if has and usable(interaction):
                    return interaction.worker
            return None
        if interaction_id:
            found = self.interactions.get(interaction_id)
            if found is not None and usable(found.head()):
                return found.head().worker
        open_calls = [i for i in live if usable(i)]
        return open_calls[-1].worker if open_calls else None

    def text_worker(self) -> Any:
        """The voiceless worker for tasks whose call is gone (made on first use)."""
        with self._message_lock:
            if self._text_worker is None:
                self._text_worker = M.make_text_worker(self.rt)
            return self._text_worker

    def _task_message(self, run_id: str, text: str, interaction_id: str | None,
                      choice_task_id: str | None, staged: list[A.Attachment] | None = None) -> dict[str, Any]:
        if interaction_id:
            self.interaction(interaction_id)  # 404 for an unknown call
        if staged and choice_task_id and choice_task_id.startswith("choice_"):
            raise ServiceError(409, "Pictures and files can't go with a spoken choice. Pick, then send them.",
                               reason="not_now")
        if choice_task_id and choice_task_id.startswith("choice_"):
            # A spoken choice's card ("Something else..."): only the voice that asked it can take it.
            worker = self._live_worker(interaction_id=interaction_id)
            if worker is None or choice_task_id not in worker.choice_tasks:
                raise ServiceError(409, "That question has closed. Say your answer instead.")
            asyncio.run_coroutine_threadsafe(M.deliver(worker, choice_task_id, text, choice_task_id),
                                             worker.loop).result(M.START_WAIT_S + 5)
            return {"ok": True, "run_id": None, "delivered_as": "answer", "task": None}
        key, row = self._message_target(run_id)
        worker = self._live_worker(key)
        if worker is None:
            worker = self.text_worker()
        with worker.interaction.lock:
            task_id = next((t for t, r in worker.interaction.runs.items() if r.idem_key == key), None)
        if task_id is None or worker is self._text_worker:
            task_id = M.adopt_row(worker, row)
        future = asyncio.run_coroutine_threadsafe(M.deliver(worker, task_id, text, staged=staged or None), worker.loop)
        try:
            got = future.result(M.START_WAIT_S + 10)
        except LookupError:
            raise ServiceError(404, "I couldn't find that task.") from None
        except M.AttachmentsNotTaken:
            raise ServiceError(409, "That task is waiting on an approval. Answer it first, then send the "
                                    "pictures or files.", reason="not_now") from None
        except Exception as exc:
            logger.warning("speakeasy: a message to a task could not be delivered (%s)", type(exc).__name__)
            raise ServiceError(502, "I couldn't get that to the task. Try again.") from None
        with worker.interaction.lock:
            run = worker.interaction.runs.get(got["task_id"])
            new_key, new_run = (run.idem_key, run.run_id) if run else (key, row["run_id"])
        # The id the apps open it by (GET /voice/work/{id}, the task feed): a run id Hermes refuses a
        # moment later (the task then runs in a chat thread) or no run id yet stays openable (aliases).
        new_run = self.store.openable_run_id(new_key) or new_run
        name = self.settings.get()["assistant_name"]
        task = self.store.work(idem_key=new_key, assistant_name=name)
        if task is not None:
            task["task_id"] = got["task_id"]
        name_for = M.name_for_key(self.store, key)
        call = self._live_worker(interaction_id=interaction_id)
        if call is not None:
            call.interaction.feed.publish("transcript", {"kind": "text", "role": "user", "source": "typed",
                                                      "text": text, "task_id": got["task_id"],
                                                      "run_id": new_run, "at": time.time()})
            asyncio.run_coroutine_threadsafe(self._note_message(call, name_for, text), call.loop)
        self.publish_all(new_key)
        logger.info("speakeasy: a typed message went to a task as a %s", got["delivered_as"])
        return {"ok": True, "run_id": new_run, "delivered_as": got["delivered_as"], "task": task}

    # -- models: which model works a chat, how hard it thinks (models.py, docs/API.md "Models") ------

    def _fetch_model_options(self) -> dict[str, Any]:
        fetch = getattr(self.hermes, "model_options", None)
        if fetch is None:
            raise RuntimeError("this Hermes client can't list models")
        return fetch()

    def _hermes_reasoning(self) -> str | None:
        """The effort Hermes' own config runs at (agent.reasoning_effort), for "Default" labels."""
        # Hermes' own config reader: Hermes 0.23+ installs no longer ship PyYAML.
        data = S.hermes_config(self.home)
        value = (data.get("agent") or {}).get("reasoning_effort") if isinstance(data, dict) else None
        if value is False:
            return "none"
        value = str(value).strip().lower() if value not in (None, "") else None
        return value if value in MD.LEVELS else None

    def _warm_catalog(self) -> None:
        """Read the catalog in the background (never on a task's start path)."""
        if not self._catalog_warming.acquire(blocking=False):
            return

        def warm() -> None:
            try:
                self.catalog.raw()
            finally:
                self._catalog_warming.release()
        threading.Thread(target=warm, daemon=True, name="speakeasy-models").start()

    def _entries(self) -> dict[str, dict[str, Any]] | None:
        raw = self.catalog.peek()
        return self.catalog.entries(raw) if raw is not None else None

    def models(self, query: dict[str, str] | None = None) -> dict[str, Any]:
        """``GET /voice/models[?refresh=1]``: what the model chip offers (models.public)."""
        refresh = (query or {}).get("refresh") in {"1", "true"}
        try:
            served = self.store.last_default_served()
        except Exception:
            served = None
        return MD.public(self.catalog, self.settings.get(), refresh=refresh, served_default=served)

    def _body_choice(self, body: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return MD.parse_choice(body, lambda: self.catalog.entries(self.catalog.raw()))
        except MD.ChoiceError as exc:
            raise ServiceError(400, str(exc), reason="model") from None

    def _check_job_models(self, jobs: dict[str, Any]) -> None:
        picked = [jobs.get(k) for k in ("model", "fast_model", "smart_model") if isinstance(jobs.get(k), str) and jobs.get(k)]
        if not picked:
            return
        entries = self.catalog.entries(self.catalog.raw())
        if entries and any(m not in entries for m in picked):
            raise ServiceError(400, "That model isn't one Hermes offers. Pick one from the list.", reason="model")

    def _save_chat_choice(self, session_id: str, choice: dict[str, Any]) -> None:
        old = self.store.model_choice("session:" + session_id) or {}
        model = choice["model"] if "model" in choice else old.get("model")
        reasoning = choice["reasoning"] if "reasoning" in choice else old.get("reasoning")
        if "model" in choice and "reasoning" not in choice and model and reasoning:
            entries = self._entries()
            if entries and model in entries:  # a new model keeps the chat's level only if it takes it
                reasoning = MD.clamp(reasoning, entries[model]["reasoning_levels"])
        self.store.set_model_choice("session:" + session_id, model, reasoning)

    def set_session_model(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``PUT /voice/history/{session_id}/model {model?, reasoning?}``: Use for this chat ("default" or
        null clears that half). The next message to the chat, its follow-ups and its linked tasks use it."""
        if not isinstance(body, dict) or not body or not set(body) <= {"model", "reasoning"}:
            raise ServiceError(400, "Send model and/or reasoning.")
        if not H.valid_session_id(session_id):
            raise ServiceError(404, "I couldn't find that conversation.")
        meta = H.session_meta(self.rt.state_db, session_id)
        if meta is None:
            raise ServiceError(404, "I couldn't find that conversation.")
        choice = self._body_choice(body) or {}
        self._save_chat_choice(meta["id"], choice)
        name = self.settings.get()["assistant_name"]
        jobs = [w for w in (self.store.work(idem_key=k, assistant_name=name)
                            for k, _, _ in self.store.keys_for_session(meta["id"])[:3]) if w]
        logger.info("speakeasy: a chat's model choice changed")
        return {"ok": True, "session_id": meta["id"], **self._session_models(meta["id"], meta, jobs)}

    def _resolved(self, layers: list[tuple[str, dict[str, Any] | None]]) -> tuple[dict[str, Any] | None,
                                                                                    dict[str, dict[str, Any]] | None]:
        entries = self._entries()
        if entries is None:
            self._warm_catalog()
        return MD.resolve(layers, entries), entries

    def _default_layer(self) -> dict[str, Any]:
        jobs = self.settings.get().get("jobs") or {}
        return {"model": jobs.get("model") or None, "reasoning": jobs.get("reasoning") or None}

    def choose_model(self, task_id: str, idem: str, session_id: str | None, request: str | None
                     ) -> dict[str, Any] | None:
        """The overrides a run starts with (Runtime.choose_model): the voice phrase, the task's own pick,
        its chat's (or the chat a linked task continues), what its earlier turn ran on, the default.
        Records what was asked on the task; never reads the catalog on this path (it may be cold)."""
        settings = self.settings.get()
        raw = self.catalog.peek()
        entries = self.catalog.entries(raw) if raw is not None else None
        layers: list[tuple[str, dict[str, Any] | None]] = []
        kind, _ = MD.voice_preset(request)
        if kind:
            preset = MD.presets(settings, entries, self.catalog.hermes_default(raw) if raw else None)[kind]
            layers.append(("voice", {**preset, "preset": kind}))
        layers.append(("task", self.store.model_choice("task:" + task_id)))
        chain, key = [], idem
        for _ in range(6):
            key = self.store.parent_of(key) if key else None
            if not key or key in chain:
                break
            chain.append(key)
        sessions = [session_id] if session_id else []
        for k in [idem, *chain]:
            src = self.store.linked_source(k)
            if src and src not in sessions:
                sessions.append(src)
        for sid in sessions:
            layers.append(("chat", self.store.model_choice("session:" + sid)))
        for k in chain:  # a follow-up keeps what its task was asked to run on ("for this")
            earlier = self.store.run_model(k)
            if earlier and earlier.get("source") in {"voice", "task"}:
                layers.append((earlier["source"], {kk: earlier.get(kk) for kk in ("model", "reasoning", "preset")}))
        layers.append(("default", self._default_layer()))
        choice, entries = self._resolved(layers)
        record: dict[str, Any] = {"model": (choice or {}).get("model"), "reasoning": (choice or {}).get("reasoning"),
                                  "source": (choice or {}).get("source") or "hermes"}
        if choice and choice.get("preset"):
            record["preset"] = choice["preset"]
        try:
            self.store.set_run_model(idem, record)
        except Exception:
            logger.debug("speakeasy: run model not recorded", exc_info=True)
        if choice:
            logger.info("speakeasy: a task runs on a chosen model (%s)", record["source"])
        return MD.run_fields(choice) or None

    def describe_model(self, record: dict[str, Any] | None) -> dict[str, Any] | None:
        raw = self.catalog.peek()
        entries = self.catalog.entries(raw) if raw is not None else {}
        return MD.describe(record, entries, self.catalog.hermes_default(raw) if raw else None,
                           self.catalog.hermes_reasoning)

    def _session_models(self, sid: str, meta: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any]:
        """``model`` (what did the chat's latest work), ``model_choice`` (its Use-for-this-chat pick, or
        null) and ``next_model`` (what the next message runs on) for a session page."""
        chat = self.store.model_choice("session:" + sid)
        did = next((j["model"] for j in jobs if isinstance(j.get("model"), dict)), None)
        if did is None and isinstance(meta.get("model"), str) and meta["model"]:
            # No Speakeasy task ran here: the model Hermes recorded for the session (no provider, no level).
            entries = self._entries() or {}
            found = next((mid for mid, e in entries.items() if e["model"] == meta["model"]), None)
            did = {"id": found or meta["model"], "name": entries[found]["name"] if found else MD.friendly_name(meta["model"]),
                   "provider": entries[found]["provider"] if found else "",
                   "provider_name": entries[found]["provider_name"] if found else "",
                   "reasoning": None, "reasoning_label": None, "source": "hermes", "served": True}
        choice, _ = self._resolved([("chat", chat), ("default", self._default_layer())])
        upcoming = self.describe_model({"model": (choice or {}).get("model"),
                                        "reasoning": (choice or {}).get("reasoning"),
                                        "source": (choice or {}).get("source") or "hermes"})
        return {"model": did, "next_model": upcoming,
                "model_choice": {"model": chat.get("model"), "reasoning": chat.get("reasoning")} if chat else None}

    # -- history: every Hermes session, readable and writable by id (docs/API.md "History") ---------

    def _linked(self, session_id: str) -> H.Linked | None:
        keys = self.store.keys_for_session(session_id)
        if not keys:
            return None
        statuses = [status for _, _, status in keys[:8]]
        running = any(st in ACTIVE_RUN_STATES for st in statuses)
        needs_you = any(st in {"waiting_for_approval", "resolving_approval"} for st in statuses)
        work = self.store.work(idem_key=keys[0][0], assistant_name=self.settings.get()["assistant_name"]) or {}
        title = (work.get("job") or {}).get("name") or work.get("title")
        needs_you = needs_you or self._open_question([k for k, _, _ in keys] + self._continuations(session_id)) is not None
        run_ids = [rid for rid in (r or self.store.openable_run_id(k) for k, r, _ in keys[:5]) if rid]
        return H.Linked(keys=[k for k, _, _ in keys], name=title, running=running, needs_you=needs_you, run_ids=run_ids)

    def _continuations(self, session_id: str) -> list[str]:
        """Linked tasks that continued this chat from a history page (idem keys, oldest first), with every
        later task in their sessions (a 2nd+ message's follow-up runs in the linked task's session)."""
        keys = self.store.history_links(session_id)
        out: list[str] = []
        for key in keys:
            out.append(key)
            linked = self.store.session_for(key)
            if linked:
                out += [k for k, _, _ in reversed(self.store.keys_for_session(linked)) if k not in keys]
        return list(dict.fromkeys(out))

    def _linked_with_continuations(self, session_id: str) -> H.Linked | None:
        """For the list: a chat continued from Speakeasy shows its linked tasks' state (running,
        needs you) without becoming a Speakeasy row."""
        own = self._linked(session_id)
        if own is not None:
            return own
        keys = self._continuations(session_id)
        if not keys:
            return None
        statuses = [(self.store.run_row(k) or {}).get("status") for k in keys[-4:]]
        return H.Linked(keys=[], name=None, running=any(st in ACTIVE_RUN_STATES for st in statuses),
                        needs_you=any(st in {"waiting_for_approval", "resolving_approval"} for st in statuses)
                        or self._open_question(keys) is not None,
                        run_ids=[])

    # -- schedules: Hermes cron jobs (docs/API.md "Schedules") --------------------------------------
    def schedule_call(self, action: str, job_id: str | None = None, body: Any = None) -> dict[str, Any]:
        """One schedules action; Hermes' refusals come back as plain sentences with their status."""
        from .schedules import ScheduleError
        S = self.schedules
        calls = {
            "list": lambda: S.list(),
            "detail": lambda: S.detail(job_id or ""),
            "preview": lambda: S.preview(body or {}),
            "create": lambda: S.create(body or {}),
            "pause": lambda: S.pause(job_id or ""),
            "resume": lambda: S.resume(job_id or ""),
            "run": lambda: S.run_now(job_id or ""),
            "update": lambda: S.update(job_id or "", body or {}),
            "delete": lambda: S.delete(job_id or ""),
        }
        try:
            return calls[action]()
        except ScheduleError as exc:
            raise ServiceError(exc.status, exc.message) from None

    def board_call(self, slug: str | None = None) -> dict[str, Any]:
        """The Kanban boards (``slug`` None) or one board's columns; refusals as plain sentences."""
        from .kanban_board import BoardError
        try:
            return self.boards.list() if slug is None else self.boards.board(slug)
        except BoardError as exc:
            raise ServiceError(exc.status, exc.message) from None

    QUESTION_KEPT_S = 24 * 3600  # as long as the board keeps an unanswered decision (JobBoard.decisionKeptFor)
    QUESTION_GRACE_S = 5.0       # a message landing with the question itself isn't "moving on"
    QUESTION_SWEEP_S = 15.0

    def _open_question(self, keys: list[str], now: float | None = None) -> str | None:
        """The newest question a conversation still waits on (its idem key), else None."""
        open_keys = self._open_questions(keys, now)
        return open_keys[0] if open_keys else None

    def _open_questions(self, keys: list[str], now: float | None = None) -> list[str]:
        """The tasks whose question a conversation still waits on (idem keys, newest first). The single
        rule every surface uses (the list's Needs you, the session page's cards, the board): each
        thread of work (a Hermes session) can wait on its newest task's question only (a reply or a
        follow-up after it moved on); two threads' questions are independent. A question isn't open once
        answered, set aside, dismissed, superseded, asked and answered on another task, over a day old,
        or when the conversation went on without it (a newer message in its session, or in the chat a
        linked task continued). Closures found here are recorded (``moved_on``) and published, so the
        board, notifications and Live Activity clear too."""
        rows = self.store.question_rows(list(dict.fromkeys(keys)))
        if not rows:
            return []
        now = time.time() if now is None else now
        answered = {r["question"] for r in rows if r["set_aside"] and r["question"]}
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(r["session_id"] or r["key"], []).append(r)
        out: list[dict[str, Any]] = []
        closed: list[dict[str, Any]] = []
        for group in groups.values():
            newest = max(group, key=lambda r: (r["created"], r["rowid"]))
            for r in group:
                if not r["question"] or r["set_aside"] or r["closed"] or r["dismissed"]:
                    continue
                if r is not newest:
                    closed.append(r)  # a newer task of the same thread moved on
                    continue
                if (r["question"] in answered or r["status"] not in {"completed", "running", "working"}
                        or now - r["created"] >= self.QUESTION_KEPT_S):
                    continue
                if self._question_moved_on(r):
                    closed.append(r)
                    continue
                out.append(r)
        for r in closed:
            if r["status"] in {"completed", "running", "working"} and now - r["created"] < self.QUESTION_KEPT_S:
                self._close_question(r["key"], "moved_on")
        out.sort(key=lambda r: (r["created"], r["rowid"]), reverse=True)
        return [r["key"] for r in out]

    def _question_moved_on(self, row: dict[str, Any]) -> bool:
        """The conversation went on after this task asked: the user wrote in its own session again,
        or the chat it continued (a Discord/Telegram thread) has newer turns from either side."""
        after = row["settled"] + self.QUESTION_GRACE_S
        sid = row["session_id"]
        if sid and H.moved_on_after(self.rt.state_db, sid, after, replies=False):
            return True
        peers = [k for k, _, _ in self.store.keys_for_session(sid)] if sid else [row["key"]]
        sources = {s for s in (self.store.linked_source(k) for k in [row["key"], *peers]) if s}
        return any(H.moved_on_after(self.rt.state_db, s, after, replies=True) for s in sources)

    def _close_question(self, key: str, reason: str) -> bool:
        """Record a question closed unanswered and tell every client (decision_resolved clears the
        card, the board's Needs you, a delivered notification and the Live Activity)."""
        if not self.store.close_question(key, reason):
            return False
        row = self.store.run_row(key) or {}
        events = getattr(self.rt, "history_events", None)
        if events is not None:
            for sid in {row.get("session_id"), self.store.linked_source(key)} - {None}:
                events.publish("decision_resolved", {"run_id": row.get("run_id"), "session_id": sid})
        try:
            self._republish_all()
        except Exception:
            logger.debug("speakeasy: republish after a closed question failed", exc_info=True)
        return True

    def sweep_questions(self, force: bool = False) -> None:
        """Close every open question whose conversation moved on (throttled; read paths call it, so
        the board and the apps agree without a page being open)."""
        now = time.monotonic()
        last = self.__dict__.get("_question_sweep_at")
        if not force and last is not None and now - last < self.QUESTION_SWEEP_S:
            return
        self.__dict__["_question_sweep_at"] = now
        try:
            seen: set[str] = set()
            for key in self.store.open_question_candidates(time.time() - self.QUESTION_KEPT_S):
                if key in seen:
                    continue
                sid = self.store.session_for(key)
                group = [k for k, _, _ in self.store.keys_for_session(sid)] if sid else [key]
                seen.update(group)
                self._open_questions(group or [key])
        except Exception:
            logger.warning("speakeasy: question sweep failed", exc_info=True)

    def question_action(self, run_id: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /voice/tasks/{run_id}/question/{dismiss|undo}``: the ✕ on a decision card closes it on
        every device without telling the agent anything; ``undo`` (the toast's Undo) puts it back."""
        if not ID_RE.fullmatch(run_id) or not isinstance(body, dict) or body:
            raise ServiceError(400, "body must be empty")
        key = self.store.key_for_run(run_id)
        if key is None:
            raise ServiceError(404, "task not found")
        if action == "dismiss":
            if not self._close_question(key, "dismissed"):
                raise ServiceError(409, "nothing is waiting on you there")
            return {"run_id": run_id, "question": "dismissed"}
        if not self.store.reopen_question(key):
            raise ServiceError(409, "that question can't come back")
        row = self.store.run_row(key) or {}
        events = getattr(self.rt, "history_events", None)
        if events is not None:
            events.publish("history_changed", {"run_id": run_id, "session_id": row.get("session_id")})
        self._republish_all()
        return {"run_id": run_id, "question": "open"}

    # Cached history replies: the apps poll an open page every few seconds and the list every ~12 s,
    # and state.db can be many GB. A reply is reused while nothing it depends on changed (H.fingerprint
    # of state.db + this store's revision), and the list at most LIST_CACHE_S (titles/archive set in
    # other Hermes apps don't add message rows).
    LIST_CACHE_S = 20.0
    SESSION_CACHE_S = 120.0
    _HISTORY_CACHE_MAX = 64

    def _cached(self, key: tuple[Any, ...], stamp: Any, ttl: float, build: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        cache = self.__dict__.setdefault("_history_cache", {})
        lock = self.__dict__.setdefault("_history_cache_lock", threading.Lock())
        now = time.monotonic()
        if stamp is not None:
            with lock:
                hit = cache.get(key)
            if hit is not None and hit[0] == stamp and now - hit[1] < ttl:
                return copy.deepcopy(hit[2])
        reply = build()
        if stamp is not None:
            with lock:
                cache[key] = (stamp, now, copy.deepcopy(reply))
                while len(cache) > self._HISTORY_CACHE_MAX:
                    cache.pop(next(iter(cache)))
        return reply

    def _history_stamp(self) -> tuple[Any, ...]:
        """What the history watcher compares (one shared poll for every open stream, every
        HistoryEvents.POLL_S): Speakeasy's own store revision and state.db's newest message/session."""
        return (self.store.revision, H.fingerprint(self.rt.state_db))

    def forget_history_cache(self) -> None:
        with self.__dict__.setdefault("_history_cache_lock", threading.Lock()):
            self.__dict__.setdefault("_history_cache", {}).clear()

    def history(self, query: dict[str, str]) -> dict[str, Any]:
        """``GET /voice/history?cursor=&q=&source=&limit=``."""
        self.sweep_questions()
        args = tuple(sorted((k, str(v)) for k, v in (query or {}).items()))
        stamp = H.fingerprint(self.rt.state_db)
        return self._cached(("list", args), None if stamp is None else (stamp, self.store.revision),
                            self.LIST_CACHE_S, lambda: self._history(query))

    def _history(self, query: dict[str, str]) -> dict[str, Any]:
        try:
            limit = int(query.get("limit") or H.PAGE)
        except ValueError:
            raise ServiceError(400, "limit must be a number") from None
        q = (query.get("q") or "").strip()[:200]
        try:
            return H.list_sessions(self.rt.state_db, cursor=query.get("cursor") or None, q=q, source=query.get("source") or "",
                                   limit=limit, linked=self._linked_with_continuations,
                                   speakeasy_ids=self.store.speakeasy_sessions, folded=self.store.folded_sessions,
                                   marks=self.store.session_marks,
                                   archived=str(query.get("archived") or "") in {"1", "true"},
                                   automated=str(query.get("automated") or "") not in {"0", "false", "hide"},
                                   include_archived=(None if query.get("include_archived") in (None, "")
                                                     else str(query.get("include_archived")) in {"1", "true"}))
        except H.HistoryError as exc:
            raise ServiceError(exc.status, exc.message) from None

    def history_session(self, session_id: str, query: dict[str, str]) -> dict[str, Any]:
        """``GET /voice/history/{session_id}?before=``: the conversation, its Speakeasy tasks (with their
        approval/question cards and helpers) and messages sent from the app that are still on their way."""
        if not H.valid_session_id(session_id):
            raise ServiceError(404, "I couldn't find that conversation.")
        linked = [s for s in (self.store.session_for(k) for k in self._continuations(session_id)) if s]
        stamp = H.fingerprint(self.rt.state_db, [session_id, *linked])
        return self._cached(("session", session_id, str((query or {}).get("before") or "")),
                            None if stamp is None else (stamp, self.store.revision, self._approvals_stamp()),
                            self.SESSION_CACHE_S, lambda: self._history_session(session_id, query))

    def _approvals_stamp(self) -> tuple[Any, ...]:
        """Approvals live in the workers (an open call's or the voiceless one): which are waiting now."""
        with self.lock:
            workers = [i.worker for i in self.interactions.values() if i.worker is not None]
        if self._text_worker is not None:
            workers.append(self._text_worker)
        out = []
        for worker in workers:
            try:
                out += [(task_id, run.run_id) for task_id, run in worker.pending_approvals()]
            except Exception:
                return (time.monotonic(),)  # can't tell: never reuse
        return tuple(sorted(out))

    def _history_session(self, session_id: str, query: dict[str, str]) -> dict[str, Any]:
        meta = H.session_meta(self.rt.state_db, session_id)
        if meta is None:
            raise ServiceError(404, "I couldn't find that conversation.")
        sid = meta["id"]
        try:
            before = int(query["before"]) if query.get("before") else None
        except ValueError:
            raise ServiceError(400, "before must be a number") from None
        try:
            page = H.session_messages(self.rt.state_db, sid, before=before)
        except H.HistoryError as exc:
            raise ServiceError(exc.status, exc.message) from None
        link = self._linked(sid)
        own = self._speakeasy_owned(meta, link)
        name = self.settings.get()["assistant_name"]
        keys = list(link.keys) if link else []
        entries = page["entries"]
        continued = [] if own else self._continuations(sid)
        if continued:
            # The chat's own messages and what was said to it from Speakeasy (its linked tasks'
            # sessions), merged by time so the page reads as one conversation and ends with the newest
            # message. Each linked entry shows on the one page whose span holds it. Never posted to the chat.
            more: list[dict[str, Any]] = []
            seen: set[str] = set()
            for key in continued:
                linked_sid = self.store.session_for(key)
                if linked_sid and linked_sid not in seen:
                    seen.add(linked_sid)
                    try:
                        got = H.session_messages(self.rt.state_db, linked_sid)["entries"]
                    except H.HistoryError:
                        got = []
                    more += [{**e, "id": f"{linked_sid[-6:]}-{e['id']}"} for e in got]
            lower = H.message_time(self.rt.state_db, page["before"]) if page["has_more"] else None
            upper = H.message_time(self.rt.state_db, before) if before is not None else None
            if before is not None and upper is None:
                more = []  # can't place this page's span: linked entries stay on the newest page
            entries = H.merge_linked(entries, more, lower=lower, upper=upper)
        if continued and before is None:
            keys = list(reversed(continued)) + keys
        said = self._voice_said(sid)
        if said:
            entries = [{**e, "voice": True} if e.get("kind") == "user" and H.said_key(e.get("text") or "") in said
                       else e for e in entries]
        open_keys = set(self._open_questions(keys))
        jobs = []
        for key in keys[:8]:
            work = self.store.work(idem_key=key, assistant_name=name, activity_detail=True)
            if work is not None:
                if (work.get("result") or {}).get("question"):
                    # One source of truth for which question still waits (_open_question): every other
                    # task's question reads as handled, so an answered card never shows again and the
                    # same question asked by two tasks shows once. (set_aside: what older apps read.)
                    work["question_open"] = key in open_keys
                    if key not in open_keys:
                        work["set_aside"] = True
                jobs.append(work)
        running = any(j.get("status") in ACTIVE_RUN_STATES for j in jobs)
        # No Speakeasy task carrying the live line (a Kanban worker, a chat mid-turn): its own steps do.
        live = H.session_live(self.rt.state_db, sid, entries) if not running and before is None else None
        title = (meta.get("title") or "").strip() or (link.name if link else None)
        first_user = next((e["text"] for e in page["entries"] if e["kind"] == "user"), None)
        needs_you = (any(j.get("status") in {"waiting_for_approval", "resolving_approval"} for j in jobs)
                     or bool(open_keys))
        return {"session": {
            "id": sid, "title": H.tidy_title(title) if title else (
                H.tidy_title(H.preview_text(first_user, 60)) if first_user else "Untitled conversation"),
            "source": "speakeasy" if own else meta["source"],
            "source_label": H.source_label(meta["source"], meta.get("display_name"), meta.get("chat_type"), own),
            "state": "needs_you" if needs_you else ("running" if running or live else "idle"),
            **({"live": live} if live else {}),
            **({"automated": True} if meta["source"] in H.AUTOMATED_SOURCES and not own else {}),
            "send_via": "task" if own and link else ("session_job" if meta["source"] == "api_server" else "linked"),
            **({"archived": True} if meta.get("archived") else {}),
            **({"pinned": True} if (self.store.session_marks().get(sid) or {}).get("pinned") else {}),
            "entries": entries, "has_more": page["has_more"], "before": page["before"],
            "jobs": jobs, "approvals": self._session_approvals(keys), **self._session_models(sid, meta, jobs),
            "queued": self.queued_for(keys, sid), "steered": self.steered_for(keys, sid)}}

    @staticmethod
    def _speakeasy_owned(meta: dict[str, Any], link: H.Linked | None) -> bool:
        """Speakeasy's own session: an API-server session one of its tasks ran in. A chat thread a task
        continued stays the platform's (a message to it must never be posted back there)."""
        return meta["source"] == "api_server" and (link is not None or meta["id"].startswith(H.SPEAKEASY_PREFIX))

    def history_message(self, session_id: str, body: dict[str, Any], device_id: str | None = None) -> dict[str, Any]:
        """``POST /voice/history/{session_id}/message {text, client_id?, attachments?}``: to that session by
        id, never routed. A Speakeasy task's session: task_messages (steer, answer or follow-up run).
        Another API-server session: a new job in it. Anything else (a chat): a Speakeasy task linked to it,
        seeded with its recent messages; the reply shows in Speakeasy and nothing is posted to the chat.
        ``attachments``: ids this device staged, claimed all or nothing and given back if the send fails
        (exactly as on /voice/work[/{id}/message])."""
        if not isinstance(body, dict) or "text" not in body or not set(body) <= {
                "text", "client_id", "source", "attachments", "model", "reasoning", "answers", "mode"}:
            raise ServiceError(400, "Send the message as text.")
        mode = self._message_mode(body)
        choice = self._body_choice(body)
        text, client_id = body.get("text"), body.get("client_id")
        if (body.get("source") or "typed") not in {"typed", "voice_message"}:
            raise ServiceError(400, "source must be typed or voice_message.")
        ids = self._attachment_ids(body)
        if isinstance(text, str) and not text.strip() and ids:
            text = LOOK_TEXT
        if not isinstance(text, str) or not text.strip():
            raise ServiceError(400, "The message is empty.")
        text = text.strip()
        if len(text) > M.MAX_TEXT:
            raise ServiceError(400, f"The message is too long (at most {M.MAX_TEXT} characters).")
        if client_id is not None and (not isinstance(client_id, str) or not ID_RE.fullmatch(client_id)):
            raise ServiceError(400, "client_id must be an id.")
        if not H.valid_session_id(session_id):
            raise ServiceError(404, "I couldn't find that conversation.")
        meta = H.session_meta(self.rt.state_db, session_id)
        if meta is None:
            raise ServiceError(404, "I couldn't find that conversation.")
        if choice is not None:
            self._save_chat_choice(meta["id"], choice)  # a message carrying a choice: Use for this chat
        voice = body.get("source") == "voice_message"
        answers = body.get("answers")
        if answers is not None and (not isinstance(answers, str) or not ID_RE.fullmatch(answers)):
            raise ServiceError(400, "answers must be a task's run id.")

        if mode == "queue" and not answers:
            held = self._queued_reply(client_id) if client_id else None
            if held is not None:
                return held
            busy = self._session_running_key(meta)
            if busy is not None:
                if ids:
                    raise ServiceError(409, "Pictures and files can't wait for the task to finish. Send them "
                                            "now, or add them when it's done.", reason="not_now")
                held = self._queue(client_id, "session", meta["id"], meta["id"], busy, text)
                if voice:
                    self._note_voice_message(meta["id"], text)
                return held

        def deliver(staged: list[A.Attachment] | None) -> dict[str, Any]:
            got = (self._answer_question(meta, answers, text, staged) if answers
                   else self._history_message(meta, text, staged))
            if voice:
                self._note_voice_message(meta["id"], text)
            return got
        got = self._once(client_id, lambda: self._with_staged(device_id, ids, deliver))
        self._note_steered(client_id, got, meta["id"], text)
        return got

    _VOICE_SAID_KEEP = 60

    def _note_voice_message(self, session_id: str, text: str) -> None:
        """Remember that these words went to this session as a voice message, so its page shows them
        as one (waveform + transcript) after a reload. Only a short hash is kept, never the words."""
        key = "voice_said:" + session_id
        try:
            said = json.loads(self.store.get_meta(key) or "[]")
        except ValueError:
            said = []
        said = [h for h in said if isinstance(h, str)][-(self._VOICE_SAID_KEEP - 1):] + [H.said_key(text)]
        try:
            self.store.set_meta(key, json.dumps(said))
        except Exception:  # noqa: BLE001 - a missing mark only shows the message as typed
            logger.debug("speakeasy: couldn't note a voice message", exc_info=True)

    def _voice_said(self, session_id: str) -> set[str]:
        try:
            said = json.loads(self.store.get_meta("voice_said:" + session_id) or "[]")
        except (ValueError, TypeError):
            return set()
        return {h for h in said if isinstance(h, str)} if isinstance(said, list) else set()

    def _history_message(self, meta: dict[str, Any], text: str,
                         staged: list[A.Attachment] | None = None) -> dict[str, Any]:
        sid = meta["id"]
        link = self._linked(sid)
        if self._speakeasy_owned(meta, link) and link is not None:
            for run_id in link.run_ids:
                try:
                    got = self._task_message(run_id, text, None, None, staged)
                except ServiceError as exc:
                    if exc.status in {404, 409} and exc.reason != "not_now":
                        continue  # a deleted/cancelled task: the next one in this session takes it
                    raise
                return {"ok": True, "session_id": sid, "via": "task", "run_id": got.get("run_id"),
                        "delivered_as": got.get("delivered_as"), "task": got.get("task")}
        if meta["source"] == "api_server":
            got = self._start_job(text, staged, session_id=sid)
            return {"ok": True, "session_id": sid, "via": "session_job", "run_id": got.get("run_id"),
                    "delivered_as": "follow_up", "task": got.get("task")}
        return self._continue_linked(meta, text, staged)

    def _answer_question(self, meta: dict[str, Any], run_id: str, text: str,
                         staged: list[A.Attachment] | None) -> dict[str, Any]:
        """A reply written for one decision card ("Reply…"): delivered to the task that asked, as its
        answer (a follow-up in that task's session, or a steer when its run still waits), even when
        another task of the conversation is newer. A task this conversation doesn't hold: refused."""
        sid = meta["id"]
        key = self.store.key_for_run(run_id)
        mine = set(self.store.history_links(sid)) | set(self._continuations(sid)) | {
            k for k, _, _ in self.store.keys_for_session(sid)}
        if key is None or key not in mine:
            raise ServiceError(404, "I couldn't find that question in this conversation.")
        got = self._task_message(run_id, text, None, None, staged)
        return {"ok": True, "session_id": sid, "via": "answer", "run_id": got.get("run_id"),
                "delivered_as": got.get("delivered_as") or "answer", "task": got.get("task")}

    def _send_via(self, session_id: str) -> str:
        """How a message to this session is sent (the session page's ``send_via``)."""
        meta = H.session_meta(self.rt.state_db, session_id)
        if meta is None:
            return "linked"
        link = self._linked(meta["id"])
        if self._speakeasy_owned(meta, link) and link is not None:
            return "task"
        return "session_job" if meta["source"] == "api_server" else "linked"

    def _session_running_key(self, meta: dict[str, Any]) -> str | None:
        """The task a message to this session would wait on: its newest Speakeasy task still running."""
        sid = meta["id"]
        link = self._linked(sid)
        keys = list(reversed(self._continuations(sid))) + (list(link.keys) if link else [])
        for key in keys:
            row = self.store.run_row(key)
            if row is not None and not row["dismissed"] and row["status"] in QUEUE_BEHIND:
                return key
        return None

    def _continue_linked(self, meta: dict[str, Any], text: str,
                         staged: list[A.Attachment] | None = None) -> dict[str, Any]:
        """A chat Speakeasy can't write into (its reply would go back to the chat): the newest linked
        task for it takes the message (steer, answer or follow-up in its own session); the first
        message starts one, seeded with the chat's recent messages."""
        sid = meta["id"]
        for key in reversed(self._continuations(sid)):
            run_id = self.store.openable_run_id(key) or (self.store.run_row(key) or {}).get("run_id")
            if not run_id:
                continue
            try:
                got = self._task_message(run_id, text, None, None, staged)
            except ServiceError as exc:
                if exc.status in {404, 409} and exc.reason != "not_now":
                    continue
                raise
            return {"ok": True, "session_id": sid, "via": "linked", "run_id": got.get("run_id"),
                    "delivered_as": got.get("delivered_as"), "task": got.get("task")}
        # No linked task can take it any more (cleared from the board, cancelled, never got a run id):
        # it still continues the SAME linked session, never a new one (that was a second row per reply).
        linked_sid = next((s for s in (self.store.session_for(k) for k in reversed(self._continuations(sid))) if s), None)
        if linked_sid:
            got = self._start_job(text, staged, session_id=linked_sid, link_to=sid)
            logger.info("speakeasy: a message to a %s session continued its linked session", meta["source"])
            return {"ok": True, "session_id": sid, "via": "linked", "run_id": got.get("run_id"),
                    "delivered_as": "follow_up", "task": got.get("task")}
        label = H.source_label(meta["source"], meta.get("display_name"), meta.get("chat_type"), False)
        title = (meta.get("title") or "").strip() or "this conversation"
        context = H.linked_context(self.rt.state_db, sid, title, label, text)
        got = self._start_job(text, staged, context=context, link_to=sid)
        logger.info("speakeasy: a message to a %s session started a linked task", meta["source"])
        return {"ok": True, "session_id": sid, "via": "linked", "run_id": got.get("run_id"),
                "delivered_as": "linked", "task": got.get("task")}

    # -- session actions from the panel's context menu (rename, pin, archive, read) -------------------
    def history_action(self, session_id: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /voice/history/{id}/{rename|pin|archive|read}``. Rename and archive go through Hermes' own
        ``PATCH /api/sessions/{id}`` (so Hermes' other apps show the same title and hide the same
        conversations); pin and read are Speakeasy's marks, shared by every device. Every change is
        published on ``/voice/history/events`` at once."""
        if not isinstance(body, dict):
            raise ServiceError(400, "Send a JSON object.")
        if not H.valid_session_id(session_id) or H.session_meta(self.rt.state_db, session_id) is None:
            raise ServiceError(404, "I couldn't find that conversation.")
        allowed = {"rename": {"title"}, "pin": {"pinned"}, "archive": {"archived"}, "read": {"at"}}[action]
        if not set(body) <= allowed:
            raise ServiceError(400, f"{action} takes only: {', '.join(sorted(allowed))}")
        event: dict[str, Any] = {"session_id": session_id}
        if action == "rename":
            title = body.get("title")
            if not isinstance(title, str) or not title.strip() or len(title) > 100:
                raise ServiceError(400, "A title is 1 to 100 characters.")
            title = " ".join(title.split())
            self._hermes_patch(session_id, {"title": title})
            event.update(title=title)
            kind = "session_updated"
        elif action == "archive":
            archived = body.get("archived", True)
            if not isinstance(archived, bool):
                raise ServiceError(400, "archived is true or false.")
            self._hermes_patch(session_id, {"archived": archived})
            event.update(archived=archived)
            kind = "session_archived"
        elif action == "pin":
            pinned = body.get("pinned", True)
            if not isinstance(pinned, bool):
                raise ServiceError(400, "pinned is true or false.")
            self.store.set_pinned(session_id, pinned)
            event.update(pinned=pinned)
            kind = "session_updated"
        else:
            at = body.get("at")
            if at is not None and not isinstance(at, (int, float)):
                raise ServiceError(400, "at is a time in seconds.")
            self.store.set_read(session_id, None if at is None else min(float(at), time.time()))
            event.update(unread=False)
            kind = "session_updated"
        self.forget_history_cache()
        self.history_events.publish(kind, event)
        return {"ok": True, **event}

    def _hermes_patch(self, session_id: str, fields: dict[str, Any]) -> None:
        patch = getattr(self.rt.hermes, "patch_session", None)
        if patch is None:
            raise ServiceError(501, "This Hermes can't rename or archive conversations.")
        try:
            patch(session_id, fields)
        except HermesError as exc:
            if exc.status == 400 and "title" in fields:
                raise ServiceError(400, "Another conversation already has that title.") from None
            if exc.status == 404:
                raise ServiceError(404, "Hermes doesn't have that conversation.") from None
            raise ServiceError(502, "Hermes didn't take that change. Try again.") from None

    def _session_approvals(self, keys: list[str]) -> list[dict[str, Any]]:
        """Approvals the session's Speakeasy tasks wait on, from whichever worker holds them (an open
        call's, or the voiceless one): {run_id, task_id, description, choices}."""
        with self.lock:
            workers = [i.worker for i in self.interactions.values() if i.worker is not None]
        if self._text_worker is not None:
            workers.append(self._text_worker)
        wanted, out = set(keys), []
        for worker in workers:
            for task_id, run in worker.pending_approvals():
                if run.idem_key in wanted:
                    approval = run.approval or {}
                    out.append({"run_id": run.run_id, "task_id": task_id,
                                "description": H.A.clip(H.A.redact(approval.get("description") or ""), 400),
                                "choices": [c for c in (approval.get("choices") or ["once", "deny"])
                                            if c in {"once", "session", "deny"}]})
        return out

    def history_approval(self, session_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /voice/history/{session_id}/approval {run_id, choice}``: answer the approval one of the
        session's Speakeasy tasks waits on, with or without a call. Never "always"."""
        run_id = body.get("run_id") if isinstance(body, dict) else None
        choice = body.get("choice") if isinstance(body, dict) else None
        if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id) or choice not in {"once", "session", "deny"}:
            raise ServiceError(400, "run_id and a choice of once, session or deny are required")
        key = self.store.key_for_run(run_id)
        if key is None or key not in {k for k, _, _ in self.store.keys_for_session(session_id)}:
            raise ServiceError(404, "I couldn't find that approval.")
        with self.lock:
            workers = [i.worker for i in self.interactions.values() if i.worker is not None and i.worker.loop]
        if self._text_worker is not None:
            workers.append(self._text_worker)
        for worker in workers:
            for task_id, run in worker.pending_approvals():
                if run.idem_key == key:
                    if choice not in ((run.approval or {}).get("choices") or ["once", "deny"]):
                        raise ServiceError(400, "That choice wasn't offered.")
                    done = asyncio.run_coroutine_threadsafe(worker.resolve_approval(task_id, choice),
                                                            worker.loop).result(30)
                    if done is None:
                        raise ServiceError(502, "Hermes didn't take that answer. Try again.")
                    self.publish_all(key)
                    return {"ok": True, "choice": done}
        raise ServiceError(409, "That approval has closed.")

    @staticmethod
    async def _note_message(worker: Any, task_name: str, text: str) -> None:
        M.note_message(worker, task_name, text)

    # -- AI thread titles ------------------------------------------------------------------------

    def pending_thread_titles(self) -> dict[str, Any]:
        """Past tasks still under their stand-in label, for the app's on-device titler."""
        return {"tasks": self.store.stand_in_titles()}

    def rename_thread(self, body: dict[str, Any]) -> dict[str, Any]:
        """Set one task's title from the app's on-device Apple Intelligence titler. Same store
        path as the cloud titler's answer: the title also refines the name of the job it named."""
        task_id, title = body.get("task_id"), body.get("title")
        if (not set(body) <= {"task_id", "title"} or not isinstance(task_id, str)
                or not 1 <= len(task_id) <= 128 or not isinstance(title, str)):
            raise ServiceError(400, "task_id and title are required")
        title = " ".join(title.split())
        if not 2 <= len(title) <= 90:
            raise ServiceError(400, "title needs 2-90 characters")
        key = self.store.key_for_task(task_id)
        if key is None:
            raise ServiceError(404, "task not found")
        if not self.store.rename_stand_in(key, title):
            # The cloud titler (or a chat thread) named it after the app asked: that name wins.
            raise ServiceError(409, "task already has a title")
        return {"task_id": task_id, "title": title, "renamed": True}

    # -- look at this: screen sharing, pictures and files ------------------------------------------
    @staticmethod
    def _refused(exc: A.Refused) -> ServiceError:
        return ServiceError(exc.status, exc.message, reason=exc.reason)

    def _call_open(self, interaction: Interaction) -> None:
        """409 once the call has ended or paused: things go to the call that resumes it."""
        with interaction.lock:
            over = interaction.call_closed or interaction.paused or interaction.successor is not None
        if over or interaction.attachments.ended:
            raise ServiceError(409, "the call has ended", reason="ended")

    def check_upload(self, interaction_id: str, headers: Any, length: int) -> dict[str, Any]:
        """``POST /voice/interactions/{id}/attachments``, before its body is read: headers, size cap,
        the call, and whether it still takes this upload. The upload itself is ``accept_upload``."""
        kind = (headers.get("X-Speakeasy-Kind") or "").strip().lower()
        if kind not in A.KINDS:
            raise ServiceError(400, "X-Speakeasy-Kind must be screen, picture or file")
        mime = A.media_type(headers.get("Content-Type"))
        if kind in A.IMAGE_KINDS and mime not in A.IMAGE_TYPES:
            raise ServiceError(415, "images must be image/jpeg or image/png", reason="unsupported")
        capture_id = (headers.get("X-Speakeasy-Capture-Id") or "").strip() or None
        if (kind == "screen") != (capture_id is not None) or capture_id and not ID_RE.fullmatch(capture_id):
            raise ServiceError(400, "a screen capture needs its X-Speakeasy-Capture-Id; pictures and files take none")
        raw_name = headers.get("X-Speakeasy-Filename")
        if kind == "file" and not (raw_name or "").strip():
            raise ServiceError(400, "files need X-Speakeasy-Filename")
        if length <= 0:
            raise ServiceError(400, "empty upload")
        if length > (A.MAX_FILE_BYTES if kind == "file" else A.MAX_IMAGE_BYTES):
            raise ServiceError(413, "too large to share", reason="too_large")
        interaction = self.interaction(interaction_id)
        try:
            if kind == "screen":
                interaction.attachments.check_capture(capture_id or "")  # 410 when closed, the call's end included
            else:
                self._call_open(interaction)
                interaction.attachments.check_room(kind, length)
        except A.Refused as exc:
            raise self._refused(exc) from None
        return {"interaction": interaction, "kind": kind, "mime": mime, "size": length, "capture_id": capture_id,
                "name": A.safe_filename(raw_name) if (raw_name or "").strip() else None,
                "app": A.safe_app(headers.get("X-Speakeasy-App"))}

    def accept_upload(self, upload: dict[str, Any], data: bytes) -> dict[str, Any]:
        """Keep a checked upload: images must be what they say (415); a picture waits in memory, a
        file is saved under the shared folder now, a screen capture goes to its open request (410
        once closed; the same request again returns the same attachment). Never logs bytes or names."""
        interaction, kind, mime = upload["interaction"], upload["kind"], upload["mime"]
        if len(data) != upload["size"]:
            raise ServiceError(400, "upload ended early")
        if kind in A.IMAGE_KINDS and _sniff(data[:16]) != mime:
            raise ServiceError(415, f"the bytes aren't {mime}", reason="unsupported")
        if kind == "picture":
            data = A.strip_metadata(data, mime)  # a picture from a phone may carry where it was taken
        shared = interaction.attachments
        try:
            if kind == "screen":
                attachment = shared.deliver_capture(upload["capture_id"], data=data, mime=mime, app=upload["app"])
            elif kind == "picture":
                self._call_open(interaction)
                attachment = shared.add("picture", mime=mime, size=len(data), data=data, name=upload["name"],
                                        app=upload["app"])
            else:
                self._call_open(interaction)
                with self.shared.lock:  # saved and held in one step: never deleted in between
                    path = self.shared.save(data, upload["name"] or "file")
                    try:
                        attachment = shared.add("file", mime=mime, size=len(data), path=str(path), name=path.name)
                    except A.Refused:
                        self.discard_shared_files([str(path)])
                        raise
        except A.Refused as exc:
            raise self._refused(exc) from None
        except OSError as exc:
            logger.warning("speakeasy: could not save a shared file (%s)", type(exc).__name__)
            raise ServiceError(500, "could not save the file") from None
        logger.info("speakeasy: %s shared on %s (%d KB)", kind, interaction.interaction_id[:10],
                    max(1, len(data) // 1024))
        publish_state(self.store, interaction, self.settings.get()["assistant_name"])
        if kind != "screen":
            self._sync_notes(interaction)
        return {"id": attachment.id, "kind": attachment.kind}

    def remove_attachment(self, interaction_id: str, attachment_id: str) -> dict[str, Any]:
        """The ✕ on a pending thumbnail or file chip. 409 once it went with a request."""
        interaction = self.interaction(interaction_id)
        try:
            removed = interaction.attachments.remove(attachment_id)
        except A.Refused as exc:
            raise self._refused(exc) from None
        if removed.path:
            self.discard_shared_files([removed.path])
        publish_state(self.store, interaction, self.settings.get()["assistant_name"])
        self._sync_notes(interaction)
        return {"id": attachment_id, "removed": True}

    # -- pictures and files staged on a device (iPhone/iPad composer) ------------------------------
    # Hermes' transcription (resolved on first use; tests set their own).
    transcriber: Any = None

    def transcribe_voice(self, suffix: str, data: bytes) -> dict[str, Any]:
        """``POST /voice/transcribe`` after its checked body arrived: the clip's text, never logged."""
        if self.transcriber is None:
            self.transcriber = T.hermes_transcriber()
        return T.transcribe(data, suffix, self.transcriber, self.dir / "voice-memos")

    def check_stage(self, device_id: str, headers: Any, length: int) -> dict[str, Any]:
        """``POST /voice/attachments``, before its body is read: a picture (JPEG or PNG, the phone
        re-encodes) or a file (PDF or text), within its cap and this device's room."""
        self.expire_staged()
        kind = (headers.get("X-Speakeasy-Kind") or "").strip().lower()
        if kind not in {"picture", "file"}:
            raise ServiceError(400, "X-Speakeasy-Kind must be picture or file")
        mime = A.media_type(headers.get("Content-Type"))
        if kind == "picture" and mime not in A.IMAGE_TYPES:
            raise ServiceError(415, "pictures must be image/jpeg or image/png", reason="unsupported")
        if kind == "file" and mime not in A.STAGED_FILE_TYPES:
            raise ServiceError(415, "files must be PDF or plain text", reason="unsupported")
        raw_name = headers.get("X-Speakeasy-Filename")
        if kind == "file" and not (raw_name or "").strip():
            raise ServiceError(400, "files need X-Speakeasy-Filename")
        if length <= 0:
            raise ServiceError(400, "empty upload")
        if length > (A.MAX_FILE_BYTES if kind == "file" else A.MAX_IMAGE_BYTES):
            raise ServiceError(413, "too large to send", reason="too_large")
        try:
            self.staged.check_room(device_id, kind, length)
        except A.Refused as exc:
            raise self._refused(exc) from None
        return {"device_id": device_id, "kind": kind, "mime": mime, "size": length,
                "name": A.safe_filename(raw_name) if (raw_name or "").strip() else None}

    def accept_stage(self, upload: dict[str, Any], data: bytes) -> dict[str, Any]:
        """Keep a checked staged upload: the bytes must be what they say (415). A picture loses its
        metadata (location included) and waits in memory; a file is saved under the shared folder.
        Never logs bytes or names."""
        device_id, kind, mime = upload["device_id"], upload["kind"], upload["mime"]
        if len(data) != upload["size"]:
            raise ServiceError(400, "upload ended early")
        if kind == "picture":
            if _sniff(data[:16]) != mime:
                raise ServiceError(415, f"the bytes aren't {mime}", reason="unsupported")
            data = A.strip_metadata(data, mime)
        elif not A.staged_file_ok(mime, data):
            raise ServiceError(415, "that file isn't the PDF or text it says it is", reason="unsupported")
        try:
            if kind == "picture":
                attachment = self.staged.add(device_id, "picture", mime=mime, size=len(data), data=data,
                                             name=upload["name"])
            else:
                with self.shared.lock:  # saved and held in one step: never deleted in between
                    path = self.shared.save(data, upload["name"] or "file")
                    try:
                        attachment = self.staged.add(device_id, "file", mime=mime, size=len(data),
                                                     path=str(path), name=path.name)
                    except A.Refused:
                        self.discard_shared_files([str(path)])
                        raise
        except A.Refused as exc:
            raise self._refused(exc) from None
        except OSError as exc:
            logger.warning("speakeasy: could not save a staged file (%s)", type(exc).__name__)
            raise ServiceError(500, "could not save the file") from None
        logger.info("speakeasy: %s staged from a device (%d KB)", kind, max(1, len(data) // 1024))
        return {"id": attachment.id, "kind": attachment.kind, "name": attachment.name, "size": attachment.size,
                "expires_in": A.STAGED_TTL_S}

    def remove_staged(self, device_id: str, attachment_id: str) -> dict[str, Any]:
        """The x on a composer chip: forget it (404 once sent, removed or expired)."""
        try:
            removed = self.staged.remove(device_id, attachment_id)
        except A.Refused as exc:
            raise self._refused(exc) from None
        if removed.path:
            self.discard_shared_files([removed.path])
        return {"id": attachment_id, "removed": True}

    def expire_staged(self, now: float | None = None) -> int:
        """Drop what a device staged and never sent (STAGED_TTL_S), saved copies included."""
        gone = self.staged.expire(now)
        paths = [a.path for a in gone if a.path]
        if paths:
            self.discard_shared_files(paths)
        return len(gone)

    def set_screen(self, interaction_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """The panel's screen button: exactly ``{on, seq}``. An older seq than the last one applied is
        ignored (the reply carries the state in force). Turning it off closes capture requests."""
        on, seq = (body.get("on"), body.get("seq")) if isinstance(body, dict) else (None, None)
        if (set(body or {}) != {"on", "seq"} or not isinstance(on, bool) or not isinstance(seq, int)
                or isinstance(seq, bool) or not 0 <= seq < 2 ** 53):
            raise ServiceError(400, "body must be exactly {\"on\": true|false, \"seq\": <count>}")
        interaction = self.interaction(interaction_id)
        self._call_open(interaction)
        try:
            changed = interaction.attachments.set_screen(on, seq)
        except A.Refused as exc:
            raise self._refused(exc) from None
        if changed:
            logger.info("speakeasy: screen sharing %s on %s", "on" if on else "off", interaction_id[:10])
            publish_state(self.store, interaction, self.settings.get()["assistant_name"])
            self._sync_notes(interaction)
            if on and interaction.worker is not None:
                interaction.worker.sharing_turned_on()  # a request waiting for the screen goes now
        return interaction.attachments.screen_state()

    def capture_status(self, interaction_id: str, capture_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """The app on one capture request: ``capturing`` (200 only while it is open, else 410: don't
        capture), ``uploading`` (the wait is extended) or ``failed`` with its reason (closes it)."""
        status, reason = (body.get("status"), body.get("reason")) if isinstance(body, dict) else (None, None)
        if (not isinstance(body, dict) or not set(body) <= {"status", "reason"}
                or status not in {"capturing", "uploading", "failed"}):
            raise ServiceError(400, "body must be {\"status\": \"capturing\"|\"uploading\"|\"failed\", \"reason\"?}")
        if status == "failed" and reason not in A.FAILURE_REASONS:
            raise ServiceError(400, "failed needs a reason: " + ", ".join(sorted(A.FAILURE_REASONS)))
        if status != "failed" and "reason" in body:
            raise ServiceError(400, "reason is only for failed")
        interaction = self.interaction(interaction_id)
        if not interaction.attachments.capture_status(capture_id, status, reason):
            raise ServiceError(410, "that capture request is closed", reason="closed")
        if status == "failed":
            logger.info("speakeasy: screen capture failed on %s (%s)", interaction_id[:10], reason)
            publish_state(self.store, interaction, self.settings.get()["assistant_name"])
        return {"capture_id": capture_id, "status": status}

    def _sync_notes(self, interaction: Interaction) -> None:
        """Tell the voice, silently, when sharing or what's waiting on the panel changed. A call still
        connecting gets the notes once it connects (one waiting thread per call, bounded)."""
        worker = interaction.worker
        if worker is not None and worker.loop is not None and worker.call_connected():
            self._speak_notes(interaction, worker)
        elif interaction.attachments.claim_note_waiter():
            threading.Thread(target=self._notes_when_connected, args=(interaction,), daemon=True,
                             name="speakeasy-share-notes").start()

    def _notes_when_connected(self, interaction: Interaction) -> None:
        deadline = time.monotonic() + EARLY_CONNECT_WAIT_S
        try:
            while time.monotonic() < deadline and not interaction.attachments.ended:
                worker = interaction.worker
                if worker is not None and worker.loop is not None and worker.call_connected():
                    break
                time.sleep(0.1)
        finally:
            interaction.attachments.release_note_waiter()
        worker = interaction.worker
        if worker is not None and worker.loop is not None and worker.call_connected():
            self._speak_notes(interaction, worker)

    @staticmethod
    def _speak_notes(interaction: Interaction, worker: Any) -> None:
        for kind, value in interaction.attachments.take_notes():
            worker.speak_from_thread("session.thinking.append", P.sharing_note(kind, value))

    def _live_shared_paths(self) -> set[str]:
        with self.lock:
            live = list(self.interactions.values())
        paths: set[str] = set()
        for interaction in live:
            paths |= interaction.attachments.file_paths()
        return paths | self.staged.file_paths()

    def discard_shared_files(self, paths: list[str]) -> int:
        """Delete Speakeasy's copies nothing uses any more: no task records them and no call holds them."""
        wanted = {str(p) for p in paths if p}
        if not wanted:
            return 0
        with self.shared.lock:
            busy = self.store.shared_paths_in_use(wanted) | self._live_shared_paths()
            return self.shared.delete(sorted(wanted - busy))

    def forget_task_files(self, keys: list[str]) -> int:
        """Cleared tasks: delete the copies of what was shared with them (unless still used elsewhere)."""
        try:
            removed = self.discard_shared_files(self.store.forget_shared_files(keys))
        except Exception as exc:
            logger.warning("speakeasy: could not delete a cleared task's shared files: %s", type(exc).__name__)
            return 0
        if removed:
            logger.info("speakeasy: deleted %d shared file(s) of cleared tasks", removed)
        return removed

    def prune_shared(self, now: float | None = None) -> int:
        """Speakeasy's copies of shared files last saved over a week ago go (idle loop: at startup,
        then hourly), with their task records."""
        before = (time.time() if now is None else now) - A.SHARED_KEEP_S
        try:
            removed = self.shared.prune(before)
            self.store.prune_shared_files(before)
        except Exception as exc:
            logger.warning("speakeasy: shared folder prune failed: %s", type(exc).__name__)
            return 0
        if removed:
            logger.info("speakeasy: pruned %d shared file(s) older than a week", removed)
        return removed

    # -- card images ---------------------------------------------------------------------------
    def _card(self, run_id: str, index: int) -> dict[str, Any]:
        if not ID_RE.fullmatch(run_id) or not 1 <= index <= MAX_CARDS:
            raise ServiceError(404, "card not found")
        cards = self.store.result_cards(run_id)
        if index > len(cards) or not isinstance(cards[index - 1], dict):
            raise ServiceError(404, "card not found")
        return cards[index - 1]

    def card_image(self, run_id: str, index: int) -> tuple[bytes, str]:
        card = self._card(run_id, index)
        path = card.get("path") if card.get("kind") == "image" else None
        try:
            if isinstance(path, str):
                return read_local_image(path, self.image_roots())
            url = card.get("image_url")
            if not isinstance(url, str):
                raise ServiceError(404, "image not found")
            return fetch_image(url)
        except (ImageRejected, OSError, TimeoutError):
            raise ServiceError(404, "image unavailable") from None

    def shared_image(self, run_id: str, number: int) -> tuple[bytes, str]:
        """An image the user shared with a task (its card's ``shared[number - 1]``): only Speakeasy's
        own copy in the shared folder, re-vetted on every read like any local image."""
        path = self.store.shared_image(run_id, number) if ID_RE.fullmatch(run_id) else None
        if path is None or not self.shared.owns(path):
            raise ServiceError(404, "image not found")
        try:
            return read_local_image(path, self.image_roots())
        except (ImageRejected, OSError):
            raise ServiceError(404, "image unavailable") from None

    def live_image(self, run_id: str) -> tuple[bytes, str]:
        """The latest image a task produced or is looking at, re-vetted on every read."""
        key = self.store.key_for_run(run_id) if ID_RE.fullmatch(run_id) else None
        live = self.store.live_image(key) if key else None
        if live is None:
            raise ServiceError(404, "no image yet")
        try:
            if live["kind"] == "path":
                return read_local_image(live["ref"], self.image_roots())
            return fetch_image(live["ref"])
        except (ImageRejected, OSError, TimeoutError):
            raise ServiceError(404, "image unavailable") from None

    # -- email drafts ------------------------------------------------------------------------
    def decide_draft(self, draft_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """approve | deny | revise one draft, bound to the exact hash the user saw."""
        action, digest, instructions = body.get("action"), body.get("sha256"), body.get("instructions")
        if (not set(body) <= {"action", "sha256", "instructions"} or action not in {"approve", "deny", "revise"}
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ServiceError(400, "action (approve|deny|revise) and sha256 are required")
        if action == "revise":
            if not isinstance(instructions, str) or not instructions.strip() or len(instructions) > 2000:
                raise ServiceError(400, "revise needs instructions (up to 2000 characters)")
        elif instructions is not None:
            raise ServiceError(400, "instructions are only for revise")
        if not re.fullmatch(r"ed_[0-9a-f]{24}", draft_id or ""):
            raise ServiceError(404, "draft not found")
        draft = self.store.draft(draft_id)
        if draft is None:
            raise ServiceError(404, "draft not found")
        if not secrets.compare_digest(draft["sha256"], digest):
            raise ServiceError(409, "draft changed; review the current draft")
        target = {"approve": "approved", "deny": "denied", "revise": "revising"}[action]
        # Idempotent: repeating a decision already taken returns the current state, never re-sends.
        already = {"approve": {"approved", "sent", "failed"}, "deny": {"denied"}, "revise": {"revising"}}[action]
        if draft["status"] in already:
            return self._draft_view(draft)
        if not self.store.transition_draft(draft_id, {"pending"}, target):
            current = self.store.draft(draft_id) or draft
            if current["status"] in already:
                return self._draft_view(current)
            raise ServiceError(409, f"draft is {current['status']}")
        names = P.Names.from_settings(self.settings.get())
        public = {k: v for k, v in draft.items() if not k.startswith("_") and k not in
                  {"draft_id", "sha256", "status", "created_at", "updated_at", "error"}}
        message = {"approve": lambda: P.draft_approved_message(names, canonical_json(public)),
                   "deny": lambda: P.draft_denied_message(names),
                   "revise": lambda: P.draft_revise_message(names, instructions or "")}[action]()
        key, session_id = draft["_key"], draft["_session_id"]
        self.store.progress(key, "milestone", {"approve": "Email approved; sending", "deny": "Email draft denied",
                                               "revise": "Revising the email draft"}[action])
        idem = f"speakeasy_draft_{draft_id}_{action}"
        try:
            run_id = self.hermes.start_run(message, idem, session_id)
        except HermesError as exc:
            if action == "approve":
                # Nothing reached Hermes: mark failed rather than pretend it was sent.
                self.store.transition_draft(draft_id, {"approved"}, "failed", error=exc.message[:200])
            elif action == "revise":
                self.store.transition_draft(draft_id, {"revising"}, "pending", error=exc.message[:200])
            self.publish_all(key)
            raise ServiceError(502, "Hermes did not accept the decision") from None
        self.store.transition_draft(draft_id, {target}, target, action_run_id=run_id)
        self.publish_all(key)
        interaction = self.interaction_for_key(key)
        if interaction is not None and interaction.worker is not None:
            interaction.worker.speak_from_thread("session.thinking.append", P.draft_outcome_note(action))
        threading.Thread(target=self._follow_draft_run, args=(draft_id, action, run_id, key), daemon=True,
                         name="speakeasy-draft").start()
        return self._draft_view(self.store.draft(draft_id) or draft)

    def _follow_draft_run(self, draft_id: str, action: str, run_id: str, key: str) -> None:
        final: dict[str, Any] = {}

        def on_event(event: dict[str, Any]) -> None:
            kind = event.get("event") or ""
            if kind.startswith("run.") and kind[4:] in TERMINAL:
                final["status"] = kind[4:]
                if isinstance(event.get("output"), str):
                    final["output"] = event["output"]
        try:
            if not self.hermes.events(run_id, on_event) or "output" not in final:
                result = self.hermes.get_run(run_id)
                final.setdefault("status", result.get("status"))
                if isinstance(result.get("output"), str):
                    final.setdefault("output", result["output"])
        except Exception as exc:
            final.setdefault("status", "unknown")
            final.setdefault("error", type(exc).__name__)
        self.draft_run_finished(draft_id, action, final.get("status") or "unknown", final.get("output") or "")

    def draft_run_finished(self, draft_id: str, action: str, status: str, output: str) -> None:
        draft = self.store.draft(draft_id)
        if draft is None:
            return
        key = draft["_key"]
        result = split_result(output, self.image_roots()) if output else None
        spoken = (result or {}).get("spoken") or ""
        if action == "approve":
            failed = status != "completed" or bool(re.search(
                r"(?i)\b(?:failed to send|couldn't send|could not send|not sent|unable to send|send failed)\b", spoken))
            self.store.transition_draft(draft_id, {"approved"}, "failed" if failed else "sent",
                                        error=(notice_text(spoken, 200) or f"status {status}") if failed else None)
            self.store.progress(key, "milestone", "Email failed to send" if failed else "Email sent")
        elif action == "revise":
            _, drafts = extract_email_drafts(output or "")
            if status == "completed" and drafts:
                self.store.add_draft(key, draft["_session_id"], drafts[-1])  # supersedes the revising one
                self.store.progress(key, "milestone", "Revised email draft ready")
            else:
                self.store.transition_draft(draft_id, {"revising"}, "pending", error="No revised draft came back")
        self.publish_all(key)
        interaction = self.interaction_for_key(key)
        if interaction is not None and interaction.worker is not None and spoken:
            interaction.worker.speak_from_thread("session.commentary.append", spoken)

    @staticmethod
    def _draft_view(draft: dict[str, Any]) -> dict[str, Any]:
        return {"draft": {k: v for k, v in draft.items() if not k.startswith("_")}}

    # -- settings / brief / status / onboarding ---------------------------------------------------
    def get_settings(self) -> dict[str, Any]:
        return {"settings": self.settings.get()}

    def routing_choices(self) -> dict[str, Any]:
        from . import routing_choice
        return routing_choice.choices(self.home)

    def routing_models(self, provider: str) -> dict[str, Any]:
        from . import routing_choice
        return {"provider": provider, "models": routing_choice.provider_models(provider)}

    def choose_routing(self, body: dict[str, Any]) -> dict[str, Any]:
        from . import hermes_config, routing_choice
        body = body if isinstance(body, dict) else {}
        provider, model, thinking = body.get("provider"), body.get("model", ""), body.get("thinking")
        if not isinstance(provider, str) or not isinstance(model, str) or \
                not (thinking is None or isinstance(thinking, bool)):
            raise ServiceError(400, "provider (string), model (string) and thinking (true/false) expected")
        try:
            return routing_choice.choose(self.home, provider, model, thinking)
        except routing_choice.RoutingChoiceError as exc:
            raise ServiceError(400, str(exc)) from None
        except hermes_config.ConfigWriteRefused as exc:
            raise ServiceError(409, f"Hermes config left unchanged: {exc}") from None

    def patch_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        if isinstance(body, dict) and isinstance(body.get("jobs"), dict):
            self._check_job_models(body["jobs"])
        if isinstance(body, dict) and ("server" in body or "address_drop" in body):
            # Where the server is published is decided on the computer, never from a paired device.
            raise ServiceError(403, "server addresses are set on the computer that runs Hermes "
                                    "(hermes voice setup / hermes voice config)")
        try:
            saved = self.settings.patch(body)
        except SettingsError as exc:
            raise ServiceError(400, str(exc)) from None
        if isinstance(body, dict) and "delivery" in body:
            self.sync_thread_routes(saved)
        return {"settings": saved}

    def thread_channels(self, s: dict[str, Any]) -> list[dict[str, Any]]:
        """Every destination that wants a thread per task: opted-in channels, plus the default."""
        wanted = [c for c in s["delivery"]["channels"] if c.get("new_thread")]
        target = s["delivery"]["target"]
        if s["delivery"]["new_thread"] and target != "none" and not any(c["target"] == target for c in wanted):
            wanted.append({"target": target, "label": D.target_label(target), "new_thread": True})
        return wanted

    def sync_thread_routes(self, s: dict[str, Any]) -> None:
        """Keep Hermes' webhook routes in step with the channels that open threads (hot-reloaded)."""
        try:
            sync_routes(self.home, self.thread_channels(s),
                        supported=capability(self.home, D.threads_supported())["supported"])
        except OSError as exc:
            logger.warning("speakeasy: could not update thread routes (%s)", type(exc).__name__)

    def get_brief(self) -> dict[str, Any]:
        return self.brief.get()

    def put_brief(self, body: dict[str, Any]) -> dict[str, Any]:
        if set(body) != {"brief"}:
            raise ServiceError(400, "body must be {\"brief\": \"...\"}")
        try:
            return self.brief.put(body["brief"])
        except BriefInvalid as exc:
            raise ServiceError(422, str(exc)) from None

    def get_tune(self) -> dict[str, Any]:
        return self.tune.get()

    def start_tune(self) -> dict[str, Any]:
        try:
            return self.tune.start()
        except BriefInvalid as exc:
            raise ServiceError(409, str(exc)) from None

    def apply_tune(self, body: dict[str, Any]) -> dict[str, Any]:
        accept = body.get("accept") if isinstance(body, dict) else None
        if not isinstance(accept, list) or not all(isinstance(a, str) for a in accept):
            raise ServiceError(400, "accept must be a list of edit ids")
        try:
            return self.tune.apply(accept)
        except BriefInvalid as exc:
            raise ServiceError(409, str(exc)) from None

    def dismiss_tune(self) -> dict[str, Any]:
        return self.tune.dismiss()

    def rewrite_brief(self) -> dict[str, Any]:
        try:
            return {**self.brief.rewrite(force=True), "brief": self.brief.text()}
        except BriefInvalid as exc:
            raise ServiceError(409, str(exc)) from None

    def _codex_status_fn(self) -> Callable[[Path | None], tuple[bool, str]]:
        if self._codex_login is not None:
            return self._codex_login
        from .codex_transport import login_status
        return login_status

    def codex_discovery(self) -> dict[str, Any]:
        """The Codex copy voice should use and how it is signed in (``kind``: chatgpt, api_key or
        none). The configured/newest copy is checked first; only when it has no ChatGPT sign-in is
        every other copy on the machine checked, and the first one signed in with ChatGPT is saved
        as ``voice.codex_path`` so calls use it. The user is asked to sign in only when none is."""
        from .codex_login import discover, sign_in_kind
        import functools
        configured = self.settings.get()["voice"]["codex_path"]
        status_fn = self._codex_status_fn()
        version_fn = functools.lru_cache(maxsize=32)(S.codex_version)
        candidates = self._codex_candidates() if self._codex_candidates else S.codex_candidates()
        default = find_codex(configured) if configured else find_codex("", version_fn=version_fn,
                                                                         candidates=candidates)
        too_old = lambda b: S.codex_too_old(b, version_fn=version_fn)  # noqa: E731
        if default is not None and not too_old(default):  # the usual case: one status check
            first = discover(default, status_fn, [], too_old)
            if first["kind"] == "chatgpt":
                return {**first, "message": status_fn(default)[1]}
        found = discover(default, status_fn, candidates, too_old)
        binary = found["binary"]
        if binary is not None:
            found["message"] = status_fn(binary)[1]
        elif found["too_old"]:
            found["message"] = ("This Codex is too old for voice. Tap Sign in to ChatGPT to update it, "
                                "or run hermes voice setup on the Hermes computer.")
        else:
            ok, found["message"] = status_fn(None)
            if ok:  # only an injected status check (tests) says a missing Codex is signed in
                found["kind"] = sign_in_kind(ok, found["message"])
        if found["kind"] == "chatgpt" and found["changed"]:
            self._remember_codex(binary)
        return found

    def _remember_codex(self, binary: Path) -> None:
        """Save a signed-in Codex copy as voice.codex_path (only when the default pick differs)."""
        s = self.settings.get()
        if s["voice"]["codex_path"] == str(binary):
            return
        if not s["voice"]["codex_path"] and find_codex("") == binary:
            return
        logger.info("speakeasy: using the Codex copy that is signed in to ChatGPT: %s", binary)
        self.settings.patch({"voice": {"codex_path": str(binary)}})

    def _codex_signed_in(self, binary: Path) -> None:
        from .codex_transport import _LOGIN_CACHE, mark_signed_in
        _LOGIN_CACHE.pop(str(binary), None)
        mark_signed_in(binary)
        self._remember_codex(binary)

    def codex_state(self) -> tuple[bool, bool, str]:
        found = self.codex_discovery()
        return found["binary"] is not None, found["kind"] != "none", found["message"]

    # -- Sign in to ChatGPT from the app (codex_login.py) --------------------------------------------
    def codex_login_status(self) -> dict[str, Any]:
        with self._install_lock:
            install = dict(self._install)
        if install["state"] == "installing":
            return {"state": "installing", "message": install["message"], "sign_in": "none", "codex_found": False}
        out = self.codex_signin.status()
        if install["state"] == "failed" and out["state"] in {"idle", "cancelled"}:
            out = {"state": "failed", "message": install["message"]}
        if out["state"] == "pending":
            return {**out, "sign_in": "none", "codex_found": True}
        found = self.codex_discovery()
        if out["state"] in {"idle", "cancelled"} and found["kind"] == "chatgpt":
            out = {"state": "signed_in", "message": "Signed in to ChatGPT."}
        return {**out, "sign_in": found["kind"], "codex_found": found["installed"],
                **({"codex_message": found["message"]} if out["state"] == "idle" else {})}

    def codex_login_start(self, body: dict[str, Any]) -> dict[str, Any]:
        """Start Codex's device-code sign-in, unless a working sign-in already exists anywhere."""
        if not isinstance(body, dict) or not set(body) <= {"switch_from_api_key"}:
            raise ServiceError(400, "body may contain only: switch_from_api_key")
        switch = body.get("switch_from_api_key", False)
        if not isinstance(switch, bool):
            raise ServiceError(400, "switch_from_api_key must be true or false")
        with self._install_lock:
            if self._install["state"] == "installing":
                return self.codex_login_status()
            self._install = {"state": "", "message": "", "switch": switch}
        pending = self.codex_signin.status()
        if pending["state"] == "pending":
            return {**pending, "sign_in": "none", "codex_found": True}
        found = self.codex_discovery()
        if found["kind"] == "chatgpt":
            return {"state": "signed_in", "message": "Signed in to ChatGPT.", "sign_in": "chatgpt", "codex_found": True}
        if found["kind"] == "api_key" and not switch:
            raise ServiceError(409, "Codex is signed in with an OpenAI API key, not a ChatGPT account. "
                                    "Switch to ChatGPT to use your plan.", reason="api_key_signed_in")
        if found["binary"] is None:
            self._start_codex_install(switch)
            return self.codex_login_status()
        return self._begin_device_login(found["binary"])

    def _begin_device_login(self, binary: Path) -> dict[str, Any]:
        from .codex_login import CodexLoginError
        try:
            out = self.codex_signin.start(binary)
        except CodexLoginError as exc:
            raise ServiceError(502, str(exc), reason="codex_login_failed") from None
        return {**out, "sign_in": "none", "codex_found": True}

    def _start_codex_install(self, switch: bool) -> None:
        with self._install_lock:
            self._install = {"state": "installing", "message": "Installing Codex on the Hermes computer…",
                             "switch": switch}

        def run() -> None:
            try:
                ok = bool(self._codex_install())
            except Exception:  # noqa: BLE001
                logger.exception("speakeasy: installing Codex failed")
                ok = False
            from .codex_transport import _LOGIN_CACHE
            _LOGIN_CACHE.clear()
            if ok:
                found = self.codex_discovery()
                with self._install_lock:
                    cancelled = self._install["state"] != "installing"
                    self._install = {"state": "", "message": "", "switch": switch}
                if found["binary"] is not None and found["kind"] == "none" and not cancelled:
                    try:
                        self._begin_device_login(found["binary"])
                    except ServiceError as exc:
                        with self._install_lock:
                            self._install = {"state": "failed", "message": exc.message, "switch": switch}
                    return
                if found["binary"] is not None:
                    return
            with self._install_lock:
                self._install = {"state": "failed", "switch": switch, "message":
                                 "Couldn't install Codex on the Hermes computer. Run hermes voice setup there, "
                                 "or use Grok or an API key instead."}

        threading.Thread(target=run, daemon=True, name="speakeasy-codex-install").start()

    def codex_login_cancel(self) -> dict[str, Any]:
        with self._install_lock:
            if self._install["state"] in {"installing", "failed"}:
                self._install = {"state": "", "message": "", "switch": False}
        return self.codex_signin.cancel()

    @staticmethod
    def updated_underneath() -> bool:
        """True when the plugin was reinstalled into a running gateway: Hermes re-imports the new
        files but this running server keeps the old ones, and mixing them breaks every handoff
        (seen live: new call code calling the old Runtime.route). Only a restart fixes it."""
        import sys
        live = sys.modules.get(SidebandWorker.__module__)
        return live is not None and getattr(live, "SidebandWorker", SidebandWorker) is not SidebandWorker

    def status(self) -> dict[str, Any]:
        s = self.settings.get()
        discovery = self.codex_discovery()
        found, codex_kind, codex_message = discovery["binary"] is not None, discovery["kind"], discovery["message"]
        signed_in = codex_kind != "none"
        api_key_set = bool(self.openai_key())
        provider = s["voice"]["provider"]
        voice_ready = signed_in if provider == "codex" else api_key_set
        grok_signed_in, grok_key_set, grok_message = self.grok_state()
        grok_ready = grok_beta_on(s) and (grok_signed_in or grok_key_set)
        if provider == "grok":
            voice_ready = grok_ready
        return {
            "assistant_name": s["assistant_name"], "user_name": s["user_name"], "provider": provider,
            "voice": default_voice(s), "voice_ready": voice_ready and not self.updated_underneath(),
            "restart_needed": self.updated_underneath(),
            "codex_found": found, "codex_signed_in": signed_in, "codex_message": codex_message,
            # chatgpt | api_key | none: the app offers "Sign in to ChatGPT" only for none (and a
            # switch for api_key), never while a working ChatGPT sign-in exists (codex_login).
            "codex_sign_in": codex_kind,
            "api_key_set": api_key_set, "brief_state": self.brief.status()["state"],
            "hermes_api_ok": self.hermes.health(), "hermes_api_key_set": bool(self.hermes_key()),
            "delivery_target": s["delivery"]["target"], **self.thread_status(),
            "continuity_enabled": s["continuity"]["enabled"], "devices": len(self.devices.devices()),
            "home_control_enabled": s["home_control"]["enabled"],
            "routing_model": routing_model(), "routing_hint": ROUTING_HINT, "routing_explainer": ROUTING_EXPLAINER,
            "routing_choice": self.routing_choices(),
            "advertised_url": s["server"]["advertised_url"], "tailscale_name": s["server"]["tailscale_name"],
            "tunnel": s["server"]["tunnel"] or None,
            "attachments": self.attachment_status(),
            # Grok voice (beta): calls relay audio through the plugin (transport type "relay").
            "grok_beta": grok_beta_on(s), "grok_signed_in": grok_signed_in, "grok_api_key_set": grok_key_set,
            "grok_ready": grok_ready, "grok_message": grok_message,
            # Listening mode: POST /voice/sessions takes "room" (the app sends it only when this is true),
            # also with resume_from (listening mode during a call pauses it, then resumes it with "room").
            "room_listening": True,
            "room_on_resume": True,
            "focus": True,
            # POST /voice/interactions/{id}/input-text takes a typed turn on a live call (the app shows
            # its composer only when this is true; older plugins 404 the route).
            "input_text": True,
            "version": __version__,
            # What this server supports from the UX contract; a missing flag means an older server.
            "capabilities": dict(CAPABILITIES),
        }

    def setup_check(self) -> dict[str, Any]:
        """The Setup check screen's rows (setup_check.py), from the same facts as status()."""
        from . import setup_check
        return setup_check.check(self.status())

    def setup_check_fix(self, row_id: str) -> dict[str, Any]:
        """Do one row's plugin-side fix (e.g. turn Grok voice on), then answer the fresh rows."""
        from . import setup_check
        if not setup_check.run_fix(row_id, self.status(),
                                   turn_on_grok=lambda: self.settings.patch({"beta": {"grok_voice": True}})):
            raise ServiceError(409, "That item has nothing Speakeasy can fix by itself right now.")
        return setup_check.check(self.status())

    def attachment_status(self) -> dict[str, Any]:
        """Whether Hermes reads images (``images``: the input path exists; ``vision``: native,
        described, none or unknown) and the attachment limits. Reads the cached check only."""
        support = getattr(self.hermes, "image_support", None) or {}
        return {"images": bool(support.get("images")), "vision": support.get("vision") or "unknown",
                "max_attachments": A.MAX_ATTACHMENTS, "max_image_bytes": A.MAX_IMAGE_BYTES,
                "max_request_image_bytes": A.MAX_REQUEST_IMAGE_BYTES, "max_file_bytes": A.MAX_FILE_BYTES,
                "file_types": sorted(A.STAGED_FILE_TYPES)}

    # -- home control ---------------------------------------------------------------------------------
    def get_home(self) -> dict[str, Any]:
        """Settings › Home: on/off, whether Home Assistant is found, and every device it could use."""
        return {**self.home_control.overview(), "explainer": HOME_EXPLAINER}

    def put_home(self, body: dict[str, Any]) -> dict[str, Any]:
        """Turn it on/off and/or set exactly which devices it may use. Turning it on needs Home
        Assistant to answer; the first time, the default picks (lights, thermostats, fans) are saved."""
        if not isinstance(body, dict) or not body or not set(body) <= {"enabled", "entities"}:
            raise ServiceError(400, "body may contain only: enabled, entities")
        patch: dict[str, Any] = {}
        if "entities" in body:
            patch["entities"] = body["entities"]
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise ServiceError(400, "enabled must be true or false")
            if body["enabled"]:
                found = self.home_control.detect()
                if not found["available"]:
                    raise ServiceError(409, found["reason"])
                if "entities" not in body and self.settings.get()["home_control"]["entities"] is None:
                    from .home_control import default_selection
                    patch["entities"] = default_selection(self.home_control.snapshot().states)
            patch["enabled"] = body["enabled"]
        self.patch_settings({"home_control": patch})
        return self.get_home()

    def thread_status(self) -> dict[str, Any]:
        cap = capability(self.home, D.threads_supported())
        return {"threads_supported": cap["supported"], "threads_reason": cap["reason"],
                "thread_platforms": sorted(THREAD_PLATFORMS)}

    def destinations(self) -> dict[str, Any]:
        return {**D.destinations(self.home), "current": self.settings.get()["delivery"]["target"],
                **self.thread_status()}

    def suggest_channels(self) -> dict[str, Any]:
        """One read-only Hermes run proposes channels; validated, never saved (the app asks)."""
        entries = D.flat_chats(D.destinations(self.home))
        threads_ok = self.thread_status()["threads_supported"]
        try:
            picked = suggest(self._suggest_run, entries, threads_ok)
        except SuggestError as exc:
            raise ServiceError(502, str(exc)) from None
        return {"suggestions": [dict(c, new_thread=c["new_thread"] and c["target"].split(":", 1)[0] in THREAD_PLATFORMS)
                                for c in picked]}

    def onboarding(self) -> dict[str, Any]:
        s = self.settings.get()
        _, signed_in, message = self.codex_state()
        voice_ready = signed_in if s["voice"]["provider"] == "codex" else bool(self.openai_key())
        if s["voice"]["provider"] == "grok":
            grok_signed_in, grok_key_set, message = self.grok_state()
            voice_ready = grok_beta_on(s) and (grok_signed_in or grok_key_set)
        brief_state = self.brief.status()["state"]
        steps = {
            "paired": bool(self.devices.devices()),
            "codex_signed_in": signed_in,
            "voice_ready": voice_ready,
            "names_set": s["onboarding"]["names_set"],
            "delivery_set": s["onboarding"]["delivery_set"],
            "brief_ready": brief_state in {"ready", "edited"},
            "home_offered": s["onboarding"].get("home_offered", False),
        }
        required = ("paired", "voice_ready", "names_set", "delivery_set")
        return {"steps": steps, "complete": all(steps[k] for k in required), "brief_state": brief_state,
                "codex_message": message, "assistant_name": s["assistant_name"], "user_name": s["user_name"],
                "delivery_target": s["delivery"]["target"], "continuity_enabled": s["continuity"]["enabled"],
                "home_control_enabled": s["home_control"]["enabled"],
                "advertised_url": s["server"]["advertised_url"], "tailscale_name": s["server"]["tailscale_name"]}

    def save_onboarding(self, body: dict[str, Any]) -> dict[str, Any]:
        allowed = {"assistant_name", "user_name", "delivery_target", "write_brief", "continuity_enabled", "home_enabled"}
        if not isinstance(body, dict) or not set(body) <= allowed or not body:
            raise ServiceError(400, f"body may contain only: {', '.join(sorted(allowed))}")
        patch: dict[str, Any] = {}
        onboarding: dict[str, bool] = {}
        if "assistant_name" in body or "user_name" in body:
            for key in ("assistant_name", "user_name"):
                if key in body:
                    patch[key] = body[key]
            onboarding["names_set"] = True
        if "delivery_target" in body:
            if not valid_delivery_target(body["delivery_target"]):
                raise ServiceError(400, "delivery_target must be none or a Hermes send target like telegram or discord:<chat_id>")
            patch["delivery"] = {"target": body["delivery_target"]}
            onboarding["delivery_set"] = True
        if "continuity_enabled" in body:
            if not isinstance(body["continuity_enabled"], bool):
                raise ServiceError(400, "continuity_enabled must be true or false")
            patch["continuity"] = {"enabled": body["continuity_enabled"]}
        if "home_enabled" in body:
            # The setup step's answer: yes turns it on with the default device picks, no leaves it off.
            if not isinstance(body["home_enabled"], bool):
                raise ServiceError(400, "home_enabled must be true or false")
            onboarding["home_offered"] = True
        if onboarding:
            patch["onboarding"] = onboarding
        if patch:
            self.patch_settings(patch)
        if body.get("home_enabled") is not None:
            self.put_home({"enabled": body["home_enabled"]})
        if body.get("write_brief") is True:
            try:
                self.brief.rewrite(force=False)
            except BriefInvalid:
                pass
        return {**self.onboarding(), "settings": self.settings.get()}


def transport_answer(transport: Any) -> str:
    return getattr(transport, "_answer", "") or ""
