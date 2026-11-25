"""Volume ducking: the Mac's output volume drops while either side of a call speaks and comes back
after a quiet spell or when the call ends. Every test uses a fake volume backend; the suite-wide
``no_real_volume`` fixture keeps the real one out of every other test.

Negative assertions drain the worker with ``close()`` (it runs every queued step, then joins), so
"nothing happened" never races the worker thread. Quiet spells use a fake clock.
"""
from __future__ import annotations

import json
import subprocess
import threading

import pytest

from fakes import SDP, FakeLiveWorker, FakeTransport, http, wait_for
from speakeasy import duck
from speakeasy.duck import USER_CHANGE_TOLERANCE, Ducker, duck_config
from speakeasy.settings import DEFAULTS, Settings, SettingsError, patch_for


class FakeVolume:
    def __init__(self, volume: int = 60, muted: bool = False, clock=None):
        self.volume, self.muted = volume, muted
        self.clock = clock                          # when given, every set records the fake time
        self.readable = True
        self.settable = True
        self.raise_on_read = False
        self.reads = 0
        self.sets: list[int] = []
        self.set_at: list[tuple[int, float]] = []
        self.failed_sets = 0
        self.lock = threading.Lock()

    def read(self):
        with self.lock:
            self.reads += 1
            if self.raise_on_read:
                raise RuntimeError("audio stack exploded")
            return (self.volume, self.muted) if self.readable else None

    def set_volume(self, volume: int) -> bool:
        with self.lock:
            if not self.settable:
                self.failed_sets += 1
                return False
            self.volume = volume
            self.sets.append(volume)
            if self.clock is not None:
                self.set_at.append((volume, self.clock()))
            return True


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def settings_fn(enabled=True, level=30, hold_s=4.0):
    return lambda: {"audio_duck": {"enabled": enabled, "level": level, "hold_s": hold_s}}


@pytest.fixture
def make(tmp_path):
    duckers: list[Ducker] = []

    def build(backend, settings=None, clock=None, **kw):
        extra = {"clock": clock} if clock else {}
        duckers.append(Ducker(tmp_path, settings or settings_fn(**kw), backend, **extra))
        return duckers[-1]

    yield build
    for d in duckers:
        d.close()


def state_file(home):
    return home / "speakeasy" / "duck-state.json"


def write_state(home, state) -> None:
    state_file(home).parent.mkdir(parents=True, exist_ok=True)
    state_file(home).write_text(state if isinstance(state, str) else json.dumps(state))


def retry_scheduled(d: Ducker) -> bool:
    """The worker has scheduled its next restore try (the set failed and it set a new deadline)."""
    with d._cond:
        return d._deadline is not None


def duck_threads() -> int:
    return sum(t.name == "speakeasy-duck" and t.is_alive() for t in threading.enumerate())


# -- the duck cycle -----------------------------------------------------------------------------

def test_speech_ducks_then_the_volume_returns_after_a_quiet_spell(tmp_path, make):
    vol, clock = FakeVolume(60), FakeClock()
    d = make(vol, clock=clock, hold_s=0.5)
    d.note("user")
    wait_for(lambda: vol.sets == [30])
    assert json.loads(state_file(tmp_path).read_text()) == {"volume": 60, "ducked_to": 30}
    clock.now += 0.6                               # the quiet spell passes
    wait_for(lambda: vol.sets == [30, 60])
    assert not state_file(tmp_path).exists()


def test_continued_speech_keeps_one_duck_and_pushes_the_restore_out(make):
    vol, clock = FakeVolume(70), FakeClock()
    d = make(vol, clock=clock, hold_s=0.5)
    d.note("user")
    wait_for(lambda: vol.sets == [30])
    for _ in range(5):                             # 2 s of talk, a delta every 0.4 s: never quiet for 0.5 s
        clock.now += 0.4
        d.note("assistant")
    assert vol.sets == [30]
    clock.now += 0.6
    wait_for(lambda: vol.sets == [30, 70])


def test_call_end_restores_at_once(tmp_path, make):
    vol = FakeVolume(55)
    d = make(vol, hold_s=30)
    d.note("user", "call_a")
    wait_for(lambda: vol.sets == [30])
    d.restore("call_a")
    wait_for(lambda: vol.sets == [30, 55])
    d.restore("call_a")                            # a repeat restore is a no-op
    d.close()
    assert vol.sets == [30, 55]
    assert not state_file(tmp_path).exists()


