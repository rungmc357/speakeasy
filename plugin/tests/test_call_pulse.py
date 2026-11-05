"""The call pulse: a spoken line about every PULSE_EVERY_S while work runs and the call is quiet,
written from the task's real tool calls (activity.py). Invented data only."""
from __future__ import annotations

import asyncio
import time

import pytest

from fakes import http
from speakeasy import activity as A
from speakeasy import calls, router
from speakeasy.calls import BackendRun
from speakeasy.prompt import builder as P
from test_routing import _progress_worker, spoken, start_call

WALL = 1_800_000_000.0


class Clock:
    """Monotonic for calls.py; activity items are stamped on the wall clock (WALL + same offset)."""

    def __init__(self, monkeypatch):
        self.t = 1000.0
        monkeypatch.setattr(calls.time, "monotonic", lambda: self.t)
        monkeypatch.setattr(calls.time, "time", lambda: WALL + self.t)

    def wall(self) -> float:
        return WALL + self.t


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def setup(service, clock, writer=None, key="idem_p1"):
    worker = _progress_worker(service, writer if writer is not None else (lambda r, s, t: None))
    worker.connected_at = clock.t
    backend = BackendRun("t_p1", 1, key, status="running")
    backend.started = clock.t
    worker.interaction.runs[backend.delegation_id] = backend
    return worker, backend


def tool(service, backend, clock, name, preview, call_id, done=True, result="ok"):
    A.observe(service.store, backend.idem_key, {"event": "tool.started", "tool": name, "preview": preview,
                                                "tool_call_id": call_id}, now=clock.wall())
    if done:
        A.observe(service.store, backend.idem_key, {"event": "tool.completed", "tool": name, "preview": result,
                                                    "tool_call_id": call_id}, now=clock.wall() + 1)


def tick(worker):
    return asyncio.run(worker.pulse_tick())


def test_pulse_fires_at_45s_with_new_tool_activity_and_feeds_the_wording_model(service, clock):
    seen = []

    def writer(request, steps, told):
        seen.append(list(steps))
        return "I've gone through the checkout logs and I'm rerunning the test now."
    worker, backend = setup(service, clock, writer)
    clock.t += 10
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c1")
    tool(service, backend, clock, "search_files", "timeout", "c2")
    clock.t += 5
    tool(service, backend, clock, "terminal", "pytest -q tests/test_checkout.py", "c3", done=False)
    clock.t = worker.connected_at + calls.PULSE_EVERY_S - 1
    assert tick(worker) is False and spoken(worker) == []          # not yet
    clock.t = worker.connected_at + calls.PULSE_EVERY_S + 1
    assert tick(worker) is True
    assert spoken(worker) == ["I've gone through the checkout logs and I'm rerunning the test now."]
    steps = seen[0]
    # The tool calls themselves, in plain words with the tool and its argument, and what runs now.
    assert any(s.startswith("Read checkout.log (read_file") for s in steps)
    assert steps[-1].startswith("Right now (") and "Ran the tests (terminal: pytest" in steps[-1]


def test_wording_model_failure_falls_back_to_the_plain_sentences(service, clock):
    def broken(request, steps, told):
        raise TimeoutError
    worker, backend = setup(service, clock, broken)
    tool(service, backend, clock, "web_search", "hotel prices near the port", "c1")
    tool(service, backend, clock, "web_extract", "https://www.example.com/hotels", "c2")
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker) is True
    line = spoken(worker)[0]
    assert "example.com" in line and len(line.split()) <= 20


def test_nothing_new_says_the_tool_running_now_with_its_time(service, clock):
    worker, backend = setup(service, clock)
    tool(service, backend, clock, "terminal", "pytest -q", "c1", done=False)
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker)
    clock.t += 75  # nothing new for well over a minute: the running tool, and how long
    assert tick(worker)
    second = spoken(worker)[1]
    assert "running the tests" in second and "minutes in" in second
    assert not second.lower().startswith("still on it")


def test_nothing_running_and_nothing_new_is_at_most_the_task_name(service, clock):
    worker, backend = setup(service, clock)
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker) is False and spoken(worker) == []
    assert P.pulse_fallback([], None, 0, "Pricing check", 0) == "Still going on pricing check."
    assert P.pulse_fallback([], None, 0, None, 0) is None


@pytest.mark.parametrize("why", ["muted_app", "muted_relay", "user_speaking", "assistant_speaking", "approval",
                                 "no_task", "paused", "setting_off", "talking_only", "disconnected"])
