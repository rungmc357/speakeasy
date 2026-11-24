"""SPK-565: background threads end when their owner closes, and the per-file thread guard catches a leak."""
from __future__ import annotations

import threading
import time

import thread_hygiene
from fakes import FakeTransport
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


def test_service_close_ends_its_notice_and_push_senders(home):
    from speakeasy.service import VoiceService
    service = VoiceService(home, notifier=None, start_threads=True, codex_factory=FakeTransport,
                           codex_login=lambda binary: (True, "Signed in to Codex."))
    senders = [service.notices.thread, service.push.thread]
    assert all(t is not None and t.is_alive() for t in senders)
    service.close()
    assert _wait(lambda: not any(t.is_alive() for t in senders))


def test_push_sender_sends_what_is_queued_then_ends_when_closed():
    from speakeasy.push import PushNotifier
    sent: list[str] = []

    class Relay:
        def send(self, payload):
            sent.append(payload["kind"])
            return {"ok": True}

    push = PushNotifier(devices=None, settings=lambda: {}, relay=Relay())
    push._send = lambda device_id, payload, kind=None: push.relay.send(payload)  # the relay call only
    for n in range(2):
        push.queue.put(("dev", {"kind": f"wake {n}"}, "result"))
    push.close()
    assert _wait(lambda: not push.thread.is_alive())
    assert sent == ["wake 0", "wake 1"]


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


def test_run_guard_catches_a_slow_creep_no_single_file_trips(monkeypatch):
    monkeypatch.setattr(thread_hygiene, "GRACE_S", 0.2)
    stop = threading.Event()
    earlier = [threading.Thread(target=stop.wait, name="speakeasy-creep", daemon=True)
               for _ in range(thread_hygiene.RUN_LIMIT)]
    for t in earlier:
        t.start()
    try:
        guard = thread_hygiene.FileGuard()  # this file starts nothing; earlier ones left the pile
        problem = guard.problem("tests/test_example.py")
        assert problem is not None and "for the whole run" in problem and "speakeasy-creep ×" in problem
    finally:
        stop.set()
        for t in earlier:
            t.join(2)
