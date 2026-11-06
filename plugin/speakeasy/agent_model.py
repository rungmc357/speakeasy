"""Can this Hermes run a turn at all: does it have an AI model (a provider it can reach)?

A brand-new Hermes whose setup wizard was skipped has no provider, and every turn fails at once
with "Hermes is not connected to any AI provider yet". Setup, the Setup check and the apps' "You're
connected" screen ask this module so they never look all green while the first task would fail.

``state()`` answers ``"ok"``, ``"missing"`` or ``"unknown"``. It asks Hermes' own provider
resolver (the ladder a turn uses: config pin, API keys, sign-ins, ...), which reads only local
files. ``"missing"`` only when that resolver says nothing is configured; anything else it can't
tell (an older Hermes without the resolver, an unexpected error) is ``"unknown"`` and shows
nothing, so a check that can't run never blocks anyone.
"""
from __future__ import annotations

import threading
import time
from typing import Callable

OK, MISSING, UNKNOWN = "ok", "missing", "unknown"
NO_PROVIDER_CODE = "no_provider_configured"
_NO_PROVIDER_PHRASES = ("not connected to any ai provider", NO_PROVIDER_CODE)

# What setup and the apps tell someone whose Hermes has no model yet.
SETUP_LINE = "Hermes has no AI model yet: run `hermes model` (or ask it in chat), then setup again."
COMMAND = "hermes model"


def says_no_model(text: str) -> bool:
    """A Hermes error (a failed run's message) that means no AI provider is configured."""
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in _NO_PROVIDER_PHRASES)


def resolve() -> str:
    """Ask Hermes' provider resolver in this process (its HERMES_HOME is the profile's)."""
    try:
        from hermes_cli.auth import AuthError, resolve_provider  # type: ignore[import-not-found]
    except Exception:
        return UNKNOWN
    try:
        return OK if resolve_provider("auto") else UNKNOWN
    except AuthError as exc:
        if getattr(exc, "code", None) == NO_PROVIDER_CODE or says_no_model(str(exc)):
            return MISSING
        return UNKNOWN
    except Exception:
        return UNKNOWN


class ModelCheck:
    """``resolve()`` cached for the status route, which apps poll. A miss is rechecked often (the
    user is fixing it), a yes rarely. Never blocks a caller for long: the resolver can reach the
    AWS credential chain (a short metadata lookup on servers) when nothing else is configured."""

    MISSING_TTL_S = 20.0
    OK_TTL_S = 300.0
    FIRST_WAIT_S = 3.0

    def __init__(self, resolver: Callable[[], str] = resolve, clock: Callable[[], float] = time.monotonic):
        self.resolver, self.clock = resolver, clock
        self._value: str | None = None
        self._at = 0.0
        self._lock = threading.Lock()
        self._running: threading.Thread | None = None

    def _ttl(self) -> float:
        return self.OK_TTL_S if self._value == OK else self.MISSING_TTL_S

    def _refresh(self) -> None:
        try:
            value = self.resolver()
        except Exception:
            value = UNKNOWN
        with self._lock:
            self._value, self._at, self._running = value, self.clock(), None

    def state(self, wait: bool = False) -> str:
        """The cached answer, refreshed in the background when stale. ``wait`` (and the very first
        call) waits briefly for a fresh answer."""
        with self._lock:
            fresh = self._value is not None and self.clock() - self._at < self._ttl()
            if fresh:
                return self._value or UNKNOWN
            thread = self._running
            if thread is None:
                thread = self._running = threading.Thread(target=self._refresh, daemon=True,
                                                          name="speakeasy-model-check")
                thread.start()
            first = self._value is None
        if wait or first:
            thread.join(self.FIRST_WAIT_S)
        with self._lock:
            return self._value or UNKNOWN

    def saw(self, value: str) -> None:
        """A run just proved the answer (a turn failed for no provider, or one completed)."""
        with self._lock:
            self._value, self._at = value, self.clock()
