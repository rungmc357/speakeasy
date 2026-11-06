"""Typed messages to one task, by its id, never through routing (docs/API.md "Messages to a task"),
and the voice saying where a spoken follow-up went."""
import json
import threading
import time

from fakes import http, wait_for
from test_routing import start_call, tasks
from speakeasy import messages as M


def send(server, run_id, text, **extra):
    return http(server.base_url, "POST", f"/voice/work/{run_id}/message", {"text": text, **extra}, server.token)


def say(worker, delegation, words):
    """A new user turn: the voice spoke in between, so it isn't joined onto the last one."""
    worker.feed({"type": "session.output_transcript.delta", "delta": "Okay.", "start_ms": 1, "end_ms": 2})
    worker.delegate(delegation, words)


def one_task(server, worker, words, status=None, delegation="call_1"):
    say(worker, delegation, words)
    pick = lambda t: t["task_id"] == delegation and t.get("run_id") and (status is None or t["status"] == status)
    return wait_for(lambda: [t for t in tasks(server) if pick(t)])[0]


def notes(worker):
    return [c for k, _, c in worker.sent if k == "session.thinking.append"]


def spoken(worker):
    return [c for k, _, c in worker.sent if k == "session.commentary.append"]


def route_to(task_id, request="x"):
    return json.dumps({"status_task_id": None, "follow_up_task_id": task_id, "conversation": None,
                       "conversation_part": None, "parts": [request], "channel": None, "show": False})


# -- POST /voice/work/{run_id}/message --------------------------------------------------------------

