"""SPK-362: builds 73 and 74 hold Projects out. On a Hermes that has projects, the plugin says it has
no projects (``projects: false`` on /voice/status), so no phone or Mac shows Home's project chip or a
card's "Lantern?", and nothing Home starts is filed into a project, even when a project was chosen
before the update. ``SPEAKEASY_PROJECTS=1`` in the plugin's environment turns Projects back on.
Invented projects and requests only."""
import pytest

from fakes import http, wait_for
from test_history import get, post, state  # noqa: F401

pdb = pytest.importorskip("hermes_cli.projects_db")

FLAG = "SPEAKEASY_PROJECTS"
WAIT_S = 30.0   # a job reaching the fake Hermes; generous so a loaded machine doesn't fail it


def put(server, path, body):
    return http(server.base_url, "PUT", path, body, server.token)


def make_project(service, name):
    with pdb.connect_closing(service.projects.db_path) as conn:
        return pdb.create_project(conn, name=name, folders=[], board_slug=None)


def links(service):
    with service.store._lock:
        return service.store._db.execute("SELECT kind, target, project_id FROM project_links").fetchall()


def chosen_before(server, service, monkeypatch, name, notes):
    """A project picked on Home's chip (with notes) while Projects was on, then the update turns it off."""
    monkeypatch.setenv(FLAG, "1")
    pid = make_project(service, name)
    assert put(server, f"/voice/projects/{pid}/notes", {"notes": notes})[0] == 200
    assert put(server, "/voice/projects/selected", {"project_id": pid})[0] == 200
    monkeypatch.delenv(FLAG)
    return pid


def test_a_hermes_with_projects_reports_none(server, service, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    make_project(service, "Lantern")
    assert service.projects.db_path.exists()      # Hermes really has a project
    # What GET /voice/status serves (server.py routes it to service.status).
    assert service.status()["capabilities"]["projects"] is False
    assert get(server, "/voice/projects")[0] == 501
    assert put(server, "/voice/projects/selected", {"project_id": None})[0] == 501


def test_flag_turns_projects_back_on(server, service, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    assert service.status()["capabilities"]["projects"] is True
    assert get(server, "/voice/projects")[0] == 200


def test_a_project_chosen_before_files_no_home_job(server, service, hermes, state, monkeypatch):
    chosen_before(server, service, monkeypatch, "Lantern", "Swift 6 only.")
    service.home_message({"text": "Find why the checkout test fails"})   # what POST /voice/home/message runs
    call = wait_for(lambda: next((c for c in hermes.calls if "checkout test" in str(c["input"])), None), WAIT_S)
    assert "Swift 6 only." not in str(call["input"])   # no project's notes in the job's brief
    assert links(service) == []                       # and the job isn't filed anywhere


def test_a_project_chosen_before_files_no_call_job(server, service, hermes, state, monkeypatch):
    from test_tasks import start_call
    chosen_before(server, service, monkeypatch, "Lantern", "Walnut only.")
    _, worker = start_call(server, service)
    worker.delegate("call_shelf", "Size the shelf brackets")
    call = wait_for(lambda: next((c for c in hermes.calls if "shelf brackets" in str(c["input"])), None), WAIT_S)
    assert "Walnut only." not in str(call["input"])
    assert links(service) == []


def test_home_cards_offer_no_project(server, service, hermes, state, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    make_project(service, "Lantern")
    get(server, "/voice/now")          # Home's baseline: work from before it counts as already seen
    hermes.hold = True                 # still working: a live card
    _, started = post(server, "/voice/work", {"text": "Fix the lantern build"})
    run_id = started["run_id"]

    def card():
        _, now = get(server, "/voice/now")
        items = [i for i in now.get("items") or [] if i.get("run_id") == run_id]
        return items[0] if items else None
    item = wait_for(card, WAIT_S)
    assert "project_suggestion" not in item and "project" not in item   # no "Lantern?" on the card
