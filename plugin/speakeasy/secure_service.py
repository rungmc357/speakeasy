"""The service side of secure input (secure_input.py): who a waiting turn belongs to, telling the
user (board milestone, push, the live call), and the two routes the app answers through.

``GET /voice/secrets`` lists the open requests this device may answer (no values, ever).
``POST /voice/secrets/{id}`` answers one: ``{"value": "..."}`` or ``{"action": "skip"}``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import secure_input as S
from . import runstate as RS

logger = logging.getLogger(__name__)

_ACTIVE = RS.LIVE - {"cancel_requested", "waiting_on_task"}


class SecureInputMixin:
    """Mixed into ``VoiceService``; needs ``store``, ``push``, ``settings``, ``interaction_for_key``."""

    secure: S.SecureInputs
    secure_wired: dict[str, bool]
    store: Any
    push: Any
    interaction_for_key: Any

    def _init_secure(self, wire: bool) -> None:
        self.secure = S.SecureInputs(notify=self._secure_notify, settled=self._secure_settled)
        self.secure_wired = {"secret": False, "sudo": False, "login": False}
        if wire:
            self.wire_secure_input()

    def wire_secure_input(self) -> dict[str, bool]:
        """Point Hermes' secret/sudo lookups for Speakeasy's runs at this service."""
        self.secure_wired = S.install(self.secure, self._secure_target)
        return self.secure_wired

    def _close_secure(self) -> None:
        S.uninstall(self.secure)

    # -- whose turn is waiting -----------------------------------------------------------------
    def _secure_target(self, session_key: str, session_id: str) -> S.Target | None:
        """A live Speakeasy job for this turn: its run id is the turn's approval session key
        (``/v1/runs``), or the turn runs in a session one of its jobs is running in."""
        key = None
        if session_key:
            found = self.store.key_for_run(session_key)
            row = self.store.run_row(found) if found else None
            if row and row.get("status") in _ACTIVE:
                key = found
        if key is None and session_id:
            for k, _rid, status in self.store.keys_for_session(session_id)[:8]:
                if status in _ACTIVE:
                    key = k
                    break
        if key is None:
            return None
        row = self.store.run_row(key) or {}
        title = self.store.title(key) or self.store.request_text(key) or "Your task"
        interaction = self.interaction_for_key(key)
        device = getattr(interaction, "device_id", "") or ""
        return S.Target(key=key, run_id=row.get("run_id"), session_id=row.get("session_id") or session_id or None,
                        title=" ".join(str(title).split())[:120], device_id=device)

    # -- telling the user ------------------------------------------------------------------------
    def _secure_notify(self, pending: S.Pending) -> None:
        key = pending.target.key
        milestone, body = {
            "secret": ("Waiting for you: a key, in a secure box", f"Your agent needs your {pending.label}"),
            "sudo": ("Waiting for you: your Mac's password for one command",
                     "Your agent needs your Mac's password to run a command"),
            "login": (f"Waiting for you: a login for {pending.site}", f"Your agent needs a login for {pending.site}"),
            "code": (f"Waiting for you: the code {pending.site} sent", f"Your agent needs the code {pending.site} sent you"),
        }[pending.kind]
        try:
            self.store.progress(key, "milestone", milestone)
        except Exception as exc:
            logger.warning("speakeasy: secure input milestone failed: %s", type(exc).__name__)
        try:
            self.push.notify("needs_you", key=f"secure:{pending.id}", title=pending.target.title, body=body,
                             run_id=pending.target.run_id, level="restricted")
        except Exception as exc:
            logger.warning("speakeasy: secure input push failed: %s", type(exc).__name__)
        self._secure_feed(pending)
        self._secure_say(pending)

    def _secure_feed(self, pending: S.Pending) -> None:
        """A call's event feed gets ``secure_inputs`` (the open requests for that device, no values)
        so a call screen shows the card at once; other screens poll ``GET /voice/secrets``."""
        interaction = self.interaction_for_key(pending.target.key)
        feed = getattr(interaction, "feed", None) if interaction is not None else None
        if feed is None:
            return
        try:
            feed.publish("secure_inputs", self.secure.visible(getattr(interaction, "device_id", "") or "",
                                                              key=None))
        except Exception as exc:
            logger.warning("speakeasy: secure input feed failed: %s", type(exc).__name__)

    def _secure_say(self, pending: S.Pending) -> None:
        """On a live call: one short line, never the value, never a request to say it."""
        interaction = self.interaction_for_key(pending.target.key)
        worker = getattr(interaction, "worker", None) if interaction is not None else None
        loop = getattr(worker, "loop", None)
        if worker is None or loop is None or not worker.call_connected():
            return
        delegation = None
        with interaction.lock:
            for run in interaction.runs.values():
                if run.idem_key == pending.target.key:
                    delegation = run.say_id
        line = {"sudo": S.VOICE_LINE_SUDO, "login": S.VOICE_LINE_LOGIN}.get(pending.kind, S.VOICE_LINE)

        async def say() -> None:
            await worker.append("session.thinking.append", delegation, S.VOICE_RULE)
            await worker.append("session.commentary.append", delegation, line)

        try:
            asyncio.run_coroutine_threadsafe(say(), loop)
        except Exception as exc:
            logger.warning("speakeasy: secure input voice line failed: %s", type(exc).__name__)

    def _secure_settled(self, pending: S.Pending) -> None:
        self._secure_feed(pending)
        done = {"secret": "Got it — saved securely", "sudo": "Got it — running the command",
                "login": "Got it — login saved to your vault", "code": "Got it — entering the code"}[pending.kind]
        text = {"provided": done,
                "skipped": "Skipped", "expired": "The secure box timed out", "cancelled": "Secure box closed"}
        try:
            self.store.progress(pending.target.key, "milestone", text.get(pending.outcome, "Secure box closed"))
        except Exception:
            pass

    # -- routes ----------------------------------------------------------------------------------
    def secure_list(self, device_id: str) -> dict[str, Any]:
        return {"requests": self.secure.visible(device_id), "wired": dict(self.secure_wired)}

    def secure_answer(self, request_id: str, device_id: str, raw: bytearray) -> tuple[int, dict[str, Any]]:
        try:
            return 200, self.secure.answer(request_id, device_id, raw)
        except S.SecureInputError as exc:
            body: dict[str, Any] = {"error": exc.message}
            if exc.reason:
                body["reason"] = exc.reason
            return exc.status, body
