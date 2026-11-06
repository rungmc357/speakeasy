"""The call pulse: a spoken line about every PULSE_EVERY_S while work runs and the call is quiet,
written from the task's real tool calls (activity.py). Invented data only."""
from __future__ import annotations

import asyncio
import time

import pytest

from fakes import ModuleClock, http
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
        # calls.py's own clock only: patching time.monotonic itself froze every other test's loops.
        monkeypatch.setattr(calls, "time", ModuleClock(lambda: self.t, lambda: WALL + self.t))

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
    clock.t += calls.PULSE_IDLE_EVERY_S + 1  # nothing new: it waits longer, then the running tool and how long
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


def test_back_off_when_nothing_new(service, clock):
    worker, backend = setup(service, clock)
    tool(service, backend, clock, "terminal", "make build", "c1", done=False)
    times = []
    end = clock.t + 900
    while clock.t < end:
        clock.t += calls.PULSE_TICK_S
        if tick(worker):
            times.append(clock.t)
    gaps = [b - a for a, b in zip(times, times[1:])]
    # The first update (the build starting is new); then, with nothing new, longer and longer waits.
    assert gaps[0] >= calls.PULSE_IDLE_EVERY_S
    assert all(b >= a for a, b in zip(gaps, gaps[1:])) and gaps[-1] >= 2 * calls.PULSE_IDLE_EVERY_S
    assert all(g <= calls.PULSE_STALE_MAX_S + calls.PULSE_TICK_S for g in gaps)
    lines = spoken(worker)
    assert len({calls._said_key(x) for x in lines}) == len(lines)  # never the same words twice
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
        monkeypatch.setattr(calls, name, value)  # the default interval setting (45 s) means PULSE_EVERY_S
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


# -- with the mic open: an early first line, no silent back-off, at most PULSE_MAX_WORDS -------------

def run_quiet(worker, clock, seconds, step=calls.PULSE_TICK_S):
    """Tick the pulse for `seconds` of call time; (seconds into the run, line) for each line said."""
    start, said = clock.t, []
    while clock.t < start + seconds:
        clock.t += step
        if tick(worker):
            said.append((clock.t - start, spoken(worker)[-1]))
    return said


def test_open_mic_first_line_lands_12_to_20s_in(service, clock):
    worker, backend = setup(service, clock, lambda r, s, t: "I'm reading the checkout logs.")
    worker.interaction.mic_muted = False  # the app said the mic is on
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c1", done=False)
    said = run_quiet(worker, clock, 30, step=1.0)  # every second: nothing slips in before 12 s
    assert said and calls.PULSE_FIRST_OPEN_S <= said[0][0] <= 20
    assert said[0][1] == "I'm reading the checkout logs."
    # On the pulse loop's own tick it is the first tick past PULSE_FIRST_OPEN_S.
    worker, backend = setup(service, clock, lambda r, s, t: "I'm reading the checkout logs.", key="idem_p2")
    worker.interaction.mic_muted = False
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c2", done=False)
    first = run_quiet(worker, clock, 30)[0][0]
    assert calls.PULSE_FIRST_OPEN_S <= first <= 20


@pytest.mark.parametrize("mic", [None, True])
def test_never_reported_or_muted_keeps_the_45s_first_line(service, clock, mic):
    worker, backend = setup(service, clock, lambda r, s, t: "I'm reading the checkout logs.")
    worker.interaction.mic_muted = mic
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c1", done=False)
    said = run_quiet(worker, clock, 60, step=1.0)
    if mic is None:
        assert [t for t, _ in said] == [calls.PULSE_EVERY_S]
    else:
        assert said == []


