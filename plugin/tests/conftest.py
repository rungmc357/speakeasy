"""Pytest wiring: fake Hermes, temp HERMES_HOME, a VoiceService with a fake live worker, a live server."""
from __future__ import annotations

import errno
import socket

import pytest

import temp_homes
from fakes import FakeHermesServer, FakeLiveWorker, FakeTransport, make_home, http  # noqa: F401
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


@pytest.fixture(autouse=True)
def no_real_volume(monkeypatch):
    """Never touch the Mac's real output volume from tests: services get no volume backend unless a
    test passes its own ``volume_backend``."""
    monkeypatch.setattr(_duck, "default_backend", lambda: None)


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