def test_a_new_cycle_after_a_restore_ducks_again(make):
    vol = FakeVolume(80)
    d = make(vol, hold_s=30)
    d.note("user")
    wait_for(lambda: vol.sets == [30])
    d.restore()
    wait_for(lambda: vol.sets == [30, 80])
    d.note("assistant")
    wait_for(lambda: vol.sets == [30, 80, 30])


def test_one_call_ending_does_not_unduck_another_that_is_talking(make):
    clock = FakeClock()
    vol = FakeVolume(60, clock=clock)
    d = make(vol, clock=clock, hold_s=0.5)
    d.note("user", "call_a")
    d.note("assistant", "call_b")
    wait_for(lambda: vol.sets == [30])
    d.restore("call_a")                            # A hangs up while B is still talking
    hung_up_at = clock.now
    clock.now += 0.6                               # then B goes quiet
    wait_for(lambda: vol.sets == [30, 60])
    assert vol.set_at[-1][1] > hung_up_at          # restored by B's quiet spell, not by A hanging up


def test_the_last_talking_call_ending_restores_at_once(make):
    vol = FakeVolume(60)
    d = make(vol, hold_s=30)
    d.note("user", "call_a")
    d.note("user", "call_b")
    wait_for(lambda: vol.sets == [30])
    d.restore("call_a")
    d.restore("call_b")
    wait_for(lambda: vol.sets == [30, 60])


# -- when not to duck -----------------------------------------------------------------------------

@pytest.mark.parametrize("volume,muted", [(60, True), (30, False), (12, False)],
                         ids=["muted", "at-level", "below-level"])
def test_muted_or_already_quiet_output_is_left_alone(tmp_path, make, volume, muted):
    vol = FakeVolume(volume, muted)
    d = make(vol)
    d.note("user")
    d.close()
    assert vol.reads >= 1 and vol.sets == []
    assert not state_file(tmp_path).exists()


