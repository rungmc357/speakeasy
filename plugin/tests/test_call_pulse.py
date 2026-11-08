"""The call pulse: while work runs and the call is quiet, a short spoken line about it. The line is the
task card's own (store.work: the live line from live_status.py and its "So far" recap, or the task's own
STATUS and detail) and is said only when the card changed. Invented data only."""
from __future__ import annotations

import asyncio
import json

import pytest

from fakes import ModuleClock, http
from speakeasy import activity as A
from speakeasy import calls
from speakeasy import live_status as LS
from speakeasy import store as store_mod
from speakeasy.calls import BackendRun
from test_routing import _progress_worker, spoken, start_call

WALL = 1_800_000_000.0


class Clock:
    """Monotonic for calls.py; activity items, the store and the live line run on the wall clock
    (WALL + the same offset)."""

    def __init__(self, monkeypatch):
        self.t = 1000.0
        # calls.py's and store.py's own clocks only: patching time.monotonic itself froze every other
        # test's loops.
        monkeypatch.setattr(calls, "time", ModuleClock(lambda: self.t, lambda: WALL + self.t))
        monkeypatch.setattr(store_mod, "time", ModuleClock(lambda: self.t, lambda: WALL + self.t))

    def wall(self) -> float:
        return WALL + self.t


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


def setup(service, clock, key="idem_p1", delegation="t_p1", request="Find why the checkout test fails",
          title=None, model=None):
    """A running task with a real store row (the card the pulse reads), on a fake call. `model`: the
    side model's reply for the card's live line (None: the deterministic line from its steps)."""
    service.store.live_summarizer = LS.Summarizer(model, clock=clock.wall, spawn=lambda fn: fn(),
                                                  enabled=lambda: model is not None)
    worker = _progress_worker(service)
    worker.connected_at = clock.t
    service.store.reserve_run(key, "int_x", "call_" + key, 1)
    service.store.progress(key, "request", request)
    if title:
        service.store.set_title(key, title)
    service.store.update_run(key, "run_" + key, "running")
    backend = BackendRun(delegation, 1, key, status="running")
    backend.started = clock.t
    worker.interaction.runs[backend.delegation_id] = backend
    worker.interaction.latest_delegation_id = delegation  # the last thing talked about: no name needed
    return worker, backend


def tool(service, backend, clock, name, preview, call_id, done=True, result="ok"):
    A.observe(service.store, backend.idem_key, {"event": "tool.started", "tool": name, "preview": preview,
                                                "tool_call_id": call_id}, now=clock.wall())
    if done:
        A.observe(service.store, backend.idem_key, {"event": "tool.completed", "tool": name, "preview": result,
                                                    "tool_call_id": call_id}, now=clock.wall() + 1)


def tick(worker):
    return asyncio.run(worker.pulse_tick())


def card(service, backend):
    return service.store.work(idem_key=backend.idem_key)


def run_quiet(worker, clock, seconds, step=calls.PULSE_TICK_S):
    """Tick the pulse for `seconds` of call time; (seconds into the run, line) for each line said."""
    start, said = clock.t, []
    while clock.t < start + seconds:
        clock.t += step
        if tick(worker):
            said.append((clock.t - start, spoken(worker)[-1]))
    return said


# -- what is said: the card's line and recap ------------------------------------------------------

def test_the_spoken_line_is_the_cards_live_line_and_recap(service, clock):
    worker, backend = setup(service, clock)
    clock.t += 10
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c1")
    tool(service, backend, clock, "search_files", "timeout", "c2")
    clock.t += 5
    tool(service, backend, clock, "terminal", "pytest -q tests/test_checkout.py", "c3", done=False)
    clock.t = worker.connected_at + calls.PULSE_EVERY_S - 1
    assert tick(worker) is False and spoken(worker) == []          # not yet
    clock.t = worker.connected_at + calls.PULSE_EVERY_S + 1
    work = card(service, backend)
    assert tick(worker) is True
    said = spoken(worker)[0]
    # Word for word what the card shows: its line, then its "So far" recap.
    assert said == calls.spoken_card(work["now_line"], work["so_far"])
    assert said.startswith(work["now_line"] + ".") and "So far: " in said
    assert "pytest" not in said and "/srv" not in said


