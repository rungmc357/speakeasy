"""Profile switching (build 75; docs/API.md "Profiles"): GET /voice/profiles lists the profiles this
Hermes serves; the app names the one it looks at in ``X-Speakeasy-Profile`` (a NAME, never a key).
The plugin reaches another profile at /p/<name>/ with THAT profile's own API key, read on the server.
A temp Hermes install with invented profiles and invented keys; the fake Hermes checks each profile's
key the way Hermes does."""
import json
import time

from fakes import FAKE_API_KEY, http, wait_for
from test_agents import add_profile
from test_history import add_msg, add_session, make_db

NOW = time.time()
GARDEN_KEY = "garden-profile-key-invented-0001"


def as_profile(server, method, path, body=None, profile=None):
    headers = {"X-Speakeasy-Profile": profile} if profile is not None else None
    return http(server.base_url, method, path, body, server.token, headers=headers)


def setup_install(home, hermes, *, garden_key: str | None = GARDEN_KEY):
    cfg = home / "config.yaml"
    cfg.write_text((cfg.read_text() if cfg.exists() else "") + "gateway:\n  multiplex_profiles: true\n")
    garden = add_profile(home, "garden", display="Garden helper",
                         config="model:\n  default: invented/garden-model-1\n")
    if garden_key:
        (garden / ".env").write_text(f"API_SERVER_KEY={garden_key}\n")
    add_profile(home, "parked-one").joinpath("gateway.parked").write_text("")
    add_profile(home, "loner", config="gateway:\n  standalone: true\n")
    make_db(home / "state.db")
    make_db(garden / "state.db")
    hermes.profile_keys = {"garden": garden_key or "a-key-speakeasy-cannot-know-000"}
    return garden


def test_profiles_lists_only_served_profiles_with_their_own_model(server, home, hermes):
    setup_install(home, hermes)
    status, body = as_profile(server, "GET", "/voice/profiles")
    assert status == 200, body
    assert [p["id"] for p in body["profiles"]] == ["default", "garden"]   # parked/standalone never
    garden = body["profiles"][1]
    assert garden["name"] == "Garden helper" and garden["model"] == "invented/garden-model-1"
    assert garden["current"] is False and body["profiles"][0]["current"] is True
    assert body["current"] == "default" and body["own"] == "default"
    # Looking at garden: the list says so.
    _, body = as_profile(server, "GET", "/voice/profiles", profile="garden")
    assert body["current"] == "garden" and body["profiles"][1]["current"] is True
    # No key, path or secret in the reply.
    raw = json.dumps(body)
    assert GARDEN_KEY not in raw and FAKE_API_KEY not in raw and "API_SERVER_KEY" not in raw
    assert as_profile(server, "GET", "/voice/status")[1]["capabilities"]["profiles"] is True


def test_an_unknown_or_unserved_profile_is_refused_never_redirected(server, home, hermes):
    setup_install(home, hermes)
    for name in ("parked-one", "loner", "nobody", "../garden", "Garden Helper"):
        status, body = as_profile(server, "GET", "/voice/now", profile=name)
        assert status == 400 and body.get("reason") == "profile", (name, body)
    status, _ = as_profile(server, "POST", "/voice/work", {"text": "x"}, profile="nobody")
    assert status == 400 and not hermes.calls


def test_without_multiplexing_only_the_own_profile_is_listed(server, home):
    add_profile(home, "garden")
    _, body = as_profile(server, "GET", "/voice/profiles")
    assert [p["id"] for p in body["profiles"]] == ["default"]
    assert as_profile(server, "GET", "/voice/now", profile="garden")[0] == 400


def test_work_started_while_looking_at_a_profile_runs_there_with_its_own_key(server, service, home, hermes):
    setup_install(home, hermes)
    status, started = as_profile(server, "POST", "/voice/work", {"text": "Plan the tomato beds"}, profile="garden")
    assert status == 202, started
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["agent"] == "garden"                                     # POST /p/garden/v1/runs
    # The fake Hermes only accepts the garden key on /p/garden: the run finishing proves the
    # plugin sent garden's own key (and the events stream read with it too).
    wait_for(lambda: (service.store.work(started["run_id"]) or {}).get("status") == "completed")
    # Not in the allowlist (agents.allowed is empty): switching is not the agent picker.
    assert service.settings.get()["agents"]["allowed"] == []
    # Home and Work cut to the profile looked at: garden's run shows under garden, not under the own.
    from speakeasy import service as S
    body = {"items": [{"id": "a", "run_id": started["run_id"]}, {"id": "b", "run_id": None}],
            "stream": {"entries": [{"id": "e1", "kind": "card", "run_id": started["run_id"]}]},
            "stack": {"items": [{"id": "s1", "run_id": started["run_id"]}], "count": 1, "on_work": []},
            "recommendations": {"items": [{"id": "r"}]}, "call_summary": {"x": 1},
            "background": {"running": 1, "items": [{}]}, "as_of": time.time()}
    for view, want in ((("garden",), {"a"}), ((None,), {"b"})):
        token = S._VIEW.set(view)
        try:
            got = service._filter_now(body)
        finally:
            S._VIEW.reset(token)
        assert {i["id"] for i in got["items"]} == want
        assert bool(got["stack"]["items"]) == (view == ("garden",))
        assert got["profile"] == (view[0] or "default")
    token = S._VIEW.set(("garden",))
    try:
        got = service._filter_now(body)
    finally:
        S._VIEW.reset(token)
    assert got["recommendations"]["items"] == [] and got["call_summary"] is None
    assert got["background"] == {"running": 0, "items": []}
    # An app that doesn't switch (no header) gets Home exactly as before.
    assert service._filter_now(body) is body
    _, legacy = as_profile(server, "GET", "/voice/now")
    _, garden_now = as_profile(server, "GET", "/voice/now", profile="garden")
    assert "profile" not in legacy and garden_now["profile"] == "garden"


