"""snappy (latency plan #2 server, #4c): a call's accepted request is a Working card on Home at once,
before the settle and routing, and the run later fills in that same card (never a second one); the
pre-routing checks run alongside routing, and every stage is logged. Invented data only."""
from __future__ import annotations

import logging
import time

import pytest

from fakes import http, wait_for
from speakeasy import calls, router
from test_routing import start_call

REQUEST = "Find a quiet table for four near the harbour on Friday"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)


def items(server):
    return http(server.base_url, "GET", "/voice/now?tz=America/New_York", None, server.token)[1].get("items") or []


def handoff(worker, delegation_id, words):
    worker.feed({"type": "session.output_transcript.delta", "delta": "Okay.", "start_ms": 1, "end_ms": 2})
    worker.delegate(delegation_id, words)


def slow_route(gate, seen=None):
    def route(request, tasks, marked, chats=None, call_so_far="", replied_task_id=None, focus=None):
        if seen is not None:
            seen.append(time.monotonic())
        gate.wait(10)
        return router.fallback_decision(request)
    return route


def test_the_working_card_is_on_home_before_routing_finishes(server, service, hermes):
    import threading
    hermes.hold = True
    _, worker = start_call(server, service)
    gate = threading.Event()
    worker.rt.route = slow_route(gate)
    t0 = time.monotonic()
    handoff(worker, "call_table", REQUEST)
    held = wait_for(lambda: next((i for i in items(server) if i.get("handoff")), None), timeout=3)
    took = time.monotonic() - t0
    assert took < 1.0, took                       # on screen well inside a second of the "okay"
    assert held["kind"] == "working" and held["line"] == REQUEST
    assert held["id"].startswith("s:") and held["run_id"] is None
    assert held["on_current_call"] is True
    gate.set()                                     # routing returns: the task starts
    wait_for(lambda: hermes.calls)
    real = wait_for(lambda: next((i for i in items(server) if i.get("run_id")), None))
    assert real["id"] == held["id"]                # the run fills in the same card
    same = [i for i in items(server) if i["id"] == held["id"]]
    assert len(same) == 1 and not same[0].get("handoff")
    assert not [i for i in items(server) if i.get("handoff")]   # never a second card


def test_home_hears_about_it_on_the_feed_at_once(server, service, hermes):
    import threading
    hermes.hold = True
    _, worker = start_call(server, service)
    gate = threading.Event()
    worker.rt.route = slow_route(gate)
    before = service.history_events.seq
    handoff(worker, "call_table", REQUEST)
    wait_for(lambda: any(k == "now" for s, k, _ in service.history_events.events if s > before), timeout=2)
    gate.set()


def test_a_request_that_starts_nothing_leaves_no_card(server, service, hermes):
    _, worker = start_call(server, service)

    def ignore(request, tasks, marked, chats=None, call_so_far="", replied_task_id=None, focus=None):
        return router.Decision([router.Part(router.IGNORE, request)])

    worker.rt.route = ignore
    handoff(worker, "call_hmm", "and the other thing about the harbour")
    wait_for(lambda: any(k == "session.commentary.append" for k, _, _ in worker.sent))
    wait_for(lambda: not [i for i in items(server) if i.get("handoff")])
    assert not hermes.calls


def test_a_held_card_that_is_never_released_leaves_on_its_own(server, service, monkeypatch):
    from speakeasy import now as now_mod
    model = service.now_model
    model.hold_handoff("call_x", "s:task_abc", "se_abc", "Book the boat", None)
    assert [i for i in model.build(peek=True)["items"] if i.get("handoff")]
    monkeypatch.setattr(now_mod, "HANDOFF_KEPT_S", -1.0)
    assert not [i for i in model.build(peek=True)["items"] if i.get("handoff")]


def test_routing_runs_alongside_the_quick_lanes_and_each_stage_is_logged(server, service, hermes, caplog,
                                                                            monkeypatch):
    hermes.hold = True
    _, worker = start_call(server, service)
    started: list[float] = []
    quick_started: list[float] = []

    def route(request, tasks, marked, chats=None, call_so_far="", replied_task_id=None, focus=None):
        started.append(time.monotonic())
        time.sleep(0.4)
        return router.fallback_decision(request)

    async def slow_quick(self, delegation_id, revision, request):
        quick_started.append(time.monotonic())
        import asyncio
        await asyncio.sleep(0.4)
        return False

    worker.rt.route = route
    monkeypatch.setattr(calls.SidebandWorker, "quick_answer", slow_quick)
    caplog.set_level(logging.INFO, logger="speakeasy")
    caplog.set_level(logging.INFO)
    t0 = time.monotonic()
    handoff(worker, "call_table", REQUEST)
    wait_for(lambda: hermes.calls)
    took = time.monotonic() - t0
    assert started and quick_started
    assert abs(started[0] - quick_started[0]) < 0.3     # side by side, not one after the other
    assert took < 0.2 + 0.4 + 0.4, took                 # settle + max(quick, route), never the sum
    line = next(r.getMessage() for r in caplog.records if "handoff stages" in r.getMessage())
    for stage in ("settle=", "reuse=", "quick=", "candidates=", "route=", "total="):
        assert stage in line, line


def test_a_quick_answer_drops_the_routing_it_did_not_need(server, service, hermes, monkeypatch):
    _, worker = start_call(server, service)

    async def answered(self, delegation_id, revision, request):
        await self.append("session.commentary.append", delegation_id, "It's three o'clock.")
        return True

    monkeypatch.setattr(calls.SidebandWorker, "quick_answer", answered)
    handoff(worker, "call_time", "what time is it in Lisbon")
    wait_for(lambda: any(c == "It's three o'clock." for k, _, c in worker.sent))
    time.sleep(0.3)
    assert not hermes.calls                              # the routing decision went nowhere
    wait_for(lambda: not [i for i in items(server) if i.get("handoff")])