def test_the_side_models_card_line_is_what_is_said(service, clock):
    reply = json.dumps({"now": "Rerunning the checkout test to confirm the timeout",
                        "so_far": "Found the slow payment step in the logs"})
    worker, backend = setup(service, clock, model=lambda messages: reply)
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c1")
    tool(service, backend, clock, "terminal", "pytest -q tests/test_checkout.py", "c2", done=False)
    # The user opened the task (the only read that may ask the side model): its words are in.
    service.store.work(idem_key=backend.idem_key, activity_detail=True)
    clock.t += calls.PULSE_EVERY_S + 1
    assert card(service, backend)["now_line"] == "Rerunning the checkout test to confirm the timeout"
    assert tick(worker)
    assert spoken(worker) == ["Rerunning the checkout test to confirm the timeout. "
                              "So far: found the slow payment step in the logs."]


def test_a_tasks_own_status_is_said_as_the_card_shows_it(service, clock):
    """The task wrote STATUS/DETAIL: the card shows those words, and so does the voice."""
    worker, backend = setup(service, clock)
    clock.t += calls.PROGRESS_AFTER_S + 1
    asyncio.run(worker._handle_hermes_event(backend, {
        "event": "message.interim", "text": "STATUS: Checking the router\nDETAIL: Found the router bug"}))
    work = card(service, backend)
    assert (work["short_status"], work["detail"]) == ("Checking the router", "Found the router bug")
    assert spoken(worker) == ["Checking the router. Found the router bug."]
    # The pulse that comes next has nothing new to say: the card hasn't changed.
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker) is False and len(spoken(worker)) == 1


def test_no_card_line_yet_says_nothing(service, clock):
    """Started under a minute ago with no step reported: the card only has the handoff line."""
    worker, backend = setup(service, clock)
    clock.t += calls.PULSE_EVERY_S + 1
    assert card(service, backend).get("now_line") is None
    assert tick(worker) is False and spoken(worker) == []


def test_unchanged_card_stays_quiet_then_the_quiet_spell_is_said_with_back_off(service, clock):
    worker, backend = setup(service, clock)
    tool(service, backend, clock, "read_file", "cart.py", "c1")
    said = run_quiet(worker, clock, 110)  # same card line ("Reading cart.py") all along
    assert [line for _, line in said] == ["Reading cart.py."]
    # Two minutes with no step: the card says so ("No new steps · 2 min"), and so does the voice,
    # then only as the clock moves on, each time waiting longer.
    said = run_quiet(worker, clock, 900)
    lines, times = [line for _, line in said], [t for t, _ in said]
    assert lines[0] == "No new steps for about two minutes."
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps and gaps[0] >= calls.PULSE_IDLE_EVERY_S and all(b >= a for a, b in zip(gaps, gaps[1:]))
    assert all(g <= calls.PULSE_STALE_MAX_S + calls.PULSE_TICK_S for g in gaps)
    assert len({calls._said_key(x) for x in lines}) == len(lines)  # never the same words twice


def test_a_long_wait_on_one_step_follows_the_card(service, clock):
    worker, backend = setup(service, clock)
    tool(service, backend, clock, "terminal", "make build", "c1", done=False)
    said = run_quiet(worker, clock, 900)
    lines, times = [line for _, line in said], [t for t, _ in said]
    assert lines[0] == "Building the project." and times[0] == calls.PULSE_EVERY_S
    assert all(line.startswith("Still waiting on the build, about ") for line in lines[1:])
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(b >= a for a, b in zip(gaps[1:], gaps[2:])) and gaps[-1] >= 2 * calls.PULSE_IDLE_EVERY_S
    assert len(set(lines)) == len(lines)
    # The build finishes and the next step changes the card: back to the normal spacing.
    clock.t = worker.connected_at + 2000
    A.observe(service.store, backend.idem_key, {"event": "tool.completed", "tool": "terminal", "preview": "ok",
                                                "tool_call_id": "c1"}, now=clock.wall())
    tool(service, backend, clock, "read_file", "build.log", "c2")
    assert tick(worker) is True and backend.pulse_idle == 0
    assert spoken(worker)[-1].startswith("Reading build.log.")


