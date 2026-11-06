"""Steer or queue (docs/API.md "Steer or queue"): a message to a running task goes into it now (steer,
the default) or waits and is sent as the next turn once the task completes or fails (queue). Invented
tasks and words only."""
import json
import time

import pytest

from fakes import http, wait_for
from speakeasy.store import StateStore
from test_history import NOW, add_msg, add_session, state  # noqa: F401  (fixture)


def post(server, path, body):
    return http(server.base_url, "POST", path, body, server.token)


def get(server, path):
    return http(server.base_url, "GET", path, None, server.token)


def delete(server, path):
    return http(server.base_url, "DELETE", path, None, server.token)


def running_job(server, service, hermes, text="Sort the seed packets by month"):
    hermes.hold = True
    status, started = post(server, "/voice/work", {"text": text})
    assert status == 202, started
    run_id = started["run_id"]
    wait_for(lambda: hermes.calls and (service.store.work(run_id) or {}).get("status") == "running")
    return run_id


def finish(hermes, run_id, failed=False):
    run = hermes.runs[run_id]
    if failed:
        run["status"] = "failed"
        run["error"] = "provider exploded"
        run["events"] = [{"event": "run.failed", "completed": False, "partial": False, "interrupted": False,
                          "error": "provider exploded"}]
    else:
        run["status"] = "completed"
    run["done"].set()


def send(server, run_id, text, **extra):
    return post(server, f"/voice/work/{run_id}/message", {"text": text, **extra})


def test_status_advertises_message_modes(server):
    _, status = get(server, "/voice/status")
    assert status["capabilities"]["message_modes"] is True and status["capabilities"]["task_messages"] is True


def test_steer_is_the_default_and_queue_waits_for_the_turn_to_end(server, service, hermes):
    run_id = running_job(server, service, hermes)
    status, steered = send(server, run_id, "Put the tomatoes first", client_id="m_steer")
    assert status == 200 and steered["delivered_as"] == "steer", steered
    wait_for(lambda: hermes.steers)
    status, queued = send(server, run_id, "Then list what to sow in April", client_id="m_q1", mode="queue")
    assert status == 200, queued
    assert queued["delivered_as"] == "queued" and queued["queue_position"] == 1 and queued["run_id"] == run_id
    status, second = send(server, run_id, "And print it as a checklist", client_id="m_q2", mode="queue")
    assert second["queue_position"] == 2
    time.sleep(0.4)
    assert len(hermes.steers) == 1 and len(hermes.calls) == 1        # nothing went into the running turn
    work = get(server, f"/voice/work/{run_id}")[1]["work"]
    assert [q["text"] for q in work["queued"]] == ["Then list what to sow in April", "And print it as a checklist"]
    assert [q["position"] for q in work["queued"]] == [1, 2] and all(q["at"] for q in work["queued"])
    # The turn ends: the first held message is the next turn in the same session, sent once.
    finish(hermes, run_id)
    wait_for(lambda: len(hermes.calls) >= 2)
    assert "sow in April" in hermes.calls[1]["input"] and hermes.calls[1]["session_id"] == hermes.calls[0]["session_id"]
    wait_for(lambda: service.store.queued_message("m_q1")["state"] == "sent")
    time.sleep(0.5)
    assert len(hermes.calls) == 2                                      # the second waits for that turn
    assert service.store.queued_message("m_q1")["state"] == "sent"
    assert service.store.queue_position("m_q2") == 1
    finish(hermes, hermes.calls[1]["run_id"])
    wait_for(lambda: len(hermes.calls) >= 3)
    assert "checklist" in hermes.calls[2]["input"] and hermes.calls[2]["session_id"] == hermes.calls[0]["session_id"]
    finish(hermes, hermes.calls[2]["run_id"])
    time.sleep(0.5)
    assert len(hermes.calls) == 3 and service.drain_queued() == 0    # each sent exactly once
    assert len(hermes.steers) == 1


