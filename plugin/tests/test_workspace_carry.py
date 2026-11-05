"""The workspace survives a quiet call's auto-pause and a new call: every task that was running,
waiting on the user, or finished but not cleared stays on the board until the user clears it, and
a new call knows about the open ones (the router can send follow-ups to them)."""
from __future__ import annotations

import time

from fakes import SDP, http, wait_for
from speakeasy.calls import interaction_tasks


def start_call(server, service, key):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def board(server):
    return http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]


def call_tasks(service, interaction_id):
    return interaction_tasks(service.store, service.interaction(interaction_id), "Hermes")


def three_jobs(server, service, hermes, worker):
    """One finished, one still running, one waiting for approval."""
    worker.delegate("call_done", "Check my calendar tomorrow")
    wait_for(lambda: [t for t in board(server) if t["status"] == "completed"])
    hermes.approval_first = True
    worker.delegate("call_ask", "Clean up my downloads folder")
    wait_for(lambda: [t for t in board(server) if t["status"] == "waiting_for_approval"])
    hermes.approval_first = False
    hermes.hold = True
    worker.delegate("call_run", "Research every hotel in Kyoto")
    wait_for(lambda: len(board(server)) == 3 and [t for t in board(server) if t["status"] in {"running", "working"}])
    return {t["task_id"]: t["status"] for t in board(server)}


def idle_pause(service, interaction_id):
    service.interaction(interaction_id).last_activity = time.monotonic() - 10_000
    assert interaction_id in service.idle_check(now=time.monotonic())


def test_new_call_after_idle_pause_keeps_the_workspace(server, service, hermes):
    first, worker = start_call(server, service, "req_ws_1")
    before = three_jobs(server, service, hermes, worker)
    assert len(before) == 3
    idle_pause(service, first["interaction_id"])
    # the idle board (no call) still shows all three
    assert {t["task_id"] for t in board(server)} == set(before)

    said: list[str] = []
    real = service.instructions
    service.instructions = lambda **kw: said.append(real(**kw)) or said[-1]
    second, worker2 = start_call(server, service, "req_ws_2")  # a NEW call, not Resume
    shown = {t["task_id"]: t["status"] for t in call_tasks(service, second["interaction_id"])}
    assert set(shown) == set(before), shown
    # the new call's voice is told about the open jobs
    block = said[-1][said[-1].find("# Open jobs from earlier calls"):]
    assert block, said[-1][-800:]
    assert "finished" in block and "waiting for the user's approval" in block and "still working" in block
    assert "Don't open the call with them" in block
    # ... and the router can route a follow-up to them
    assert {t.task_id for t in worker2.open_tasks()} >= {"call_run", "call_ask"}
    # the approval is answerable from the new call
    snap = http(server.base_url, "GET", f"/voice/interactions/{second['interaction_id']}", token=server.token)[1]
    assert snap["approval"] and snap["approval"]["delegation_id"] == "call_ask"


def test_cleared_task_stays_cleared_on_a_new_call(server, service, hermes):
    first, worker = start_call(server, service, "req_ws_c1")
    worker.delegate("call_done", "Check my calendar tomorrow")
    done = wait_for(lambda: [t for t in board(server) if t["status"] == "completed"])[0]
    assert http(server.base_url, "POST", "/voice/tasks/dismiss", {"run_ids": [done["run_id"]]}, server.token)[0] == 200
    http(server.base_url, "POST", f"/voice/interactions/{first['interaction_id']}/pause", {}, server.token)
    second, _ = start_call(server, service, "req_ws_c2")
    assert not call_tasks(service, second["interaction_id"])
    assert not board(server)


