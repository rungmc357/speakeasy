"""Mic-aware narration (issue #14): with the mic open the user hears a progress line at least every
UNMUTED_EVERY_S, news or not; muted or never-reported keeps the classic gate (news only, one per
PROGRESS_EVERY_S, nothing before PROGRESS_AFTER_S) exactly as origin/main has it.

Every test runs on a fake monotonic clock with stubbed wording calls, so the suite is fast and never
reaches a real model. That last part is load-bearing: an un-stubbed ``router.progress_update`` imports
``agent.auxiliary_client.call_llm`` from ~/.hermes/hermes-agent, whose ``hermes_bootstrap`` import
``os.execv``s the process into Hermes' own 3.14 interpreter. Under pytest that replaces the test
process mid-test (exit 1, no traceback, empty log) — the crash this file used to hit on its first test.

Acceptance criteria, as tagged on each test:
  AC1 unmuted 3-minute task with one early STATUS line: >= 4 lines, no two identical, none over 22 words
  AC2 first unmuted line lands between 12s and 20s, nothing before 12s
  AC3 muted and never-reported behave like origin/main: no line without news, nothing before 45s, one per 45s
  AC4 speech.progress = false: zero lines in every mic state
  AC5 toggling mute mid-task changes cadence on the next decision, no restart
  AC6 the route rejects non-bools with 400 and unknown interactions with 404
  AC7 urgent / needs-you events bypass the spacing in every mic state
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import os
import time

import pytest

from fakes import SDP, FakeLiveWorker, http
from speakeasy import calls, router
from speakeasy.calls import BackendRun, Interaction
from speakeasy.text import HEADS_UP_RE, derive_tool_status

_REAL_SLEEP = asyncio.sleep
_real_time = time.perf_counter  # the tests patch time.monotonic, never these two
_real_pause = time.sleep

MAX_WORDS = 22
BASE = 1000.0  # fake monotonic origin; never 0.0, because progress_spoken_at == 0.0 reads as "never spoke"
STEP = "Pulling this week's events"
STEP_DETAIL = "Pulling this week's events from the venue calendar"
URGENT = "Waiting on you to confirm"  # matches HEADS_UP_RE ("waiting on you", "confirm")
BROKEN = "Hitting a broken login page"  # matches HEADS_UP_RE ("broken")
MIC_STATES = [True, False, None]
QUIET_STATES = [True, None]  # muted, never reported


# -- hermeticity --------------------------------------------------------------------------------------

_MODEL_ENTRY_POINTS = ("progress_update", "plan_steps", "smart_title", "polish_request", "working_status",
                       "result_shape")


@pytest.fixture(autouse=True)
def hermetic(monkeypatch):
    """No test here may reach Hermes' auxiliary model. conftest stubs only ``router.aux_call``; the
    other ``call_llm`` callers in router.py are stubbed here to their documented "unavailable" value
    (None), and a tripwire turns any relaunch attempt into a named failure instead of a vanished
    process."""
    for name in _MODEL_ENTRY_POINTS:
        monkeypatch.setattr(router, name, lambda *a, **k: None)
    # Route tests start a call; with hermes_cli unimportable the key falls back to the environment.
    monkeypatch.setenv("SPEAKEASY_OPENAI_API_KEY", "sk-test-dummy")
    relaunched: list[str] = []

    def tripwire(path, args):  # pragma: no cover - only fires on a regression
        relaunched.append(str(path))
        pytest.fail(f"test reached a real model call: hermes_bootstrap tried to os.execv {path}")
    monkeypatch.setattr(os, "execv", tripwire)
    yield
    assert not relaunched, relaunched


class Clock:
    def __init__(self) -> None:
        self.t = BASE

    def at(self, seconds: float) -> None:
        self.t = BASE + seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(calls.time, "monotonic", lambda: c.t)
    return c


# -- wording stubs (what rt.progress_call returns) ----------------------------------------------------

class Cooperative:
    """A wording model that honours the contract: short, concrete, never repeats ``already_said``."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], list[str]]] = []

    def __call__(self, request, steps, told):
        self.calls.append((list(steps), list(told)))
        latest = " ".join(steps[-1].rstrip(".").split()[:10]) if steps else "the task"
        return f"Update {len(self.calls)}: {latest[0].lower() + latest[1:]}."


