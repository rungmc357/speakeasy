"""Pytest wiring: fake Hermes, temp HERMES_HOME, a VoiceService with a fake live worker, a live server."""
from __future__ import annotations

import errno
import functools
import inspect
import os
import socket
import subprocess

import pytest

import offline_model
import process_state
import temp_homes
from fakes import FakeHermesServer, FakeLiveWorker, FakeTransport, make_home, http  # noqa: F401
from speakeasy import delivery as _delivery
from speakeasy import duck as _duck
from speakeasy import router as _router

_REAL_DEFAULT_BACKEND = _duck.default_backend
_REAL_RESULT_HEADLINE = _router.result_headline


def pytest_sessionstart(session):
    """Clear temp homes a killed or timed-out earlier run left behind (this run's own go at exit)."""
    temp_homes.sweep()


@pytest.fixture
def real_result_headline():
    """The unpatched side-model headline call, for tests that fake the model it calls."""
    return _REAL_RESULT_HEADLINE


@pytest.fixture(autouse=True)
def no_real_routing_model(monkeypatch):
    """Never call the user's real auxiliary model from tests: routing falls back to the rules
    unless a test passes its own ``route_call``."""
    from speakeasy import router
    monkeypatch.setattr(router, "aux_call", lambda messages, timeout=router.ROUTE_TIMEOUT_S: None)
    monkeypatch.setattr(router, "routing_model", lambda config=None: "Hermes auxiliary default (auto)")
    monkeypatch.setattr(router, "_title_llm", lambda messages, timeout: None)
    monkeypatch.setattr(router, "_hermes_title", lambda text, timeout: None)
    monkeypatch.setattr(router, "result_headline", lambda request, report, timeout=10.0: None)
    # Task metadata uses the title model directly, not aux_call/_title_llm. Leaving these live
    # lets a fake task probe the host's providers while holding the shared profile scope lock;
    # even an unrelated pair/status request can then time out waiting for that lock.
    monkeypatch.setattr(router, "polish_request", lambda request, timeout=10.0: None)
    monkeypatch.setattr(router, "working_status", lambda request, timeout=10.0: None)
    monkeypatch.setattr(router, "plan_steps", lambda request, timeout=10.0: None)
    monkeypatch.setattr(router, "result_shape", lambda *args, **kwargs: None)
    monkeypatch.setattr(router, "verdict_rewrite", lambda request, report, line, timeout=10.0: None)


@pytest.fixture(autouse=True)
def no_real_volume(monkeypatch):
    """Never touch the Mac's real output volume from tests: services get no volume backend unless a
    test passes its own ``volume_backend``."""
    monkeypatch.setattr(_duck, "default_backend", lambda: None)


# Never launch the Hermes CLI from a test: ``hermes send`` into a temp home builds it a dependency
# environment of its own (``<home>/installs``, over a GB) unless lazy installs are off, and can outlive
# the test. Installed at import, like the API port guard below, so a notifier built outside any test's
# fixtures is covered too; it fails, as a real send from a home with no chats does. One given its own
# runner keeps it. The environment default covers plugin copies imported under another package name
# (test_live_reload), whose notifier class this guard never sees.
os.environ.setdefault("HERMES_DISABLE_LAZY_INSTALLS", "1")

# Hermes' entry-point bootstrap (first import of gateway.run, run_agent, hermes_cli.main …) switches
# the process onto the install's committed dependency environment, which may be built for another
# Python: the first test to import gateway.run would swap sys.path, PYTHONPATH and PATH for the rest
# of the run (later ``import PIL`` resolved to a python3.14 tree inside a 3.11 run). Import it once
# here with that activation off and the path/environment put back, so later imports are no-ops.
#
# The run never reads the developer's Hermes home: it gets its own, offline, before the bootstrap
# (which loads the home's .env) or any Hermes config reader runs.
offline_model.pin_run_home()
process_state.import_hermes_bootstrap_inert()
# …and no test reaches a real model: Hermes' side-model calls fail at once for the whole run (also in
# threads a test leaves behind), exactly as on a Hermes with no usable provider, which every plugin
# caller already handles as "no side model". The calls are counted in the run's summary.
offline_model.block_real_model_calls()


def pytest_terminal_summary(terminalreporter):
    calls = offline_model.REAL_MODEL_CALLS
    if calls:
        terminalreporter.write_line(f"speakeasy: {len(calls)} side-model call(s) were blocked "
                                    "(tests never reach a real model)")


