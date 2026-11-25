"""Background threads a test leaves running: stop them after each test, and fail a test file that
still leaves more than a handful behind.

Why: every fake call worker, fake Hermes server, tunnel watcher and notice sender a test forgot to
stop used to live on for the rest of the run. A full serial run reached about 3,400 live threads by
``test_routing.py`` and starved the host until a test hung (SPK-565). Two parts:

* ``track()`` wraps the start of the plugin's long-lived background helpers (``TunnelWatcher``,
  ``Notices``, ``PushNotifier``) so ``stop_leftovers()`` can end whichever ones a test left running, together with the
  fakes' own registries (``FakeLiveWorker.live``, ``FakeHermesServer.live``). Tests that close their
  service and server properly lose nothing; the rest are cleaned up for them.
* Every call's delayed steps (``calls.CallTimers``: a held drop's 45 s expiry, a paused call's
  15 min expiry, the screen hold, the unattended check) are cancelled after each test. They used to
  fire into later tests' runs and touch a closed store (SPK-596); the service cancels them on close.
* ``FileGuard`` compares the live threads before and after a test file. Threads the file started
  that are still alive after a short grace period are a leak; more than ``FILE_LIMIT`` of them fails
  the file with the thread names, so the growth is caught where it starts.
"""
from __future__ import annotations

import collections
import functools
import re
import threading
import time
import weakref

from fakes import FakeHermesServer, FakeLiveWorker
from speakeasy import calls as _calls
from speakeasy import push as _push
from speakeasy import tunnel as _tunnel

# Threads one test file may leave alive after the grace period. A few short-lived timers (a pending
# debounce, a 10 s settle timer) are normal; a leak per test grows far past this in one file.
FILE_LIMIT = 16
# Live threads the whole process may hold at a file boundary: catches a slow creep (one thread a file)
# that no single file trips. A clean run sits well under 100 (SPK-565 measured about 3,400 before).
RUN_LIMIT = 150
# How long threads get to finish before they count: a loaded host drains streams slowly. Only a file
# over a limit waits; a clean file pays nothing.
GRACE_S = 10.0

_watchers: list = []
_notices: list = []   # Notices and PushNotifier senders: both stop with close()
_call_timers: "weakref.WeakSet" = weakref.WeakSet()   # every calls.CallTimers (one per service)
# A call timer thread (``speakeasy-call-timer-*``) alive after its file: none may be (SPK-596).
CALL_TIMER_PREFIX = "speakeasy-call-timer-"


def track() -> None:
    real_start = _tunnel.TunnelWatcher.start
    real_init = _calls.Notices.__init__

    @functools.wraps(real_start)
    def start(self):
        real_start(self)
        _watchers.append(self)

    @functools.wraps(real_init)
    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        if self.thread is not None:
            _notices.append(self)

    real_push_init = _push.PushNotifier.__init__

    @functools.wraps(real_push_init)
    def push_init(self, *args, **kwargs):
        real_push_init(self, *args, **kwargs)
        if self.thread is not None:
            _notices.append(self)

    real_timers_init = _calls.CallTimers.__init__

    @functools.wraps(real_timers_init)
    def timers_init(self, *args, **kwargs):
        real_timers_init(self, *args, **kwargs)
        _call_timers.add(self)

    _calls.CallTimers.__init__ = timers_init  # type: ignore[method-assign]
    _tunnel.TunnelWatcher.start = start  # type: ignore[method-assign]
    _calls.Notices.__init__ = init  # type: ignore[method-assign]
    _push.PushNotifier.__init__ = push_init  # type: ignore[method-assign]


def stop_leftovers() -> None:
    """End every tracked helper still running. Each stop is idempotent, so a test that already
    closed its service/server is unaffected."""
    for timers in list(_call_timers):
        timers.cancel_pending()
    while _watchers:
        _watchers.pop().stop()
    while _notices:
        _notices.pop().close()
    for worker in list(FakeLiveWorker.live):
        worker.stop_loop()
    for server in list(FakeHermesServer.live):
        try:
            server.close()
        except Exception:
            pass


def _label(thread: threading.Thread) -> str:
    return re.sub(r"^Thread-\d+ ?", "", thread.name).strip("()") or thread.name


class FileGuard:
    def __init__(self) -> None:
        self.before = set(threading.enumerate())

    def _settle(self) -> tuple[list[threading.Thread], list[threading.Thread]]:
        """(threads this file started that are still alive, every live thread) once short-lived
        ones had GRACE_S to finish."""
        deadline = time.monotonic() + GRACE_S
        while True:
            alive = [t for t in threading.enumerate() if t.is_alive()]
            left = [t for t in alive if t not in self.before]
            timers = [t for t in left if t.name.startswith(CALL_TIMER_PREFIX)]
            if (len(left) <= FILE_LIMIT and len(alive) <= RUN_LIMIT and not timers) or time.monotonic() >= deadline:
                return left, alive
            time.sleep(0.1)

    def problem(self, path: str) -> str | None:
        left, alive = self._settle()
        fix = ("Stop the servers, call workers and executors its tests start (close the service, "
               "server.stop(), worker.stop_loop()).")
        timers = [t for t in left if t.name.startswith(CALL_TIMER_PREFIX)]
        if timers:
            return (f"{path} left {len(timers)} call timer(s) pending after its tests: {_names(timers)}. "
                    "Schedule a call's delayed steps with rt.timers.later() and close the service.")
        if len(left) > FILE_LIMIT:
            return (f"{path} left {len(left)} background threads running after its tests "
                    f"(limit {FILE_LIMIT}): {_names(left)}. {fix}")
        if len(alive) > RUN_LIMIT:
            return (f"{len(alive)} threads are alive after {path} (limit {RUN_LIMIT} for the whole run): "
                    f"{_names(alive)}. Earlier files leave threads behind a few at a time. {fix}")
        return None


def _names(threads: list[threading.Thread]) -> str:
    counted = collections.Counter(_label(t) for t in threads).most_common(8)
    return ", ".join(f"{name} ×{n}" for name, n in counted)
