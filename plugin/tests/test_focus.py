"""Vision Pro: requests go to the task the user is facing."""
from fakes import http, wait_for
from speakeasy import router
from test_routing import start_call, tasks


def _focus(server, session, body):
    return http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/focus", body, server.token)


def test_wants_new():
    assert router.wants_new("new thing: book a haircut") and router.wants_new("something else, check the score")
    assert not router.wants_new("make it shorter")


def test_focus_rejects_bad_bodies(server, service):
    session, _ = start_call(server, service)
    assert _focus(server, session, {"detail": "x"})[0] == 400
    assert _focus(server, session, {"task_id": "../x"})[0] == 400
    assert _focus(server, session, {"task_id": None, "detail": "x" * 201})[0] == 400
    assert _focus(server, session, {"task_id": None})[0] == 200


def test_an_unnamed_request_goes_to_the_task_you_face(server, service, hermes):
    hermes.hold = True
    session, worker = start_call(server, service)
    worker.delegate("call_a", "Plan a four day trip to Lisbon")
    wait_for(lambda: len(hermes.calls) >= 1)
    worker.feed({"type": "session.output_transcript.delta", "delta": "Got it.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_b", "Draft the launch email for the newsletter")
    wait_for(lambda: len(hermes.calls) >= 2)
    lisbon = next(t["task_id"] for t in tasks(server) if "Lisbon" in str(t["events"]))
    assert _focus(server, session, {"task_id": lisbon, "detail": "day two of the draft"})[0] == 200
    wait_for(lambda: any("looking at the task" in c for k, _, c in worker.sent if k == "session.thinking.append"))
    worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 3, "end_ms": 4})
    worker.delegate("call_c", "make it lighter, fewer museums")
    wait_for(lambda: hermes.steers or len(hermes.calls) >= 3)
    sent = [t for _, t in hermes.steers] + [c["input"] for c in hermes.calls[2:]]
    assert any("day two of the draft" in s for s in sent), sent
    first_run = list(hermes.runs)[0]
    assert not hermes.steers or hermes.steers[-1][0] == first_run
