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
from . import attachments as A
from . import delivery as D
from . import duck as duck_mod
from . import messages as M
from .brief import BriefInvalid, BriefManager
from .calls import (ACTIVE_RUN_STATES, APPROVED_MILESTONE, MAX_TASKS, ServiceError, Interaction, Notices, Runtime, SidebandWorker,
                    end_shared, interaction_tasks, publish_state)
from .cards import ImageRejected, _sniff, default_image_roots, fetch_image, read_local_image
from .devices import DeviceStore
from .emails import canonical_json, extract_email_drafts
from .hermes_api import HermesAPI, HermesError

RESTART_NEEDED = ("Speakeasy was updated while Hermes was running. Restart the Hermes gateway "
                  "(hermes gateway restart) to finish the update.")
from .prompt import builder as P
from .router import home_plan_call as router_home_plan_call, routing_model
from .threads import THREAD_PLATFORMS
from .settings import (GROK_BETA_OFF, OPENAI_KEY_NAME, Settings, SettingsError, default_voice, find_codex, hermes_api_base,
                       grok_beta_on, hermes_secret, valid_delivery_target)
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
# UX contract flags in GET /voice/status ("capabilities"); clients hide what a server lacks.
CAPABILITIES = {"card_actions": True, "job_progress": True, "spoken_choices": True, "summary_points": True,
                "task_messages": True}
