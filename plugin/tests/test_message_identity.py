"""Message identity and receipts (SPK-80): every typed send carries its client_id end to end, gets a
receipt_id and an explicit delivered_as, is sent once across restarts, and the history row it wrote
carries its own client_id. A stopped or interrupted task leaves its held and steered messages unused.
Invented tasks and words only."""
import time

from fakes import wait_for
from speakeasy.store import StateStore
from test_history import NOW, add_msg, add_session, state  # noqa: F401  (fixture)
from test_message_modes import finish, get, post, running_job, send, steer_row


def test_every_send_says_where_it_went_with_a_receipt(server, service, hermes):
    hermes.hold = True
    status, started = post(server, "/voice/work", {"text": "Sort the seed packets by month", "client_id": "w_1"})
    assert status == 202 and started["delivered_as"] == "new_run", started
    assert started["client_id"] == "w_1" and started["receipt_id"].startswith("rcpt_")
    run_id = started["run_id"]
    wait_for(lambda: hermes.calls and (service.store.work(run_id) or {}).get("status") == "running")
    _, steered = send(server, run_id, "Put the tomatoes first", client_id="t_1")
    assert steered["delivered_as"] == "steer" and steered["client_id"] == "t_1" and steered["receipt_id"]
    _, queued = send(server, run_id, "Then the beans", client_id="q_1", mode="queue")
    assert queued["delivered_as"] == "queued" and queued["receipt_id"]
    assert send(server, run_id, "Then the beans", client_id="q_1", mode="queue")[1]["receipt_id"] == queued["receipt_id"]
    finish(hermes, run_id)
    wait_for(lambda: service.store.queued_message("q_1")["state"] == "sent")
    sent = service.store.queued_message("q_1")["reply"]
    assert sent["client_id"] == "q_1" and sent["receipt_id"] == queued["receipt_id"]
    assert sent["delivered_as"] == "follow_up"
    finish(hermes, hermes.calls[1]["run_id"])
    wait_for(lambda: (service.store.work(hermes.calls[1]["run_id"]) or {}).get("status") == "completed")
    _, after = send(server, run_id, "And print it", client_id="t_2")
    assert after["delivered_as"] == "follow_up" and after["receipt_id"] != steered["receipt_id"]


def test_a_retry_after_a_restart_is_never_sent_again(server, service, hermes):
    run_id = running_job(server, service, hermes)
    _, first = send(server, run_id, "Put the tomatoes first", client_id="r_1")
    wait_for(lambda: hermes.steers)
    steers, calls = len(hermes.steers), len(hermes.calls)
    service._message_replies.clear()                       # the service restarted: memory is gone
    service.store.boot = "after-restart"
    status, again = send(server, run_id, "Put the tomatoes first", client_id="r_1")
    assert status == 200 and again["receipt_id"] == first["receipt_id"] and again["delivered_as"] == "steer"
    assert again["replayed"] is True
    time.sleep(0.3)
    assert len(hermes.steers) == steers and len(hermes.calls) == calls


def test_a_send_cut_off_by_a_restart_is_pending_and_never_resent(server, service, hermes):
    run_id = running_job(server, service, hermes)
    service.store.begin_receipt("r_cut", "task")           # being sent when the process died
    service.store.boot = "after-restart"
    status, body = send(server, run_id, "Use the red packets", client_id="r_cut")
    assert status == 200 and body["delivered_as"] == "pending" and body["receipt_state"] == "interrupted"
    time.sleep(0.3)
    assert hermes.steers == [] and len(hermes.calls) == 1