def no_model(request, steps, told):
    return None  # the wording model is unavailable: the daemon's own fallback speaks


def echo_model(request, steps, told):
    """A model that ignores already_said: the same steps always give the same words."""
    return f"I'm {steps[-1][0].lower() + steps[-1][1:].rstrip('.')}." if steps else None


def verbose_model(request, steps, told):
    """A model that ignores the length rule (the router prompt itself says only 'under 30 words')."""
    return ("Right now I'm still going through everything carefully, " + (steps[-1] if steps else "") +
            ", and after that I will compare every single option side by side before I come back to you.")


WRITERS = {"cooperative": Cooperative, "no_model": lambda: no_model, "echo_model": lambda: echo_model,
           "verbose_model": lambda: verbose_model}


# -- harness ------------------------------------------------------------------------------------------

def worker(service, muted=None, writer=None):
    service.rt.progress_call = writer if writer is not None else Cooperative()
    interaction = Interaction("int_x", "sess_x")
    interaction.mic_muted = muted
    return FakeLiveWorker(service.rt, interaction), interaction


def spoken(fake):
    return [c for k, _, c in fake.sent if k == "session.commentary.append"]


def running(age: float = 0.0, clock: Clock | None = None) -> BackendRun:
    backend = BackendRun("t1", 1, "idem_1", status="running")
    if clock is not None:
        backend.started = clock.t - age
    else:
        backend.started -= age
    return backend


def say(fake, backend, milestone=STEP):
    return asyncio.run(fake.maybe_speak_progress(backend, milestone))


def status_event(step: str, detail: str | None = None) -> dict:
    return {"event": "message.interim", "text": f"STATUS: {step}\nDETAIL: {detail or step}"}


def tool_event(tool: str = "terminal", preview: str = "") -> dict:
    return {"event": "tool.started", "tool": tool, "preview": preview}


class Sim:
    """Discrete-event fake time for one call. The only clock is ``Clock``; it moves only here, to the
    next scripted event or the next product ``asyncio.sleep`` wake-up, so the product's own narration
    heartbeat (whatever its tick) runs exactly as on a live call, minus the waiting."""

    def __init__(self, fake, backend, clock):
        self.fake, self.backend, self.clock = fake, backend, clock
        self.sleepers: list[tuple[float, int, asyncio.Future]] = []
        self.parked: dict[asyncio.Task, asyncio.Future] = {}
        self.seq = itertools.count()
        self.said: list[tuple[float, str]] = []

    def now(self) -> float:
        return round(self.clock.t - self.backend.started, 3)

    async def sleep(self, delay, result=None):
        if delay <= 0:
            await _REAL_SLEEP(0)
            return result
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self.sleepers, (self.clock.t + float(delay), next(self.seq), fut))
        task = asyncio.current_task()
        self.parked[task] = fut
        try:
            await fut
        finally:
            self.parked.pop(task, None)
        return result

    async def settle(self) -> None:
        """Run every task until each one is parked in a fake sleep or finished (wording calls run in
        a real thread, so this also waits for those, bounded in real time)."""
        me, deadline = asyncio.current_task(), _real_time() + 10
        while True:
            await _REAL_SLEEP(0)
            others = [t for t in asyncio.all_tasks() if t is not me and not t.done()]
            if all(t in self.parked and not self.parked[t].done() for t in others):
                return
            assert _real_time() < deadline, f"simulated call never settled: {others}"
            _real_pause(0.0005)

    def wake_due(self) -> None:
        while self.sleepers and self.sleepers[0][0] <= self.clock.t:
            _, _, fut = heapq.heappop(self.sleepers)
            if not fut.done():
                fut.set_result(None)

    async def run(self, script, until: float) -> None:
        async def _send(kind, delegation_id, content):
            self.fake.sent.append((kind, delegation_id, content))
            if kind == "session.commentary.append":
                self.said.append((self.now(), content))
        self.fake._send = _send
        with self.fake.interaction.lock:
            self.fake.interaction.runs[self.backend.delegation_id] = self.backend  # the task is on the call
        events = sorted(script, key=lambda pair: pair[0])
        while True:
            next_event = events[0][0] if events else float("inf")
            next_wake = self.sleepers[0][0] - self.backend.started if self.sleepers else float("inf")
            at = min(next_event, next_wake)
            if at > until:
                break
            self.clock.t = max(self.clock.t, self.backend.started + at)
            while events and events[0][0] <= at:
                _, event = events.pop(0)
                if callable(event):
                    event()  # e.g. the app reporting a mic toggle
                else:
                    asyncio.get_running_loop().create_task(self.fake.handle_hermes_event(self.backend, event))
                await self.settle()
            self.wake_due()
            await self.settle()
        # The task finishes: let the heartbeat see it and stop, then drop anything left.
        self.clock.t = self.backend.started + until
        self.status_at_end = self.backend.status
        self.backend.status = "completed"
        for _, _, fut in self.sleepers:
            if not fut.done():
                fut.set_result(None)
        self.sleepers.clear()
        await self.settle()
        for task in [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]:
            task.cancel()