PAUSE_NOTICE_AFTER_S = 15 * 60
IDLE_CHECK_S = 15
IMAGE_SUPPORT_EVERY_S = 3600  # re-check whether Hermes reads images (its model setup rarely changes)
EARLY_CONNECT_WAIT_S = 8  # words heard while connecting wait this long for the call's event channel
CAPABILITIES["job_activity"] = True  # live activity / findings / touched / hero on tasks (activity.py)
CAPABILITIES["tool_log"] = True  # activity items carry tool/started_at/ended_at; GET /voice/work/{id} adds args/result



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
                 tune_run: Callable[[str, str], tuple[str, str]] | None = None,
                 volume_backend: duck_mod.VolumeBackend | None = None,
                 grok_credential: Callable[[], tuple[str, str]] | None = None,
                 grok_connect: Callable[..., Any] | None = None,
                 grok_signin: Callable[[], tuple[bool, bool, str]] | None = None):
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
        self.shared = A.SharedFolder(self.home)
        self.rt.shared = self.shared
        self.rt.discard_files = self.discard_shared_files
        # Messages sent to a task by id (POST /voice/work/{id}/message): replies by client_id for
        # MESSAGE_REPLAY_S, and the voiceless worker for tasks whose call is gone.
        self._message_replies: dict[str, tuple[float, threading.Event, dict[str, Any] | None]] = {}
        self._message_lock = threading.Lock()
        self._text_worker: Any = None
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
        self._codex_factory = codex_factory
        self._openai_negotiate = openai_negotiate
        self._openai_worker = openai_worker
        self._codex_login = codex_login
        self._grok_credential = grok_credential
        self._grok_connect = grok_connect
        self._grok_signin = grok_signin
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
        self.duck.close()  # put the volume back before a reload swaps this code out
        if self._text_worker is not None:
            self._text_worker.stop()

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
                             "seed": seed, "voice": voice, "max_call_s": s["voice"]["max_call_minutes"] * 60}
                live_id, answer = "grok_" + interaction_id[3:], "relay"
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
            if transport is not None and hasattr(transport, "stop"):
                transport.stop()
            self.store.fail_session(request_id)
            from .codex_transport import CodexStartError
            if isinstance(exc, CodexStartError):
                logger.warning("speakeasy: voice start failed: %s", exc)
                raise ServiceError(502, str(exc)) from None
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
                                  device_id=device_id, attachments=A.CallAttachments(screen),
                                  room=room if source is None else "", local=local)
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
        self._check_image_support_soon()
        self.prune_shared()
        next_image_check = time.monotonic() + IMAGE_SUPPORT_EVERY_S
        while not self._stop.wait(IDLE_CHECK_S):
            try:
                self.idle_check()
            except Exception as exc:
                logger.warning("speakeasy: idle check failed: %s", type(exc).__name__)
            ticks += 1
            if ticks % 4 == 0:  # about once a minute
                self.settle_stuck_tasks()
            if time.monotonic() >= next_image_check:  # about once an hour
                self._check_image_support_soon()
                self.prune_shared()
                next_image_check = time.monotonic() + IMAGE_SUPPORT_EVERY_S

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
        name = self.settings.get()["assistant_name"]
        return {"work": self.store.work(assistant_name=name), "away": self.store.away(),
                "tasks": self.store.latest_tasks(MAX_TASKS)}

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
        return {"work": self.store.work(run_id, assistant_name=self.settings.get()["assistant_name"],
                                        activity_detail=True)}

    # -- messages sent to one task on purpose (docs/API.md "Messages to a task") -------------------
    MESSAGE_REPLAY_S = 600

    def task_message(self, run_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``POST /voice/work/{run_id}/message``: text typed to one task. Goes to that task by id, never
        through routing: a steer while it runs, the answer while it waits on the user, a follow-up job
        once it has finished. Idempotent on client_id."""
        if not isinstance(body, dict) or "text" not in body or not set(body) <= {
                "text", "source", "client_id", "interaction_id", "choice_task_id"}:
            raise ServiceError(400, "Send the message as text.")
        text, source = body.get("text"), body.get("source") or "typed"
        client_id, interaction_id = body.get("client_id"), body.get("interaction_id")
        choice_task_id = body.get("choice_task_id")
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
        if client_id is None:
            return self._task_message(run_id, text, interaction_id, choice_task_id)
        mine, event = self._claim_message(client_id)
        if not mine:
            event.wait(M.START_WAIT_S + 15)
            with self._message_lock:
                done = self._message_replies.get(client_id)
            if done is None or done[2] is None:
                raise ServiceError(409, "That message is still being sent. Try again in a moment.")
            return done[2]
        try:
            reply = self._task_message(run_id, text, interaction_id, choice_task_id)
        except Exception:
            with self._message_lock:
                self._message_replies.pop(client_id, None)  # a failed send may be retried with the same id
            event.set()
            raise
        with self._message_lock:
            self._message_replies[client_id] = (time.monotonic(), event, reply)
        event.set()
        return reply

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
                      choice_task_id: str | None) -> dict[str, Any]:
        if interaction_id:
            self.interaction(interaction_id)  # 404 for an unknown call
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
        future = asyncio.run_coroutine_threadsafe(M.deliver(worker, task_id, text), worker.loop)
        try:
            got = future.result(M.START_WAIT_S + 10)
        except LookupError:
            raise ServiceError(404, "I couldn't find that task.") from None
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
        return paths

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
        grok_signed_in, grok_key_set, grok_message = self.grok_state()
        grok_ready = grok_beta_on(s) and (grok_signed_in or grok_key_set)
        if provider == "grok":
            voice_ready = grok_ready
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
            "attachments": self.attachment_status(),
            # Grok voice (beta): calls relay audio through the plugin (transport type "relay").
            "grok_beta": grok_beta_on(s), "grok_signed_in": grok_signed_in, "grok_api_key_set": grok_key_set,
            "grok_ready": grok_ready, "grok_message": grok_message,
            # Listening mode: POST /voice/sessions takes "room" (the app sends it only when this is true),
            # also with resume_from (listening mode during a call pauses it, then resumes it with "room").
            "room_listening": True,
            "room_on_resume": True,
            "focus": True,
            "version": __version__,
            # What this server supports from the UX contract; a missing flag means an older server.
            "capabilities": dict(CAPABILITIES),
        }

    def attachment_status(self) -> dict[str, Any]:
        """Whether Hermes reads images (``images``: the input path exists; ``vision``: native,
        described, none or unknown) and the attachment limits. Reads the cached check only."""
        support = getattr(self.hermes, "image_support", None) or {}
        return {"images": bool(support.get("images")), "vision": support.get("vision") or "unknown",
                "max_attachments": A.MAX_ATTACHMENTS, "max_image_bytes": A.MAX_IMAGE_BYTES,
                "max_request_image_bytes": A.MAX_REQUEST_IMAGE_BYTES, "max_file_bytes": A.MAX_FILE_BYTES}

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
