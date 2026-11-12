"""Tap → call live fast: the call.connect stage line, the pre-warmed Codex spare, and one Codex start
per tap when the phone retries while the server is still starting (Nov 11: 10-16 s connects, the
phone's 12 s watchdog retried, and the server started Codex voice twice)."""
from __future__ import annotations

import logging
import threading
import time

import pytest

from fakes import SDP, FakeLiveWorker, FakeTransport, wait_for
from speakeasy.codex_spare import CodexSpare, DeviceConnects
from speakeasy.connect_stages import ConnectStages
from speakeasy.service import ServiceError


class PrepTransport(FakeTransport):
    """A fake Codex transport with the real two-phase shape: prepare() (initialize + thread/start,
    the slow part) then start() (realtime only). *gate* holds prepare/start until released."""
    made: list["PrepTransport"] = []

    def __init__(self, prepare_gate: threading.Event | None = None, start_gate: threading.Event | None = None):
        super().__init__()
        self.prepare_gate, self.start_gate = prepare_gate, start_gate
        self.prepares = 0
        self.starts = 0
        self.stopped = False
        self.timings = {"spawn": 1}
        self.thread_id = None
        PrepTransport.made.append(self)

    def alive(self) -> bool:
        return not self.closed

    def prepare(self) -> None:
        if self.thread_id:
            return
        if self.prepare_gate is not None:
            assert self.prepare_gate.wait(10)
        self.prepares += 1
        self.thread_id = f"thr_{len(PrepTransport.made)}"
        self.timings.update(initialize=2, thread=3)

    def start(self, sdp, instructions, seed, voice):
        self.prepare()
        if self.start_gate is not None:
            assert self.start_gate.wait(10)
        self.starts += 1
        self.timings["realtime"] = 4
        return super().start(sdp, instructions, seed, voice)

    def stop(self) -> None:
        self.stopped = True
        super().stop()


@pytest.fixture(autouse=True)
def _reset():
    PrepTransport.made = []
    yield


def _svc(home, factory, spare=True):
    from speakeasy.service import VoiceService
    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=factory, codex_spare=spare)
    svc.settings.patch({"voice": {"provider": "codex"}})
    svc._start_worker = lambda interaction, provider, transport: FakeLiveWorker(svc.rt, interaction)
    return svc


# -- the stage line ------------------------------------------------------------------------------

def test_stages_line_names_every_stage_and_the_total():
    now = [100.0]
    stages = ConnectStages(clock=lambda: now[0])
    now[0] += .010; stages.mark("checks")
    now[0] += 1.5; stages.mark("prepare")
    stages.add({"spawn": 2, "thread": 1400}, prefix="voice.")
    stages.note("spare", "miss")
    now[0] += .5; stages.mark("voice")
    assert stages.line() == ("call.connect stages checks=10ms prepare=1500ms voice.spawn=2ms "
                             "voice.thread=1400ms voice=500ms spare=miss total=2010ms")


def test_a_codex_session_logs_one_call_connect_line(home, caplog):
    svc = _svc(home, PrepTransport, spare=False)
    try:
        with caplog.at_level(logging.INFO, logger="speakeasy.connect_stages"):
            svc.create_session({"sdp": SDP}, "req_stage_1", device_id="dev_a")
        lines = [r.getMessage() for r in caplog.records if "call.connect stages" in r.getMessage()]
        assert len(lines) == 1, lines
        line = lines[0]
        for part in ("checks=", "context=", "carry=", "instructions=", "discover=", "prepare=", "voice.thread=",
                     "voice=", "voice.realtime=", "admit=", "worker=", "spare=miss", "provider=codex"):
            assert part in line, (part, line)
        assert line.rstrip().split()[-1].startswith("total=") and line.endswith("ms")
    finally:
        svc.close()


def test_a_failed_start_still_logs_the_stages(home, caplog):
    class Broken(PrepTransport):
        def start(self, *a):
            raise RuntimeError("boom")
    svc = _svc(home, Broken, spare=False)
    try:
        with caplog.at_level(logging.INFO, logger="speakeasy.connect_stages"):
            with pytest.raises(ServiceError):
                svc.create_session({"sdp": SDP}, "req_stage_fail", device_id="dev_a")
        assert any("call.connect stages" in r.getMessage() and "outcome=failed" in r.getMessage()
                   for r in caplog.records)
    finally:
        svc.close()


# -- the pre-warmed spare ------------------------------------------------------------------------

