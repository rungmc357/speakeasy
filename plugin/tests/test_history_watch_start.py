"""A listener must not lose a change made before its watcher thread gets CPU time."""
import threading

import pytest

from fakes import wait_for
from speakeasy.service import HistoryEvents


@pytest.mark.parametrize("reopen", [False, True])
def test_first_change_is_not_swallowed_by_a_delayed_watcher(monkeypatch, reopen):
    stamp = [0]
    feed = HistoryEvents(lambda: stamp[0])
    feed.POLL_S = 0.01
    if reopen:
        feed.opened()
        previous = feed._watcher
        feed.closed()
        previous.join(timeout=2)
        assert not previous.is_alive()

    entered, release = threading.Event(), threading.Event()
    real_watch = feed._watch

    def delayed_watch():
        entered.set()
        assert release.wait(2), "test did not release watcher"
        real_watch()

    monkeypatch.setattr(feed, "_watch", delayed_watch)
    feed.opened()
    watcher = feed._watcher
    try:
        assert entered.wait(2)
        cursor = feed.seq
        stamp[0] += 1
        release.set()
        wait_for(lambda: feed.since(cursor), timeout=2)
        assert [kind for _, kind, _ in feed.since(cursor)] == ["history_changed"]
    finally:
        release.set()
        feed.closed()
        watcher.join(timeout=2)
        assert not watcher.is_alive()
