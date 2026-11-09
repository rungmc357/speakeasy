"""Live reload without restarting the gateway, run against a real copy of the plugin on disk."""
from __future__ import annotations

import importlib
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from fakes import make_home

SRC = Path(__file__).resolve().parents[1] / "speakeasy"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _health(port: int) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
        return json.loads(r.read())


def _set_version(pkg: Path, version: str) -> None:
    text = (pkg / "plugin.yaml").read_text()
    lines = [f"version: {version}" if ln.startswith("version:") else ln for ln in text.splitlines()]
    (pkg / "plugin.yaml").write_text("\n".join(lines) + "\n")


@pytest.fixture
def plugin(hermes):
    """A copy of the plugin under its own package name, as Hermes would load it."""
    root = Path(tempfile.mkdtemp(prefix="speakeasy-reload-"))
    name = f"sp_reload_{int(time.time() * 1000) % 10**8}"
    pkg = root / name
    shutil.copytree(SRC, pkg, ignore=shutil.ignore_patterns("__pycache__"))
    _set_version(pkg, "1.0.0")
    sys.path.insert(0, str(root))
    home = make_home(hermes.httpd.server_address[1])  # its Hermes calls reach the fake, never 8642
    reloader_mod = importlib.import_module(f"{name}.reloader")
    port = _free_port()
    r = reloader_mod.LiveReloader(name, pkg, home, "127.0.0.1", port, check_s=3600)  # ticks driven by the test
    r.start()
    yield r, pkg, port, name, reloader_mod
    r.stop()
    sys.path.remove(str(root))
    for mod in [m for m in sys.modules if m == name or m.startswith(name + ".")]:
        sys.modules.pop(mod, None)
    shutil.rmtree(root, ignore_errors=True)
    shutil.rmtree(home, ignore_errors=True)


def _settle(r) -> list:
    """Two checks: the reloader loads files only once they hold still across checks."""
    return [r.tick(), r.tick()]


def test_an_update_loads_on_the_same_port_without_a_restart(plugin):
    r, pkg, port, name, _ = plugin
    assert _health(port)["version"] == "1.0.0"
    old_service = r.server.service
    _set_version(pkg, "1.0.1")
    assert _settle(r)[-1] == "reloaded"
    assert _health(port)["version"] == "1.0.1"
    assert r.server.service is not old_service
    # the running code is all from the new load, one consistent set
    assert sys.modules[f"{name}.service"].VoiceService is type(r.server.service)
    assert json.loads((r.state_dir / "reload-result.json").read_text())["ok"] is True


def test_modules_reimported_underneath_are_put_back(plugin):
    """What broke the friend's install: Hermes re-imported some files while the old server ran."""
    r, pkg, port, name, _ = plugin
    running = sys.modules[f"{name}.calls"]
    sys.modules.pop(f"{name}.calls")
    importlib.import_module(f"{name}.calls")            # a fresh copy sneaks in
    assert sys.modules[f"{name}.calls"] is not running
    assert r.tick() == "restored"
    assert sys.modules[f"{name}.calls"] is running
    assert _health(port)["version"] == "1.0.0"


def test_broken_new_code_keeps_the_old_version_running(plugin):
    r, pkg, port, name, _ = plugin
    _set_version(pkg, "1.0.2")
    (pkg / "router.py").write_text((pkg / "router.py").read_text() + "\ndef broken(:\n")
    assert _settle(r)[-1] == "failed"
    assert _health(port)["version"] == "1.0.0"
    result = json.loads((r.state_dir / "reload-result.json").read_text())
    assert result["ok"] is False and "SyntaxError" in result["error"]
    assert _settle(r) == [None, None]                  # doesn't retry the same broken files in a loop


def test_a_live_call_or_running_task_defers_the_automatic_reload(plugin):
    r, pkg, port, name, _ = plugin
    r.server.service.busy = lambda: True
    _set_version(pkg, "1.0.3")
    assert _settle(r) == [None, None] and r.pending
    assert _health(port)["version"] == "1.0.0"
    r.server.service.busy = lambda: False
    assert r.tick() == "reloaded"
    assert _health(port)["version"] == "1.0.3"