def test_an_unreadable_volume_is_never_ducked_and_warns_once(tmp_path, make, caplog):
    vol = FakeVolume(60)
    vol.readable = False
    d = make(vol, hold_s=30)
    d.note("user")
    d.restore()
    d.note("user")
    d.close()
    assert vol.sets == [] and not state_file(tmp_path).exists()
    assert sum("can't read the Mac's volume" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.parametrize("how", ["settings", "env"])
def test_turned_off(make, monkeypatch, how):
    if how == "env":
        monkeypatch.setenv("SPEAKEASY_VOICE_DUCK", "0")
    vol = FakeVolume(60)
    d = make(vol, enabled=how != "settings")
    d.note("user")
    d.close()
    assert vol.sets == []


def test_unreadable_settings_never_break_the_call(make):
    vol = FakeVolume(60)

    def boom():
        raise OSError("settings gone")

    d = make(vol, settings=boom)
    d.note("user")                                 # must not raise
    d.close()
    assert vol.sets == []


def test_no_backend_is_a_no_op(tmp_path):
    before = duck_threads()
    d = Ducker(tmp_path, settings_fn(), None)      # not macOS, or a CLI command
    assert duck_threads() == before                # no worker started
    d.note("user")
    d.restore()
    d.close()
    assert not state_file(tmp_path).exists()


def test_settings_are_read_once_per_cycle_and_refreshed_after_the_ttl(make):
    reads: list[bool] = []
    enabled = {"on": True}

    def counting():
        reads.append(True)
        return {"audio_duck": {"enabled": enabled["on"], "level": 30, "hold_s": 30}}

    vol, clock = FakeVolume(60), FakeClock()
    d = make(vol, settings=counting, clock=clock)
    for _ in range(200):
        d.note("user")
    assert len(reads) == 1                          # one read for the whole cycle
    wait_for(lambda: vol.sets == [30])
    d.restore()
    wait_for(lambda: vol.sets == [30, 60])
    enabled["on"] = False
    d.note("user")                                  # a new cycle inside the TTL reuses the cached read
    assert len(reads) == 1
    wait_for(lambda: vol.sets == [30, 60, 30])
    d.restore()
    wait_for(lambda: vol.sets == [30, 60, 30, 60])
    clock.now += duck.CONFIG_TTL_S + 1
    d.note("user")                                  # after the TTL: read again, and now it's off
    assert len(reads) == 2
    d.close()
    assert vol.sets == [30, 60, 30, 60]


# -- restore edge cases ---------------------------------------------------------------------------

def test_a_volume_left_ducked_by_a_crash_comes_back_on_start(tmp_path, make):
    write_state(tmp_path, {"volume": 64, "ducked_to": 30})
    vol = FakeVolume(30)
    make(vol)
    wait_for(lambda: vol.volume == 64)
    assert not state_file(tmp_path).exists()


def test_older_state_without_the_ducked_level_still_restores(tmp_path, make):
    write_state(tmp_path, {"volume": 48, "muted": False})
    vol = FakeVolume(30)
    make(vol)
    wait_for(lambda: vol.volume == 48)


@pytest.mark.parametrize("bad", ["not json", json.dumps({"volume": "loud"}), json.dumps({"volume": 400}),
                                 json.dumps({"volume": True}), json.dumps([1, 2])])
def test_a_corrupt_state_file_is_ignored(tmp_path, make, bad):
    write_state(tmp_path, bad)
    vol = FakeVolume(30)
    make(vol).close()
    assert vol.sets == []


def test_a_volume_the_user_changed_while_ducked_is_kept(tmp_path, make):
    vol = FakeVolume(60)
    d = make(vol, hold_s=30)
    d.note("user")
    wait_for(lambda: vol.sets == [30])
    vol.volume = 15                                 # the user turned it down further
    d.close()
    assert vol.volume == 15 and vol.sets == [30]
    assert not state_file(tmp_path).exists()


@pytest.mark.parametrize("drift,restored", [(USER_CHANGE_TOLERANCE, True), (USER_CHANGE_TOLERANCE + 1, False)])
def test_the_user_change_tolerance_boundary(make, drift, restored):
    vol = FakeVolume(60)
    d = make(vol, hold_s=30)
    d.note("user")
    wait_for(lambda: vol.sets == [30])
    vol.volume = 30 + drift                         # macOS reporting a step off, or the user moving it
    d.close()
    assert (vol.volume == 60) is restored


def test_muting_while_ducked_still_brings_the_level_back(make):
    vol = FakeVolume(80)
    d = make(vol, hold_s=30)
    d.note("user")
    wait_for(lambda: vol.sets == [30])
    vol.muted = True                                # muted mid-call: unmuting later must not be stuck at 30
    d.close()
    assert vol.volume == 80 and vol.muted           # level back; unmuting stays the user's call


def test_a_failed_restore_keeps_the_saved_volume_for_a_retry(tmp_path, make):
    vol = FakeVolume(60)
    d = make(vol, hold_s=30)
    d.note("user")
    wait_for(lambda: vol.sets == [30])
    vol.settable = False                            # osascript failed or timed out
    d.close()
    assert vol.volume == 30
    assert json.loads(state_file(tmp_path).read_text())["volume"] == 60
    vol.settable = True
    make(vol)                                       # the next start retries
    wait_for(lambda: vol.volume == 60)
    assert not state_file(tmp_path).exists()


def test_a_failed_timed_restore_is_retried_a_few_times(tmp_path, make, monkeypatch):
    monkeypatch.setattr(duck, "RESTORE_RETRY_S", 0.05)  # the worker's wait is real time
    vol, clock = FakeVolume(60), FakeClock()
    d = make(vol, clock=clock, hold_s=0.5)
    d.note("user")
    wait_for(lambda: vol.sets == [30])
    vol.settable = False
    clock.now += 0.6                                # the quiet spell ends, but the set fails
    wait_for(lambda: vol.failed_sets == 1 and retry_scheduled(d))
    vol.settable = True
    clock.now += duck.RESTORE_RETRY_S + 0.1         # no new speech: the retry puts it back
    wait_for(lambda: vol.sets == [30, 60])
    assert not state_file(tmp_path).exists()


def test_restore_retries_are_bounded(tmp_path, make, monkeypatch):
    monkeypatch.setattr(duck, "RESTORE_RETRY_S", 0.05)
    vol, clock = FakeVolume(60), FakeClock()
    d = make(vol, clock=clock, hold_s=0.5)
    d.note("user")
    wait_for(lambda: vol.sets == [30])
    vol.settable = False
    clock.now += 0.6
    for attempt in range(1, duck.RESTORE_RETRIES + 2):
        # The fake clock may only move on once the worker has set the retry's deadline: moved
        # earlier, the deadline lands after the new time and never arrives (SPK-596, a loaded host).
        wait_for(lambda: vol.failed_sets == attempt and (attempt > duck.RESTORE_RETRIES or retry_scheduled(d)))
        clock.now += duck.RESTORE_RETRY_S + 0.1
    vol.settable = True
    clock.now += 10 * duck.RESTORE_RETRY_S          # out of retries: it waits for the next restore
    d.close()                                       # ...which close() (or the next start) provides
    assert vol.failed_sets == duck.RESTORE_RETRIES + 1 and vol.sets == [30, 60]


def test_a_failed_duck_leaves_nothing_to_restore(tmp_path, make):
    vol = FakeVolume(60)
    vol.settable = False
    d = make(vol)
    d.note("user")
    d.close()
    assert not state_file(tmp_path).exists()


def test_an_unwritable_state_file_means_no_duck(tmp_path, make):
    (tmp_path / "speakeasy").write_text("a file where the directory should be")
    vol = FakeVolume(60)
    d = make(vol)
    d.note("user")
    d.close()
    assert vol.reads >= 1 and vol.sets == []


def test_the_worker_survives_a_backend_error(tmp_path, make):
    write_state(tmp_path, {"volume": 60, "ducked_to": 30})
    vol = FakeVolume(30)
    vol.raise_on_read = True                        # the startup restore blows up
    d = make(vol, hold_s=30)
    wait_for(lambda: vol.reads >= 1)
    vol.raise_on_read = False
    d.restore()                                     # the same worker still serves the next step
    wait_for(lambda: vol.volume == 60)


def test_close_restores_and_stops(tmp_path):
    before = duck_threads()
    vol = FakeVolume(60)
    d = Ducker(tmp_path, settings_fn(hold_s=30), vol)
    d.note("user")
    wait_for(lambda: vol.sets == [30])
    d.close()
    assert vol.volume == 60
    assert duck_threads() == before
    d.note("user")                                  # after close: ignored
    d.restore()
    d.close()
    assert vol.sets == [30, 60]


# -- the output device changes while ducked --------------------------------------------------------

class DeviceVolume(FakeVolume):
    """A fake volume that knows the default output device, and each device's own level."""

    def __init__(self, volume: int = 60, device: str = "BuiltInSpeakerDevice"):
        super().__init__(volume)
        self.device = device
        self.levels: dict[str, int] = {}
        self.gone: set[str] = set()

    def output_device(self):
        return self.device

    def set_device_volume(self, device: str, volume: int) -> bool:
        if device in self.gone:
            return False
        self.levels[device] = volume
        return True

    def switch(self, device: str, volume: int) -> None:
        with self.lock:
            self.device, self.volume = device, volume


def test_a_device_switch_while_ducked_restores_the_ducked_device_and_leaves_the_new_one(tmp_path, make):
    vol = DeviceVolume(60)
    d = make(vol, hold_s=30)
    d.note("assistant", "call_a")
    wait_for(lambda: vol.sets == [30])
    assert json.loads(state_file(tmp_path).read_text())["device"] == "BuiltInSpeakerDevice"
    vol.switch("AirPods-UID", 45)                  # AirPods connect mid-call
    d.restore("call_a")
    d.close()
    assert vol.levels == {"BuiltInSpeakerDevice": 60}, "the speakers get their volume back"
    assert vol.sets == [30] and vol.volume == 45, "the AirPods are left as they are"
    assert not state_file(tmp_path).exists()


def test_a_ducked_device_that_is_gone_just_ends_the_duck(tmp_path, make):
    vol = DeviceVolume(60, device="USB-Speakers")
    d = make(vol, hold_s=30)
    d.note("assistant")
    wait_for(lambda: vol.sets == [30])
    vol.gone.add("USB-Speakers")
    vol.switch("BuiltInSpeakerDevice", 50)         # unplugged: macOS falls back to the speakers
    d.restore()
    d.close()
    assert vol.sets == [30] and vol.volume == 50
    assert not state_file(tmp_path).exists()


def test_the_same_device_restores_as_before(tmp_path, make):
    vol = DeviceVolume(60)
    d = make(vol, hold_s=30)
    d.note("assistant")
    wait_for(lambda: vol.sets == [30])
    d.restore()
    wait_for(lambda: vol.sets == [30, 60])
    assert vol.levels == {}


def test_a_crash_restore_after_a_device_switch_targets_the_saved_device(tmp_path, make):
    write_state(tmp_path, {"volume": 70, "ducked_to": 30, "device": "BuiltInSpeakerDevice"})
    vol = DeviceVolume(40, device="AirPods-UID")
    d = make(vol)
    d.close()
    assert vol.levels == {"BuiltInSpeakerDevice": 70} and vol.sets == []
    assert not state_file(tmp_path).exists()


# -- only calls on this Mac ------------------------------------------------------------------------

class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key, default)


