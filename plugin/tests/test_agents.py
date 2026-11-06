"""Agents: which Hermes profile a chat talks to (docs/API.md "Agents", speakeasy/agents.py). A temp
Hermes install with invented profiles; the fake Hermes answers every profile at /p/<name>/."""
import time

import pytest

from fakes import http, wait_for
from speakeasy import agents as AG
from test_history import add_msg, add_session, make_db

NOW = time.time()


def get(server, path):
    return http(server.base_url, "GET", path, token=server.token)


def post(server, path, body):
    return http(server.base_url, "POST", path, body, server.token)


def patch(server, body):
    return http(server.base_url, "PATCH", "/voice/settings", body, server.token)


def add_profile(home, name, *, display=None, description=None, config="model: {}\n"):
    d = home / "profiles" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(config)
    meta = []
    if display:
        meta.append(f"display_name: {display}")
    if description:
        meta.append(f"description: {description}")
    if meta:
        (d / "profile.yaml").write_text("\n".join(meta) + "\n")
    return d


@pytest.fixture
def install(home):
    """Speakeasy in the root profile of an install that multiplexes three more profiles."""
    cfg = home / "config.yaml"
    cfg.write_text((cfg.read_text() if cfg.exists() else "") + "gateway:\n  multiplex_profiles: true\n")
    add_profile(home, "garden", display="Garden helper", description="Plans the vegetable beds.")
    add_profile(home, "research-desk")
    add_profile(home, "parked-one").joinpath("gateway.parked").write_text("")
    add_profile(home, "loner", config="gateway:\n  standalone: true\n")
    make_db(home / "state.db")
    return home


def test_nothing_allowed_offers_only_speakeasys_own_agent(server, install):
    status, body = get(server, "/voice/agents")
    assert status == 200, body
    assert [a["id"] for a in body["agents"]] == ["default"] and body["default"] == "default"
    assert body["agents"][0] == {"id": "default", "name": "Hermes", "default": True}
    # Parked and standalone profiles aren't served by this gateway: never offered.
    assert [a["id"] for a in body["available"]] == ["default", "garden", "research-desk"]
    garden = body["available"][1]
    assert garden == {"id": "garden", "name": "Garden helper", "description": "Plans the vegetable beds."}
    assert body["available"][2]["name"] == "Research desk"
    assert get(server, "/voice/status")[1]["capabilities"]["agents"] is True


def test_allowlist_is_saved_and_shapes_the_picker(server, install):
    status, body = patch(server, {"agents": {"allowed": ["research-desk", "garden", "gone-away"]}})
    assert status == 200, body
    assert body["settings"]["agents"]["allowed"] == ["research-desk", "garden", "gone-away"]
    _, roster = get(server, "/voice/agents")
    # Own agent first, then the allowed ones in the user's order; a profile that no longer exists isn't offered.
    assert [a["id"] for a in roster["agents"]] == ["default", "research-desk", "garden"]
    assert patch(server, {"agents": {"allowed": ["Not A Name!"]}})[0] == 400
    assert patch(server, {"agents": {"allowed": "garden"}})[0] == 400
    assert patch(server, {"agents": {"colour": []}})[0] == 400


def test_without_multiplexing_only_the_own_agent_is_reachable(server, home):
    add_profile(home, "garden")
    _, body = get(server, "/voice/agents")
    assert [a["id"] for a in body["available"]] == ["default"] and body["multiplexed"] is False


def test_an_agent_not_allowed_is_refused(server, install, hermes):
    status, body = post(server, "/voice/work", {"text": "Plan the tomato beds", "agent": "garden"})
    assert status == 400 and "allowed" in body["error"]
    assert post(server, "/voice/work", {"text": "x", "agent": 7})[0] == 400
    assert not hermes.calls