def test_recap_is_only_said_when_it_changed(service, clock):
    worker, backend = setup(service, clock)
    tool(service, backend, clock, "read_file", "cart.py", "c1")
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker) and spoken(worker)[-1] == "Reading cart.py."  # "So far: read a file" adds nothing
    tool(service, backend, clock, "patch", "cart.py", "c2")
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker) and "So far: read a file, edited cart.py." in spoken(worker)[-1]
    tool(service, backend, clock, "read_file", "totals.py", "c3")
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker) and spoken(worker)[-1] == "Reading totals.py."  # same recap: not again


@pytest.mark.parametrize("why", ["muted_app", "muted_relay", "user_speaking", "assistant_speaking", "approval",
                                 "no_task", "paused", "setting_off", "talking_only", "disconnected"])
def test_pulse_stays_quiet(service, clock, why):
    worker, backend = setup(service, clock)
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
    worker, backend = setup(service, clock)
    tool(service, backend, clock, "read_file", "a.py", "c1")
    clock.t += 40
    worker.fragments.append({"speaker": "assistant", "text": "Sure, one sec.", "at": clock.t})
    clock.t += 10  # 50 s into the call but only 10 s since it spoke
    assert tick(worker) is False
    clock.t += calls.PULSE_EVERY_S
    assert tick(worker) is True


def test_no_double_speak_with_a_status_the_task_wrote(service, clock):
    worker, backend = setup(service, clock)
    tool(service, backend, clock, "read_file", "a.py", "c1")
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker)
    clock.t += 5  # Hermes narrates right after: the per-call spacing holds it back
    event = {"event": "message.interim", "text": "STATUS: Checking the cart code\nDETAIL: Found the failing assertion in cart.py"}
    asyncio.run(worker._handle_hermes_event(backend, event))
    assert len(spoken(worker)) == 1
    # ...and once the next status speaks, the pulse waits a full interval after it.
    clock.t += calls.PROGRESS_EVERY_S
    asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim",
                                                      "text": "STATUS: Fixing the cart code\nDETAIL: Patched cart.py and rerunning"}))
    assert spoken(worker)[-1] == "Fixing the cart code. Patched cart.py and rerunning."
    clock.t += 10
    assert tick(worker) is False and len(spoken(worker)) == 2


def test_two_tasks_rotate_and_never_back_to_back(service, clock):
    worker, a = setup(service, clock, key="idem_pa", title="Fix the cart totals")
    _, b = setup(service, clock, key="idem_pb", delegation="t_p2", title="Compare flight prices")
    worker.interaction.runs[b.delegation_id] = b
    b.started = a.started
    said = []
    for minute in range(6):
        tool(service, a, clock, "read_file", f"a{minute}.py", f"a{minute}")
        tool(service, b, clock, "web_search", f"flights option {minute}", f"b{minute}")
        for _ in range(12):
            clock.t += calls.PULSE_TICK_S
            if tick(worker):
                said.append((clock.t, worker.sent[-1][1], worker.sent[-1][2]))
    stamps = [t for t, _, _ in said]
    assert all(y - x >= calls.PULSE_EVERY_S for x, y in zip(stamps, stamps[1:]))
    assert {d for _, d, _ in said} == {"t_p1", "t_p2"}  # both get heard
    # Two jobs running: each line says which one it's about, and stays under the cap.
    for _, d, line in said:
        assert line.startswith("The cart totals: " if d == "t_p1" else "Flight prices: ")  # subject, never the title
        assert len(line.split()) <= calls.PULSE_MAX_WORDS


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
    # The answer travels through the fake agent, the task store and the voice relay on worker threads;
    # under a many-way parallel run that takes far longer than 10 s, so allow plenty and say what ran.
    try:
        wait_for(lambda: any("24 degrees" in c for c in spoken(worker)), timeout=60)
    except AssertionError:
        runs = [(r.delegation_id, r.status) for r in worker.interaction.runs.values()]
        raise AssertionError(f"muted answer not spoken; runs={runs} spoken={spoken(worker)}")