@pytest.mark.parametrize("peer,headers,local", [
    ("127.0.0.1", {}, True),
    ("::1", {}, True),
    ("127.0.0.1", {"X-Forwarded-For": "100.101.102.103"}, False),       # proxied from another device
    ("127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}, True),              # proxied, but from this Mac
    ("127.0.0.1", {"Tailscale-User-Login": "someone@example.com"}, False),
    ("127.0.0.1", {"X-Forwarded-For": "not an address"}, False),
    ("203.0.113.9", {}, False),
])
def test_same_machine(peer, headers, local):
    from speakeasy.server import same_machine
    assert same_machine(peer, _Headers(headers)) is local


# -- settings -------------------------------------------------------------------------------------

def test_settings_default_and_survive_other_patches(tmp_path):
    s = Settings(tmp_path)
    assert s.get()["audio_duck"] == DEFAULTS["audio_duck"] == {"enabled": False, "level": 30, "hold_s": 4.0}
    s.patch({"audio_duck": {"enabled": True}})
    s.patch({"audio_duck": {"level": 20}})
    s.patch({"assistant_name": "Nova"})           # an unrelated PATCH must not drop the duck settings
    assert s.get()["audio_duck"] == {"enabled": True, "level": 20, "hold_s": 4.0}
    s.patch(patch_for("audio_duck.enabled", "false"))
    assert s.get()["audio_duck"]["enabled"] is False
    s.patch({"audio_duck": {"level": 25.0, "hold_s": 2}})
    assert s.get()["audio_duck"] == {"enabled": False, "level": 25, "hold_s": 2.0}


@pytest.mark.parametrize("bad", [{"enabled": "yes"}, {"level": 101}, {"level": -1}, {"level": 30.5},
                                 {"level": True}, {"hold_s": 0.1}, {"hold_s": 61}, {"hold_s": "4"}])
def test_bad_settings_are_refused_with_the_field_named(tmp_path, bad):
    with pytest.raises(SettingsError, match="audio_duck"):
        Settings(tmp_path).patch({"audio_duck": bad})


def test_duck_config_reads_validated_settings(monkeypatch):
    assert duck_config({"audio_duck": {"enabled": True, "level": 25, "hold_s": 2.0}}) == (True, 25, 2.0)
    assert duck_config({}) == (False, 30, 4.0)  # off unless turned on
    monkeypatch.setenv("SPEAKEASY_VOICE_DUCK", "0")
    assert duck_config({"audio_duck": {"enabled": True, "level": 25, "hold_s": 2.0}})[0] is False


# -- the macOS backend ----------------------------------------------------------------------------

def test_the_real_backend_is_only_used_on_macos(monkeypatch, real_default_backend):
    monkeypatch.setattr(duck.sys, "platform", "linux")
    assert real_default_backend() is None
    monkeypatch.setattr(duck.sys, "platform", "darwin")
    assert isinstance(real_default_backend(), duck.OsascriptVolume)


def test_osascript_backend(monkeypatch):
    calls: list[list[str]] = []
    result: dict = {"proc": None, "exc": None}

    def fake_run(argv, **kw):
        calls.append(argv)
        assert kw["timeout"] == duck.OSASCRIPT_TIMEOUT_S
        if result["exc"]:
            raise result["exc"]
        return result["proc"]

    def ok(out: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0, out, "")

    monkeypatch.setattr(duck.subprocess, "run", fake_run)
    vol = duck.OsascriptVolume()
    result["proc"] = ok("output volume:42, input volume:missing value, alert volume:100, output muted:false")
    assert vol.read() == (42, False)
    result["proc"] = ok("output volume:missing value, input volume:50, alert volume:100, output muted:missing value")
    assert vol.read() is None                       # no output device
    result["proc"] = ok("")
    assert vol.set_volume(35) is True
    assert calls[-1] == ["osascript", "-e", "set volume output volume 35"]
    result["proc"] = subprocess.CompletedProcess([], 1, "", "execution error")
    assert vol.read() is None and vol.set_volume(35) is False
    for exc in (subprocess.TimeoutExpired("osascript", 2), FileNotFoundError("osascript")):
        result["exc"] = exc
        assert vol.read() is None and vol.set_volume(35) is False


# -- through a real call ---------------------------------------------------------------------------

@pytest.fixture
def ducked(home):
    """A VoiceService with a fake volume behind a paired server; yields (volume, start_call, stop)."""
    from speakeasy.server import SpeakeasyServer
    from speakeasy.service import VoiceService
    vol = FakeVolume(60)
    workers: list[FakeLiveWorker] = []

    def make_worker(rt, interaction):
        workers.append(FakeLiveWorker(rt, interaction))
        return workers[-1]

    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                       codex_login=lambda binary: (True, "Signed in to Codex."),
                       openai_negotiate=lambda key, payload: {"session": {"id": "sess_fake"},
                                                              "transport": {"sdp": "v=0\r\n"}},
                       openai_worker=make_worker, volume_backend=vol)
    svc.settings.patch({"voice": {"provider": "openai"}, "audio_duck": {"enabled": True, "hold_s": 30}})
    srv = SpeakeasyServer(svc, "127.0.0.1", 0)
    srv.start()
    _, paired = http(srv.base_url, "POST", "/voice/pair", {"code": svc.devices.new_pairing_code(),
                                                          "device_name": "Test Mac"})

    def start_call(key: str = "req_duck_1", headers: dict | None = None) -> FakeLiveWorker:
        status, _ = http(srv.base_url, "POST", "/voice/sessions", {"sdp": SDP}, paired["token"],
                         {"Idempotency-Key": key, **(headers or {})})
        assert status == 201
        return workers[-1]

    stopped: list[bool] = []

    def stop() -> None:
        if not stopped:
            stopped.append(True)
            srv.stop()

    yield vol, start_call, stop
    stop()


