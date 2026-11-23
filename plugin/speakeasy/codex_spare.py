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

MAX_AGE_S = 15 * 60.0       # warm a fresh replacement for a spare this old (the old one stays usable meanwhile)
HARD_MAX_AGE_S = 45 * 60.0  # never use a spare older than this (a long-idle thread or sign-in may have gone stale)
WAIT_WARMING_S = 25.0       # a tap waits for a warm-up already under way: it is ahead of a cold start
WARM_TIMEOUT_S = 60.0       # a background warm-up may wait this long for thread/start (a tap waits 20 s)
RETRY_S = (15.0, 30.0, 60.0, 120.0, 300.0)  # a failed warm-up tries again after these waits (then every 5 min)


class CodexSpare:
    """Nov 23: on the Mac mini thread/start took 1-34 s (Codex's skills watcher waits on a busy
    fseventsd), a 20 s timeout failed a warm-up now and then, and a failed warm-up was never retried,
    so every tap after it started cold (4 of 6 calls Nov 20-23: 7-15 s to connect). Now a failed
    warm-up retries with backoff, a background warm-up waits up to 60 s, and an aging spare is
    replaced by warming the new one first (there is never a gap without a spare)."""

    def __init__(self, make: Callable[[Any], Any], *, max_age_s: float = MAX_AGE_S,
                 hard_max_age_s: float | None = None, retry_s: tuple[float, ...] = RETRY_S,
                 clock: Callable[[], float] = time.monotonic, background: bool = True) -> None:
        """*make(key)* returns a new, unprepared transport for *key* (the Codex binary path)."""
        self.make = make
        self.max_age_s = max_age_s
        self.hard_max_age_s = max(max_age_s, hard_max_age_s if hard_max_age_s is not None
                                  else HARD_MAX_AGE_S * max_age_s / MAX_AGE_S)
        self.retry_s = retry_s
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
        self.failures = 0              # warm-ups failed in a row (the retry backoff)
        self.last_failure = ""         # why the last warm-up failed ("" after a success)
        self.closed = False
        self.stats = {"warmed": 0, "hits": 0, "misses": 0, "discarded": 0, "failed": 0}
        self.last_take: dict[str, Any] = {}

    # -- warming -------------------------------------------------------------------------------
    def warm(self, key: Any, wait: bool = False, replace: bool = False) -> None:
        """Have one spare ready for *key*. No-op when one is ready or being warmed for it.
        *replace*: warm a fresh one even though a usable spare is ready; the old one stays
        usable until the new one is ready (refresh)."""
        if key is None:
            return
        start = None
        with self.lock:
            if self.closed:
                return
            ready = self.spare is not None and self.key == key and self._usable(self.spare)
            if ready and not replace:
                return
            if self.warming is not None and self.warming_key == key:
                done = self.warming
            else:
                if self.spare is not None and not ready:  # wrong key or dead: never more than one spare
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
        failure = ""
        try:
            transport = self.make(key)
            transport.thread_timeout = WARM_TIMEOUT_S   # nobody is waiting on this one: let it finish
            transport.prepare()
        except Exception as exc:
            failure = type(exc).__name__
            if transport is not None:
                _stop(transport)
            transport = None
        old = None
        retry_in = None
        with self.lock:
            if self.warming is done:
                self.warming = None
                self.warming_key = None
            current = not self.closed and generation == self.generation
            # A refresh keeps the old spare usable while the new one warms; the new one replaces it.
            keep = transport is not None and current and (self.spare is None or self.key == key)
            if keep:
                old = self.spare
                self.spare, self.key, self.ready_at = transport, key, self.clock()
                self.stats["warmed"] += 1
                self.failures, self.last_failure = 0, ""
                self._schedule_locked(self.max_age_s + 1, self._refresh, key)
            elif transport is None and current:
                self.stats["failed"] += 1
                self.failures += 1
                self.last_failure = failure
                retry_in = self.retry_s[min(self.failures, len(self.retry_s)) - 1] if self.retry_s else None
                if retry_in is not None and self.spare is None:
                    self._schedule_locked(retry_in, self._retry, key)
                elif self.spare is not None and retry_in is not None:
                    # An old spare is still usable: try the refresh again later, before it gets too old.
                    self._schedule_locked(retry_in, self._refresh, key)
        if transport is not None and not keep:
            _stop(transport)
        elif keep:
            logger.info("speakeasy: codex spare ready in %d ms", int((self.clock() - began) * 1000))
            if old is not None:
                self.stats["discarded"] += 1
                logger.info("speakeasy: codex spare dropped (replaced by a fresh one)")
                _stop_later(old, self.background)
        if failure:
            logger.info("speakeasy: codex spare warm-up failed (%s)%s", failure,
                        f"; trying again in {int(retry_in)} s" if retry_in is not None and current else "")
        done.set()

    def _schedule_locked(self, delay: float, fn: Callable[[Any], None], key: Any) -> None:
        if not self.background:
            return
        if self.timer is not None:
            self.timer.cancel()
        self.timer = threading.Timer(delay, fn, args=(key,))
        self.timer.daemon = True
        self.timer.start()

    def _refresh(self, key: Any) -> None:
        """The spare is getting old: warm its replacement (the old one serves taps meanwhile)."""
        with self.lock:
            stale = self.spare is not None and self.clock() - self.ready_at >= self.max_age_s
            dead = self.spare is not None and not self._usable(self.spare)
            if dead:
                self._drop_locked("too old" if self.clock() - self.ready_at >= self.hard_max_age_s else "died")
        if stale or dead:
            self.warm(key, replace=not dead)

    def _retry(self, key: Any) -> None:
        with self.lock:
            missing = self.spare is None and not self.closed
        if missing:
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
                self.last_take = {"age": f"{int(self.clock() - self.ready_at)}s"}
                if done is not None:
                    self.last_take["waited"] = "warm-up"
                return spare
            if spare is not None:
                why = "other-key" if self.key != key else ("too-old" if self.clock() - self.ready_at >= self.hard_max_age_s
                                                           else "dead")
                self._drop_locked("stale or for another Codex copy")
            elif done is not None:
                why = "warm-up-failed" if self.last_failure else "warming"
            else:
                why = f"failed:{self.last_failure}" if self.last_failure else "none"
            self.stats["misses"] += 1
            self.last_take = {"why": why}
            return None

    def _usable(self, transport: Any) -> bool:
        alive = getattr(transport, "alive", None)
        if callable(alive) and not alive():
            return False
        return self.clock() - self.ready_at < self.hard_max_age_s

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
            _stop_later(spare, self.background)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            self.generation += 1
            if self.timer is not None:
                self.timer.cancel()
            spare, self.spare, self.key = self.spare, None, None
        if spare is not None:
            _stop(spare)


def _stop_later(transport: Any, background: bool) -> None:
    if background:
        threading.Thread(target=_stop, args=(transport,), daemon=True, name="speakeasy-codex-spare-stop").start()
    else:
        _stop(transport)


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