def test_muted_status_is_held_but_heads_up_is_not(service, clock):
    worker, backend = setup(service, clock)
    worker.interaction.mic_muted = True
    clock.t += calls.PROGRESS_AFTER_S + 1
    asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim",
                                                      "text": "STATUS: Reading config files\nDETAIL: Reading the config files now"}))
    assert spoken(worker) == []
    asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim",
                                                      "text": "STATUS: Fixing the broken helper\nDETAIL: The helper failed to start"}))
    assert spoken(worker) == ["Fixing the broken helper. The helper failed to start."]
    worker.interaction.mic_muted = False  # unmuted, a normal status is said once the spacing allows
    clock.t += calls.PROGRESS_EVERY_S + 1
    asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim",
                                                      "text": "STATUS: Checking the router\nDETAIL: Found the router bug"}))
    assert spoken(worker)[-1] == "Checking the router. Found the router bug."


def test_the_pulse_runs_on_its_own_during_a_live_call(server, service, hermes, monkeypatch):
    """End to end on a live call: nobody narrates, the task only makes tool calls, and the call
    still hears from it, in the card's words (timings shrunk; the rule is the same)."""
    from fakes import wait_for
    for name, value in (("PULSE_TICK_S", 0.05), ("PULSE_EVERY_S", 0.4), ("PROGRESS_AFTER_S", 0.1),
                        ("PULSE_QUIET_S", 0.05)):
        monkeypatch.setattr(calls, name, value)  # the default interval setting (45 s) means PULSE_EVERY_S
    hermes.hold = True
    hermes.live_events = [{"event": "tool.started", "tool": "web_search", "preview": "lisbon forecast tomorrow"}]
    _, worker = start_call(server, service)
    worker.delegate("call_w", "What's the weather in Lisbon tomorrow")
    wait_for(lambda: any("lisbon forecast" in line.lower() for line in spoken(worker)), timeout=10)
    line = next(line for line in spoken(worker) if "lisbon forecast" in line.lower())
    key = next(r.idem_key for r in worker.interaction.runs.values())
    assert line.startswith(service.store.work(idem_key=key)["now_line"])


# -- the open mic -----------------------------------------------------------------------------------

def test_open_mic_first_line_lands_12_to_20s_in(service, clock):
    worker, backend = setup(service, clock)
    worker.interaction.mic_muted = False  # the app said the mic is on
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c1", done=False)
    said = run_quiet(worker, clock, 30, step=1.0)  # every second: nothing slips in before 12 s
    assert said and calls.PULSE_FIRST_OPEN_S <= said[0][0] <= 20
    assert said[0][1] == "Reading checkout.log."  # a one-step recap ("read a file") only restates it
    worker, backend = setup(service, clock, key="idem_p2")
    worker.interaction.mic_muted = False
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c2", done=False)
    first = run_quiet(worker, clock, 30)[0][0]
    assert calls.PULSE_FIRST_OPEN_S <= first <= 20


@pytest.mark.parametrize("mic", [None, True])
def test_never_reported_or_muted_keeps_the_45s_first_line(service, clock, mic):
    worker, backend = setup(service, clock)
    worker.interaction.mic_muted = mic
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c1", done=False)
    said = run_quiet(worker, clock, 59, step=1.0)
    if mic is None:
        assert [t for t, _ in said] == [calls.PULSE_EVERY_S]
    else:
        assert said == []