def say(worker, words, side="input"):
    worker.feed({"type": f"session.{side}_transcript.delta", "delta": words, "start_ms": 1, "end_ms": 2})


def test_a_call_ducks_while_the_assistant_talks_and_restores_on_hang_up(ducked, home):
    vol, start_call, _ = ducked
    worker = start_call()
    say(worker, "Sure", side="output")
    wait_for(lambda: vol.sets == [30])
    worker.feed({"type": "session.closed"})
    wait_for(lambda: vol.sets == [30, 60])
    assert not state_file(home).exists()


def test_the_user_talking_never_ducks(ducked):
    vol, start_call, stop = ducked
    worker = start_call()
    say(worker, "turn the music down a bit")
    say(worker, "and the lights")
    assert worker.fragments                        # the transcript still arrives
    stop()
    assert vol.sets == []


@pytest.mark.parametrize("headers", [{"X-Forwarded-For": "100.101.102.103"},     # an iPhone over the tailnet
                                     {"Tailscale-User-Login": "someone@example.com"},
                                     {"Forwarded": "for=100.101.102.103"}])
def test_a_call_from_another_device_never_ducks_this_mac(ducked, headers):
    vol, start_call, stop = ducked
    worker = start_call("req_duck_remote", headers)
    say(worker, "Sure, here's what I found", side="output")
    stop()
    assert vol.sets == []