def run_script(fake, backend, clock, script, until: float = 180.0):
    """Play ``(seconds, event)`` pairs (an event dict through the real ``handle_hermes_event``, or a
    callable such as a mic report) on simulated time with the product's heartbeat running, and return
    every spoken progress line as ``(seconds, line)``."""
    sim = Sim(fake, backend, clock)
    asyncio.sleep = sim.sleep
    try:
        asyncio.run(sim.run(script, until))
    finally:
        asyncio.sleep = _REAL_SLEEP
    backend.status = getattr(sim, "status_at_end", backend.status)  # as the task stood when the script ended
    return sim.said


def quiet_tool_work(start: float = 5, end: float = 180, every: float = 5) -> list[tuple[float, dict]]:
    """A task grinding away after its first STATUS line: tool calls, no further STATUS lines."""
    out, at, i = [], start, 0
    while at <= end:
        out.append((at, tool_event("read_file" if i % 3 == 0 else "terminal")))
        at, i = at + every, i + 1
    return out


def assert_distinct_and_short(lines: list[str]) -> None:
    repeated = sorted({line for line in lines if lines.count(line) > 1})
    long = [line for line in lines if len(line.split()) > MAX_WORDS]
    assert not repeated and not long, f"repeated lines: {repeated}; lines over {MAX_WORDS} words: {long}"


def origin_main_oracle(script, started_at: float = 0.0) -> list[float]:
    """origin/main's narration, computed independently of the code under test.

    origin/main (plugin/speakeasy/calls.py, maybe_speak_progress) consults the gate only on a
    ``message.interim`` STATUS event, and speaks iff: elapsed >= PROGRESS_AFTER_S, and either nothing
    was said yet / PROGRESS_EVERY_S passed since the last line / the STATUS matches HEADS_UP_RE, and
    there is news (activity_count > told_count). Activity is recorded from STATUS details and from
    allowlisted tool previews, with an identical consecutive detail de-duplicated."""
    activity: list[str] = []
    count = told = 0
    last: float | None = None
    said: list[float] = []
    for at, event in sorted(script, key=lambda pair: pair[0]):
        detail = step = None
        if event["event"] == "message.interim":
            lines = dict(line.split(": ", 1) for line in event["text"].splitlines())
            step, detail = lines["STATUS"], lines["DETAIL"]
        else:
            derived = derive_tool_status(event.get("tool"), event.get("preview"))
            detail = derived[1] if derived else None
        if detail and not (activity and activity[-1] == detail):
            activity.append(detail)
            count += 1
        if step is None:
            continue
        urgent = bool(HEADS_UP_RE.search(step))
        if (at - started_at < calls.PROGRESS_AFTER_S or (last is not None and at - last < calls.PROGRESS_EVERY_S
                                                         and not urgent) or count <= told):
            continue
        last, told = at, count
        said.append(at)
    return said


# -- AC1 + AC2: the unmuted scenario, end to end through the event handler and the heartbeat ---------

@pytest.mark.parametrize("work", ["tool_calls", "total_silence"])
@pytest.mark.parametrize("writer", list(WRITERS))
def test_unmuted_three_minute_task_with_one_early_status_keeps_talking(service, clock, writer, work):
    """AC1, AC2. One STATUS line at 3s, then three minutes of tool calls (or of nothing at all): a
    listening user hears the first line between 12s and 20s, then one at least every
    UNMUTED_EVERY_S — at least four in all, all different, none over 22 words — whatever the wording
    model does."""
    fake, _ = worker(service, muted=False, writer=WRITERS[writer]())
    backend = running(clock=clock)
    script = [(3, status_event(STEP, STEP_DETAIL))] + (quiet_tool_work() if work == "tool_calls" else [])
    said = run_script(fake, backend, clock, script)
    times, lines = [at for at, _ in said], [line for _, line in said]
    assert len(lines) >= 4, f"only {len(lines)} lines in three minutes: {said}"
    assert 12 <= times[0] <= 20, said
    assert all(at >= calls.UNMUTED_FIRST_S for at in times), said
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(calls.UNMUTED_EVERY_S <= gap <= calls.UNMUTED_EVERY_S + 5 for gap in gaps), gaps
    assert_distinct_and_short(lines)


