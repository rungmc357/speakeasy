"""SPK-565: background threads end when their owner closes, and the per-file thread guard catches a leak."""
from __future__ import annotations

import threading
import time

import thread_hygiene
from speakeasy.calls import Notices


class _Store:
    def __init__(self):
        self.claimed: set[str] = set()
        self.outcomes: dict[str, str] = {}

    def claim_notice(self, key):
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True

    def notice_outcome(self, key, outcome):
        self.outcomes[key] = outcome


class _SlowNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, target, text):
        time.sleep(0.05)
        self.sent.append((target, text))
        return True


def _notices():
    store, notifier = _Store(), _SlowNotifier()
    return Notices(store, notifier, lambda: "discord:general", lambda: None), store, notifier


def _wait(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.01)
    return pred()


def test_notice_sender_sends_what_is_queued_then_ends_when_closed():
    notices, store, notifier = _notices()
    for n in range(3):
        assert notices.post(f"k{n}", f"Still working on the report, part {n}")
    notices.close()
    notices.close()  # twice is fine
    assert _wait(lambda: not notices.thread.is_alive())
    assert [text for _, text in notifier.sent] == [f"Still working on the report, part {n}" for n in range(3)]
    assert store.outcomes == {"k0": "sent", "k1": "sent", "k2": "sent"}


def test_a_notice_posted_after_close_still_goes_out():
    notices, store, notifier = _notices()
    notices.close()
    assert _wait(lambda: not notices.thread.is_alive())
    assert notices.post("late", "The draft is ready")
    assert _wait(lambda: store.outcomes.get("late") == "sent")
    assert notifier.sent == [("discord:general", "The draft is ready")]
    assert not notices.post("late", "The draft is ready")  # still deduplicated


def test_service_close_ends_its_notice_sender(service):
    sender = service.notices.thread
    assert sender is not None and sender.is_alive()
    service.close()
    assert _wait(lambda: not sender.is_alive())


def test_file_guard_names_the_threads_a_file_leaves_behind(monkeypatch):
    monkeypatch.setattr(thread_hygiene, "GRACE_S", 0.2)
    guard = thread_hygiene.FileGuard()
    stop = threading.Event()
    leaked = [threading.Thread(target=stop.wait, name="speakeasy-leaky", daemon=True)
              for _ in range(thread_hygiene.FILE_LIMIT + 3)]
    for t in leaked:
        t.start()
    try:
        problem = guard.problem("tests/test_example.py")
        assert problem is not None
        assert "tests/test_example.py left" in problem and "speakeasy-leaky ×" in problem
    finally:
        stop.set()
        for t in leaked:
            t.join(2)
    assert guard.problem("tests/test_example.py") is None