def test_queue_to_a_task_that_is_not_running_is_just_sent(server, service, hermes):
    status, started = post(server, "/voice/work", {"text": "Compare two rain barrels"})
    run_id = started["run_id"]
    wait_for(lambda: (service.store.work(run_id) or {}).get("status") == "completed")
    status, body = send(server, run_id, "Which one is cheaper?", mode="queue", client_id="m_idle")
    assert status == 200 and body["delivered_as"] == "follow_up", body
    wait_for(lambda: len(hermes.calls) >= 2)


def test_a_queued_message_is_sent_after_a_failed_turn_but_kept_after_a_stop(server, service, hermes):
    run_id = running_job(server, service, hermes)
    send(server, run_id, "Try the shorter list", mode="queue", client_id="m_fail")
    finish(hermes, run_id, failed=True)
    wait_for(lambda: len(hermes.calls) >= 2)
    assert "shorter list" in hermes.calls[1]["input"]
    finish(hermes, hermes.calls[1]["run_id"])
    # A stopped task keeps its queue: shown, never sent on its own.
    other = running_job(server, service, hermes, "Draft the compost schedule")
    send(server, other, "Add the leaf pile", mode="queue", client_id="m_stop")
    calls = len(hermes.calls)
    assert post(server, f"/voice/work/{other}/stop", {})[0] == 202
    wait_for(lambda: (service.store.work(other) or {}).get("status") in {"cancelled", "interrupted"})
    time.sleep(0.5)
    assert service.drain_queued() == 0 and len(hermes.calls) == calls
    assert [q["text"] for q in get(server, f"/voice/work/{other}")[1]["work"]["queued"]] == ["Add the leaf pile"]


def test_cancel_and_steer_now(server, service, hermes):
    run_id = running_job(server, service, hermes)
    send(server, run_id, "Skip the herbs", mode="queue", client_id="m_c1")
    send(server, run_id, "Mention the frost date", mode="queue", client_id="m_c2")
    status, body = delete(server, f"/voice/work/{run_id}/queued/m_c1")
    assert status == 200 and body["cancelled"], body
    assert delete(server, f"/voice/work/{run_id}/queued/m_c1")[0] == 200     # a retry: still cancelled
    assert delete(server, f"/voice/work/{run_id}/queued/m_none")[0] == 404
    # Steer now: the held message goes into the running turn and leaves the queue.
    status, now = post(server, f"/voice/work/{run_id}/queued/m_c2/steer", {})
    assert status == 200 and now["delivered_as"] == "steer" and now["client_id"] == "m_c2", now
    wait_for(lambda: hermes.steers)
    assert "frost date" in hermes.steers[-1][1]
    assert get(server, f"/voice/work/{run_id}")[1]["work"]["queued"] == []
    again = post(server, f"/voice/work/{run_id}/queued/m_c2/steer", {})
    assert again[0] == 200 and again[1]["delivered_as"] == "steer"         # a retry is the same send
    assert delete(server, f"/voice/work/{run_id}/queued/m_c2")[0] == 409
    finish(hermes, run_id)
    time.sleep(0.6)
    assert len(hermes.calls) == 1 and len(hermes.steers) == 1               # nothing left to send


def test_the_same_client_id_is_held_once(server, service, hermes):
    run_id = running_job(server, service, hermes)
    replies = [send(server, run_id, "Add the bean poles", mode="queue", client_id="m_same")[1] for _ in range(3)]
    assert {r["delivered_as"] for r in replies} == {"queued"} and {r["queue_position"] for r in replies} == {1}
    assert len(service.store.queued_messages()) == 1
    finish(hermes, run_id)
    wait_for(lambda: service.store.queued_message("m_same")["state"] == "sent")
    after = send(server, run_id, "Add the bean poles", mode="queue", client_id="m_same")[1]
    assert after["queue_state"] == "sent" and after["sent_as"] == "follow_up" and after["queue_position"] is None
    time.sleep(0.4)
    assert len(hermes.calls) == 2