def test_typed_to_a_running_task_steers_it_and_the_voice_only_gets_a_silent_note(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Draft the garden newsletter")
    before = len(spoken(worker))
    status, body = send(server, task["run_id"], "Mention the tomato swap too", client_id="c_1")
    assert status == 200, body
    assert body["ok"] and body["delivered_as"] == "steer" and body["run_id"] == task["run_id"]
    assert body["task"]["run_id"] == task["run_id"]
    wait_for(lambda: hermes.steers)
    assert "Mention the tomato swap too" in hermes.steers[-1][1] and "typed" in hermes.steers[-1][1]
    wait_for(lambda: any("typed to the task" in n for n in notes(worker)))
    note = next(n for n in notes(worker) if "typed to the task" in n)
    assert "Say nothing about it" in note and "Sent." not in note
    assert len(spoken(worker)) == before                  # nothing said out loud
    assert not any("Queued" in n for n in notes(worker))  # the spoken follow-up note is not sent for it
    assert len(hermes.calls) == 1                         # no second job
    assert any(t.get("kind") == "text" and "tomato swap" in t["text"] for t in worker.turns())


def test_typed_while_muted_asks_for_silence(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service)
    worker.muted = True
    task = one_task(server, worker, "Draft the garden newsletter")
    assert send(server, task["run_id"], "Shorter please")[0] == 200
    note = wait_for(lambda: [n for n in notes(worker) if "typed to the task" in n])[0]
    assert "muted: say nothing" in note


def test_typed_to_a_task_waiting_on_its_question_is_the_answer(server, service, hermes):
    from test_question_cards import ASK
    hermes.responder = lambda prompt, sid: (ASK if "Raised beds please" not in prompt
                                            else "Ordered.\nDONE: soil\nSPOKEN: Ordered.")
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Plan the garden beds", status="completed")
    status, body = send(server, task["run_id"], "Raised beds please, but only three", client_id="c_q")
    assert status == 200 and body["delivered_as"] == "answer", body
    wait_for(lambda: len(hermes.calls) >= 2)
    assert "Raised beds please" in hermes.calls[1]["input"] and "garden" in hermes.calls[1]["input"].lower()
    assert body["run_id"] == hermes.calls[1]["run_id"]


def test_typed_to_a_finished_task_starts_a_linked_follow_up(server, service, hermes):
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Compare three bike locks", status="completed")
    status, body = send(server, task["run_id"], "Now check which one fits a thick frame", client_id="c_f")
    assert status == 200 and body["delivered_as"] == "follow_up", body
    wait_for(lambda: len(hermes.calls) >= 2)
    first, second = hermes.calls[0], hermes.calls[1]
    assert second["session_id"] == first["session_id"]          # continues the same job's session
    assert "bike locks" in second["input"].lower() and "thick frame" in second["input"]
    assert body["run_id"] == second["run_id"] != task["run_id"]
    assert body["task"]["job"]["id"] == first["session_id"]
    # The parent's row was replaced by the follow-up: a message to the parent goes to the newest one.
    wait_for(lambda: [t for t in tasks(server) if t.get("run_id") == second["run_id"] and t["status"] == "completed"])
    status, again = send(server, task["run_id"], "And the price?", client_id="c_f2")
    assert status == 200 and again["delivered_as"] == "follow_up", again
    wait_for(lambda: len(hermes.calls) >= 3)
    assert hermes.calls[2]["session_id"] == first["session_id"]


def test_typed_to_a_failed_task_starts_a_follow_up(server, service, hermes):
    hermes.fail_with = "provider exploded"
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Summarize the soil report", status="failed")
    hermes.fail_with = None
    status, body = send(server, task["run_id"], "Try again with the short version")
    assert status == 200 and body["delivered_as"] == "follow_up", body
    wait_for(lambda: len(hermes.calls) >= 2)
    assert "short version" in hermes.calls[1]["input"]


def test_cancelled_deleted_and_unknown_tasks_are_refused_in_one_sentence(server, service, hermes):
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Look up train times", status="completed")
    key = service.store.key_for_run(task["run_id"])
    status, body = send(server, "run_nope", "hello")
    assert status == 404 and body["error"] == "I couldn't find that task."
    service.store.update_run(key, None, "cancelled")
    status, body = send(server, task["run_id"], "hello")
    assert status == 409 and "cancelled" in body["error"] and body["error"].count(".") <= 2
    service.store.update_run(key, None, "completed")
    service.store.dismiss_key(key)
    status, body = send(server, task["run_id"], "hello")
    assert status == 409 and "deleted" in body["error"]
    for bad in ({"text": "  "}, {"text": "x" * 4001}, {"text": "ok", "source": "held_voice"}, {"words": "x"},
                {"text": "ok", "client_id": "has spaces"}):
        assert http(server.base_url, "POST", f"/voice/work/{task['run_id']}/message", bad, server.token)[0] == 400
    assert len(hermes.calls) == 1


def test_the_same_client_id_twice_is_sent_once(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Draft the garden newsletter")
    results = []
    threads = [threading.Thread(target=lambda: results.append(send(server, task["run_id"], "Add a recipe",
                                                                   client_id="same_1")))
               for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert [r[0] for r in results] == [200, 200, 200]
    assert len({json.dumps(r[1], sort_keys=True) for r in results}) == 1
    time.sleep(0.3)
    assert len(hermes.steers) == 1
    assert sum("typed to the task" in n for n in notes(worker)) == 1


def test_two_devices_each_get_their_message_through(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Draft the garden newsletter")
    code = service.devices.new_pairing_code()
    status, paired = http(server.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Test Phone"})
    assert status == 201
    phone = paired["token"]
    a = send(server, task["run_id"], "From the laptop", client_id="dev_a")
    b = http(server.base_url, "POST", f"/voice/work/{task['run_id']}/message",
             {"text": "From the phone", "client_id": "dev_b"}, phone)
    assert a[0] == b[0] == 200
    wait_for(lambda: len(hermes.steers) >= 2)
    said = " ".join(s for _, s in hermes.steers)
    assert "From the laptop" in said and "From the phone" in said
    assert http(server.base_url, "POST", f"/voice/work/{task['run_id']}/message", {"text": "x"}, "bad")[0] == 401


def test_typed_with_no_call_open_still_reaches_the_task(server, service, hermes):
    session, worker = start_call(server, service)
    task = one_task(server, worker, "Compare three bike locks", status="completed")
    worker.feed({"type": "session.closed"})
    status, body = send(server, task["run_id"], "Which is lightest?")
    assert status == 200 and body["delivered_as"] == "follow_up", body
    wait_for(lambda: len(hermes.calls) >= 2)
    assert hermes.calls[1]["session_id"] == hermes.calls[0]["session_id"]
    wait_for(lambda: service.store.work(body["run_id"]) and service.store.work(body["run_id"])["status"] == "completed")
    assert not any("typed to the task" in n for n in notes(worker))   # the ended call hears nothing


def test_typing_to_one_task_while_the_voice_asks_about_another_leaves_routing_alone(server, service, hermes):
    hermes.hold = True
    seen = []
    service.rt.route_call = lambda messages: seen.append(messages[-1]["content"]) or None
    _, worker = start_call(server, service)
    a = one_task(server, worker, "Draft the garden newsletter", delegation="call_a")
    one_task(server, worker, "Plan the seed order for spring", delegation="call_b")
    fragments = len(worker.fragments)
    assert send(server, a["run_id"], "Make the newsletter title punchier")[0] == 200
    wait_for(lambda: hermes.steers)
    assert len(worker.fragments) == fragments          # typed text is not part of what was said
    assert worker.focus() == (None, "")
    say(worker, "call_c", "Yes, order the heirloom ones")
    wait_for(lambda: seen)
    # Routing never sees it as said, or as focus: only as what the task itself was told (its history).
    request_line = seen[-1].split("Request: ", 1)[1]
    assert "punchier" not in request_line and "looking at" not in seen[-1]
    assert "punchier" not in seen[-1].split("The call so far", 1)[-1].split("Request: ")[0] or "call so far" not in seen[-1]


def test_a_spoken_choice_answered_with_something_else(server, service, hermes):
    _, worker = start_call(server, service)
    choice_id = __import__("asyncio").run_coroutine_threadsafe(
        worker.offer_choice("Short or long?", ["Short", "Long"]), worker.loop).result(5)
    assert choice_id
    status, body = send(server, "run_none", "Medium, with pictures", choice_task_id=choice_id)
    assert status == 200 and body["delivered_as"] == "answer", body


# -- the voice says where a spoken follow-up went; "no, the X one" moves it ----------------------

def test_spoken_follow_up_is_announced_and_a_correction_moves_it(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service)
    a = one_task(server, worker, "Update the pricing page copy", delegation="call_a")
    b = one_task(server, worker, "Fix the dashboard chart colors", delegation="call_b")
    ids = {t["run_id"]: t["task_id"] for t in tasks(server)}
    service.rt.route_call = lambda messages: route_to(ids[a["run_id"]], "also add a FAQ section")
    calls = len(hermes.calls)
    say(worker, "call_f", "also add a FAQ section")
    wait_for(lambda: hermes.steers)
    assert hermes.steers[-1][0] == a["run_id"]
    wait_for(lambda: any("Adding that to" in n for n in notes(worker)))
    say(worker, "call_no", "no, the dashboard one")
    wait_for(lambda: len(hermes.steers) >= 3)
    by_run = {}
    for run_id, text in hermes.steers:
        by_run.setdefault(run_id, []).append(text)
    assert any("FAQ" in s for s in by_run[b["run_id"]])                       # moved there
    assert any("meant for a different task" in s for s in by_run[a["run_id"]])  # the wrong one told
    assert len(hermes.calls) == calls                                          # no new job
    assert any("Moved it to" in n for n in notes(worker))


def test_wrong_task_with_no_name_asks_which_one(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service)
    a = one_task(server, worker, "Update the pricing page copy", delegation="call_a")
    one_task(server, worker, "Fix the dashboard chart colors", delegation="call_b")
    one_task(server, worker, "Book the team lunch for Friday", delegation="call_c")
    ids = {t["run_id"]: t["task_id"] for t in tasks(server)}
    service.rt.route_call = lambda messages: route_to(ids[a["run_id"]], "make it shorter")
    say(worker, "call_f", "make it shorter")
    wait_for(lambda: hermes.steers)
    say(worker, "call_no", "wrong task")
    wait_for(lambda: "Which one?" in spoken(worker))
    calls = len(hermes.calls)
    say(worker, "call_ans", "the lunch one")
    wait_for(lambda: len(hermes.steers) >= 3)
    assert len(hermes.calls) == calls


def test_correction_phrases():
    assert M.retarget_phrase("no, the dashboard one") == "dashboard"
    assert M.retarget_phrase("No the pricing page task") == "pricing page"
    assert M.retarget_phrase("wrong task") == ""
    assert M.retarget_phrase("not that one") == ""
    assert M.retarget_phrase("that's the wrong one, the lunch one") == "lunch"
    for normal in ("no thanks", "no, turn them on", "the dashboard looks great", "wrong answer, try again",
                   "no", "not now", "book the one by the window"):
        assert M.retarget_phrase(normal) is None, normal


# -- capability flag -----------------------------------------------------------------------------

def test_status_advertises_task_messages_and_keeps_older_flags(server):
    status, body = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert status == 200
    caps = body["capabilities"]
    assert caps["task_messages"] is True
    assert {"card_actions", "job_progress", "spoken_choices", "summary_points"} <= set(caps)


def test_an_older_client_that_never_uses_messages_sees_no_change(server, service, hermes):
    """No message, no hold: no transcript events, no hold state, the old answer route unchanged."""
    _, worker = start_call(server, service)
    one_task(server, worker, "Compare three bike locks", status="completed")
    with worker.interaction.feed.cond:
        kinds = {k for _, k, _ in worker.interaction.feed.ring}
    assert "transcript" not in kinds
    assert worker.typed == [] and worker.silent_ids == set() and worker.last_routed is None
    assert http(server.base_url, "POST", "/voice/interactions/x/target", {"hold": True}, server.token)[0] == 404
