"""A Hermes event stream must never outlive its call's loop: its thread is a non-daemon asyncio
executor worker, and Python joins every executor worker at exit, so one stuck waiting on a loop kept
the test suite from exiting after its last test (and could hold up a gateway shutdown)."""
from __future__ import annotations

import asyncio
import threading
import time

import subprocess
import sys
from pathlib import Path

import pytest

from speakeasy import calls


def _loop_thread():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    while not loop.is_running():
        time.sleep(0.01)
    return loop, thread


async def _value():
    return 42


def test_on_loop_runs_the_coroutine_while_the_loop_runs():
    loop, thread = _loop_thread()
    try:
        assert calls.on_loop(_value(), loop) == 42
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()


def test_on_loop_gives_up_once_the_loop_has_stopped():
    loop, thread = _loop_thread()
    loop.call_soon_threadsafe(loop.stop)  # what the typed worker's stop() does on service close
    thread.join(2)
    started = time.monotonic()
    with pytest.raises(RuntimeError):
        calls.on_loop(_value(), loop)  # used to wait forever: the coroutine never runs
    assert time.monotonic() - started < calls.LOOP_GONE_POLL_S * 4
    loop.close()
    with pytest.raises(RuntimeError):
        calls.on_loop(_value(), loop)  # a closed loop fails at once, without an unawaited-coroutine warning


def test_a_stream_thread_ends_when_the_loop_stops_mid_run():
    """The real shape: the stream runs in the loop's executor and calls back for each event; the
    loop stops mid-run; the next event must end the stream instead of parking the worker for good."""
    loop, thread = _loop_thread()
    events, release, ended = [], threading.Event(), threading.Event()

    async def handle(event):
        events.append(event)

    def stream():
        try:
            calls.on_loop(handle("first"), loop)
            release.wait(5)
            calls.on_loop(handle("after the loop stopped"), loop)
        finally:
            ended.set()

    asyncio.run_coroutine_threadsafe(asyncio.to_thread(stream), loop)
    while not events:
        time.sleep(0.01)
    loop.call_soon_threadsafe(loop.stop)
    thread.join(2)
    release.set()
    assert ended.wait(calls.LOOP_GONE_POLL_S * 4)  # the worker is idle again: exit can join it
    assert events == ["first"]
    loop.close()


def test_python_exits_while_a_stream_waits_on_a_parked_loop():
    """The suite's hang, end to end: a loop that never runs the event's coroutine (it was parked in
    select() by a test's fake clock) and a stream thread in the loop's executor waiting on it."""
    script = """
import asyncio, threading, time
from speakeasy import calls
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
while not loop.is_running():
    time.sleep(0.01)
async def forever():
    await asyncio.Event().wait()
asyncio.run_coroutine_threadsafe(asyncio.to_thread(lambda: calls.on_loop(forever(), loop)), loop)
time.sleep(0.3)
print("main done", flush=True)
"""
    plugin = Path(__file__).resolve().parents[1]
    started = time.monotonic()
    out = subprocess.run([sys.executable, "-c", script], cwd=plugin, capture_output=True, text=True, timeout=30,
                         env={"PYTHONPATH": str(plugin), "HERMES_DISABLE_LAZY_INSTALLS": "1"})
    assert out.returncode == 0 and "main done" in out.stdout, out.stderr[-2000:]
    assert time.monotonic() - started < 15