def test_a_dropped_transport_restores_too(ducked):
    vol, start_call, _ = ducked
    worker = start_call()
    say(worker, "hello", side="output")
    wait_for(lambda: vol.sets == [30])
    worker.call_closed()                           # what both transports run when the connection ends
    wait_for(lambda: vol.sets == [30, 60])


def test_shutting_the_service_down_mid_call_restores(ducked):
    vol, start_call, stop = ducked
    say(start_call(), "hello", side="output")
    wait_for(lambda: vol.sets == [30])
    stop()                                         # live reload / gateway stop: service.close()
    assert vol.volume == 60


def test_cli_services_never_touch_the_volume(home, monkeypatch):
    """`hermes voice ...` builds a short-lived service (start_threads=False): it must not restore,
    at its start, a duck the gateway is holding mid-call."""
    from speakeasy.service import VoiceService
    built: list[bool] = []
    monkeypatch.setattr(duck, "default_backend", lambda: built.append(True) or FakeVolume())
    write_state(home, {"volume": 70, "ducked_to": 30})
    VoiceService(home, notifier=None, start_threads=False).close()
    assert built == [] and state_file(home).exists()


def test_calls_without_a_ducker_still_take_transcript(server, service):
    from test_routing import start_call
    _, worker = start_call(server, service)
    worker.rt.duck = None
    say(worker, "hello there")
    assert any(f["text"] == "hello there" for f in worker.fragments)