def test_hermes_voice_reload_loads_it_now_even_mid_call(plugin):
    r, pkg, port, name, _ = plugin
    cli = importlib.import_module(f"{name}.cli")
    r.server.service.busy = lambda: True
    _set_version(pkg, "1.0.4")
    stop = threading.Event()

    def ticker():
        while not stop.wait(.2):
            r.tick()
    threading.Thread(target=ticker, daemon=True).start()
    try:
        assert cli.cmd_reload(r.home, timeout=10) == 0
    finally:
        stop.set()
    assert _health(port)["version"] == "1.0.4"
    assert not (r.state_dir / "reload-request").exists()


def test_busy_is_true_only_for_a_live_call_or_work_a_reload_cannot_pick_up(service):
    from speakeasy.calls import BackendRun, Interaction
    assert service.busy() is False
    call = Interaction("int_a", "sess_a")
    service.interactions["int_a"] = call
    assert service.busy() is True                      # call open
    call.call_closed = True
    assert service.busy() is False
    call.runs["d1"] = BackendRun("d1", 1, "idem_1", status="running")
    assert service.busy() is True                      # no Hermes run id to follow on from
    call.runs["d1"].run_id = "run_0001"
    assert service.busy() is False                     # a followable run carries on through a reload
    call.runs["d1"].status = "completed"
    assert service.busy() is False


def test_a_paused_call_holds_back_a_reload_only_until_its_pause_runs_out(service):
    """A paused call (listening mode pauses calls too) can be resumed, so a reload waits for it; but a
    call paused and never resumed, or ended on the device while paused, must not block updates forever."""
    from speakeasy.calls import Interaction
    call = Interaction("int_p", "sess_p")
    call.call_closed = True
    call.paused = True
    service.interactions["int_p"] = call
    assert service.busy() is True
    service.pause_expired(call)          # PAUSE_NOTICE_AFTER_S later
    assert call.pause_bookkept and service.busy() is False


def test_hermes_style_reinstall_drops_and_reimports_the_whole_package(plugin):
    """`hermes plugins install` swaps the folder in one step; a plugin re-discovery then evicts the
    package and every submodule and imports it again. The running server must keep one consistent
    set of code, and the new version loads on the next idle check."""
    r, pkg, port, name, _ = plugin
    running_calls = sys.modules[f"{name}.calls"]
    new = pkg.parent / (name + "_staging")
    shutil.copytree(pkg, new, ignore=shutil.ignore_patterns("__pycache__"))
    _set_version(new, "2.0.0")
    backup = pkg.parent / (name + "_backup")
    pkg.rename(backup)
    new.rename(pkg)                                        # atomic swap, like the installer
    shutil.rmtree(backup)
    for mod in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
        del sys.modules[mod]                               # Hermes' _evict_modules
    importlib.invalidate_caches()
    importlib.import_module(name)
    importlib.import_module(f"{name}.service")             # re-discovery imports the new code
    assert r.tick() == "restored"                          # running server keeps its own code...
    assert sys.modules[f"{name}.calls"] is running_calls
    assert _health(port)["version"] == "1.0.0"
    assert r.tick() == "reloaded"                          # ...until the new version loads whole
    assert _health(port)["version"] == "2.0.0"
    assert sys.modules[f"{name}.calls"] is not running_calls


def test_repeated_discovery_passes_log_the_re_import_once_and_name_the_modules(plugin, caplog):
    """SPK-270: Hermes' plugin discovery re-imports the package every few minutes with unchanged
    files; the watcher put the running code back each time and logged it every 2 s (~1,800 lines an
    hour), rotating agent.log away. It still restores every time, but logs once per cause, naming
    which modules differed and whether they were missing or replaced."""
    import logging
    r, pkg, port, name, _ = plugin
    running_calls = sys.modules[f"{name}.calls"]
    caplog.set_level(logging.INFO, logger=f"{name}.reloader")
    for _ in range(5):
        for mod in [m for m in list(sys.modules) if m == name or m.startswith(name + ".")]:
            if not mod.endswith((".reloader", ".adapter")):
                del sys.modules[mod]                       # Hermes' discovery pass: evict...
        importlib.invalidate_caches()
        importlib.import_module(name)
        importlib.import_module(f"{name}.calls")           # ...and import part of it again
        assert r.tick() == "restored"
        assert sys.modules[f"{name}.calls"] is running_calls
    lines = [rec.getMessage() for rec in caplog.records if "re-imported underneath" in rec.getMessage()]
    assert len(lines) == 1, lines
    assert "replaced: " in lines[0] and "calls" in lines[0] and "missing: " in lines[0]
    assert r.restores == 5
    assert _health(port)["version"] == "1.0.0"             # the running server never changed


