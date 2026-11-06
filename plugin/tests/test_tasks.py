"""Parallel tasks, one Hermes session per task, follow-up routing, approvals, stop, dismiss,
pause/resume, idle, recap, notices."""
from __future__ import annotations

import threading
import time

from fakes import SDP, http, wait_for
from speakeasy import router
from speakeasy.prompt import builder as P


def start_call(server, service, key="req_call_1"):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def tasks(server):
    return http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]


def test_result_is_spoken_and_shown(server, service, hermes):
    hermes.responder = lambda prompt, sid: "It is 21C and sunny in Lisbon.\nDONE: Weather checked\nSPOKEN: It's 21 and sunny."
    _, worker = start_call(server, service)
    worker.delegate("call_weather", "What's the weather in Lisbon?")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    assert done[0]["result"]["spoken"] == "It's 21 and sunny."
    assert "Lisbon" in done[0]["result"]["full"]
    wait_for(lambda: any("It's 21 and sunny." in c for _, _, c in worker.sent))
    prompt = hermes.calls[0]["input"]
    assert "What's the weather in Lisbon?" in prompt and "SPOKEN:" in prompt


def test_parallel_tasks_get_their_own_sessions(server, service, hermes):
    _, worker = start_call(server, service)
    worker.delegate("call_a", "Book a table for two tonight")
    # The voice answered in between: two requests of their own. Without it, the second handoff can
    # land inside the merge window and is (correctly) read as the rest of the first sentence.
    worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_b", "Find the cheapest flight to Rome next week")
    wait_for(lambda: len([t for t in tasks(server) if t["status"] == "completed"]) == 2)
    sessions = {c["session_id"] for c in hermes.calls}
    assert len(hermes.calls) == 2 and len(sessions) == 2 and None not in sessions


def test_marked_follow_up_continues_same_session(server, service, hermes):
    _, worker = start_call(server, service)
    worker.delegate("call_first", "Draft a packing list for the Rome trip")
    first = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    worker.delegate("call_second", "Add sunscreen to it", task_id=first["task_id"])
    wait_for(lambda: len(hermes.calls) == 2)
    assert hermes.calls[1]["session_id"] == hermes.calls[0]["session_id"]


def test_router_defaults_to_new_task_and_honors_marks():
    open_tasks = [router.OpenTask("t1", "Plan the Rome trip itinerary", "running"),
                  router.OpenTask("t2", "Order new running shoes", "completed")]
    assert [p.kind for p in router.route("What's the capital of Peru?", open_tasks)] == [router.NEW]
    assert [p.task_id for p in router.route("anything", open_tasks, marked_task_id="t2")] == ["t2"]
    assert [p.kind for p in router.route("anything", open_tasks, marked_task_id="nope")] == [router.NEW]
    also = router.route("Also add a day in Florence to the Rome trip itinerary", open_tasks)
    assert also[0].task_id == "t1"


def test_stop_task(server, service, hermes):
    hermes.hold = True
    session, worker = start_call(server, service)
    worker.delegate("call_long", "Research every hotel in Kyoto")
    task = wait_for(lambda: [t for t in tasks(server) if t.get("run_id")])[0]
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/cancel-backend",
                        {"run_id": task["run_id"]}, server.token)
    assert status == 202
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "cancelled"])


def test_stop_task_by_id_with_or_without_a_call(server, service, hermes):
    """A panel row's Stop: POST /voice/work/{run_id}/stop finds the call or voiceless worker holding it."""
    hermes.hold = True
    _, worker = start_call(server, service)
    worker.delegate("call_long2", "Research every hotel in Kyoto")
    task = wait_for(lambda: [t for t in tasks(server) if t.get("run_id")])[0]
    status, body = http(server.base_url, "POST", f"/voice/work/{task['run_id']}/stop", {}, server.token)
    assert status == 202, body
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "cancelled"])
    again = http(server.base_url, "POST", f"/voice/work/{task['run_id']}/stop", {}, server.token)
    assert again[0] == 409
    typed = http(server.base_url, "POST", "/voice/work", {"text": "Find a quiet café near the office"}, server.token)[1]
    status, body = http(server.base_url, "POST", f"/voice/work/{typed['run_id']}/stop", {}, server.token)
    assert status == 202, body
    assert http(server.base_url, "POST", "/voice/work/run_nope123/stop", {}, server.token)[0] in {404, 409}
    caps = http(server.base_url, "GET", "/voice/status", token=server.token)[1]["capabilities"]
    assert caps["task_stop"] is True


