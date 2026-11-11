"""Projects (SPK-254): Hermes' own projects listed and created from the app, chats and jobs filed by
an explicit action, a job card's one-tap suggestion, "for <project>, ..." at Home, standing notes in a
filed job's brief, the once-only "Make a project for X?" offer. Invented projects and sessions only."""
import sqlite3

import pytest

from fakes import http, wait_for
from speakeasy import projects as P
from test_history import NOW, add_msg, add_session, get, make_db, never_routed, post, state  # noqa: F401

pdb = pytest.importorskip("hermes_cli.projects_db")


def put(server, path, body):
    return http(server.base_url, "PUT", path, body, server.token)


def hermes_projects(service):
    with pdb.connect_closing(service.projects.db_path) as conn:
        return {p.name: p for p in pdb.list_projects(conn)}


def make_project(service, name, folders=None, board=None):
    with pdb.connect_closing(service.projects.db_path) as conn:
        return pdb.create_project(conn, name=name, folders=folders or [], board_slug=board)


def add_cwd(path, sid, cwd):
    db = sqlite3.connect(path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
    if "cwd" not in cols:
        db.execute("ALTER TABLE sessions ADD COLUMN cwd TEXT")
    db.execute("UPDATE sessions SET cwd=? WHERE id=?", (cwd, sid))
    db.commit()
    db.close()


# -- zero projects: nothing changes ---------------------------------------------------------------

def test_zero_projects_lists_nothing_and_never_creates_hermes_file(server, service, state):
    assert http(server.base_url, "GET", "/voice/status", token=server.token)[1]["capabilities"]["projects"] is True
    status, body = get(server, "/voice/projects")
    assert status == 200 and body == {"projects": []}
    assert not service.projects.db_path.exists()   # a read never creates Hermes' projects.db
    _, listing = get(server, "/voice/history")
    assert listing["sessions"] and not any("project" in r for r in listing["sessions"])


def test_older_hermes_without_projects_turns_the_feature_off(server, service, monkeypatch):
    monkeypatch.setattr(P, "_hermes_projects", lambda: None)
    assert get(server, "/voice/projects")[0] == 501
    assert post(server, "/voice/projects", {"name": "Garden"})[0] == 501


# -- create ---------------------------------------------------------------------------------------

def test_create_is_a_name_only_hermes_project(server, service, state):
    status, body = post(server, "/voice/projects", {"name": "  Garden   shed ", "icon": "hammer", "color": "green"})
    assert status == 201, body
    project = body["project"]
    assert project["name"] == "Garden shed" and project["icon"] == "hammer" and project["color"] == "green"
    assert project["chats"] == 0 and project["jobs"] == 0 and project["has_folders"] is False
    stored = hermes_projects(service)["Garden shed"]   # Hermes itself sees it (hermes project list)
    assert stored.id == project["id"] and stored.folders == [] and stored.icon == "hammer"
    assert post(server, "/voice/projects", {"name": "garden shed"})[0] == 409
    assert post(server, "/voice/projects", {"name": ""})[0] == 400
    assert post(server, "/voice/projects", {"name": "x" * 61})[0] == 400
    assert post(server, "/voice/projects", {"name": "Ok", "icon": "😀"})[0] == 400
    assert post(server, "/voice/projects", {"name": "Ok", "color": "neon"})[0] == 400
    assert post(server, "/voice/projects", {"name": "Ok", "folder": "/tmp"})[0] == 400


# -- membership -----------------------------------------------------------------------------------

def test_move_a_chat_into_a_project_and_out_again(server, service, state):
    pid = make_project(service, "Workshop")
    assert post(server, "/voice/projects/file", {"session_id": "d1", "project_id": pid})[0] == 200
    _, listing = get(server, "/voice/projects")
    assert listing["projects"][0]["chats"] == 1
    _, page = get(server, f"/voice/projects/{pid}")
    assert [c["id"] for c in page["chats"]] == ["d1"] and page["project"]["name"] == "Workshop"
    _, history = get(server, "/voice/history")
    row = next(r for r in history["sessions"] if r["id"] == "d1")
    assert row["project"] == {"id": pid, "name": "Workshop"}
    assert post(server, "/voice/projects/file", {"session_id": "d1", "project_id": None})[0] == 200
    _, page = get(server, f"/voice/projects/{pid}")
    assert page["chats"] == []
    assert post(server, "/voice/projects/file", {"session_id": "d1", "project_id": "p_nope"})[0] == 404
    assert post(server, "/voice/projects/file", {"session_id": "d1"})[0] == 400


def test_coding_sessions_join_by_folder_and_a_move_out_wins(server, service, state, tmp_path):
    folder = tmp_path / "lantern"
    folder.mkdir()
    pid = make_project(service, "Lantern", folders=[str(folder)])
    add_cwd(state, "c1", str(folder / "src"))
    _, page = get(server, f"/voice/projects/{pid}")
    assert [c["id"] for c in page["chats"]] == ["c1"] and page["project"]["has_folders"] is True
    post(server, "/voice/projects/file", {"session_id": "c1", "project_id": None})
    assert get(server, f"/voice/projects/{pid}")[1]["chats"] == []


def test_linked_kanban_board_shows_on_the_page(server, service, state, monkeypatch):
    pid = make_project(service, "Orchard", board="orchard")
    monkeypatch.setattr(service.boards, "list", lambda: {"boards": [
        {"slug": "orchard", "name": "Orchard", "counts": {"running": 1}, "needs_you": 0, "active_at": NOW}]})
    _, page = get(server, f"/voice/projects/{pid}")
    assert page["board"]["slug"] == "orchard" and page["project"]["board"] == "orchard"


# -- jobs: started in a project, the card's chip, "for X," at Home -------------------------------

def test_a_job_started_in_a_project_is_filed_and_carries_its_notes(server, service, hermes, state):
    pid = make_project(service, "Workshop")
    assert put(server, f"/voice/projects/{pid}/notes", {"notes": "Measurements in inches. Walnut only."})[0] == 200
    status, started = post(server, "/voice/work", {"text": "Size the shelf brackets", "project_id": pid})
    assert status in (200, 202), started
    call = wait_for(lambda: next((c for c in hermes.calls if "Size the shelf brackets" in str(c["input"])), None))
    assert "Measurements in inches. Walnut only." in str(call["input"])
    assert "\u201cWorkshop\u201d" in str(call["input"])
    _, page = get(server, f"/voice/projects/{pid}")
    assert len(page["jobs"]) == 1 and page["jobs"][0]["run_id"] == started["run_id"]
    assert post(server, "/voice/work", {"text": "x", "project_id": "p_nope"})[0] == 404


def test_notes_stay_out_of_unfiled_jobs(server, service, hermes, state):
    pid = make_project(service, "Workshop")
    put(server, f"/voice/projects/{pid}/notes", {"notes": "Walnut only."})
    post(server, "/voice/work", {"text": "Find a quiet cafe near the office"})
    call = wait_for(lambda: next((c for c in hermes.calls if "quiet cafe" in str(c["input"])), None))
    assert "Walnut only." not in str(call["input"])
    assert put(server, f"/voice/projects/{pid}/notes", {"notes": "x" * 1201})[0] == 400


def test_suggestion_chip_files_or_declines_never_silently(service):
    proj = service.projects
    a, b = make_project(service, "Lantern"), make_project(service, "Garden shed")
    projects = proj._read()
    assert proj.suggest(["Fix the lantern build on the marigold step"], projects)["id"] == a
    assert proj.suggest(["Plan the garden shed roof"], projects)["id"] == b
    assert proj.suggest(["Plan the garden"], projects) is None             # part of a name is not a match
    assert proj.suggest(["Compare the lantern and the garden shed"], projects) is None  # two: never a guess
    assert proj.suggest(["Lanterns for the porch"], projects) is None      # whole words only


def test_home_card_offers_the_project_then_shows_it_once_filed(server, service, hermes, state):
    pid = make_project(service, "Lantern")
    get(server, "/voice/now")          # Home's baseline: work from before it counts as already seen
    hermes.hold = True                 # still working: a live card
    _, started = post(server, "/voice/work", {"text": "Fix the lantern build"})
    run_id = started["run_id"]

    def card():
        _, now = get(server, "/voice/now")
        items = [i for i in now.get("items") or [] if i.get("run_id") == run_id]
        return items[0] if items else None
    item = wait_for(card)
    assert item.get("project_suggestion") == {"id": pid, "name": "Lantern"} and "project" not in item
    assert post(server, "/voice/projects/file", {"run_id": run_id, "project_id": pid})[0] == 200
    item = wait_for(lambda: (lambda c: c if c and c.get("project") else None)(card()))
    assert item["project"] == {"id": pid, "name": "Lantern"} and "project_suggestion" not in item
    # Declined ("" ): neither the chip nor a project.
    post(server, "/voice/projects/file", {"run_id": run_id, "project_id": ""})
    item = wait_for(lambda: (lambda c: c if c and "project" not in c else None)(card()))
    assert "project_suggestion" not in item


def test_for_project_at_home_files_the_job(service):
    proj = service.projects
    pid = make_project(service, "Garden shed")
    assert proj.directive("for the garden shed, price cedar boards") == (pid, "price cedar boards")
    assert proj.directive("For Garden shed: price cedar boards") == (pid, "price cedar boards")
    assert proj.directive("price cedar boards for the garden shed project") == (pid, "price cedar boards")
    assert proj.directive("price cedar boards for the garden shed") is None   # could be a description
    assert proj.directive("for the barn, price boards") is None              # no such project


def test_home_message_naming_a_project_files_its_job(service, monkeypatch):
    pid = make_project(service, "Garden shed")
    sent = []
    monkeypatch.setattr(service.home_chat, "_decide", lambda *a, **k: (
        {"reply": "On it.", "action": "start", "run_id": None, "request": "Price cedar boards"}, None))
    monkeypatch.setattr(service, "start_job", lambda body, device_id=None: sent.append(body) or {"run_id": None})
    service.home_chat._turn("for the garden shed, price cedar boards", None, [], {}, None, None, None)
    assert sent and sent[0]["project_id"] == pid and sent[0]["text"] == "Price cedar boards"
    sent.clear()
    service.home_chat._turn("price cedar boards", None, [], {}, None, None, None)
    assert sent and "project_id" not in sent[0]


# -- the once-only offer --------------------------------------------------------------------------

def test_offer_after_three_chats_on_one_subject_and_dismiss_is_forever(server, service, state):
    make_project(service, "Workshop")
    store = service.store
    for n in range(3):
        key = f"k_offer{n}"
        store.reserve_run(key, "i_offer", f"d_offer{n}", 0)
        store.set_session(key, f"s_offer{n}")
        store.set_title(key, f"Marigold bed plan part {n + 1}")
    _, listing = get(server, "/voice/projects")
    assert listing["offer"] == {"subject": "Marigold", "chats": 3}
    assert post(server, "/voice/projects/offer/dismiss", {"subject": "Marigold"})[0] == 200
    assert "offer" not in get(server, "/voice/projects")[1]


def test_accepting_the_offer_files_its_chats(server, service, state):
    store = service.store
    for n in range(3):
        key = f"k_acc{n}"
        store.reserve_run(key, "i_acc", f"d_acc{n}", 0)
        store.set_session(key, f"s_acc{n}")
        store.set_title(key, f"Compost Tumbler step {n + 1}")
    status, body = post(server, "/voice/projects", {"name": "Compost", "from_offer": "Compost"})
    assert status == 201, body
    assert body["project"]["chats"] == 3 and body["project"]["jobs"] == 3
    assert "offer" not in get(server, "/voice/projects")[1]


# -- the Home project chip: where Home works -------------------------------------------------------

def test_selected_project_is_stored_plugin_side_and_reads_back(server, service, state):
    pid = make_project(service, "Lantern")
    assert get(server, "/voice/projects")[1].get("selected") is None          # Home by default
    assert put(server, "/voice/projects/selected", {"project_id": pid}) == (200, {"ok": True, "selected": pid})
    assert get(server, "/voice/projects")[1]["selected"] == pid               # every device reads the same
    assert put(server, "/voice/projects/selected", {"project_id": "p_nope"})[0] == 404
    assert put(server, "/voice/projects/selected", {"project_id": 3})[0] == 400
    assert put(server, "/voice/projects/selected", {"project_id": None})[1]["selected"] is None
    assert "selected" not in get(server, "/voice/projects")[1]


def test_chip_files_a_typed_home_job_and_back_to_home_leaves_it_unfiled(service, monkeypatch):
    pid = make_project(service, "Lantern")
    sent = []
    monkeypatch.setattr(service.home_chat, "_decide", lambda *a, **k: (
        {"reply": "On it.", "action": "start", "run_id": None, "request": "Fix the sign-in screen"}, None))
    monkeypatch.setattr(service, "start_job", lambda body, device_id=None: sent.append(body) or {"run_id": None})
    service.projects.select({"project_id": pid})
    service.home_chat._turn("fix the sign-in screen", None, [], {}, None, None, None)
    assert sent[-1]["project_id"] == pid
    other = make_project(service, "Garden shed")   # switching mid-session: new jobs follow the chip
    service.projects.select({"project_id": other})
    service.home_chat._turn("fix the sign-in screen", None, [], {}, None, None, None)
    assert sent[-1]["project_id"] == other
    service.projects.select({"project_id": None})
    service.home_chat._turn("fix the sign-in screen", None, [], {}, None, None, None)
    assert "project_id" not in sent[-1]


def test_chip_files_the_real_job_and_its_notes_go_in(server, service, hermes, state):
    pid = make_project(service, "Lantern")
    put(server, f"/voice/projects/{pid}/notes", {"notes": "Swift 6 only. Tests on the small phone."})
    put(server, "/voice/projects/selected", {"project_id": pid})
    status, _ = post(server, "/voice/home/message", {"text": "Find why the checkout test fails"})
    assert status in (200, 202)
    call = wait_for(lambda: next((c for c in hermes.calls if "checkout test" in str(c["input"])), None))
    assert "Swift 6 only." in str(call["input"])
    _, page = wait_for(lambda: (lambda r: r if r[1]["jobs"] else None)(get(server, f"/voice/projects/{pid}")))
    assert len(page["jobs"]) == 1


def test_chip_files_a_job_started_on_a_call(server, service, hermes, state):
    from test_tasks import start_call
    pid = make_project(service, "Lantern")
    put(server, f"/voice/projects/{pid}/notes", {"notes": "Walnut only."})
    put(server, "/voice/projects/selected", {"project_id": pid})
    _, worker = start_call(server, service)
    worker.delegate("call_shelf", "Size the shelf brackets")
    call = wait_for(lambda: next((c for c in hermes.calls if "shelf brackets" in str(c["input"])), None))
    assert "Walnut only." in str(call["input"])
    _, page = wait_for(lambda: (lambda r: r if r[1]["jobs"] else None)(get(server, f"/voice/projects/{pid}")))
    assert len(page["jobs"]) == 1


def test_call_job_with_home_selected_stays_unfiled(server, service, hermes, state):
    from test_tasks import start_call
    pid = make_project(service, "Lantern")
    _, worker = start_call(server, service)
    worker.delegate("call_cafe", "Find a quiet cafe near the office")
    wait_for(lambda: next((c for c in hermes.calls if "quiet cafe" in str(c["input"])), None))
    assert get(server, f"/voice/projects/{pid}")[1]["jobs"] == []


# -- an app that holds Projects out (iOS build 73+: X-Speakeasy-Projects: off) ---------------------

OFF = {"X-Speakeasy-Projects": "off"}


def test_held_out_app_home_job_goes_to_no_project_and_selection_is_untouched(server, service, hermes, state):
    pid = make_project(service, "Lantern")
    put(server, "/voice/projects/selected", {"project_id": pid})
    status, _ = http(server.base_url, "POST", "/voice/home/message", {"text": "Find why the checkout test fails"},
                     server.token, OFF)
    assert status in (200, 202)
    wait_for(lambda: next((c for c in hermes.calls if "checkout test" in str(c["input"])), None))
    assert get(server, f"/voice/projects/{pid}")[1]["jobs"] == []          # not filed
    assert get(server, "/voice/projects")[1]["selected"] == pid             # the person's choice stands


def test_held_out_app_never_files_by_selection_or_by_name(service, monkeypatch):
    pid = make_project(service, "Lantern")
    sent = []
    monkeypatch.setattr(service.home_chat, "_decide", lambda *a, **k: (
        {"reply": "On it.", "action": "start", "run_id": None, "request": "Fix the sign-in screen"}, None))
    monkeypatch.setattr(service, "start_job", lambda body, device_id=None: sent.append(body) or {"run_id": None})
    service.projects.select({"project_id": pid})
    service._projects_off["d_phone"] = True
    service.home_chat._turn("fix the sign-in screen", None, [], {}, "d_phone", None, None)
    assert "project_id" not in sent[-1]
    service.home_chat._turn("for Lantern, fix the sign-in screen", None, [], {}, "d_phone", None, None)
    assert "project_id" not in sent[-1]
    service.home_chat._turn("fix the sign-in screen", None, [], {}, "d_mac", None, None)   # an app that shows them
    assert sent[-1]["project_id"] == pid
    assert service.projects.selected() == pid


def test_held_out_app_call_job_goes_to_no_project(server, service, hermes, state):
    from test_tasks import SDP
    pid = make_project(service, "Lantern")
    put(server, "/voice/projects/selected", {"project_id": pid})
    status, _ = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                     {"Idempotency-Key": "req_off_1", **OFF})
    assert status == 201
    worker = service.workers[-1]
    worker.delegate("call_shelf", "Size the shelf brackets")
    wait_for(lambda: next((c for c in hermes.calls if "shelf brackets" in str(c["input"])), None))
    assert get(server, f"/voice/projects/{pid}")[1]["jobs"] == []
    assert get(server, "/voice/projects")[1]["selected"] == pid