def test_open_mic_first_line_that_was_held_back_still_lands_early(service, clock):
    """The first open-mic line is dropped at the last check because someone started talking. Nothing
    was said, so the task keeps its early first line: not pushed out to PULSE_EVERY_S."""
    worker, backend = setup(service, clock)
    worker.interaction.mic_muted = False
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c1", done=False)
    real = worker.card_update
    tries = []

    def card_update(*a, **kw):
        tries.append(clock.t)
        if len(tries) == 1:
            worker.user_pending = True  # the user starts talking while the line is being made
        return real(*a, **kw)
    worker.card_update = card_update  # type: ignore[method-assign]
    start, said = clock.t, []
    while clock.t < start + 45 and not said:
        clock.t += calls.PULSE_TICK_S
        if tick(worker):
            said.append(clock.t - start)
        worker.user_pending = False  # ...and has stopped by the next tick
    assert len(tries) >= 2, "the first attempt was dropped and retried"
    assert said and said[0] <= 25


@pytest.mark.parametrize("every", [30, 90, 120])
def test_cadence_follows_the_interval_setting(service, clock, every):
    service.settings.patch({"speech": {"interval_seconds": every}})
    worker, backend = setup(service, clock)
    worker.interaction.mic_muted = False
    tool(service, backend, clock, "terminal", "make build", "c1")
    times, n = [], 0
    start = clock.t
    while clock.t < start + 600:  # the task does something new every few seconds
        clock.t += calls.PULSE_TICK_S
        n += 1
        tool(service, backend, clock, "read_file", f"src/part{n}.py", f"r{n}")
        if tick(worker):
            times.append(clock.t - start)
    assert times[0] <= 20  # the early first line is not part of the setting
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert gaps and all(every <= g <= every + calls.PULSE_TICK_S for g in gaps)


def test_toggling_mute_mid_call(service, clock):
    """Open, then muted (silent), then open again: lines resume at the interval, never in a burst."""
    worker, backend = setup(service, clock)
    worker.interaction.mic_muted = False
    tool(service, backend, clock, "terminal", "make build", "c1", done=False)
    assert run_quiet(worker, clock, 60)
    worker.interaction.mic_muted = True
    assert run_quiet(worker, clock, 300) == []
    worker.interaction.mic_muted = False
    tool(service, backend, clock, "read_file", "build.log", "c2")  # something new happened meanwhile
    again = run_quiet(worker, clock, 100)
    assert again and again[0][0] <= calls.PULSE_TICK_S and len(again) <= 3
    assert all(b - a >= calls.PULSE_EVERY_S for (a, _), (b, _) in zip(again, again[1:]))


def test_set_mic_endpoint_and_resumed_head_reports(server, service):
    """POST .../mic {"muted": false} is what turns the open mic on; on a resumed call only the head
    interaction may have been told, and that is enough (and its mute wins too)."""
    from speakeasy.calls import Interaction
    old = Interaction("int_old", "sess_old")
    head = Interaction("int_head", "sess_head")
    old.successor = head
    service.interactions["int_head"] = head
    service.set_mic("int_head", {"muted": False})
    assert head.mic_muted is False and old.mic_muted is None
    worker = _progress_worker(service)
    worker.interaction = old
    assert worker.mic_open()
    service.set_mic("int_head", {"muted": True})
    assert not worker.mic_open() and worker.muted_now()
    with pytest.raises(Exception):
        service.set_mic("int_head", {"muted": None})


# -- the pieces -------------------------------------------------------------------------------------

@pytest.mark.parametrize("line,recap,so_far,said", [
    ("Editing store.py and router.py", "Read the code, edited 3 files", True,
     "Editing store.py and router.py. So far: read the code, edited 3 files."),
    ("Waiting on the tests \u00b7 4 min", None, True, "Still waiting on the tests, about four minutes now."),
    ("No new steps \u00b7 3 min", None, True, "No new steps for about three minutes."),
    ("No steps reported yet \u00b7 1 min", None, True, "Nothing reported yet, about a minute in."),
    ("Waiting on the build \u00b7 1 h 05 min", None, True, "Still waiting on the build, over an hour now."),
    ("Checking the router", "Found the router bug", False, "Checking the router. Found the router bug."),
    ("Reading cart.py", "Reading cart.py", True, "Reading cart.py."),
    ("", "Read a file", True, None),
])
def test_spoken_card(line, recap, so_far, said):
    assert calls.spoken_card(line, recap, so_far=so_far) == said