@pytest.mark.parametrize("writer", list(WRITERS))
def test_unmuted_heartbeats_never_repeat_and_stay_short_whatever_the_wording_model_does(service, clock, writer):
    """AC1 on the gate itself (decisions every UNMUTED_EVERY_S, no news after the first step): four
    heartbeats must be four different lines of at most 22 words, even when the wording model is
    down, ignores already_said, or rambles."""
    fake, _ = worker(service, muted=False, writer=WRITERS[writer]())
    backend = running(clock=clock)
    backend.activity, backend.activity_count = [STEP_DETAIL], 1
    results = []
    for at in (13, 13 + calls.UNMUTED_EVERY_S, 13 + 2 * calls.UNMUTED_EVERY_S, 13 + 3 * calls.UNMUTED_EVERY_S):
        clock.t = backend.started + at
        results.append(say(fake, backend))
    lines = spoken(fake)
    assert results == [True] * 4 and len(lines) == 4, (results, lines)
    assert_distinct_and_short(lines)


def test_the_wording_model_is_told_what_was_already_said(service, clock):
    """AC1: the heartbeat hands the lines already spoken to the wording call, so it can avoid them."""
    writer = Cooperative()
    fake, _ = worker(service, muted=False, writer=writer)
    backend = running(clock=clock)
    backend.activity, backend.activity_count = [STEP_DETAIL], 1
    clock.t = backend.started + 13
    assert say(fake, backend) is True
    clock.t += calls.UNMUTED_EVERY_S
    assert say(fake, backend) is True
    assert writer.calls[1][1] == spoken(fake)[:1]


def test_unmuted_first_line_boundary_is_exactly_twelve_seconds(service, clock):
    """AC2: 11.9s is too early, 12.0s is not."""
    fake, _ = worker(service, muted=False)
    backend = running(clock=clock)
    backend.activity, backend.activity_count = [STEP_DETAIL], 1
    clock.t = backend.started + calls.UNMUTED_FIRST_S - 0.1
    assert say(fake, backend) is False
    clock.t = backend.started + calls.UNMUTED_FIRST_S
    assert say(fake, backend) is True
    assert calls.UNMUTED_FIRST_S == 12.0


def test_unmuted_speaks_after_twelve_seconds(service):
    """AC2."""
    fake, _ = worker(service, muted=False)
    backend = running(calls.UNMUTED_FIRST_S + 1)
    backend.activity, backend.activity_count = [STEP], 1
    assert say(fake, backend) is True
    assert len(spoken(fake)) == 1


def test_unmuted_still_quiet_before_twelve_seconds(service):
    """AC2."""
    fake, _ = worker(service, muted=False)
    backend = running(calls.UNMUTED_FIRST_S - 2)
    backend.activity, backend.activity_count = [STEP], 1
    assert say(fake, backend) is False
    assert spoken(fake) == []


def test_unmuted_never_speaks_twice_inside_the_spacing(service):
    """AC1 (cadence ceiling)."""
    fake, _ = worker(service, muted=False)
    backend = running(calls.UNMUTED_FIRST_S + 1)
    backend.activity, backend.activity_count = [STEP], 1
    assert say(fake, backend) is True
    backend.activity_count = 2
    assert say(fake, backend, "Comparing four venues") is False
    assert len(spoken(fake)) == 1


def test_unmuted_speaks_again_after_the_gap_with_no_news(service, clock):
    """AC1: nothing new for UNMUTED_EVERY_S, a listening user still hears a line."""
    fake, _ = worker(service, muted=False)
    backend = running(calls.UNMUTED_FIRST_S + 1, clock)
    backend.activity, backend.activity_count = [STEP], 1
    assert say(fake, backend) is True
    clock.t += calls.UNMUTED_EVERY_S
    assert say(fake, backend) is True
    assert len(spoken(fake)) == 2