def test_a_tap_uses_the_warm_spare_and_pays_only_the_realtime_start(home, caplog):
    svc = _svc(home, PrepTransport)
    try:
        svc.warm_codex(wait=True)
        assert len(PrepTransport.made) == 1 and PrepTransport.made[0].prepares == 1
        spare = PrepTransport.made[0]
        with caplog.at_level(logging.INFO, logger="speakeasy.connect_stages"):
            created = svc.create_session({"sdp": SDP}, "req_warm_1", device_id="dev_a")
        assert svc.interaction(created["interaction_id"]).worker is not None
        assert spare.starts == 1 and spare.prepares == 1          # nothing re-prepared on the tap
        assert created["session"]["id"] == spare.thread_id
        line = next(r.getMessage() for r in caplog.records if "call.connect stages" in r.getMessage())
        assert "spare=hit" in line and "voice.thread=" not in line
        # The next tap's spare is warmed in the background right away: one spare, never more.
        wait_for(lambda: len(PrepTransport.made) == 2 and PrepTransport.made[1].prepares == 1, timeout=5)
        assert svc.codex_spare.spare is PrepTransport.made[1]
        svc.create_session({"sdp": SDP + "a=x\r\n"}, "req_warm_2", device_id="dev_a")
        assert PrepTransport.made[1].starts == 1
        wait_for(lambda: len(PrepTransport.made) == 3, timeout=5)
        time.sleep(.2)
        assert len(PrepTransport.made) == 3 and svc.codex_spare.stats["hits"] == 2
    finally:
        svc.close()
    assert PrepTransport.made[-1].stopped  # closing the service stops the spare's app-server


def test_a_spare_s_idle_time_never_counts_toward_the_call_length(tmp_path, monkeypatch):
    """The 30-minute cap is measured from transport.started_at: a spare that waited 14 minutes
    must not end the call 14 minutes early."""
    from speakeasy import codex_transport as ct
    clock = [1000.0]
    monkeypatch.setattr(ct.time, "monotonic", lambda: clock[0])

    tr = ct.CodexTransport.__new__(ct.CodexTransport)
    import queue as _q
    tr.started_at, tr.thread_id, tr.timings = 1000.0, "thr", {}
    tr.notifications = _q.Queue(maxsize=512)
    clock[0] += 14 * 60
    def request(method, params=None, timeout=20):
        raise RuntimeError("stop here")
    tr.request = request
    tr.realtime_requested = False
    tr.binary = tmp_path / "codex"
    with pytest.raises(RuntimeError):
        tr.start("v=0", "", [], "juniper")
    assert tr.started_at == 1000.0 + 14 * 60


def test_a_spare_s_idle_notices_are_cleared_but_realtime_events_kept():
    import queue as _q
    from speakeasy import codex_transport as ct
    tr = ct.CodexTransport.__new__(ct.CodexTransport)
    tr.notifications = _q.Queue(maxsize=512)
    for m in ("thread/started", "mcpServer/startupStatus", "thread/realtime/error", "account/updated", "transport/eof"):
        tr.notifications.put_nowait({"method": m})
    tr._drop_idle_notifications()
    left = [tr.notifications.get_nowait()["method"] for _ in range(tr.notifications.qsize())]
    assert left == ["thread/realtime/error", "transport/eof"]


def test_no_spare_means_a_cold_start_and_a_spare_for_next_time(home):
    svc = _svc(home, PrepTransport)
    try:
        svc.create_session({"sdp": SDP}, "req_cold_1", device_id="dev_a")
        assert PrepTransport.made[0].starts == 1
        wait_for(lambda: svc.codex_spare.spare is not None, timeout=5)
    finally:
        svc.close()


def test_a_dead_or_old_spare_is_never_used():
    now = [0.0]
    made: list[PrepTransport] = []
    spare = CodexSpare(lambda key: made.append(PrepTransport()) or made[-1], max_age_s=60,
                       clock=lambda: now[0], background=False)
    spare.warm("k")
    made[0].closed = True                 # the app-server died while it waited
    assert spare.take("k") is None and made[0].stopped
    spare.warm("k")
    now[0] += 61                          # too old: a long-idle thread or sign-in may be stale
    assert spare.take("k") is None and made[1].stopped
    spare.warm("k")
    assert spare.take("k") is made[2]


def test_a_signin_or_codex_change_drops_the_spare_and_there_is_only_ever_one():
    made: list[PrepTransport] = []
    spare = CodexSpare(lambda key: made.append(PrepTransport()) or made[-1], background=False)
    spare.warm(("codex", 0))
    spare.warm(("codex", 0))
    assert len(made) == 1                 # already warm: no second app-server
    spare.warm(("codex", 1))              # sign-in epoch moved on
    assert len(made) == 2 and made[0].stopped and spare.spare is made[1]
    assert spare.take(("codex", 0)) is None and made[1].stopped   # a tap for another key never gets it
    spare.warm(("codex", 1))
    spare.discard("signed in")
    assert made[2].stopped and spare.spare is None