def test_bad_modes_and_attachments_are_refused(server, service, hermes):
    run_id = running_job(server, service, hermes)
    assert send(server, run_id, "x", mode="later")[0] == 400
    assert post(server, "/voice/history/abc/message", {"text": "x", "mode": "soon"})[0] == 400


def test_held_messages_survive_a_reload(server, service, hermes):
    run_id = running_job(server, service, hermes)
    send(server, run_id, "Then water the seedlings", mode="queue", client_id="m_reload")
    # The plugin reloads: the turn ends while nothing is listening.
    service.store.settle_listeners.clear()
    finish(hermes, run_id)
    wait_for(lambda: (service.store.work(run_id) or {}).get("status") == "completed")
    time.sleep(0.4)
    assert len(hermes.calls) == 1
    reopened = StateStore(service.dir / "state.sqlite3")
    assert [r["text"] for r in reopened.queued_messages()] == ["Then water the seedlings"]
    # The first idle check after the reload sends it.
    assert service.drain_queued() == 1
    wait_for(lambda: len(hermes.calls) >= 2)
    assert "water the seedlings" in hermes.calls[1]["input"]
    assert service.drain_queued() == 0


def test_a_session_page_lists_queued_messages(server, service, hermes, state):
    run_id = running_job(server, service, hermes, "Draft the bake sale flyer")
    sid = wait_for(lambda: service.store.session_for(service.store.key_for_run(run_id)))
    add_session(state, sid, "api_server", at=NOW - 5)
    add_msg(state, sid, "user", "User: Draft the bake sale flyer", at=NOW - 6)
    status, body = post(server, f"/voice/history/{sid}/message",
                        {"text": "Then make a smaller version", "client_id": "m_h1", "mode": "queue"})
    assert status == 200 and body["delivered_as"] == "queued" and body["queue_position"] == 1, body
    assert body["via"] == "task" and body["session_id"] == sid
    page = get(server, f"/voice/history/{sid}")[1]["session"]
    assert page["state"] == "running"
    assert [(q["client_id"], q["text"]) for q in page["queued"]] == [("m_h1", "Then make a smaller version")]
    assert delete(server, f"/voice/history/{sid}/queued/m_h1")[0] == 200
    assert get(server, f"/voice/history/{sid}")[1]["session"]["queued"] == []
    post(server, f"/voice/history/{sid}/message", {"text": "Use the blue paper", "client_id": "m_h2", "mode": "queue"})
    status, steered = post(server, f"/voice/history/{sid}/message", {"text": "Bigger title", "client_id": "m_h3"})
    assert steered["delivered_as"] == "steer"
    finish(hermes, run_id)
    wait_for(lambda: len(hermes.calls) >= 2)
    assert hermes.calls[1]["session_id"] == sid and "blue paper" in json.dumps(hermes.calls[1]["input"])
    wait_for(lambda: get(server, f"/voice/history/{sid}")[1]["session"]["queued"] == [])


# -- what became of a steered message: sent -> seen | waiting | follow_up | unused ---------------------

def steered(server, run_id):
    return get(server, f"/voice/work/{run_id}")[1]["work"]["steered"]


def steer_row(text):
    """What Hermes writes when the agent takes a steer (agent.prompt_builder.steer_user_row)."""
    return ("[OUT-OF-BAND USER MESSAGE — a direct message from the user, delivered once at this position; not tool "
            "output and not a new delivery when replayed from conversation history]\n" + text + "\n[/OUT-OF-BAND USER MESSAGE]")


@pytest.fixture
def quick_receipts(monkeypatch):
    from speakeasy import calls
    monkeypatch.setattr(calls, "FOLLOW_UP_POLL_S", 0.1)
    monkeypatch.setattr(calls, "FOLLOW_UP_SETTLE_S", 0.3)