@pytest.mark.parametrize("wording", ["raises", "echoes_told", "echoes_steps", "rambles"])
def test_open_mic_quiet_task_backs_off_and_never_repeats(service, clock, wording):
    def writer(request, steps, told):
        if wording == "raises":
            raise TimeoutError
        if wording == "echoes_told":
            return told[-1] if told else "I'm reading the build log."
        if wording == "echoes_steps":
            return steps[-1]
        return ("I'm still working through the build for the checkout service and it is taking a while because "
                "the dependency cache was cold so every package is being fetched and compiled again from scratch.")
    worker, backend = setup(service, clock, writer)
    worker.interaction.mic_muted = False
    tool(service, backend, clock, "terminal", "make build", "c1", done=False)  # one step, then nothing for 5 min
    said = run_quiet(worker, clock, 600)
    times, lines = [t for t, _ in said], [line for _, line in said]
    assert times[0] <= 20
    gaps = [b - a for a, b in zip(times, times[1:])]
    # Nothing new after the first line: it backs off instead of talking every interval.
    assert gaps and gaps[0] >= calls.PULSE_IDLE_EVERY_S and all(b >= a for a, b in zip(gaps, gaps[1:]))
    assert len(lines) <= 5
    assert all(len(line.split()) <= calls.PULSE_MAX_WORDS for line in lines)
    assert len({calls._said_key(x) for x in lines}) == len(lines)  # no two the same, openers aside
    # With nothing new, a later line is the running step and the task's age.
    later = lines[2:]
    assert all("building the project" in line.lower() and " in" in line for line in later)


def test_muted_and_never_reported_keep_mains_timing_and_back_off(service, clock):
    """Never reported: 45 s for the first line, then (nothing new) 90 s, 180 s... Muted: silent."""
    worker, backend = setup(service, clock)
    tool(service, backend, clock, "terminal", "make build", "c1", done=False)
    assert worker.interaction.mic_muted is None and not worker.mic_open()
    times = [t for t, _ in run_quiet(worker, clock, 600)]
    assert times == [45, 135, 315]
    worker, backend = setup(service, clock, key="idem_p2")
    worker.interaction.mic_muted = True
    tool(service, backend, clock, "terminal", "make build", "c2", done=False)
    assert run_quiet(worker, clock, 600) == []
    worker.interaction.mic_muted = False
    worker.muted = True  # type: ignore[attr-defined]  # the relay's own mute wins over the app's open mic
    assert not worker.mic_open() and run_quiet(worker, clock, 120) == []


def test_open_mic_line_is_capped_at_22_words():
    long = ("I've read the checkout logs, found that the payment step times out after thirty seconds, "
            "and I'm now rerunning the integration tests to confirm the fix. More after that.")
    assert len(calls.cap_words(long).split()) <= calls.PULSE_MAX_WORDS
    assert calls.cap_words("Reading the logs.") == "Reading the logs."
    shapes = calls.anchored_lines("Reading the logs", 185)
    assert len(set(shapes)) == len(calls._ANCHORED_SHAPES) and all("about 3 minutes" in s.lower() for s in shapes)
    # "Meanwhile, same line." is the same news: the next unsaid wording is the step and its age.
    assert calls.unrepeated("Same line.", ["Same line."], "reading the logs", 185).startswith("About 3 minutes")


def test_open_mic_model_that_repeats_itself_never_repeats_aloud(service, clock):
    """A wording model stuck on one sentence, on a task that keeps doing things for ten minutes: the
    open mic still never hears the same words twice, and never "Meanwhile, meanwhile"."""
    worker, backend = setup(service, clock, lambda r, s, t: t[-1] if t else "I'm reading the build log.")
    worker.interaction.mic_muted = False
    tool(service, backend, clock, "terminal", "make build", "c0", done=False)
    said = []
    for minute in range(10):
        tool(service, backend, clock, "read_file", f"src/part{minute}.py", f"c{minute + 1}")
        said += run_quiet(worker, clock, 60)
    lines = [line for _, line in said]
    assert len(lines) >= 600 // 120 and len({calls._said_key(x) for x in lines}) == len(lines)
    assert not any(line.lower().count("meanwhile") > 1 for line in lines)