@pytest.fixture(autouse=True)
def restore_process_state(request):
    """Put ``sys.path``, the import hooks, ``os.environ`` and the cwd back after every test, so no
    test can leak them into the next (Hermes modules and config loaders change them at import time).
    A test that leaves another Python's site-packages on the path, or drops this one's, fails."""
    state = process_state.ProcessState()
    yield
    problems = state.foreign_path_changes()
    state.restore()
    if problems:
        pytest.fail(f"{request.node.nodeid} left sys.path pointing at another Python's packages "
                    f"({'; '.join(problems)}); restore it in the test", pytrace=False)

_REAL_NOTIFIER_INIT = _delivery.HermesSendNotifier.__init__
_REAL_SEND_RUNNER = inspect.signature(_REAL_NOTIFIER_INIT).parameters["runner"].default


def _no_hermes_send(cmd, **kwargs):
    return subprocess.CompletedProcess(cmd, 1, "", "")


@functools.wraps(_REAL_NOTIFIER_INIT)
def _notifier_init_without_hermes_send(self, *args, **kwargs):
    _REAL_NOTIFIER_INIT(self, *args, **kwargs)
    if self.runner is _REAL_SEND_RUNNER:
        self.runner = _no_hermes_send


_delivery.HermesSendNotifier.__init__ = _notifier_init_without_hermes_send  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def no_global_hermes_sign_ins(monkeypatch):
    """Hermes' credential pool falls back to the real global ~/.hermes/auth.json when a profile has no
    entries of its own; tests see only their temp profile's sign-ins."""
    try:
        import hermes_cli.auth as auth
    except ImportError:
        return
    monkeypatch.setattr(auth, "_global_auth_file_path", lambda: None)


# Hermes' default API server port. On a dev machine that is the owner's live Hermes, so no test may
# ever connect to it: the guard below is installed once for the whole run (also covering threads a
# test leaves behind) and every connect to the port is refused and recorded.
from speakeasy import settings as _settings  # noqa: E402

HERMES_API_PORTS = frozenset({8642, _settings.DEFAULT_HERMES_API_PORT})
REAL_HERMES_API_HITS: list[tuple[str, int]] = []
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex


def _to_real_hermes_api(address) -> bool:
    if isinstance(address, tuple) and len(address) >= 2 and address[1] in HERMES_API_PORTS:
        REAL_HERMES_API_HITS.append((str(address[0]), int(address[1])))
        return True
    return False


def _guarded_connect(self, address):
    if _to_real_hermes_api(address):
        raise ConnectionRefusedError(errno.ECONNREFUSED,
                                     f"tests may not reach a real Hermes API ({address[0]}:{address[1]})")
    return _REAL_CONNECT(self, address)


def _guarded_connect_ex(self, address):
    if _to_real_hermes_api(address):
        return errno.ECONNREFUSED
    return _REAL_CONNECT_EX(self, address)


socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]


@pytest.fixture(autouse=True)
def no_real_hermes_api():
    """Fail any test that tried to reach a real Hermes API server, even when the code under test
    swallowed the refused connection (a model-options lookup that quietly falls back still fails)."""
    start = len(REAL_HERMES_API_HITS)
    yield
    hits = REAL_HERMES_API_HITS[start:]
    if hits:
        pytest.fail(f"this test tried to reach a real Hermes API at {hits[0][0]}:{hits[0][1]} "
                    f"({len(hits)} connection(s)); give it the fake Hermes (the `hermes` fixture) "
                    "or a fake opener instead", pytrace=False)


@pytest.fixture
def real_default_backend():
    """The unpatched ``duck.default_backend``, for tests of the platform check itself."""
    return _REAL_DEFAULT_BACKEND


@pytest.fixture
def hermes():
    server = FakeHermesServer()
    yield server
    server.close()


@pytest.fixture
def home(hermes):
    path = make_home(hermes.httpd.server_address[1])
    yield path
    temp_homes.remove(path)


@pytest.fixture
def service(home):
    from speakeasy.service import VoiceService
    workers: list[FakeLiveWorker] = []

    def make_worker(rt, interaction):
        worker = FakeLiveWorker(rt, interaction)
        workers.append(worker)
        return worker

    b = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
               codex_login=lambda binary: (True, "Signed in to Codex."),
               openai_negotiate=lambda key, payload: {"session": {"id": "sess_fake"}, "transport": {"sdp": "v=0\r\n"}},
               openai_worker=make_worker)
    b.settings.patch({"voice": {"provider": "openai"}})
    b.workers = workers  # type: ignore[attr-defined]
    yield b
    b.close()


@pytest.fixture
def server(service):
    from speakeasy.server import SpeakeasyServer
    srv = SpeakeasyServer(service, "127.0.0.1", 0)
    srv.start()
    code = service.devices.new_pairing_code()
    status, paired = http(srv.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Test Mac"})
    assert status == 201
    srv.token = paired["token"]  # type: ignore[attr-defined]
    yield srv
    srv.httpd.shutdown()
    srv.httpd.server_close()