def test_dismiss_finished_tasks(server, service, hermes):
    _, worker = start_call(server, service)
    worker.delegate("call_x", "Check my calendar tomorrow")
    task = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    status, body = http(server.base_url, "POST", "/voice/tasks/dismiss", {"run_ids": [task["run_id"]]}, server.token)
    assert status == 200
    assert not [t for t in tasks(server) if t["task_id"] == task["task_id"]]


def test_approval_round_trip(server, service, hermes):
    hermes.approval_first = True
    session, worker = start_call(server, service)
    worker.delegate("call_risky", "Clean up my downloads folder")
    iid = session["interaction_id"]
    snap = wait_for(lambda: (lambda b: b if b.get("approval") else None)(
        http(server.base_url, "GET", f"/voice/interactions/{iid}", token=server.token)[1]))
    approval = snap["approval"]
    url = f"/voice/interactions/{iid}/approval"
    base = {"run_id": approval["run_id"], "request_id": approval["request_id"]}
    assert http(server.base_url, "POST", url, {**base, "choice": "always"}, server.token)[0] == 400
    assert http(server.base_url, "POST", url, {**base, "request_id": "apr_stale", "choice": "once"}, server.token)[0] == 409
    status, body = http(server.base_url, "POST", url, {**base, "choice": "once"}, server.token)
    assert status == 200 and body["choice"] == "once"
    assert hermes.approvals[-1]["choice"] == "once"
    wait_for(lambda: any("Needs your approval" in c or "approv" in c.lower() for _, _, c in worker.sent))


def test_pause_then_resume_reseeds_conversation(server, service, hermes):
    session, worker = start_call(server, service)
    worker.feed({"type": "session.input_transcript.delta", "delta": "Remind me what we planned", "start_ms": 1, "end_ms": 2})
    worker.feed({"type": "session.output_transcript.delta", "delta": "We planned the Rome trip.", "start_ms": 3, "end_ms": 4})
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/pause", {}, server.token)
    assert status == 200 and body["paused"] is True
    status, resumed = http(server.base_url, "POST", "/voice/sessions",
                           {"sdp": SDP, "resume_from": session["interaction_id"]}, server.token,
                           {"Idempotency-Key": "req_resume_1"})
    assert status == 201, resumed
    assert resumed["interaction_id"] != session["interaction_id"]
    assert resumed["resumed_from"] == session["interaction_id"]
    history = service.pause_history(service.interaction(session["interaction_id"]))
    assert any("Rome trip" in turn["text"] for turn in history)
    # a second resume of the same paused call is refused
    status, _ = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP, "resume_from": session["interaction_id"]},
                     server.token, {"Idempotency-Key": "req_resume_2"})
    assert status == 409


def test_idle_calls_close(server, service):
    session, worker = start_call(server, service)
    interaction = service.interaction(session["interaction_id"])
    interaction.last_activity = time.monotonic() - 10_000
    closed = service.idle_check(now=time.monotonic())
    assert session["interaction_id"] in closed


def test_while_you_were_away_recap():
    away = [{"request": "Order printer ink", "status": "completed", "spoken": "Ordered the ink.", "task_id": "t1"}]
    block = P.away_block(away, P.Names("Hermes", "Sam"))
    assert "Order printer ink" in block and "Ordered the ink." in block  # known, for "what happened with..."


def test_finished_work_is_not_announced_when_they_call_back():
    """Calling back hours later and saying "hey" must not start with "by the way, that's done":
    the result already went to the app and chat."""
    away = [{"request": "Order printer ink", "status": "completed", "spoken": "Ordered the ink."}]
    block = P.away_block(away, P.Names("Hermes", "Sam"))
    assert "do not open the call" in block and "Don't bring these up" in block
    assert "Open the call by telling" not in block


def test_an_approval_waiting_is_raised_once_after_they_speak():
    away = [{"request": "Send the invoice", "status": "waiting_for_approval"}]
    block = P.away_block(away, P.Names("Hermes", "Sam"))
    assert "Send the invoice" in block and "Respond to whatever Sam opens with first" in block


def test_rules_are_generic_and_templated():
    names = P.Names("Nova", "Sam", "the studio Mac")
    text = P.build_live_instructions(names)
    for heading in ("# Personality", "# Scope before you start work", "# Delegation policy", "# Parallel tasks",
                    "# Email drafts"):
        assert heading in text
    assert "Never invent people" in text  # truthfulness rule
    assert "Nova" in text and "Sam" in text and "{assistant_name}" not in text and "{user_name}" not in text
    blank = P.build_live_instructions(P.Names())
    assert "Hermes" in blank and "{" not in blank.replace("{}", "")


def test_notices_off_by_default(service):
    assert service.settings.get()["delivery"]["target"] == "none"
    assert service.notices.post("k1", "hello") is False
