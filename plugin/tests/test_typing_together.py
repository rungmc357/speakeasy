"""The two typing features on one call: typing to the call (``input-text``, pinkhoodie's) counts
exactly like saying it; typing to a job (``/voice/work/{run}/message``) goes to that job by id. One
box per screen, so neither guesses a target from what's on screen."""
from fakes import http, wait_for
from speakeasy.calls import INPUT_TEXT_TYPE
from test_routing import start_call, tasks


def typed_to_call(server, iid, text, **extra):
    return http(server.base_url, "POST", f"/voice/interactions/{iid}/input-text",
                {"type": INPUT_TEXT_TYPE, "text": text, **extra}, server.token)


def typed_to_job(server, run_id, text, iid=None):
    body = {"text": text, "client_id": f"c_{abs(hash(text))}"}
    if iid:
        body["interaction_id"] = iid
    return http(server.base_url, "POST", f"/voice/work/{run_id}/message", body, server.token)


def stream_copies(service, iid):
    return [p for _, kind, p in list(service.interaction(iid).feed.ring) if kind == "transcript"]


def test_status_lists_both_typing_features_together(server):
    status, body = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert status == 200 and body["input_text"] is True
    assert body["capabilities"]["input_text"] is True and body["capabilities"]["task_messages"] is True


def test_typing_to_the_call_cannot_aim_at_a_job(server, service):
    session, _ = start_call(server, service, key="req_together_aim")
    status, body = typed_to_call(server, session["interaction_id"], "make it after lunch", task_id="call_1")
    assert status == 400 and body["reason"] == "invalid_body"


def test_job_messages_carry_their_task_and_call_turns_never_do(server, service, hermes):
    """The app tells the two stream copies apart by task_id: a job message's copy has one (shown with
    the job's name, not as a call turn); a typed call turn's copy has none."""
    hermes.hold = True
    session, worker = start_call(server, service, key="req_together_copies")
    iid = session["interaction_id"]
    worker.feed({"type": "session.output_transcript.delta", "delta": "Okay.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_1", "Draft the garden newsletter")
    task = wait_for(lambda: [t for t in tasks(server) if t["task_id"] == "call_1" and t.get("run_id")])[0]
    fragments = len(worker.fragments)
    assert typed_to_job(server, task["run_id"], "Mention the tomato swap", iid=iid)[0] == 200
    assert len(worker.fragments) == fragments, "a job message is not part of what was said"
    assert typed_to_call(server, iid, "Also what's the weather")[0] == 200
    # The call turn is part of what was said (it joins the user's last spoken turn); the job message
    # is only a labelled text turn.
    said = [t for t in worker.turns() if t.get("kind") != "text"]
    assert said[-1]["role"] == "user" and said[-1]["text"].endswith("Also what's the weather")
    assert [t["text"] for t in worker.turns() if t.get("kind") == "text"] == [
        "(typed to Draft the garden newsletter) Mention the tomato swap"]
    copies = wait_for(lambda: len(stream_copies(service, iid)) >= 2 and stream_copies(service, iid))
    job, call = copies[0], copies[-1]
    assert job["task_id"] == "call_1" and job["text"] == "Mention the tomato swap"
    assert "task_id" not in call and call == {"role": "user", "text": "Also what's the weather", "source": "typed"}


def test_a_typed_turn_routes_exactly_like_the_same_words_said(server, service, hermes):
    """Typing to the call = speaking: the same words start the same job, with the same request."""
    hermes.hold = True
    words = "Plan the seed order for spring"

    def run(delegation, typed):
        session, worker = start_call(server, service, key=f"req_together_{delegation}")
        worker.feed({"type": "session.output_transcript.delta", "delta": "Hi.", "start_ms": 1, "end_ms": 2})
        before = len(hermes.calls)
        if typed:
            assert typed_to_call(server, session["interaction_id"], words)[0] == 200
            worker.feed({"type": "session.delegation.created", "offset_ms": 5,
                         "delegation": {"id": delegation, "target": "client"}})
        else:
            worker.delegate(delegation, words)
        wait_for(lambda: len(hermes.calls) > before)
        task = wait_for(lambda: [t for t in tasks(server) if t["task_id"] == delegation])[0]
        return len(hermes.calls) - before, task.get("request") or task.get("title"), worker.context(10_000)

    said, typed = run("call_said", typed=False), run("call_typed", typed=True)
    assert said[0] == typed[0] == 1, "one job each"
    assert said[1] and said[1] == typed[1], (said[1], typed[1])
    assert said[2].endswith(f"User: {words}") and typed[2].endswith(f"User: {words}")