def test_a_long_recap_is_dropped_rather_than_cut():
    line = "Rerunning the integration tests for the payment service"
    recap = "Found the slow step in the checkout logs, patched the retry, rebuilt the service and reran the tests"
    assert calls.spoken_card(line, recap) == line + "."
    assert len(calls.spoken_card(line, recap, limit=5).split()) <= 5


def test_card_key_ignores_the_clock_only():
    assert calls.card_key("Waiting on the tests \u00b7 4 min") == calls.card_key("Waiting on the tests \u00b7 9 min")
    assert calls.card_key("Reading cart.py") != calls.card_key("Reading totals.py")
    assert calls.card_timed("No new steps \u00b7 2 min") and not calls.card_timed("Reading cart.py")


def test_line_cap_is_22_words():
    long = ("I've read the checkout logs, found that the payment step times out after thirty seconds, "
            "and I'm now rerunning the integration tests to confirm the fix. More after that.")
    assert len(calls.cap_words(long).split()) <= calls.PULSE_MAX_WORDS
    assert calls.cap_words("Reading the logs.") == "Reading the logs."


@pytest.mark.parametrize("text,keep,out", [
    ("Reading the logs", (), "reading the logs"),
    ("Stripe webhook", (), "Stripe webhook"),
    ("GitHub actions", (), "GitHub actions"),
    ("API keys", (), "API keys"),
    ("I'm reading", (), "I'm reading"),
    ("Dana's deck", ("dana",), "Dana's deck"),
    ("Monday plan", (), "Monday plan"),
])
def test_lead_lower_keeps_names(text, keep, out):
    assert calls._lead_lower(text, frozenset(keep)) == out


def test_interval_setting_validates_and_defaults(service):
    from speakeasy.settings import SettingsError
    assert service.settings.get()["speech"]["interval_seconds"] == 45
    for value in (30, 60, 90, 120):
        assert service.settings.patch({"speech": {"interval_seconds": value}})["speech"]["interval_seconds"] == value
    for bad in (10, 50, "60", True, None, 600):
        with pytest.raises(SettingsError):
            service.settings.patch({"speech": {"interval_seconds": bad}})


def test_settings_api_takes_the_interval(server, service):
    status, body = http(server.base_url, "PATCH", "/voice/settings", {"speech": {"interval_seconds": 90}}, server.token)
    assert status == 200 and service.settings.get()["speech"]["interval_seconds"] == 90
    status, _ = http(server.base_url, "PATCH", "/voice/settings", {"speech": {"interval_seconds": 75}}, server.token)
    assert status == 400


def test_stale_wait_doubles_up_to_the_cap():
    assert calls.stale_wait(45, 0) == calls.PULSE_IDLE_EVERY_S
    assert calls.stale_wait(45, 1) == 2 * calls.PULSE_IDLE_EVERY_S
    assert calls.stale_wait(45, 9) == calls.PULSE_STALE_MAX_S
    assert calls.stale_wait(120, 0) == 120


def test_a_thread_tasks_own_words_are_said_not_the_plumbing(service, clock):
    """A task continued in a chat thread has no run id: its card is "Working in the thread" with its own
    words under it. The voice says those words, never "Working in the thread"."""
    worker, backend = setup(service, clock, key="idem_th")
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE runs SET run_id=NULL WHERE idem_key=?", ("idem_th",))
    clock.t += calls.PROGRESS_AFTER_S + 1
    service.store.thread_progress("idem_th", "Found two cheaper flights on Tuesday")
    assert asyncio.run(worker.maybe_speak_progress(backend, "Found two cheaper flights on Tuesday"))
    assert spoken(worker) == ["Found two cheaper flights on Tuesday."]