def test_pulse_stays_quiet(service, clock, why):
    worker, backend = setup(service, clock, lambda r, s, t: "I read two files.")
    tool(service, backend, clock, "read_file", "a.py", "c1")
    clock.t += calls.PULSE_EVERY_S + 1
    if why == "muted_app":
        worker.interaction.mic_muted = True
    elif why == "muted_relay":
        worker.muted = True  # type: ignore[attr-defined]  # the Grok relay's own mute
    elif why == "user_speaking":
        worker.fragments.append({"speaker": "user", "text": "so what", "at": clock.t - 1})
    elif why == "assistant_speaking":
        worker.fragments.append({"speaker": "assistant", "text": "Here's", "at": clock.t - 1})
    elif why == "approval":
        backend.status = "waiting_for_approval"
    elif why == "no_task":
        backend.status = "completed"
    elif why == "paused":
        worker.interaction.paused = True
    elif why == "setting_off":
        service.settings.patch({"speech": {"progress": False}})
    elif why == "talking_only":
        worker.interaction.talking_only = True
    elif why == "disconnected":
        worker.connected = False
    assert tick(worker) is False
    assert spoken(worker) == []


def test_pulse_counts_from_when_the_assistant_last_spoke(service, clock):
    worker, backend = setup(service, clock, lambda r, s, t: "I read a file.")
    tool(service, backend, clock, "read_file", "a.py", "c1")
    clock.t += 40
    worker.fragments.append({"speaker": "assistant", "text": "Sure, one sec.", "at": clock.t})
    clock.t += 10  # 50 s into the call but only 10 s since it spoke
    assert tick(worker) is False
    clock.t += calls.PULSE_EVERY_S
    assert tick(worker) is True


def test_back_off_after_three_updates_with_nothing_new(service, clock):
    worker, backend = setup(service, clock)
    tool(service, backend, clock, "terminal", "make build", "c1", done=False)
    times = []
    end = clock.t + 600
    while clock.t < end:
        clock.t += calls.PULSE_TICK_S
        if tick(worker):
            times.append(clock.t)
    gaps = [b - a for a, b in zip(times, times[1:])]
    # The first update (the build starting is new), then PULSE_IDLE_AFTER with nothing new at the
    # normal spacing; after that, the longer one.
    n = calls.PULSE_IDLE_AFTER
    assert gaps[:n] == [pytest.approx(calls.PULSE_EVERY_S, abs=calls.PULSE_TICK_S)] * n
    assert gaps[n:] and all(g >= calls.PULSE_IDLE_EVERY_S for g in gaps[n:])
    # New activity resets it to the normal spacing.
    tool(service, backend, clock, "read_file", "build.log", "c2")
    clock.t = times[-1] + calls.PULSE_EVERY_S + 1
    assert tick(worker) is True and backend.pulse_idle == 0


def test_no_double_speak_with_commentary_progress(service, clock):
    worker, backend = setup(service, clock, lambda r, s, t: f"Update about {s[-1][:20]}")
    tool(service, backend, clock, "read_file", "a.py", "c1")
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker)
    clock.t += 5  # Hermes narrates right after: the per-call spacing holds it back
    event = {"event": "message.interim", "text": "STATUS: Checking the cart code\nDETAIL: Found the failing assertion in cart.py"}
    asyncio.run(worker._handle_hermes_event(backend, event))
    assert len(spoken(worker)) == 1
    # ...and once the commentary update speaks, the pulse waits a full interval after it.
    clock.t += calls.PROGRESS_EVERY_S
    asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim",
                                                      "text": "STATUS: Fixing the cart code\nDETAIL: Patched cart.py and rerunning"}))
    assert len(spoken(worker)) == 2
    clock.t += 10
    assert tick(worker) is False and len(spoken(worker)) == 2


def test_two_tasks_rotate_and_never_back_to_back(service, clock):
    worker, a = setup(service, clock, lambda r, s, t: f"{r or 'task'}: {len(s)} steps", key="idem_pa")
    b = BackendRun("t_p2", 1, "idem_pb", status="running")
    b.started = a.started
    worker.interaction.runs[b.delegation_id] = b
    said = []
    for minute in range(6):
        tool(service, a, clock, "read_file", f"a{minute}.py", f"a{minute}")
        tool(service, b, clock, "web_search", f"flights option {minute}", f"b{minute}")
        for _ in range(12):
            clock.t += calls.PULSE_TICK_S
            if tick(worker):
                said.append((clock.t, worker.sent[-1][1]))
    stamps = [t for t, _ in said]
    assert all(y - x >= calls.PULSE_EVERY_S for x, y in zip(stamps, stamps[1:]))
    assert {d for _, d in said} == {"t_p1", "t_p2"}  # both get heard