def test_a_held_send_noted_before_a_restart_is_not_sent_twice(server, service, hermes):
    run_id = running_job(server, service, hermes)
    send(server, run_id, "Then water the seedlings", mode="queue", client_id="h_1")
    key = service.store.key_for_run(run_id)
    # It went out just before the restart, but the queue never heard: the receipt says it did.
    assert service.store.claim_queued("h_1")
    receipt, mine = service.store.begin_receipt("h_1", "queued")
    assert mine
    service.store.finish_receipt("h_1", {"ok": True, "delivered_as": "follow_up", "client_id": "h_1",
                                         "receipt_id": receipt["receipt_id"]})
    reopened = StateStore(service.dir / "state.sqlite3")
    row = reopened.queued_message("h_1")
    assert row["state"] == "sent" and row["reply"]["receipt_id"] == receipt["receipt_id"]
    # Cut off mid-send instead: unused, never sent again on its own.
    send(server, run_id, "And mulch them", mode="queue", client_id="h_2")
    assert service.store.claim_queued("h_2")
    service.store.begin_receipt("h_2", "queued")
    again = StateStore(service.dir / "state.sqlite3")
    assert again.queued_message("h_2")["state"] == "unused"
    assert again.queued_messages([key]) == []


def test_an_interrupted_task_leaves_its_queue_and_steers_unused_in_the_same_write(server, service, hermes):
    run_id = running_job(server, service, hermes)
    send(server, run_id, "Use the red packets", client_id="u_steer")
    wait_for(lambda: hermes.steers)
    send(server, run_id, "Then the blue ones", mode="queue", client_id="u_q")
    key = service.store.key_for_run(run_id)
    heard = []
    service.store.unused_listeners.append(lambda k, q, s: heard.append((k, q, s)))
    assert service.store.transition(key, "interrupted")   # the recovery sweep after a restart
    # Settled with the status itself: nothing to wait for.
    assert service.store.queued_message("u_q")["state"] == "unused"
    assert [r["state"] for r in service.store.steered_messages([key])] == ["unused"]
    assert heard == [(key, ["u_q"], ["u_steer"])]
    calls = len(hermes.calls)
    assert service.drain_queued() == 0 and service.sweep_steered() == 0
    time.sleep(0.4)
    assert len(hermes.calls) == calls
    work = get(server, f"/voice/work/{run_id}")[1]["work"]
    assert [(q["client_id"], q["state"], q["position"]) for q in work["queued"]] == [("u_q", "unused", None)]
    assert [(m["client_id"], m["state"]) for m in work["steered"]] == [("u_steer", "unused")]


def test_a_completed_task_still_sends_its_queue(server, service, hermes):
    run_id = running_job(server, service, hermes)
    send(server, run_id, "Then the beans", mode="queue", client_id="c_q")
    finish(hermes, run_id)
    wait_for(lambda: len(hermes.calls) >= 2)
    assert "beans" in hermes.calls[1]["input"]
    wait_for(lambda: service.store.queued_message("c_q")["state"] == "sent")


def test_history_rows_carry_the_client_id_of_their_own_send(server, service, hermes, state):
    run_id = running_job(server, service, hermes, "Draft the bake sale flyer")
    sid = wait_for(lambda: service.store.session_for(service.store.key_for_run(run_id)))
    add_session(state, sid, "api_server", at=NOW - 5)
    # The same words sent twice: two sends, two rows, each with its own id (never guessed by text alone).
    words = ["Bigger title", "Smaller footer", "Bigger title"]
    replies = [post(server, f"/voice/history/{sid}/message", {"text": w, "client_id": f"e_{i}"})[1]
               for i, w in enumerate(words)]
    assert [r["delivered_as"] for r in replies] == ["steer"] * 3 and len({r["receipt_id"] for r in replies}) == 3
    now = time.time()
    for i, w in enumerate(words + ["Smaller footer"]):   # the last was typed somewhere else: no id
        add_msg(state, sid, "user", steer_row(w), at=now + 1 + i)
        add_msg(state, sid, "assistant", f"Done that ({i + 1}).", at=now + 1.5 + i)

    def rows():
        entries = get(server, f"/voice/history/{sid}")[1]["session"]["entries"]
        return [(e["text"], e.get("client_id"), e.get("receipt_id")) for e in entries
                if e["kind"] == "user" and e["text"] in words]
    got = wait_for(lambda: (lambda r: r if len(r) == 4 else None)(rows()))
    assert got == [(w, f"e_{i}", replies[i]["receipt_id"]) for i, w in enumerate(words)] + [
        ("Smaller footer", None, None)], got
    assert rows() == got                                                  # a reload never moves an id
