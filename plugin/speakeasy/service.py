"""The Speakeasy server core: calls, tasks, approvals, email drafts, settings, brief, status.

Transport-agnostic (the HTTP handler in ``server.py`` is a thin layer over this). Constructed once
inside the gateway with the profile's HERMES_HOME resolved up front: HTTP threads do not carry the
gateway's profile scope.
"""
from __future__ import annotations

import asyncio
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
from . import delivery as D
from .brief import BriefInvalid, BriefManager
from .calls import (ACTIVE_RUN_STATES, MAX_TASKS, ServiceError, Interaction, Notices, Runtime, SidebandWorker,
                    interaction_tasks, publish_state)
from .cards import ImageRejected, default_image_roots, fetch_image, read_local_image
from .devices import DeviceStore
from .emails import canonical_json, extract_email_drafts
from .hermes_api import HermesAPI, HermesError

RESTART_NEEDED = ("Speakeasy was updated while Hermes was running. Restart the Hermes gateway "
                  "(hermes gateway restart) to finish the update.")
from .prompt import builder as P
from .router import home_plan_call as router_home_plan_call, routing_model
from .threads import THREAD_PLATFORMS
from .settings import (OPENAI_KEY_NAME, Settings, SettingsError, default_voice, find_codex, hermes_api_base,
                       hermes_secret, valid_delivery_target)
from .store import StateStore
from .suggest import SuggestError, suggest
from .threads import ThreadRunner, capability, sync_routes
from .text import ID_RE, MAX_CARDS, MAX_ROOM_CHARS, TERMINAL, notice_text, safe_room_text, split_result

logger = logging.getLogger(__name__)

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
PAUSE_NOTICE_AFTER_S = 15 * 60
IDLE_CHECK_S = 15
EARLY_CONNECT_WAIT_S = 8  # words heard while connecting wait this long for the call's event channel



_SHORTCUT_RE = re.compile(r"^[^\x00-\x1f<>{}]{1,24}$")


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