def test_muted_still_speaks_results(server, service, hermes):
    """Mute only switches the mic off: a finished task's answer is still spoken."""
    from fakes import wait_for
    hermes.responder = lambda prompt, sid: "Sunny tomorrow.\nDONE: Forecast\nSPOKEN: Sunny, 24 degrees tomorrow."
    session, worker = start_call(server, service)
    iid = session["interaction_id"]
    status, body = http(server.base_url, "POST", f"/voice/interactions/{iid}/mic", {"muted": True}, server.token)
    assert status == 200 and body["muted"] is True and worker.interaction.mic_muted
    assert http(server.base_url, "POST", f"/voice/interactions/{iid}/mic", {"muted": "yes"}, server.token)[0] == 400
    worker.delegate("call_m", "What's the weather in Lisbon tomorrow")
    wait_for(lambda: any("24 degrees" in c for c in spoken(worker)))


def test_muted_progress_from_commentary_is_held_but_heads_up_is_not(service, clock):
    worker, backend = setup(service, clock, lambda r, s, t: s[-1])
    worker.interaction.mic_muted = True
    clock.t += calls.PROGRESS_AFTER_S + 1
    asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim",
                                                      "text": "STATUS: Reading config files\nDETAIL: Reading the config files now"}))
    assert spoken(worker) == []
    worker.interaction.mic_muted = False  # unmuted, the same kind of update is said
    asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim",
                                                      "text": "STATUS: Checking the router\nDETAIL: Found the router bug"}))
    assert spoken(worker) == ["Found the router bug"]


def test_pulse_steps_marks_what_was_already_said():
    items = [{"n": 1, "text": "Read a.py", "tool": "read_file", "args": "a.py", "status": "done"},
             {"n": 2, "text": "Ran the tests", "tool": "terminal", "args": "pytest", "status": "running"}]
    steps = P.pulse_steps(items, {1}, [], items[1], 130)
    assert steps[0].startswith("Earlier: Read a.py")
    assert "Nothing new since the last update." in steps
    assert steps[-1].startswith("Right now (about two minutes in): Ran the tests")


def test_progress_prompt_takes_tool_calls():
    assert "tool calls" in router._PROGRESS_PROMPT and "Right now" in router._PROGRESS_PROMPT


def test_the_pulse_runs_on_its_own_during_a_live_call(server, service, hermes, monkeypatch):
    """End to end on a live call: nobody narrates, the task only makes tool calls, and the call
    still hears from it (timings shrunk; the rule is the same)."""
    from fakes import wait_for
    for name, value in (("PULSE_TICK_S", 0.05), ("PULSE_EVERY_S", 0.4), ("PROGRESS_AFTER_S", 0.1),
                        ("PULSE_QUIET_S", 0.05)):
        monkeypatch.setattr(calls, name, value)
    seen = []
    service.rt.progress_call = lambda request, steps, told: seen.append(steps) or "I'm looking up the forecast."
    hermes.hold = True
    hermes.live_events = [{"event": "tool.started", "tool": "web_search", "preview": "lisbon forecast tomorrow"}]
    _, worker = start_call(server, service)
    worker.delegate("call_w", "What's the weather in Lisbon tomorrow")
    wait_for(lambda: "I'm looking up the forecast." in spoken(worker), timeout=10)
    assert any("web_search" in step for step in seen[0])


@pytest.mark.parametrize("raw,said", [
    ("Ran X=Shop.app + 5 commands", "Ran a command"),
    ("Ran timeout 580 python poll_status.py 50 18", "Ran a command"),
    ("Ran the tests", "Ran the tests"),
    ("Read CartView.swift", "Read CartView.swift"),
    ("Used x", None),
])
def test_fallback_never_reads_out_a_command(raw, said):
    assert P.speakable(raw) == said


def test_fallback_folds_repeats_and_names_the_running_step():
    line = P.pulse_fallback(["Searched the code", "Searched the code"], "Ran the tests", 70, None, 0)
    assert line == "Quick update: searched the code; now running the tests."
    assert P.pulse_fallback([], "Built the project", 130, None, 0) == "Still building the project, about two minutes in."