def test_unmuted_heartbeat_is_optional_news_is_not_required(service):
    """AC1: a task whose activity is long stale still narrates while the mic is open."""
    fake, _ = worker(service, muted=False)
    backend = running(120)
    backend.activity, backend.activity_count, backend.told_count = ["Reading the calendar"], 1, 1
    assert say(fake, backend) is True
    assert spoken(fake)[0]


# -- AC3: muted / never reported = origin/main -------------------------------------------------------

def _classic_script() -> list[tuple[float, dict]]:
    statuses = [(3, "Pulling the calendar"), (20, "Reading venue pages"), (30, "Hitting a broken venue page"),
                (44, "Comparing four venues"),
                (46, "Checking ticket prices"), (47, "Checking ticket prices"), (60, "Reading reviews"),
                (70, "Reading reviews"), (92, "Shortlisting two venues"), (93, "Drafting the summary"),
                (100, "Hitting a broken booking page"), (140, "Checking the map"), (141, "Checking the map"),
                (150, "Checking the map"), (175, "Writing the answer")]
    script = [(at, status_event(step, f"{step} for the Friday show")) for at, step in statuses]
    return script + quiet_tool_work(start=2, end=180, every=5)


@pytest.mark.parametrize("muted", QUIET_STATES)
def test_muted_and_unknown_match_origin_main_on_the_same_script(service, clock, muted):
    """AC3: the same three-minute event stream speaks at exactly the moments origin/main would."""
    fake, _ = worker(service, muted=muted)
    backend = running(clock=clock)
    script = _classic_script()
    said = run_script(fake, backend, clock, script)
    times = [at for at, _ in said]
    assert times == origin_main_oracle(script), said
    assert times, "the script must exercise speaking, not only silence"
    assert all(at >= calls.PROGRESS_AFTER_S for at in times)
    steps = {at: event["text"] for at, event in script if event["event"] == "message.interim"}
    calm = [at for at, _ in said if not HEADS_UP_RE.search(steps[at])]
    assert all(b - a >= calls.PROGRESS_EVERY_S for a, b in zip(calm, calm[1:])), calm


def test_muted_and_unknown_say_exactly_the_same_thing(service, clock):
    """AC3: never-reported is indistinguishable from muted, line for line."""
    runs = {}
    for muted in QUIET_STATES:
        fake, _ = worker(service, muted=muted, writer=Cooperative())
        backend = running(clock=clock)
        runs[muted] = run_script(fake, backend, clock, _classic_script())
    assert runs[True] == runs[None]


@pytest.mark.parametrize("muted", QUIET_STATES)
def test_muted_task_with_one_early_status_and_quiet_tool_work_says_nothing(service, clock, muted):
    """AC3: origin/main never narrates tool calls, so one STATUS line plus three minutes of tool work
    is silent when muted or unreported."""
    fake, _ = worker(service, muted=muted)
    backend = running(clock=clock)
    script = [(3, status_event(STEP, STEP_DETAIL))] + quiet_tool_work()
    assert run_script(fake, backend, clock, script) == []
    assert origin_main_oracle(script) == []


@pytest.mark.parametrize("muted", QUIET_STATES)
@pytest.mark.parametrize("urgent", [URGENT, BROKEN])
def test_muted_heads_up_before_forty_five_seconds_stays_quiet_like_origin_main(service, clock, muted, urgent):
    """AC3 ("nothing before 45s") against AC7 ("bypass the spacing"): on origin/main a heads-up skips
    only the spacing, never the 45s first delay, so a muted or unreported call hears nothing at 20s."""
    fake, _ = worker(service, muted=muted)
    backend = running(clock=clock)
    script = [(20, status_event(urgent, f"{urgent} on the ticket site"))]
    assert origin_main_oracle(script) == []
    assert run_script(fake, backend, clock, script, until=40) == []


@pytest.mark.parametrize("muted", QUIET_STATES)
def test_muted_keeps_the_forty_five_second_first_line(service, clock, muted):
    """AC3: nothing before 45s even with news; at 45s it speaks."""
    fake, _ = worker(service, muted=muted)
    backend = running(clock=clock)
    backend.activity, backend.activity_count = [STEP], 1
    for at in (12, 20, 44.9):
        clock.t = backend.started + at
        assert say(fake, backend) is False, at
    clock.t = backend.started + calls.PROGRESS_AFTER_S
    assert say(fake, backend) is True