def test_open_mic_first_line_that_was_held_back_still_lands_early(service, clock):
    """The first open-mic line is worded, then dropped at the last check because someone started
    talking. Nothing was said, so the task keeps its early first line: it is not pushed out to
    PULSE_EVERY_S after a line that never went out."""
    calls_made = []

    def writer(request, steps, told):
        calls_made.append(clock.t)
        if len(calls_made) == 1:
            worker.user_pending = True  # the user starts talking while the line is being worded
        return "I'm reading the checkout logs."

    worker, backend = setup(service, clock, writer)
    worker.interaction.mic_muted = False
    tool(service, backend, clock, "read_file", "/srv/shop/logs/checkout.log", "c1", done=False)
    start, said = clock.t, []
    while clock.t < start + 45 and not said:
        clock.t += calls.PULSE_TICK_S
        if tick(worker):
            said.append(clock.t - start)
        worker.user_pending = False  # ...and has stopped by the next tick
    assert len(calls_made) >= 2, "the first attempt was dropped and retried"
    assert said and said[0] <= 25


def test_open_mic_named_lines_keep_the_age_and_never_repeat(service, clock):
    """Two jobs running, so every line starts with the job's name. A long name plus a long step must
    still leave room for the age (which moves on between lines), so a job's stale lines never come
    out identical and never lose the age to the word cap."""
    worker, a = setup(service, clock, lambda r, s, t: t[-1] if t else "I'm working on it.", key="idem_pa")
    b = BackendRun("t_p2", 1, "idem_pb", status="running")
    b.started = a.started
    worker.interaction.runs[b.delegation_id] = b
    for key, title in (("idem_pa", "Rebuild the checkout service for the spring catalogue launch"),
                       ("idem_pb", "Compare the winter flight options for the family trip")):
        service.store.reserve_run(key, "int_1", "call_" + key[-4:], 0)
        service.store.set_title(key, title)
    worker.interaction.mic_muted = False
    tool(service, a, clock, "terminal", "make build of the checkout service and all of its payment plugins", "c1",
         done=False)
    tool(service, b, clock, "web_search", "winter flights for four people leaving from the nearest airport", "c2",
         done=False)
    said = run_quiet(worker, clock, 900)
    by_task: dict[str, list[str]] = {}
    for kind, say_id, line in worker.sent:
        if kind == "session.commentary.append":
            by_task.setdefault(say_id, []).append(line)
    assert len(said) == sum(map(len, by_task.values()))
    assert set(by_task) == {"t_p1", "t_p2"}
    for lines in by_task.values():
        assert all(len(line.split()) <= calls.PULSE_MAX_WORDS for line in lines)
        anchored = [line for line in lines if line.rstrip(".\u2026").endswith(" in") or ", still" in line]
        assert len(anchored) >= 2 and all("minute" in line.lower() for line in anchored)
        assert len(set(lines)) == len(lines)


def test_anchored_lines_leave_room_for_a_name():
    step = "running the integration tests for the payment and checkout services against the staging database"
    for reserve in (0, 8, 15):
        for line in calls.anchored_lines(step, 400, reserve):
            assert "about 7 minutes" in line.lower()  # the age is never what gets cut
            if reserve <= 8:
                assert len(line.split()) + reserve <= calls.PULSE_MAX_WORDS


# -- follow-up: the interval setting, the review nits, and the cases the first round didn't cover ----

def test_age_phrase_rounds_half_up():
    assert calls._age_phrase(150) == "about 3 minutes"  # round() would say 2 (banker's rounding)
    assert calls._age_phrase(149) == "about 2 minutes"
    assert calls._age_phrase(210) == "about 4 minutes"


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


def test_proper_nouns_come_from_mid_sentence_capitals():
    names = calls.proper_nouns("Can you ask Dana about the Q3 deck. Then email Priya")
    assert {"dana", "q3", "priya"} <= names and "can" not in names and "then" not in names
    lines = calls.anchored_lines("Acme invoices", 200, keep=calls.proper_nouns("check the Acme invoices"))
    assert any("Still on Acme invoices" in line or "still Acme invoices" in line for line in lines)
    assert not any("acme" in line for line in lines)


