"""Updating the Speakeasy plugin from the app or by voice: a job with no call (POST /voice/work), the
update instructions every install can follow, deferral of the reload while anything runs, and a tree
Hermes' install scanner passes without --force."""
import sys
from pathlib import Path

import pytest

from fakes import http, wait_for
from test_routing import start_call, tasks
from speakeasy.prompt import builder as P

REQUEST = "Update my Speakeasy plugin to 0.9.1"


def finish_all(hermes):
    for run in list(hermes.runs.values()):
        run["status"] = "completed"
        run["done"].set()


def start(server, body):
    return http(server.base_url, "POST", "/voice/work", body, server.token)


# -- POST /voice/work: a job before any call ----------------------------------------------------------

def test_a_typed_job_starts_with_no_call_and_shows_on_the_board(server, service, hermes):
    assert not service.interactions
    status, body = start(server, {"text": REQUEST, "client_id": "upd_1"})
    assert status == 202, body
    assert body["ok"] and body["run_id"] and body["task_id"].startswith("job_")
    wait_for(lambda: hermes.calls)
    sent = hermes.calls[0]["input"]
    assert REQUEST in sent
    assert "hermes plugins update speakeasy" in sent and "without --force" in sent
    assert "Do not run `hermes voice reload`" in sent
    row = wait_for(lambda: [t for t in tasks(server) if t.get("run_id") == body["run_id"]])[0]
    assert row["status"] not in {"failed", "rejected", "cancelled"}, row
    assert not service.interactions                       # no call was opened for it


def test_the_same_client_id_starts_one_job(server, service, hermes):
    first = start(server, {"text": REQUEST, "client_id": "upd_twice"})
    again = start(server, {"text": REQUEST, "client_id": "upd_twice"})
    assert first[0] == again[0] == 202
    assert first[1]["run_id"] == again[1]["run_id"]
    wait_for(lambda: hermes.calls)
    assert len(hermes.calls) == 1


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "  "}, {"text": "x", "source": "voice"},
                                  {"text": "x", "client_id": "no spaces"}, {"text": "x", "extra": 1},
                                  {"text": "x" * 5000}])
def test_bad_job_bodies_are_refused(server, body):
    assert start(server, body)[0] == 400


def test_status_advertises_start_jobs(server):
    caps = http(server.base_url, "GET", "/voice/status", token=server.token)[1]["capabilities"]
    assert caps["start_jobs"] is True and caps["task_messages"] is True


def test_a_job_with_no_call_holds_back_the_reload_until_it_finishes(server, service, hermes):
    """The update job itself installs the new files; they must load only after it (and any call) ends."""
    hermes.hold = True
    status, body = start(server, {"text": REQUEST})
    assert status == 202, body
    assert service.busy() is True
    finish_all(hermes)
    wait_for(lambda: service.busy() is False)


# -- by voice, mid-call ---------------------------------------------------------------------------------

def test_update_my_plugin_by_voice_is_a_normal_task_told_how_to_update(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service)
    worker.delegate("call_u", "Update my Speakeasy plugin")
    wait_for(lambda: hermes.calls)
    sent = hermes.calls[0]["input"]
    assert "hermes plugins update speakeasy" in sent and "hermes voice reload" in sent
    assert service.busy() is True                        # the call (and the job) defer any reload
    finish_all(hermes)


def test_the_voice_knows_it_switches_over_after_hang_up():
    rules = (Path(P.__file__).parent / "rules.md").read_text()
    assert "Downloading it now. It'll switch over when we hang up." in rules


@pytest.mark.parametrize("text,expected", [
    ("Update my Speakeasy plugin", True),
    ("can you upgrade speakeasy for me", True),
    ("speakeasy has an update, grab it", True),
    ("Update the Speakeasy plugin on my Hermes. Run `hermes plugins update speakeasy`", False),  # already says it
    ("Update the Speakeasy app on my iPhone", False),
    ("Update my calendar", False),
    ("What's new in the speakeasy bar downtown", False),
])
def test_which_requests_get_the_update_instructions(text, expected):
    assert bool(P.plugin_update_focus(text)) is expected


# -- the install scanner ------------------------------------------------------------------------------

def _plugin_guard():
    from fakes import HERMES_SRC as agent  # the Hermes checkout under test (HERMES_AGENT_SRC)
    if not (agent / "tools" / "plugin_guard.py").exists():
        pytest.skip("no Hermes checkout to scan with")
    sys.path.insert(0, str(agent))
    try:
        from tools import plugin_guard
    except Exception as exc:  # pragma: no cover - Hermes' own imports missing
        pytest.skip(f"Hermes scanner unavailable ({type(exc).__name__})")
    finally:
        sys.path.remove(str(agent))
    return plugin_guard.scan_plugin, plugin_guard.should_allow_plugin_install


def test_a_plain_hermes_plugins_update_passes_the_install_scan():
    """`hermes plugins update speakeasy` (no --force) refuses a tree with a caution verdict when it
    runs without a terminal, which is how an agent runs it. Only high/critical findings count."""
    scan_plugin, allow = _plugin_guard()
    result = scan_plugin(Path(P.__file__).resolve().parents[1], source="speakeasy")
    blocking = [f"{f.file}:{f.line} {f.pattern_id}" for f in result.findings if f.severity in {"high", "critical"}]
    assert not blocking
    assert allow(result, force=False)[0] is True
