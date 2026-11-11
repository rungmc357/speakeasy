"""SPK-362: builds 73 and 74 hold Projects out. On a Hermes that has projects, the plugin says it has
no projects (``projects: false`` on /voice/status), so no phone or Mac shows Home's project chip or a
card's "Lantern?", and nothing Home starts is filed into a project, even when a project was chosen
before the update. ``SPEAKEASY_PROJECTS=1`` in the plugin's environment turns Projects back on.
Invented projects and requests only. Most calls go to the service method each route runs (named
beside it), so a loaded machine's slow HTTP doesn't fail them."""
import pytest

from fakes import http, wait_for
from test_history import get, state  # noqa: F401

pdb = pytest.importorskip("hermes_cli.projects_db")

FLAG = "SPEAKEASY_PROJECTS"
WAIT_S = 30.0   # a job reaching the fake Hermes; generous so a loaded machine doesn't fail it
HELD_OUT = "Projects aren't in this version of Speakeasy yet."


def put(server, path, body):
    return http(server.base_url, "PUT", path, body, server.token)


def make_project(service, name):
    with pdb.connect_closing(service.projects.db_path) as conn:
        return pdb.create_project(conn, name=name, folders=[], board_slug=None)


def links(service):
    with service.store._lock:
        return sorted(service.store._db.execute("SELECT kind, target, project_id FROM project_links").fetchall())


def chosen_before(service, monkeypatch, name, notes):
    """A project picked on Home's chip (with notes) while Projects was on, then the update turns it off."""
    monkeypatch.setenv(FLAG, "1")
    pid = make_project(service, name)
    service.project_notes(pid, {"notes": notes})          # PUT /voice/projects/{id}/notes
    service.projects.select({"project_id": pid})          # PUT /voice/projects/selected
    assert service.projects.selected() == pid
    monkeypatch.delenv(FLAG)
    return pid


def hermes_call(hermes, words):
    return wait_for(lambda: next((c for c in hermes.calls if words in str(c["input"])), None), WAIT_S)


def test_a_hermes_with_projects_reports_none(server, service, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    make_project(service, "Lantern")
    assert service.projects.db_path.exists()      # Hermes really has a project
    assert service.status()["capabilities"]["projects"] is False     # GET /voice/status
    assert get(server, "/voice/projects") == (501, {"error": HELD_OUT})
    assert put(server, "/voice/projects/selected", {"project_id": None})[0] == 501


def test_flag_turns_projects_back_on(server, service, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    pid = make_project(service, "Lantern")
    assert service.status()["capabilities"]["projects"] is True
    status, listing = get(server, "/voice/projects")
    assert status == 200 and [p["id"] for p in listing["projects"]] == [pid]


def test_flag_on_an_older_hermes_still_says_update_hermes(server, service, monkeypatch):
    from speakeasy import projects as P
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setattr(P, "_hermes_projects", lambda: None)   # a Hermes without projects_db
    assert service.status()["capabilities"]["projects"] is False
    assert get(server, "/voice/projects") == (
        501, {"error": "This Hermes doesn't have projects yet. Update Hermes to use them."})


def test_a_project_chosen_before_files_no_home_job(server, service, hermes, state, monkeypatch):
    chosen_before(service, monkeypatch, "Lantern", "Swift 6 only.")
    before = links(service)
    service.home_message({"text": "Find why the checkout test fails"})   # POST /voice/home/message
    call = hermes_call(hermes, "checkout test")
    assert "Swift 6 only." not in str(call["input"])   # no project's notes in the job's brief
    assert links(service) == before                   # and the job isn't filed anywhere


def test_a_project_chosen_before_files_no_call_job(server, service, hermes, state, monkeypatch):
    from test_tasks import start_call
    chosen_before(service, monkeypatch, "Lantern", "Walnut only.")
    before = links(service)
    _, worker = start_call(server, service)
    worker.delegate("call_shelf", "Size the shelf brackets")
    call = hermes_call(hermes, "shelf brackets")
    assert "Walnut only." not in str(call["input"])
    assert links(service) == before


def test_new_chat_from_a_project_page_left_open_still_starts(server, service, hermes, state, monkeypatch):
    pid = chosen_before(service, monkeypatch, "Lantern", "Walnut only.")
    before = links(service)
    got = service.start_job({"text": "Price walnut boards", "project_id": pid})   # POST /voice/work
    assert got.get("run_id") and "project" not in got
    call = hermes_call(hermes, "walnut boards")
    assert "Walnut only." not in str(call["input"])
    assert links(service) == before


def test_home_cards_and_history_show_no_project(server, service, hermes, state, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    pid = make_project(service, "Lantern")
    service.now({})                    # Home's baseline (GET /voice/now): earlier work counts as seen
    hermes.hold = True                 # still working: a live card
    run_id = service.start_job({"text": "Fix the lantern build"})["run_id"]

    def card():
        items = [i for i in service.now({}).get("items") or [] if i.get("run_id") == run_id]
        return items[0] if items else None
    item = wait_for(card, WAIT_S)
    assert "project_suggestion" not in item and "project" not in item   # no "Lantern?" on the card

    # Filed while Projects was on (this job and the chat "d1"): Home and the history say nothing of it.
    # (The flag changes with a plugin restart, which starts with no history cache; forget it here.)
    monkeypatch.setenv(FLAG, "1")
    service.projects.link("job", service.projects._job_key(service.store.key_for_run(run_id)), pid)
    service.projects.link("session", "d1", pid)
    service.forget_history_cache()
    assert "project" in card() and any("project" in r for r in service.history({})["sessions"])
    monkeypatch.delenv(FLAG)
    service.forget_history_cache()
    item = card()
    assert "project" not in item and "project_suggestion" not in item
    assert not any("project" in r for r in service.history({})["sessions"])    # GET /voice/history
