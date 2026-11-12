"""A call tap should pay only for the voice itself.

Two pieces, both for Codex voice:

``CodexSpare`` keeps at most ONE pre-warmed ``codex app-server``: spawned, ``initialize``d and with a
thread already started. ``thread/start`` alone takes 0.5-15 s (measured on the Mac mini; it talks to
OpenAI), and before this every tap paid it, so calls took 10-16 s to connect and the phone's 12 s
connect watchdog gave up and retried. A tap takes the spare and pays only ``thread/realtime/start``
(about 0.5 s); a fresh spare is warmed in the background right after. An idle app-server waits on
stdin and uses no CPU. The spare is replaced after ``MAX_AGE_S`` and dropped when the Codex copy or
its sign-in changes, so a stale or signed-out one is never used.

``DeviceConnects`` makes one tap one Codex start. When the phone retries while the server is still
starting its first session (the old 12 s watchdog did, at 23:17:56 and 23:17:57 on Nov 11), the retry
takes over the first attempt's transport instead of starting a second one; the superseded attempt
stops without starting a voice session.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_AGE_S = 15 * 60.0     # replace a spare this old (a long-idle thread or sign-in may have gone stale)
WAIT_WARMING_S = 25.0     # a tap waits for a warm-up already under way: it is ahead of a cold start


class CodexSpare:
    def __init__(self, make: Callable[[Any], Any], *, max_age_s: float = MAX_AGE_S,
                 clock: Callable[[], float] = time.monotonic, background: bool = True) -> None:
        """*make(key)* returns a new, unprepared transport for *key* (the Codex binary path)."""
        self.make = make
        self.max_age_s = max_age_s
        self.clock = clock
        self.background = background
        self.lock = threading.Lock()
        self.spare: Any = None
        self.key: Any = None
        self.ready_at = 0.0
        self.warming: threading.Event | None = None   # set when the warm-up under way has finished
        self.warming_key: Any = None
        self.generation = 0
        self.timer: threading.Timer | None = None
        self.closed = False
        self.stats = {"warmed": 0, "hits": 0, "misses": 0, "discarded": 0}

    # -- warming -------------------------------------------------------------------------------
    def warm(self, key: Any, wait: bool = False) -> None:
        """Have one spare ready for *key*. No-op when one is ready or being warmed for it."""
        if key is None:
            return
        start = None
        with self.lock:
            if self.closed:
                return
            if self.spare is not None and self.key == key and self._usable(self.spare):
                return
            if self.warming is not None and self.warming_key == key:
                done = self.warming
            else:
                if self.spare is not None:  # wrong key or dead: never more than one spare
                    self._drop_locked("replaced")
                done = self.warming = threading.Event()
                self.warming_key = key
                start = (key, self.generation, done)
        if start is not None:
            if self.background:
                threading.Thread(target=self._warm, args=start, daemon=True, name="speakeasy-codex-spare").start()
            else:
                self._warm(*start)
        if wait:
            done.wait(WAIT_WARMING_S)

    def _warm(self, key: Any, generation: int, done: threading.Event) -> None:
        transport = None
        began = self.clock()
        try:
            transport = self.make(key)
            transport.prepare()
        except Exception as exc:
            logger.info("speakeasy: codex spare warm-up failed (%s)", type(exc).__name__)
            if transport is not None:
                _stop(transport)
            transport = None
        with self.lock:
            if self.warming is done:
                self.warming = None
                self.warming_key = None
            keep = transport is not None and not self.closed and generation == self.generation and self.spare is None
            if keep:
                self.spare, self.key, self.ready_at = transport, key, self.clock()
                self.stats["warmed"] += 1
                self._schedule_refresh_locked(key)
        if transport is not None and not keep:
            _stop(transport)
        elif keep:
            logger.info("speakeasy: codex spare ready in %d ms", int((self.clock() - began) * 1000))
        done.set()

    def _schedule_refresh_locked(self, key: Any) -> None:
        if not self.background:
            return
        if self.timer is not None:
            self.timer.cancel()
        self.timer = threading.Timer(self.max_age_s + 1, self._refresh, args=(key,))
        self.timer.daemon = True
        self.timer.start()

    def _refresh(self, key: Any) -> None:
        with self.lock:
            stale = self.spare is not None and self.clock() - self.ready_at >= self.max_age_s
            if stale:
                self._drop_locked("aged out")
        if stale:
            self.warm(key)

    # -- taking --------------------------------------------------------------------------------
    def take(self, key: Any) -> Any:
        """The ready spare for *key*, or None (the caller starts cold). Waits for a warm-up already
        under way for *key*: it started earlier than a cold start would, so it finishes sooner."""
        with self.lock:
            done = self.warming if self.warming_key == key and self.spare is None else None
        if done is not None:
            done.wait(WAIT_WARMING_S)
        with self.lock:
            spare = self.spare
            if spare is not None and self.key == key and self._usable(spare):
                self.spare, self.key = None, None
                self.stats["hits"] += 1
                return spare
            if spare is not None:
                self._drop_locked("stale or for another Codex copy")
            self.stats["misses"] += 1
            return None

    def _usable(self, transport: Any) -> bool:
        alive = getattr(transport, "alive", None)
        if callable(alive) and not alive():
            return False
        return self.clock() - self.ready_at < self.max_age_s

    # -- dropping ------------------------------------------------------------------------------
    def discard(self, why: str = "sign-in changed") -> None:
        """Drop the spare and any warm-up under way (sign-in or Codex copy changed)."""
        with self.lock:
            self.generation += 1
            self._drop_locked(why)

    def _drop_locked(self, why: str) -> None:
        spare, self.spare, self.key = self.spare, None, None
        if spare is not None:
            self.stats["discarded"] += 1
            logger.info("speakeasy: codex spare dropped (%s)", why)
            threading.Thread(target=_stop, args=(spare,), daemon=True, name="speakeasy-codex-spare-stop").start() \
                if self.background else _stop(spare)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.generation += 1
            if self.timer is not None:
                self.timer.cancel()
            spare, self.spare, self.key = self.spare, None, None
        if spare is not None:
            _stop(spare)


def _stop(transport: Any) -> None:
    try:
        transport.stop()
    except Exception:
        pass


class DeviceConnects:
    """One Codex start per tap: a retry from the same device while a start is under way takes it over."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.latest: dict[str, int] = {}
        self.pending: dict[str, "_Pending"] = {}
        self.seq = 0

    def begin(self, device_id: str) -> tuple[int, "_Pending | None"]:
        """A new connect for *device_id*: (its token, the attempt it takes over or None)."""
        with self.lock:
            self.seq += 1
            token = self.seq
            if not device_id:
                return token, None
            self.latest[device_id] = token
            earlier = self.pending.get(device_id)
            if earlier is not None and not earlier.voice_started:
                earlier.owner = token
                return token, earlier
            return token, None

    def track(self, device_id: str, token: int) -> "_Pending":
        pending = _Pending(token)
        if device_id:
            with self.lock:
                self.pending[device_id] = pending
        return pending

    def is_latest(self, device_id: str, token: int) -> bool:
        if not device_id:
            return True
        with self.lock:
            return self.latest.get(device_id) == token

    def claim_voice(self, device_id: str, pending: "_Pending", token: int) -> bool:
        """The owner is about to start the voice: no later retry can take this attempt over now."""
        with self.lock:
            if pending.owner != token:
                return False
            pending.voice_started = True
            if device_id and self.pending.get(device_id) is pending:
                del self.pending[device_id]
            return True

    def finish(self, device_id: str, pending: "_Pending") -> None:
        with self.lock:
            if device_id and self.pending.get(device_id) is pending:
                del self.pending[device_id]


class _Pending:
    def __init__(self, owner: int) -> None:
        self.owner = owner
        self.voice_started = False
        self.ready = threading.Event()
        self.transport: Any = None
        self.error: BaseException | None = None