def test_the_header_follows_the_latest_request(server, service, state):
    device = service.authenticate({"Authorization": f"Bearer {server.token}", **OFF})
    assert service.projects_off(device)
    assert not service.projects_off(service.authenticate({"Authorization": f"Bearer {server.token}"}))
    assert not service.projects_off(None)


def test_chip_filed_job_naming_another_project_asks_once_never_moves(service):
    proj = service.projects
    lantern, shed = make_project(service, "Lantern"), make_project(service, "Garden shed")
    proj.select({"project_id": lantern})
    key = "k_auto_1"
    proj.link("job", key, lantern)
    proj.mark_auto(key)
    projects, links = proj._read(), proj._links()
    fields = proj.card_fields(key, ["Plan the garden shed roof"], projects, links, lantern)
    assert fields["project"]["id"] == lantern and fields["project_suggestion"]["id"] == shed
    # Same words on a job the person filed by hand: no question.
    proj.link("job", "k_hand", lantern)
    assert "project_suggestion" not in proj.card_fields("k_hand", ["Plan the garden shed roof"], projects,
                                                        proj._links(), lantern)
    # Answered once: never again.
    proj.asked(key)
    assert "project_suggestion" not in proj.card_fields(key, ["Plan the garden shed roof"], projects,
                                                        proj._links(), lantern)


def test_no_suggestion_chips_while_working_in_a_project(service):
    proj = service.projects
    lantern, shed = make_project(service, "Lantern"), make_project(service, "Garden shed")
    projects = proj._read()
    assert proj.card_fields("k_new", ["Plan the garden shed roof"], projects, {}, None)["project_suggestion"]["id"] == shed
    assert proj.card_fields("k_new", ["Plan the garden shed roof"], projects, {}, lantern) == {}


def test_panel_rows_say_running_and_last_activity(server, service, hermes, state):
    pid = make_project(service, "Lantern")
    hermes.hold = True
    try:
        _, started = post(server, "/voice/work", {"text": "Fix the lantern build", "project_id": pid})
        row = wait_for(lambda: (lambda r: r if r.get("running") else None)(get(server, "/voice/projects")[1]["projects"][0]))
        assert row["running"] == 1 and row["active_at"] > 0
    finally:
        hermes.hold = False
    for run in list(hermes.runs.values()):   # the held run finishes
        run["status"] = "completed"
        run["done"].set()
    row = wait_for(lambda: (lambda r: r if not r.get("running") else None)(get(server, "/voice/projects")[1]["projects"][0]))
    assert row["active_at"] > 0