class VoiceService:
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
                 tune_run: Callable[[str, str], tuple[str, str]] | None = None):
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
        self._suggest_run = suggest_run or self._brief_run
        self.brief = BriefManager(self.home, brief_run or self._brief_run, self.settings.get,
                                  error_fn=lambda: getattr(self.hermes, "last_error", "") or "")
        from .tune import CallLog, TuneManager, merge_calls
        self.call_log = CallLog(self.dir)
        self.rt.call_log = self.call_log
        self.tune = TuneManager(self.dir, tune_run or brief_run or self._tune_run, self.brief,
                                lambda: merge_calls(self.call_log.recent(), self.store.calls_from_tasks()),
                                names=self._tune_names)
        self.interactions: dict[str, Interaction] = {}
        self.lock = threading.Lock()
        self._codex_factory = codex_factory
        self._openai_negotiate = openai_negotiate
        self._openai_worker = openai_worker
        self._codex_login = codex_login
        self._stop = threading.Event()
        if start_threads:
            threading.Thread(target=self._idle_loop, daemon=True, name="speakeasy-idle").start()
            self.brief.start_scheduler(ready=self.hermes.health)

    # -- secrets (read per use, never stored or returned) ------------------------------------
    def hermes_key(self) -> str:
        return hermes_secret(self.home, "API_SERVER_KEY")

    def openai_key(self) -> str:
        return hermes_secret(self.home, OPENAI_KEY_NAME)

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
        return False

    def close(self) -> None:
        self._stop.set()
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

    # -- auth ---------------------------------------------------------------------------------
    def authenticate(self, headers: Any) -> str:
        supplied = headers.get("Authorization", "")
        token = supplied[7:].strip() if supplied.startswith("Bearer ") else None
        device_id = self.devices.authenticate(token)
        if device_id is None:
            raise ServiceError(401, "unauthorized")
        return device_id

    def pair(self, body: dict[str, Any]) -> dict[str, Any]:
        code, name = body.get("code"), body.get("device_name", "Mac")
        if not isinstance(code, str) or not re.fullmatch(r"\s*\d{6}\s*", code) or not isinstance(name, str):
            raise ServiceError(400, "code (6 digits) and device_name are required")
        result = self.devices.redeem(code, name)
        if result is None:
            raise ServiceError(403, "pairing code is wrong or expired")
        device_id, token = result
        return {"device_id": device_id, "token": token, "assistant_name": self.settings.get()["assistant_name"]}

    # -- voice sessions -------------------------------------------------------------------------
    def instructions(self, *, away: list[dict[str, Any]], resume: str,
                     tour: dict[str, str] | None = None, room: str = "") -> str:
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
                                         if tour is not None else "", room=room)

    def create_session(self, body: dict[str, Any], request_id: str, device_id: str = "") -> dict[str, Any]:
        if self.updated_underneath():
            raise ServiceError(503, RESTART_NEEDED)
        resume_from = body.get("resume_from")
        if (not set(body) <= {"sdp", "resume_from", "tour", "room"} or not isinstance(body.get("sdp"), str)
                or resume_from is not None and not (isinstance(resume_from, str) and ID_RE.fullmatch(resume_from))):
            raise ServiceError(400, "body must contain only an SDP offer, an optional resume_from, tour and room")
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
        if not sdp.strip() or len(sdp.encode()) > MAX_SDP or not sdp.startswith("v=0"):
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
        if room:
            logger.info("speakeasy: call has the room transcript from listening mode (%d words)", len(room.split()))
        instructions = self.instructions(away=away, resume=resume, tour=tour, room=room)
        seed = P.resume_input(history)
        provider = s["voice"]["provider"]
        voice = default_voice(s)
        transport = None
        try:
            if provider == "codex":
                transport = self._start_codex(sdp, instructions, seed, voice, s)
                live_id, answer = transport.thread_id, transport_answer(transport)
            else:
                key = self.openai_key()
                if not key:
                    raise ServiceError(409, f"voice.provider is openai but {OPENAI_KEY_NAME} is not set in the Hermes .env")
                from .openai_live import negotiate, session_payload
                result = (self._openai_negotiate or negotiate)(key, session_payload(instructions, voice, sdp, seed))
                live_id = (result.get("session") or {}).get("id")
                answer = (result.get("transport") or {}).get("sdp")
        except ServiceError:
            self.store.fail_session(request_id)
            raise
        except Exception as exc:
            if transport is not None:
                transport.stop()
            self.store.fail_session(request_id)
            from .codex_transport import CodexStartError
            if isinstance(exc, CodexStartError):
                logger.warning("speakeasy: voice start failed: %s", exc)
                raise ServiceError(502, str(exc)) from None
            logger.warning("speakeasy: voice start failed (%s)", type(exc).__name__)
            raise ServiceError(502, f"Voice session creation failed: {type(exc).__name__}") from None
        if not isinstance(live_id, str) or not live_id or not isinstance(answer, str) or not answer:
            if transport is not None:
                transport.stop()
            self.store.fail_session(request_id)
            raise ServiceError(502, "voice provider returned an invalid session response")
        client_result: dict[str, Any] = {
            "interaction_id": interaction_id, "session": {"id": live_id},
            "transport": {"type": "webrtc", "sdp": answer}, "voice_provider": provider,
        }
        interaction = Interaction(interaction_id=interaction_id, live_session_id=live_id, away=away,
                                  device_id=device_id, room=room if source is None else "")
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
            binary = find_codex(s["voice"]["codex_path"])
            if binary is None:
                raise ServiceError(409, "Codex CLI not found: install it and run `codex login`, or set voice.codex_path")
            transport = CodexTransport(binary, self.dir / "codex", s["voice"]["max_call_minutes"] * 60)
        transport._answer = transport.start(sdp, instructions, seed, voice)
        return transport

    def _start_worker(self, interaction: Interaction, provider: str, transport: Any) -> SidebandWorker:
        if provider == "codex":
            from .codex_transport import CodexSidebandWorker
            return CodexSidebandWorker(self.rt, interaction, transport)
        if self._openai_worker is not None:
            return self._openai_worker(self.rt, interaction)
        from .openai_live import OpenAISidebandWorker
        return OpenAISidebandWorker(self.rt, interaction, self.openai_key)

    def finish_transport(self, interaction_id: str) -> dict[str, Any]:
        """End: close the provider session once the client saw it close (Codex needs an explicit stop)."""
        interaction = self.interaction(interaction_id)
        worker = interaction.worker
        with interaction.lock:
            if interaction.finalization == "confirmed":
                return {"finalization": "confirmed"}
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
    def pause(self, interaction_id: str, *, stop_transport: bool = False) -> dict[str, Any]:
        """Pause closes the paid voice session but keeps the call's transcript and tasks."""
        interaction = self.interaction(interaction_id)
        with interaction.lock:
            if interaction.successor is not None:
                raise ServiceError(409, "call was already resumed")
            already_closed = interaction.call_closed
            interaction.paused = True
        worker = interaction.worker
        if already_closed and worker is not None:
            with interaction.lock:
                if not interaction.history:
                    interaction.history = worker.turns()[-400:]
        if stop_transport and worker is not None:
            try:
                worker.stop_transport()
            except Exception:
                pass
        timer = threading.Timer(PAUSE_NOTICE_AFTER_S, self.pause_expired, args=(interaction,))
        timer.daemon = True
        timer.start()
        publish_state(self.store, interaction, self.settings.get()["assistant_name"])
        return {"interaction_id": interaction_id, "paused": True}

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
        interaction.room = room
        interaction.room_nudged = room_nudged   # an empty request after listening is handled once per call (the voice waits)
        interaction.history = history
        interaction.resumed_from = source.interaction_id
        interaction.revision = source.revision
        interaction.latest_delegation_id = source.latest_delegation_id

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
                eligible = (worker is not None and not interaction.paused and not interaction.call_closed
                            and interaction.successor is None and now - interaction.last_activity > minutes * 60)
            if not eligible or not worker.call_connected():
                continue
            try:
                self.pause(interaction.interaction_id, stop_transport=True)
                paused.append(interaction.interaction_id)
            except ServiceError:
                continue
        return paused

    def _idle_loop(self) -> None:
        ticks = 0
        self.settle_stuck_tasks()
        while not self._stop.wait(IDLE_CHECK_S):
            try:
                self.idle_check()
            except Exception as exc:
                logger.warning("speakeasy: idle check failed: %s", type(exc).__name__)
            ticks += 1
            if ticks % 4 == 0:  # about once a minute
                self.settle_stuck_tasks()

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

    def publish_all(self, key: str | None = None) -> None:
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
                or not isinstance(run_id, str) or not ID_RE.fullmatch(run_id) or choice not in {"once", "deny"}):
            raise ServiceError(400, "exact run_id, request_id, and once|deny are required")
        with interaction.lock:
            backend = self._run(interaction, run_id)
            approval = backend.approval
            if backend.status != "waiting_for_approval" or not approval or request_id != approval.get("request_id"):
                raise ServiceError(409, "approval is stale or not pending for that run")
            backend.approval, backend.status = None, "resolving_approval"
        name = self.settings.get()["assistant_name"]
        try:
            result = self.hermes.approve(backend.run_id, request_id, choice)
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
        self.store.progress(backend.idem_key, "milestone", "Approved once" if choice == "once" else "Approval denied")
        publish_state(self.store, interaction, name)
        return {"interaction_id": interaction_id, "choice": choice, "run_id": backend.run_id,
                "resolved": result.get("resolved", 0)}

    def dismiss_tasks(self, body: dict[str, Any]) -> dict[str, Any]:
        run_ids = body.get("run_ids")
        if set(body) != {"run_ids"} or not isinstance(run_ids, list) or not run_ids or len(run_ids) > 50 \
                or not all(isinstance(r, str) and ID_RE.fullmatch(r) for r in run_ids):
            raise ServiceError(400, "run_ids must be a list of run ids")
        dismissed = self.store.dismiss(run_ids)
        with self.lock:
            live = list(self.interactions.values())
        name = self.settings.get()["assistant_name"]
        for interaction in live:
            with interaction.lock:
                touched = any(r.run_id in dismissed for r in interaction.runs.values())
            if touched:
                publish_state(self.store, interaction, name)
        return {"dismissed": dismissed}

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
        name = self.settings.get()["assistant_name"]
        return {"work": self.store.work(assistant_name=name), "away": self.store.away(),
                "tasks": self.store.latest_tasks(MAX_TASKS)}

    def work(self, run_id: str) -> dict[str, Any]:
        if not ID_RE.fullmatch(run_id):
            raise ServiceError(404, "not found")
        return {"work": self.store.work(run_id, assistant_name=self.settings.get()["assistant_name"])}

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

    def codex_state(self) -> tuple[bool, bool, str]:
        s = self.settings.get()
        binary = find_codex(s["voice"]["codex_path"])
        if self._codex_login is not None:
            ok, msg = self._codex_login(binary)
        else:
            from .codex_transport import login_status
            ok, msg = login_status(binary)
        return binary is not None, ok, msg

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
        found, signed_in, codex_message = self.codex_state()
        api_key_set = bool(self.openai_key())
        provider = s["voice"]["provider"]
        voice_ready = signed_in if provider == "codex" else api_key_set
        return {
            "assistant_name": s["assistant_name"], "user_name": s["user_name"], "provider": provider,
            "voice": default_voice(s), "voice_ready": voice_ready and not self.updated_underneath(),
            "restart_needed": self.updated_underneath(),
            "codex_found": found, "codex_signed_in": signed_in, "codex_message": codex_message,
            "api_key_set": api_key_set, "brief_state": self.brief.status()["state"],
            "hermes_api_ok": self.hermes.health(), "hermes_api_key_set": bool(self.hermes_key()),
            "delivery_target": s["delivery"]["target"], **self.thread_status(),
            "continuity_enabled": s["continuity"]["enabled"], "devices": len(self.devices.devices()),
            "home_control_enabled": s["home_control"]["enabled"],
            "routing_model": routing_model(), "routing_hint": ROUTING_HINT, "routing_explainer": ROUTING_EXPLAINER,
            "routing_choice": self.routing_choices(),
            "advertised_url": s["server"]["advertised_url"], "tailscale_name": s["server"]["tailscale_name"],
            # Listening mode: POST /voice/sessions takes "room" (the app sends it only when this is true),
            # also with resume_from (listening mode during a call pauses it, then resumes it with "room").
            "room_listening": True,
            "room_on_resume": True,
            "version": __version__,
        }

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