def _fail_builds(r, count: int) -> list:
    """Make the next ``count`` server builds fail like the live incident (a transient disk error)."""
    real, failures = r._build, []

    def flaky():
        if len(failures) < count:
            failures.append(1)
            raise OSError("disk I/O error")
        return real()
    r._build = flaky
    return failures


def _down_after_a_failed_reload(r, pkg):
    _fail_builds(r, 2)                                  # the new code and the fallback both fail
    _set_version(pkg, "1.0.5")
    assert _settle(r)[-1] == "failed"
    assert r.server is None
    result = json.loads((r.state_dir / "reload-result.json").read_text())
    assert result["ok"] is False and result["running"] is False


def test_a_server_left_down_by_a_failed_reload_comes_back_on_the_next_check(plugin):
    r, pkg, port, name, mod = plugin
    _down_after_a_failed_reload(r, pkg)
    assert r.tick() is None                             # backing off: not every 2 s
    r.retry_at = 0.0                                    # the backoff is up
    assert r.tick() == "recovered"
    assert _health(port)["version"] == "1.0.5"
    result = json.loads((r.state_dir / "reload-result.json").read_text())
    assert result["ok"] is True and result["running"] is True
    assert r.tick() is None                             # and it is back to normal watching


def test_hermes_voice_reload_retries_a_down_server_at_once(plugin):
    r, pkg, port, name, mod = plugin
    _down_after_a_failed_reload(r, pkg)
    (r.state_dir / "reload-request").write_text("now")
    assert r.tick() == "recovered"                      # no waiting out the backoff when asked
    assert _health(port)["version"] == "1.0.5"
    assert not (r.state_dir / "reload-request").exists()


def test_a_down_server_backs_off_and_records_every_attempt(plugin):
    r, pkg, port, name, mod = plugin
    _down_after_a_failed_reload(r, pkg)
    _fail_builds(r, 4)                                  # the next two attempts fail (disk + previous each)
    waits = []
    for _ in range(2):
        r.retry_at = 0.0
        before = time.monotonic()
        assert r.tick() == "failed"
        waits.append(r.retry_at - before)
        assert json.loads((r.state_dir / "reload-result.json").read_text())["running"] is False
    assert waits[0] < waits[1] <= mod.RETRY_MAX_S + 1
    r.retry_at = 0.0
    assert r.tick() == "recovered" and _health(port)["version"] == "1.0.5"


def test_a_module_the_running_code_imports_later_is_kept(plugin):
    """The first pairing lazily imports address_drop; the check used to evict it mid-import (KeyError)."""
    r, pkg, port, name, _ = plugin
    lazy = f"{name}.address_drop"
    sys.modules.pop(lazy, None)
    r.modules.pop(lazy, None)
    started, finish = threading.Event(), threading.Event()
    seen = {}

    def slow_import():
        # Same shape as the real race: the import is in progress when the check runs.
        spec = importlib.util.find_spec(lazy)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[lazy] = mod
        started.set(); finish.wait(5)
        spec.loader.exec_module(mod)
        seen["mod"] = sys.modules[lazy]

    t = threading.Thread(target=slow_import); t.start()
    assert started.wait(5)
    assert r.tick() is None                     # nothing was re-imported underneath: nothing to restore
    finish.set(); t.join(5)
    assert seen["mod"] is sys.modules[lazy]     # the import finished and is still there
    assert r.modules[lazy] is sys.modules[lazy]
    assert r.tick() is None
    assert _health(port)["version"] == "1.0.0"