@pytest.mark.parametrize("muted", QUIET_STATES)
def test_muted_needs_news_even_after_the_gap(service, clock, muted):
    """AC3: no line without news, however long the silence."""
    fake, interaction = worker(service, muted=muted)
    backend = running(120, clock)
    backend.activity, backend.activity_count, backend.told_count = ["Reading the calendar"], 1, 1
    interaction.progress_spoken_at = clock.t - (calls.PROGRESS_EVERY_S + 1)
    assert say(fake, backend) is False
    assert spoken(fake) == []
    backend.activity_count = 2
    assert say(fake, backend, "Comparing four venues") is True


@pytest.mark.parametrize("muted", QUIET_STATES)
def test_muted_speaks_at_most_once_per_forty_five_seconds(service, clock, muted):
    """AC3: news every 5s for three minutes still gives one line per PROGRESS_EVERY_S."""
    fake, _ = worker(service, muted=muted)
    backend = running(clock=clock)
    script = [(at, status_event("Reading venue pages", f"Reading venue page number {at}"))
              for at in range(0, 181, 5)]
    times = [at for at, _ in run_script(fake, backend, clock, script)]
    assert times == [45, 90, 135, 180]


# -- AC4: the master switch --------------------------------------------------------------------------

@pytest.mark.parametrize("muted", MIC_STATES)
def test_progress_setting_off_yields_zero_lines_in_every_state(service, clock, muted):
    """AC4: speech.progress = false silences narration for a whole three-minute task, news, heartbeat
    and heads-up alike."""
    service.settings.patch({"speech": {"progress": False}})
    fake, _ = worker(service, muted=muted)
    backend = running(clock=clock)
    script = [(at, status_event("Reading venue pages", f"Reading venue page number {at}"))
              for at in range(3, 181, 10)]
    script += [(101, status_event(BROKEN)), (102, status_event(URGENT))] + quiet_tool_work()
    assert run_script(fake, backend, clock, script) == []
    assert spoken(fake) == []


def test_progress_setting_still_mutes_everything(service):
    """AC4 on the gate directly."""
    service.settings.patch({"speech": {"progress": False}})
    fake, _ = worker(service, muted=False)
    backend = running(120)
    backend.activity, backend.activity_count = [STEP], 1
    assert say(fake, backend) is False
    assert spoken(fake) == []


# -- AC5: toggling mid-task --------------------------------------------------------------------------

def _post_mic(server, interaction_id, muted):
    status, body = http(server.base_url, "POST", f"/voice/interactions/{interaction_id}/mic", {"muted": muted},
                        server.token)
    assert status == 200, body
    return body


def test_toggling_the_mic_mid_task_changes_cadence_on_the_next_decision(server, service, clock):
    """AC5, through the real route, on one worker and one task, heartbeat running: unmute and the
    next decision speaks on the 12s/no-news rule; mute and it needs news and 45s again; unmute and the
    heartbeat is back. Nothing is restarted between toggles."""
    session, live = start_call(server, service, key="req_mic_toggle")
    iid = session["interaction_id"]
    _post_mic(server, iid, True)
    interaction = service.interaction(iid)  # the very object the route updates
    service.rt.progress_call = Cooperative()
    fake = FakeLiveWorker(service.rt, interaction)
    backend = running(clock=clock)
    same = status_event(STEP, STEP_DETAIL)  # repeated verbatim = a decision with no news
    news = status_event("Comparing four venues", "Comparing four venues by price")

    def mic(muted):
        return lambda: _post_mic(server, iid, muted)
    script = [(3, same), (15, same),       # muted: under 45s, nothing
              (20, mic(False)),            # unmuted: past 12s, so the next decision speaks
              (30, mic(True)),
              (70, same),                  # muted: spacing passed, but no news: silent
              (75, news),                  # muted with news: speaks
              (80, mic(False)),
              (100, same), (110, same)]    # unmuted, inside the spacing: silent; the heartbeat speaks at 120
    said = run_script(fake, backend, clock, script, until=130)
    times = [at for at, _ in said]
    assert len(times) == 3, said
    assert 20 <= times[0] <= 25, said
    assert times[1] == 75, said
    assert 75 + calls.UNMUTED_EVERY_S <= times[2] <= 75 + calls.UNMUTED_EVERY_S + 5, said
    assert len(service.workers) == 1 and service.workers[0] is live  # no restart, same call throughout
    assert service.interaction(iid) is interaction and interaction.mic_muted is False