def test_a_steer_is_sent_then_seen_and_the_live_line_says_so(server, service, hermes, state, quick_receipts):
    run_id = running_job(server, service, hermes)
    status, body = send(server, run_id, "Put the tomatoes first please", client_id="s_seen")
    assert body["delivered_as"] == "steer"
    wait_for(lambda: hermes.steers)
    [item] = steered(server, run_id)
    assert item["state"] == "sent" and item["label"] == "Sent to the running task" and item["client_id"] == "s_seen"
    # The agent takes it: its steer row lands in the task's transcript.
    sid = service.store.session_for(service.store.key_for_run(run_id))
    add_msg(state, sid, "user", steer_row(hermes.steers[-1][1]), at=time.time())
    item = wait_for(lambda: (lambda i: i if i["state"] == "seen" else None)(steered(server, run_id)[0]))
    assert item["label"] == "Seen by " + service.settings.get()["assistant_name"] and item["seen_at"] >= item["at"]
    task = get(server, f"/voice/work/{run_id}")[1]["work"]
    assert task["short_status"] == "Adjusting: Put the tomatoes first please"
    seq = [k for _, k, p in service.history_events.events if k == "message_updated" and p["client_id"] == "s_seen"]
    assert seq, "the live feed says the message changed"


def test_a_steer_not_read_for_a_minute_is_waiting(server, service, hermes, monkeypatch):
    from speakeasy import service as S
    run_id = running_job(server, service, hermes)
    send(server, run_id, "Skip the herbs", client_id="s_wait")
    wait_for(lambda: hermes.steers)
    assert steered(server, run_id)[0]["state"] == "sent"
    monkeypatch.setattr(S, "STEER_WAITING_AFTER_S", 0)
    item = steered(server, run_id)[0]
    assert item["state"] == "waiting" and item["label"] == "Waiting for the current step to finish"


def test_a_steer_the_turn_ended_without_reading_goes_as_a_follow_up(server, service, hermes, quick_receipts):
    run_id = running_job(server, service, hermes)
    send(server, run_id, "Add the frost dates", client_id="s_late")
    wait_for(lambda: hermes.steers)
    run = hermes.runs[run_id]
    run["events"][-1]["pending_steer"] = hermes.steers[-1][1]   # Hermes hands it back undelivered
    finish(hermes, run_id)
    item = wait_for(lambda: (lambda i: i if i["state"] == "follow_up" else None)(steered(server, run_id)[0]))
    assert item["label"] == "It finished first · sent as a follow-up" and item["run_id"]
    wait_for(lambda: len(hermes.calls) >= 2)
    assert "frost dates" in hermes.calls[1]["input"] and hermes.calls[1]["session_id"] == hermes.calls[0]["session_id"]
    time.sleep(0.5)
    assert service.sweep_steered() == 0 and len(hermes.calls) == 2       # once


def test_a_steer_the_user_stopped_before_it_was_read_is_unused(server, service, hermes, quick_receipts):
    run_id = running_job(server, service, hermes)
    send(server, run_id, "Use the red packets", client_id="s_stop")
    wait_for(lambda: hermes.steers)
    assert post(server, f"/voice/work/{run_id}/stop", {})[0] == 202
    wait_for(lambda: (service.store.work(run_id) or {}).get("status") in {"cancelled", "interrupted"})
    item = wait_for(lambda: (lambda i: i if i["state"] == "unused" else None)(steered(server, run_id)[0]), timeout=15)
    assert item["label"] == "The task stopped before reading this"
    assert len(hermes.calls) == 1


def test_a_session_page_lists_steered_messages(server, service, hermes, state):
    run_id = running_job(server, service, hermes, "Draft the bake sale flyer")
    sid = wait_for(lambda: service.store.session_for(service.store.key_for_run(run_id)))
    add_session(state, sid, "api_server", at=NOW - 5)
    status, body = post(server, f"/voice/history/{sid}/message", {"text": "Bigger title", "client_id": "s_page"})
    assert body["delivered_as"] == "steer"
    page = get(server, f"/voice/history/{sid}")[1]["session"]
    assert [(m["client_id"], m["state"]) for m in page["steered"]] == [("s_page", "sent")]