def test_a_profile_whose_key_speakeasy_cannot_read_stays_closed(server, service, home, hermes):
    # garden has no key Speakeasy can read, and Hermes holds one for it: the own key is offered
    # (as before build 75) and Hermes refuses it. Switching never opens a profile Hermes keeps shut.
    setup_install(home, hermes, garden_key=None)
    status, started = as_profile(server, "POST", "/voice/work", {"text": "Water schedule"}, profile="garden")
    assert status in {202, 502}, started
    time.sleep(0.5)
    assert not hermes.calls


def test_chats_list_the_profile_looked_at(server, service, home, hermes):
    garden = setup_install(home, hermes)
    add_session(home / "state.db", "api_mine", "api_server", title="Recipe scaling", at=NOW - 50)
    add_msg(home / "state.db", "api_mine", "user", "Scale the soup recipe", at=NOW - 50)
    add_session(garden / "state.db", "tg_garden", "telegram", title="Seed order", at=NOW - 20)
    add_msg(garden / "state.db", "tg_garden", "user", "Order the bean seeds", at=NOW - 20)
    add_msg(garden / "state.db", "tg_garden", "assistant", "Ordered two packets.", at=NOW - 19)
    _, own = as_profile(server, "GET", "/voice/history", profile="default")
    _, theirs = as_profile(server, "GET", "/voice/history", profile="garden")
    assert {r["id"] for r in own["sessions"]} == {"api_mine"}
    assert {r["id"] for r in theirs["sessions"]} == {"tg_garden"}
    assert theirs["sessions"][0]["agent"]["name"] == "Garden helper"
    # Opening it reads garden's state.db, and a message there runs on garden.
    status, page = as_profile(server, "GET", "/voice/history/tg_garden", profile="garden")
    assert status == 200, page
    assert any("Ordered two packets" in (e.get("text") or "") for e in page["session"]["entries"])
    status, body = as_profile(server, "POST", "/voice/history/tg_garden/message", {"text": "And carrots"},
                              profile="garden")
    assert status == 200, body
    wait_for(lambda: hermes.calls, timeout=15)
    assert hermes.calls[-1].get("agent") == "garden", (hermes.calls, body)


def test_home_messages_belong_to_the_profile_they_were_typed_on(server, service, home, hermes):
    setup_install(home, hermes)
    chat = service.home_chat
    from speakeasy import service as S
    token = S._VIEW.set(("garden",))
    try:
        garden_msg = chat._add("user", "Garden question", NOW)
        reply = chat._add("assistant", "Garden answer", NOW, reply_to=garden_msg)
    finally:
        S._VIEW.reset(token)
    own_msg = chat._add("user", "Own question", NOW + 1)
    assert service.store.agent("msg:" + garden_msg) == "garden"
    assert service.store.agent("msg:" + reply) == "garden"
    assert service.store.agent("msg:" + own_msg) is None
    S._VIEW.set(("garden",))
    try:
        assert service._in_view(message_id=garden_msg) and not service._in_view(message_id=own_msg)
    finally:
        S._VIEW.set(None)


def test_a_call_started_on_a_profile_sends_its_work_there(service, home, hermes):
    setup_install(home, hermes)
    iid = "vi_" + "a" * 32
    service.store.set_agent("interaction:" + iid, "garden")
    service.store.reserve_run("k_invented_1", iid, "task_1", 0)
    assert service._run_agent("task_1", "k_invented_1", [], []) == "garden"
    assert service._agent_of_key("k_invented_1") == "garden"
    service.store.reserve_run("k_invented_2", "vi_" + "b" * 32, "task_2", 0)
    assert service._run_agent("task_2", "k_invented_2", [], []) is None
