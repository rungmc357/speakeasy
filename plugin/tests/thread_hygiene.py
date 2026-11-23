"""Background threads a test leaves running: stop them after each test, and fail a test file that
still leaves more than a handful behind.

Why: every fake call worker, fake Hermes server, tunnel watcher and notice sender a test forgot to
stop used to live on for the rest of the run. A full serial run reached about 3,400 live threads by
``test_routing.py`` and starved the host until a test hung (SPK-565). Two parts:

* ``track()`` wraps the start of the plugin's long-lived background helpers (``TunnelWatcher``,
  ``Notices``) so ``stop_leftovers()`` can end whichever ones a test left running, together with the
  fakes' own registries (``FakeLiveWorker.live``, ``FakeHermesServer.live``). Tests that close their
  service and server properly lose nothing; the rest are cleaned up for them.
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

from fakes import FakeHermesServer, FakeLiveWorker
from speakeasy import calls as _calls
from speakeasy import tunnel as _tunnel

# Threads one test file may leave alive after the grace period. A few short-lived timers (a pending
# debounce, a 10 s settle timer) are normal; a leak per test grows far past this in one file.
FILE_LIMIT = 12
GRACE_S = 3.0

_watchers: list = []
_notices: list = []


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

    _tunnel.TunnelWatcher.start = start  # type: ignore[method-assign]
    _calls.Notices.__init__ = init  # type: ignore[method-assign]


def stop_leftovers() -> None:
    """End every tracked helper still running. Each stop is idempotent, so a test that already
    closed its service/server is unaffected."""
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

    def leaked(self) -> list[threading.Thread]:
        deadline = time.monotonic() + GRACE_S
        while True:
            left = [t for t in threading.enumerate() if t not in self.before and t.is_alive()]
            if len(left) <= FILE_LIMIT or time.monotonic() >= deadline:
                return left
            time.sleep(0.1)

    def problem(self, path: str) -> str | None:
        left = self.leaked()
        if len(left) <= FILE_LIMIT:
            return None
        names = collections.Counter(_label(t) for t in left).most_common(8)
        listed = ", ".join(f"{name} ×{n}" for name, n in names)
        return (f"{path} left {len(left)} background threads running after its tests "
                f"(limit {FILE_LIMIT}): {listed}. Stop the servers, call workers and executors it "
                "starts (close the service, server.stop(), worker.stop_loop()).")