def test_unmuting_after_a_muted_silence_speaks_at_the_next_decision(service, clock):
    """AC5 on the gate: the mic is read at decision time, never cached at task start."""
    fake, interaction = worker(service, muted=True)
    backend = running(clock=clock)
    backend.activity, backend.activity_count, backend.told_count = [STEP], 1, 1
    clock.t = backend.started + 20
    assert say(fake, backend) is False
    interaction.mic_muted = False
    assert say(fake, backend) is True
    interaction.mic_muted = True
    clock.t += calls.PROGRESS_EVERY_S + 1
    assert say(fake, backend) is False  # muted again: spacing passed but no news


# -- AC6: the route ----------------------------------------------------------------------------------

def start_call(server, service, key="req_mic_1"):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


@pytest.mark.parametrize("muted", [True, False])
def test_mic_report_lands_on_the_interaction(server, service, muted):
    """AC6 (happy path) and AC5 (the state the gate reads)."""
    session, _ = start_call(server, service, key=f"req_mic_{muted}")
    body = _post_mic(server, session["interaction_id"], muted)
    assert body["mic_muted"] is muted
    snapshot = http(server.base_url, "GET", f"/voice/interactions/{session['interaction_id']}", token=server.token)[1]
    assert snapshot["mic_muted"] is muted


BAD_BODIES = [{"muted": "yes"}, {"muted": "false"}, {"muted": 1}, {"muted": 0}, {"muted": None}, {"muted": [True]},
              {}, {"muted": True, "extra": 1}, {"on": True}]


def test_mic_report_rejects_anything_but_a_bool(server, service):
    """AC6: every non-bool body is a 400 and leaves the interaction's mic state untouched (one call
    for all bodies: each started call costs ~1s of teardown)."""
    session, _ = start_call(server, service)
    answers = {}
    for body in BAD_BODIES:
        status, _ = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/mic", body,
                         server.token)
        answers[repr(body)] = status
    assert answers == {repr(body): 400 for body in BAD_BODIES}
    assert service.interaction(session["interaction_id"]).mic_muted is None


def test_mic_report_for_an_unknown_interaction_is_404(server, service):
    """AC6."""
    start_call(server, service)
    for interaction_id in ("se_missing", "int_never_started"):
        status, _ = http(server.base_url, "POST", f"/voice/interactions/{interaction_id}/mic", {"muted": True},
                         server.token)
        assert status == 404, interaction_id


# -- AC7: urgent / needs-you bypass the spacing ------------------------------------------------------

@pytest.mark.parametrize("muted", MIC_STATES)
@pytest.mark.parametrize("urgent", [URGENT, BROKEN])
def test_urgent_status_bypasses_the_spacing_in_every_state(service, clock, muted, urgent):
    """AC7: a heads-up lands ten seconds after the last line, inside any state's spacing."""
    assert HEADS_UP_RE.search(urgent)
    fake, _ = worker(service, muted=muted)
    backend = running(clock=clock)
    script = [(50, status_event("Reading venue pages", "Reading venue pages for Friday")),
              (60, status_event(urgent, f"{urgent} on the ticket site")),
              (65, status_event("Reading reviews", "Reading reviews now"))]  # calm again: held to the spacing
    said = run_script(fake, backend, clock, script, until=70)
    assert [at for at, _ in said] == [50, 60], said


@pytest.mark.parametrize("muted", MIC_STATES)
def test_an_approval_request_is_spoken_inside_the_spacing_in_every_state(service, clock, muted):
    """AC7: a needs-you approval reaches the voice immediately, whatever the mic and the spacing."""
    fake, _ = worker(service, muted=muted)
    backend = running(clock=clock)
    script = [(50, status_event("Reading venue pages", "Reading venue pages for Friday")),
              (55, {"event": "approval.request", "request_id": "req_pay_1",
                    "description": "Buy two tickets for 80 dollars"})]
    said = run_script(fake, backend, clock, script, until=58)
    assert [at for at, _ in said] == [50, 55], said
    assert backend.status == "waiting_for_approval"