def test_new_chat_on_another_agent_runs_there_and_stays_there(server, service, install, hermes):
    patch(server, {"agents": {"allowed": ["garden"]}})
    status, started = post(server, "/voice/work", {"text": "Plan the tomato beds", "agent": "garden",
                                                   "model": None})
    assert status == 202, started
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["agent"] == "garden"                                 # POST /p/garden/v1/runs
    assert not {"provider", "model", "model_options"} & set(call)    # on that agent's own model
    # Its event stream is read from the same profile.
    wait_for(lambda: any(p == ("garden", "GET", f"/v1/runs/{call['run_id']}/events") for p in hermes.agent_paths))
    key = service.store.key_for_run(started["run_id"])
    sid = service.store.session_for(key)
    assert service.store.agent("session:" + sid) == "garden"
    wait_for(lambda: service.store.work(started["run_id"])["status"] == "completed")

    # The chat lives in the garden profile's state.db: the list shows it there, marked with its agent.
    garden_db = install / "profiles" / "garden" / "state.db"
    make_db(garden_db)
    add_session(garden_db, sid, "api_server", at=NOW - 1)
    add_msg(garden_db, sid, "user", "User: Plan the tomato beds", at=NOW - 2)
    add_msg(garden_db, sid, "assistant", "Three beds, staggered by two weeks.", at=NOW - 1)
    add_session(install / "state.db", "api_mine", "api_server", title="Recipe scaling", at=NOW - 50)
    add_msg(install / "state.db", "api_mine", "user", "Scale the soup recipe", at=NOW - 50)
    _, listing = get(server, "/voice/history")
    rows = {r["id"]: r for r in listing["sessions"]}
    assert set(rows) >= {sid, "api_mine"}
    assert listing["sessions"][0]["id"] == sid                       # newest first across both agents
    assert rows[sid]["agent"]["name"] == "Garden helper" and "agent" not in rows["api_mine"]
    assert rows[sid]["preview"].startswith("Three beds")

    # Its page reads from that profile too, and says which agent it is.
    status, page = get(server, f"/voice/history/{sid}")
    assert status == 200, page
    assert page["session"]["agent"]["id"] == "garden"
    assert any("Three beds" in e.get("text", "") for e in page["session"]["entries"])

    # The next message continues the chat on the same agent.
    before = len(hermes.calls)
    status, body = post(server, f"/voice/history/{sid}/message", {"text": "Add a row of basil"})
    assert status == 200, body
    wait_for(lambda: len(hermes.calls) > before)
    assert hermes.calls[-1]["agent"] == "garden" and hermes.calls[-1]["session_id"] == sid


def test_picking_the_own_agent_is_the_same_as_no_pick(server, service, install, hermes):
    patch(server, {"agents": {"allowed": ["garden"]}})
    status, started = post(server, "/voice/work", {"text": "Draft the bake sale flyer", "agent": "default"})
    assert status == 202, started
    call = wait_for(lambda: hermes.calls)[-1]
    assert "agent" not in call
    key = service.store.key_for_run(started["run_id"])
    assert service.store.agent("session:" + service.store.session_for(key)) is None


def test_stop_goes_to_the_agent_that_runs_it(server, service, install, hermes):
    patch(server, {"agents": {"allowed": ["garden"]}})
    hermes.hold = True
    status, started = post(server, "/voice/work", {"text": "Water schedule for July", "agent": "garden"})
    assert status == 202, started
    run_id = wait_for(lambda: hermes.calls)[-1]["run_id"]
    wait_for(lambda: (service.store.work(started["run_id"]) or {}).get("status") == "running")
    status, body = post(server, f"/voice/work/{started['run_id']}/stop", {})
    assert status in {200, 202}, body
    assert wait_for(lambda: any(p == ("garden", "POST", f"/v1/runs/{run_id}/stop") for p in hermes.agent_paths))


def test_labels_read_like_names():
    assert AG.label("default") == "Hermes"
    assert AG.label("venice_research") == "Venice research"
    assert AG.label("garden", "  Garden helper ") == "Garden helper"