def test_resume_restores_the_same_view(server, service, hermes):
    first, worker = start_call(server, service, "req_ws_r1")
    before = three_jobs(server, service, hermes, worker)
    idle_pause(service, first["interaction_id"])
    status, resumed = http(server.base_url, "POST", "/voice/sessions",
                           {"sdp": SDP, "resume_from": first["interaction_id"]}, server.token,
                           {"Idempotency-Key": "req_ws_r2"})
    assert status == 201, resumed
    shown = {t["task_id"]: t["status"] for t in call_tasks(service, resumed["interaction_id"])}
    assert set(shown) == set(before)


def test_finished_before_upgrade_is_not_dumped_on_the_board(server, service, hermes):
    """Rows that settled before carrying existed stay off a new call's board (no history flood)."""
    first, worker = start_call(server, service, "req_ws_u1")
    worker.delegate("call_done", "Check my calendar tomorrow")
    wait_for(lambda: [t for t in board(server) if t["status"] == "completed"])
    service.store.set_meta("board_carry_since", str(time.time() + 60))
    http(server.base_url, "POST", f"/voice/interactions/{first['interaction_id']}/end", {}, server.token)
    service.interaction(first["interaction_id"]).call_closed = True
    second, _ = start_call(server, service, "req_ws_u2")
    assert not call_tasks(service, second["interaction_id"])


def test_call_settings_defaults_and_no_limit(service, server):
    from speakeasy.settings import DEFAULTS, SettingsError, max_call_seconds, validate
    import pytest
    assert DEFAULTS["idle_pause_minutes"] == 15 and DEFAULTS["voice"]["max_call_minutes"] == 30
    clean = validate({"voice": {"max_call_minutes": 0}, "idle_pause_minutes": 0})
    assert clean["voice"]["max_call_minutes"] == 0 and max_call_seconds(clean) == float("inf")
    assert max_call_seconds(validate({})) == 30 * 60
    for bad in ({"voice": {"max_call_minutes": -1}}, {"voice": {"max_call_minutes": 241}},
                {"voice": {"max_call_minutes": True}}, {"idle_pause_minutes": 121}, {"idle_pause_minutes": False}):
        with pytest.raises(SettingsError):
            validate(bad)
    status, body = http(server.base_url, "PATCH", "/voice/settings", {"voice": {"max_call_minutes": 0}}, server.token)
    assert status == 200, body
    assert service.settings.get()["voice"]["max_call_minutes"] == 0
    # a stored value is never rewritten by the new default
    service.settings.patch({"idle_pause_minutes": 5})
    assert service.settings.get()["idle_pause_minutes"] == 5
    caps = http(server.base_url, "GET", "/voice/status", token=server.token)[1]["capabilities"]
    assert caps["call_no_limit"] is True


def test_idle_pause_says_how_long(server, service, hermes):
    first, _ = start_call(server, service, "req_ws_i1")
    service.settings.patch({"idle_pause_minutes": 10})
    idle_pause(service, first["interaction_id"])
    snap = http(server.base_url, "GET", f"/voice/interactions/{first['interaction_id']}", token=server.token)[1]
    assert snap["paused"] is True and snap["idle_paused_minutes"] == 10
    second, _ = start_call(server, service, "req_ws_i2")
    other = http(server.base_url, "POST", f"/voice/interactions/{second['interaction_id']}/pause", {}, server.token)[1]
    assert "idle_paused_minutes" not in other


def test_open_work_block_lists_each_state():
    from speakeasy.prompt import builder as P
    names = P.Names("Hermes", "Sam")
    block = P.open_work_block([
        {"title": "Kyoto hotels", "status": "running", "short_status": "Reading reviews"},
        {"title": "Clean downloads", "status": "waiting_for_approval"},
        {"title": "Calendar tomorrow", "status": "completed", "result": {"spoken": "Two meetings."}},
    ], names)
    assert "Kyoto hotels: still working (Reading reviews)" in block
    assert "Clean downloads: waiting for Sam's approval" in block
    assert "Calendar tomorrow: finished: Two meetings." in block
    assert P.open_work_block([], names) == ""
    full = P.build_live_instructions(names, open_work=block)
    assert "Open jobs from earlier calls" in full
