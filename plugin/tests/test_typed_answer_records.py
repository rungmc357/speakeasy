"""SPK-303: a typed reply to a job's question is that question's answer.

Seen on a call: a job asked "Which layout first?", its card had been dismissed (✕, or swiped), and the
user then typed the answer. The answer reached the job, but Home kept the card on "Paused. Its
question is in the conversation." forever: the answer closed the question as answered while the older
``question_closed`` column still said "dismissed", and Home read that column alone. The answer's own
bubble also showed the question glued after the user's words ("Long bed first: Which layout first?").

Home's chat path had the same gap: a message the coordinator sent to an asking job (message_job) went
out as an ordinary follow-up and never recorded the answer, so the card stayed on Needs you / Paused.
Every fixture is invented.
"""
from __future__ import annotations

import json

from fakes import http, wait_for
from test_home_chat import coordinator, post as home_post, stream
from test_now import task
from test_routing import start_call, tasks

ASK = """Voice: plan the garden beds
I sketched two bed layouts and priced the soil.

```speakeasy-question
{"question": "Which layout should I order soil for?", "options": ["Four raised beds", "One long bed"], "recommended": 1}
```
DONE: garden
SPOKEN: I sketched two layouts and priced the soil."""

QUESTION = {"kind": "question", "question": "Which layout should I order soil for?",
            "options": ["Four raised beds", "One long bed"], "recommended": 1}


def _now(server):
    return http(server.base_url, "GET", "/voice/now?tz=America/New_York", None, server.token)[1]


def _card(body, run_id):
    return next((i for i in body["items"] if i.get("run_id") == run_id), None)


def _paused(body):
    return [i for i in body["items"] if "Paused" in str(i.get("line") or "")]


def test_an_answer_after_a_dismiss_closes_the_question_as_answered(service):
    store = service.store
    run = task(store, "k_bed", "completed", session="s_bed", request="Plan the garden beds",
               result={"spoken": "Two layouts.", "headline": "Two layouts priced.", "question": QUESTION})
    assert store.close_question("k_bed", "dismissed") == ["k_bed"]
    item = _card(service.now({"tz": "America/New_York"}), run)
    assert item and item["kind"] == "waiting" and item.get("dismissed_question"), item
    # The answer after the dismiss wins outright, in every column every app reads.
    assert store.close_question("k_bed", "answered", answer="One long bed") == ["k_bed"]
    assert store.question_state("k_bed") == "answered"
    work = store.work(run)
    assert work.get("question_closed") is None and work.get("answered") is True
    assert work.get("question_answer") == "One long bed"
    item = _card(service.now({"tz": "America/New_York"}), run)
    assert item is None or (item["kind"] != "waiting" and not item.get("dismissed_question")), item
    # Undo of the old dismiss can't reopen an answered question.
    assert store.reopen_question("k_bed") is False
    assert store.question_state("k_bed") == "answered"


def test_a_typed_answer_on_a_call_after_a_dismiss_leaves_paused(server, service, hermes):
    hermes.responder = lambda prompt, sid: (ASK if "garden" in prompt.lower() and "One long bed" not in prompt
                                            else "Ordered.\nDONE: soil\nSPOKEN: Soil is ordered for the long bed.")
    session, worker = start_call(server, service)
    worker.delegate("call_q", "Plan the garden beds for spring")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    run_id = done["run_id"]
    status, _ = http(server.base_url, "POST", f"/voice/tasks/{run_id}/question/dismiss", {}, server.token)
    assert status == 200
    assert _paused(_now(server)), "dismissed and unanswered: the card says Paused"
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/answer",
                        {"task_id": done["task_id"], "text": "One long bed first"}, server.token)
    assert status == 200 and body["sent"], body
    wait_for(lambda: len(hermes.calls) >= 2)
    key = service.store.key_for_run(run_id)
    assert service.store.question_state(key) == "answered"
    assert service.store.question_answer(key) == "One long bed first"
    now = _now(server)
    assert not _paused(now), [i.get("line") for i in now["items"]]
    # The thread shows the user's own words for the answer, never the question glued after them.
    asks = [e["text"] for e in now["stream"]["entries"] if e["kind"] == "ask"]
    assert "One long bed first" in asks, asks
    assert not any("Which layout" in a for a in asks), asks
    # The job itself still gets the question with the answer, so it knows what was answered.
    assert "One long bed first" in hermes.calls[1]["input"]