def test_a_tap_waits_for_a_warm_up_already_under_way():
    gate = threading.Event()
    made: list[PrepTransport] = []
    spare = CodexSpare(lambda key: made.append(PrepTransport(prepare_gate=gate)) or made[-1])
    spare.warm("k")
    got: list = []
    t = threading.Thread(target=lambda: got.append(spare.take("k")))
    t.start()
    time.sleep(.1)
    assert not got
    gate.set()
    t.join(5)
    assert got == [made[0]] and len(made) == 1
    spare.close()


def test_a_bad_spare_falls_back_to_one_cold_start(home):
    class Flaky(PrepTransport):
        def start(self, *a):
            if self is PrepTransport.made[0]:   # the spare
                raise TimeoutError("Codex realtime startup incomplete")
            return super().start(*a)
    svc = _svc(home, Flaky)
    try:
        svc.warm_codex(wait=True)
        created = svc.create_session({"sdp": SDP}, "req_flaky", device_id="dev_a")
        assert PrepTransport.made[0].stopped
        live = [t for t in PrepTransport.made if t.starts == 1]
        assert len(live) == 1 and created["session"]["id"] == live[0].thread_id
    finally:
        svc.close()


def test_a_login_status_flip_moves_the_signin_epoch(tmp_path):
    from speakeasy import codex_transport as ct
    binary = tmp_path / "codex"
    states = iter([0, 1, 1, 0])
    run = lambda *a, **k: type("R", (), {"returncode": next(states), "stdout": "Logged in using ChatGPT", "stderr": ""})()
    before = ct.signin_epoch(binary)
    ct.login_status(binary, runner=run, ttl_s=0)
    ct.login_status(binary, runner=run, ttl_s=0)      # signed out → epoch moves
    assert ct.signin_epoch(binary) == before + 1
    ct.login_status(binary, runner=run, ttl_s=0)      # unchanged
    ct.login_status(binary, runner=run, ttl_s=0)      # signed in again
    assert ct.signin_epoch(binary) == before + 2


# -- one Codex start per tap: a retry attaches ---------------------------------------------------

def test_a_retry_while_the_first_start_is_preparing_takes_it_over(home):
    gate = threading.Event()
    svc = _svc(home, lambda: PrepTransport(prepare_gate=gate), spare=False)
    first: list = []
    try:
        t = threading.Thread(target=lambda: first.append(_call(svc, SDP, "req_try_1")))
        t.start()
        wait_for(lambda: len(PrepTransport.made) == 1, timeout=5)
        second: list = []
        t2 = threading.Thread(target=lambda: second.append(_call(svc, SDP + "a=retry\r\n", "req_try_2")))
        t2.start()
        time.sleep(.2)
        gate.set()
        t.join(10); t2.join(10)
        assert len(PrepTransport.made) == 1, "the retry must not start a second Codex"
        only = PrepTransport.made[0]
        assert only.starts == 1 and only.started[0].endswith("a=retry\r\n")  # the retry's offer
        assert isinstance(first[0], ServiceError) and first[0].status == 409
        assert second[0]["session"]["id"] == only.thread_id
        assert not only.stopped
    finally:
        gate.set()
        svc.close()


def test_a_retry_after_the_voice_started_closes_the_first(home):
    start_gate = threading.Event()
    svc = _svc(home, lambda: PrepTransport(start_gate=start_gate), spare=False)
    first: list = []
    try:
        t = threading.Thread(target=lambda: first.append(_call(svc, SDP, "req_late_1")))
        t.start()
        wait_for(lambda: PrepTransport.made and PrepTransport.made[0].prepares == 1, timeout=5)
        time.sleep(.1)   # the first is inside realtime/start with the offer the phone dropped
        second: list = []
        t2 = threading.Thread(target=lambda: second.append(_call(svc, SDP + "a=r\r\n", "req_late_2")))
        t2.start()
        time.sleep(.2)
        start_gate.set()
        t.join(10); t2.join(10)
        assert isinstance(first[0], ServiceError) and first[0].status == 409
        assert PrepTransport.made[0].stopped, "two Codex voices must never run for one device"
        assert second[0]["session"]["id"] == PrepTransport.made[1].thread_id and not PrepTransport.made[1].stopped
    finally:
        start_gate.set()
        svc.close()


def test_different_devices_never_attach():
    connects = DeviceConnects()
    a, _ = connects.begin("phone")
    connects.track("phone", a)
    b, earlier = connects.begin("mac")
    assert earlier is None and connects.is_latest("phone", a) and connects.is_latest("mac", b)


def _call(svc, sdp, key):
    try:
        return svc.create_session({"sdp": sdp}, key, device_id="dev_phone")
    except ServiceError as exc:
        return exc