def test_unrepeated_all_said_says_nothing():
    """Every candidate already said: nothing (a repeat is worse than silence), whatever the order."""
    step, age = "reading the logs", 185
    candidates = ["Same line."] + [f"{o}, same line." for o in calls._PULSE_OPENERS] + calls.anchored_lines(step, age)
    # Said in this order in time, newest first in the list: the oldest is the last one.
    said = [(1000.0 - i, line) for i, line in enumerate(candidates)]
    assert calls.unrepeated("Same line.", said, step, age) == ""
    assert calls.unrepeated("Same line.", list(reversed(candidates)), step, age) == ""


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


@pytest.mark.parametrize("every", [30, 90, 120])
def test_open_mic_cadence_follows_the_interval_setting(service, clock, every):
    service.settings.patch({"speech": {"interval_seconds": every}})
    worker, backend = setup(service, clock, lambda r, s, t: "I'm reading the build log.")
    worker.interaction.mic_muted = False
    tool(service, backend, clock, "terminal", "make build", "c1", done=False)
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


def test_never_reported_call_follows_the_interval_too(service, clock):
    service.settings.patch({"speech": {"interval_seconds": 120}})
    worker, backend = setup(service, clock)
    tool(service, backend, clock, "terminal", "make build", "c1", done=False)
    times = [t for t, _ in run_quiet(worker, clock, 400)]
    assert times[0] == 120 and all(b - a >= 120 for a, b in zip(times, times[1:]))


def test_toggling_mute_mid_call(service, clock):
    """Open, then muted (silent), then open again: lines resume at the interval, never in a burst."""
    worker, backend = setup(service, clock, lambda r, s, t: "I'm reading the build log.")
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
    from test_routing import _progress_worker
    worker = _progress_worker(service)
    worker.interaction = old
    assert worker.mic_open()
    service.set_mic("int_head", {"muted": True})
    assert not worker.mic_open() and worker.muted_now()
    with pytest.raises(Exception):
        service.set_mic("int_head", {"muted": None})


def test_two_tasks_at_once_on_an_open_mic(service, clock):
    """Two tasks running: each gets its early first line, the call never stacks two lines inside
    the interval, and neither task repeats itself."""
    worker, a = setup(service, clock, lambda r, s, t: t[-1] if t else "I'm working on it.", key="idem_ta")
    b = BackendRun("t_p2", 1, "idem_tb", status="running")
    b.started = a.started
    worker.interaction.runs[b.delegation_id] = b
    worker.interaction.mic_muted = False
    tool(service, a, clock, "terminal", "make build", "c1", done=False)
    tool(service, b, clock, "web_search", "flights to lisbon", "c2", done=False)
    said = run_quiet(worker, clock, 400)
    by_task: dict[str, list[str]] = {}
    for kind, say_id, line in worker.sent:
        if kind == "session.commentary.append":
            by_task.setdefault(say_id, []).append(line)
    assert set(by_task) == {"t_p1", "t_p2"}
    assert all(len(set(lines)) == len(lines) for lines in by_task.values())
    times = [t for t, _ in said]
    assert all(y - x >= calls.PULSE_FIRST_OPEN_S for x, y in zip(times, times[1:]))


def test_progress_update_never_repeats_the_same_words(service, clock):
    """The quick recap (progress_tick) with a wording model that keeps saying the same thing while
    the task keeps doing things: said once, then silent until the words change."""
    worker, backend = setup(service, clock, lambda r, s, t: "Quick update: still comparing the two quotes.")
    tool(service, backend, clock, "web_search", "quote a", "c1")
    said = []
    for n in range(20):
        clock.t += calls.PULSE_TICK_S * 3
        tool(service, backend, clock, "read_file", f"quote{n}.pdf", f"r{n}")
        tick(worker)
    said = spoken(worker)
    assert len({calls._said_key(x) for x in said}) == len(said)


def test_stale_wait_doubles_up_to_the_cap():
    assert calls.stale_wait(45, 0) == calls.PULSE_IDLE_EVERY_S
    assert calls.stale_wait(45, 1) == 2 * calls.PULSE_IDLE_EVERY_S
    assert calls.stale_wait(45, 9) == calls.PULSE_STALE_MAX_S
    assert calls.stale_wait(120, 0) == 120