def test_home_chat_follow_up_to_an_asking_job_records_the_answer(server, service, hermes):
    store = service.store
    run = task(store, "k_hc", "completed", session="s_hc", request="Plan the garden beds",
               result={"spoken": "Two layouts.", "headline": "Two layouts priced.", "question": QUESTION})
    item = _card(_now(server), run)
    assert item and item["kind"] == "question"
    seen = coordinator(service, {"tool": "message_job", "text": "",
                                 "args": {"job": "j1", "request": "One long bed", "reply": "Going with the long bed."}})
    status, body = home_post(server, {"text": "the long one", "client_id": "c_ans1"})
    assert status == 200 and body["action"] == "follow_up", body
    assert "Asking the user: Which layout should I order soil for?" in seen[0][0]["content"]
    assert store.question_state("k_hc") == "answered"
    # The job's own reading of the reply ("the long one" -> the option it names) is the receipt.
    assert store.question_answer("k_hc") in ("One long bed", "the long one")
    assert not [i for i in _now(server)["items"] if i["kind"] == "question" and i.get("run_id") == run]


def test_home_chat_answer_to_a_dismissed_question_records_it(server, service, hermes):
    store = service.store
    run = task(store, "k_hd", "completed", session="s_hd", request="Plan the garden beds",
               result={"spoken": "Two layouts.", "headline": "Two layouts priced.", "question": QUESTION})
    assert store.close_question("k_hd", "dismissed") == ["k_hd"]
    assert _paused(_now(server))
    seen = coordinator(service, {"tool": "message_job", "text": "",
                                 "args": {"job": "j1", "request": "One long bed", "reply": "Long bed it is."}})
    status, body = home_post(server, {"text": "long bed first", "client_id": "c_ans2"})
    assert status == 200 and body["action"] == "follow_up", body
    jobs = seen[0][0]["content"]
    # The coordinator sees a paused job that still asks, so it can tell the reply is the answer.
    assert "paused, waiting on the user" in jobs and "Asking the user: Which layout" in jobs, jobs
    assert "Paused. Its question" not in jobs
    assert store.question_state("k_hd") == "answered"
    assert not _paused(_now(server))


def test_a_swipe_reply_to_an_asking_card_is_its_answer_even_as_new_work(server, service, hermes):
    store = service.store
    run = task(store, "k_sw", "completed", session="s_sw", request="Plan the garden beds",
               result={"spoken": "Two layouts.", "headline": "Two layouts priced.", "question": QUESTION})
    # The coordinator misreads the reply as a new job; the quoted card says it answers its question.
    coordinator(service, {"tool": "start_job", "text": "",
                          "args": {"request": "Order soil for one long bed", "more_jobs": [], "reply": "On the soil."}})
    status, body = home_post(server, {"text": "one long bed", "client_id": "c_ans3", "reply_to": f"r:{run}"})
    assert status == 200 and body["action"] == "follow_up", body
    assert store.question_state("k_sw") == "answered"
    assert len([t for t in tasks(server) if t.get("request") == "Order soil for one long bed"]) == 0


def test_a_message_to_a_job_with_no_question_records_nothing(server, service, hermes):
    store = service.store
    run = task(store, "k_nq", "completed", session="s_nq", request="Plan the garden beds",
               result={"spoken": "Done.", "headline": "Beds planned."})
    coordinator(service, {"tool": "message_job", "text": "",
                          "args": {"job": "j1", "request": "Add a herb bed", "reply": "Adding a herb bed."}})
    status, body = home_post(server, {"text": "add a herb bed too", "client_id": "c_nq"})
    assert status == 200 and body["action"] == "follow_up", body
    assert store.question_state("k_nq") is None
    assert json.dumps(store.work(run)).count("question_answer") == 0
