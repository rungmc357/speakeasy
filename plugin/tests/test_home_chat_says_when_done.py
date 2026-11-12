"""SPK-407: a job Home's chat started says in the thread when it is done, unprompted.

Before: every assistant row in Home's thread was written inside a user turn, and nothing called into
HomeChat when a job settled, so a job that finished with nothing newer sent only changed its card in
place ("it forgot to let me know"). Now the store's settle listener posts one short assistant line
under the message that started the job: the job's own answer in a sentence or two, linked to its card.
Exactly one per run, also across a repeated final event or a restart; none when the answer was
already opened or heard on a call. Invented data only; the coordinator and Hermes are faked."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from fakes import http, wait_for
from speakeasy import home_chat as HC
from test_home_chat import coordinator, post, stream

SPOKEN = "Riverside Beans opens at 6 and stays quiet until nine. Second sentence here. A third one."
ASK = {"reply": "Let me find you a quiet cafe.", "action": "start",
       "request": "Find a quiet cafe near the river that opens early"}


def assistant_rows(service, run_id):
    """Every assistant row in Home's thread linked to this run (the turn's reply and any later line)."""
    with service.store._lock:
        rows = service.store._db.execute(
            "SELECT id, text, reply_to, at FROM home_messages WHERE role='assistant' AND run_id=? ORDER BY at, rowid",
            (run_id,)).fetchall()
    return [dict(zip(("id", "text", "reply_to", "at"), r)) for r in rows]


def finish_lines(service, run_id):
    return [r for r in assistant_rows(service, run_id) if "Riverside Beans" in r["text"] or "stopped" in r["text"]
            or "question" in r["text"].lower()]


def start_held(server, service, hermes, client_id="c_cafe"):
    """A Home message starts a job that keeps running until the test finishes it."""
    hermes.hold = True
    hermes.responder = lambda prompt, sid: f"Found one.\nDONE: Cafe found\nSPOKEN: {SPOKEN}"
    coordinator(service, ASK)
    status, body = post(server, {"text": "find me a quiet cafe near the river, early", "client_id": client_id})
    assert status == 200 and body["action"] == "start" and body["run_id"], body
    run_id = body["run_id"]
    wait_for(lambda: run_id in hermes.runs)
    return run_id, body["message"]["id"][2:]


def finish(service, hermes, run_id):
    hermes.runs[run_id]["status"] = "completed"
    hermes.runs[run_id]["done"].set()
    key = service.store.key_for_run(run_id)
    wait_for(lambda: service.store.run_status(key) == "completed")
    return key


def test_a_home_job_that_finishes_says_so_once_in_the_thread(server, service, hermes):
    service.home_chat.done_grace_s = 0
    run_id, user_id = start_held(server, service, hermes)
    before = assistant_rows(service, run_id)
    assert not [r for r in before if "Riverside" in r["text"]]
    key = finish(service, hermes, run_id)

    # No further user turn: the thread gets one new assistant row for the job, with its answer.
    new = wait_for(lambda: [r for r in assistant_rows(service, run_id) if r["id"] not in {b["id"] for b in before}])
    assert len(new) == 1, new
    line = new[0]
    assert line["text"] == "Riverside Beans opens at 6 and stays quiet until nine. Second sentence here."
    assert line["reply_to"] == user_id                     # under the message that started it
    # A repeated final event, a second listener call and a restart never add a second line.
    service.store.settle(key, "completed", result={"spoken": "Something else."})
    service.home_chat.note_settled(key, "completed")
    HC.HomeChat(service)
    service.home_chat = HC.HomeChat(service)
    service.home_chat.deliver_due(time.time() + 10_000)
    after = [r for r in assistant_rows(service, run_id) if r["id"] not in {b["id"] for b in before}]
    assert [r["id"] for r in after] == [line["id"]]
    # It reaches the phone as the job's answer line: tied to its card (a tap opens the job), not a
    # reply of any exchange, and only once at the bottom (no made-up d:<run> line beside it).
    entries = stream(server)
    mine = [e for e in entries if e["id"] == "m:" + line["id"]]
    assert len(mine) == 1 and mine[0]["answer"] is True and mine[0]["run_id"] == run_id
    card = next(e for e in entries if e["kind"] == "card" and e["id"] == f"c:{run_id}")
    assert mine[0]["item_id"] == card["item_id"]
    assert mine[0]["reply_to"] == "m:" + user_id
    assert not [e for e in entries if e["id"] == f"d:{run_id}"]
    assert entries.index(card) < entries.index(mine[0])


def test_a_newer_message_while_it_ran_still_gets_one_line_at_the_bottom(server, service, hermes):
    service.home_chat.done_grace_s = 0
    run_id, _ = start_held(server, service, hermes)
    coordinator(service, {"reply": "Ha, fair.", "action": "none"})
    post(server, {"text": "coffee is life", "client_id": "c_chat"})
    finish(service, hermes, run_id)
    wait_for(lambda: finish_lines(service, run_id))
    entries = stream(server)
    bottom = [e for e in entries if e.get("run_id") == run_id and e.get("answer")]
    assert len(bottom) == 1 and bottom[0]["id"].startswith("m:ha_"), bottom   # SPK-314's d: line gives way
    assert entries[-1]["id"] == bottom[0]["id"]


def test_after_its_grace_the_line_goes_up_though_the_chat_worker_marked_it_told(server, service, hermes):
    """The trap: a typed chat's worker marks each answer it relays as spoken (runs.seen), the moment
    the job finishes. That is not you hearing it, so the line still goes up after the grace."""
    chat = service.home_chat
    now = [time.time()]
    chat.clock = lambda: now[0]
    chat.done_grace_s = 600
    run_id, _ = start_held(server, service, hermes)
    key = finish(service, hermes, run_id)
    wait_for(lambda: _state(service, run_id) == "due" and service.store.is_seen(key))
    assert chat.deliver_due() == [] and not finish_lines(service, run_id)   # still in its grace
    now[0] += 601
    assert chat.deliver_due() == [run_id]
    assert len(finish_lines(service, run_id)) == 1


def test_an_answer_opened_or_heard_on_the_call_before_the_line_lands_gets_none(server, service, hermes, monkeypatch):
    chat = service.home_chat
    now = [time.time()]
    chat.clock = lambda: now[0]
    chat.done_grace_s = 600
    opened, _ = start_held(server, service, hermes, "c_open")
    finish(service, hermes, opened)
    wait_for(lambda: chat.deliver_due() == [] and _state(service, opened) == "due")
    status, body = http(server.base_url, "POST", f"/voice/tasks/{opened}/seen", {}, server.token)
    assert status == 200 and body["seen"] is True             # opened in the app
    heard, _ = start_held(server, service, hermes, "c_heard")
    key = finish(service, hermes, heard)
    wait_for(lambda: _state(service, heard) == "due")
    # A live call that follows the job and whose voice read its answer out (now.call_state's shape).
    call = SimpleNamespace(lock=threading.Lock(), runs={"t_cafe": SimpleNamespace(idem_key=key)},
                           focus_task_id=None, talking_about=None, talking_at=0.0, interaction_id="vi_call",
                           spoken_tasks={"t_cafe": now[0]})
    monkeypatch.setattr(service.now_model, "live_call", lambda: call)
    now[0] += 601
    assert chat.deliver_due() == []
    assert not finish_lines(service, opened) and not finish_lines(service, heard)
    assert _state(service, opened) == _state(service, heard) == "skipped"


def test_a_line_due_while_the_plugin_was_down_goes_up_once_after_the_restart(server, service, hermes):
    chat = service.home_chat
    chat.done_grace_s = 600
    run_id, _ = start_held(server, service, hermes)
    finish(service, hermes, run_id)
    wait_for(lambda: _state(service, run_id) == "due")
    assert not finish_lines(service, run_id)                   # still in its grace
    later = time.time() + 601
    HC.HomeChat(service, clock=lambda: later)                  # the restart: its timer was lost
    HC.HomeChat(service, clock=lambda: later)
    wait_for(lambda: _state(service, run_id) == "posted")      # each new HomeChat re-arms a timer
    time.sleep(1.5)                                            # both timers have fired
    assert len(finish_lines(service, run_id)) == 1


def test_a_failed_job_says_it_stopped(server, service, hermes):
    service.home_chat.done_grace_s = 0
    hermes.fail_with = "the provider refused the request"
    coordinator(service, ASK)
    status, body = post(server, {"text": "find me a quiet cafe near the river, early", "client_id": "c_fail"})
    assert status == 200 and body["run_id"]
    run_id = body["run_id"]
    lines = wait_for(lambda: [r for r in assistant_rows(service, run_id) if r["text"].startswith("It stopped")])
    assert len(lines) == 1 and lines[0]["text"].startswith("It stopped before finishing.")


def test_a_job_that_asks_says_its_question(server, service, hermes):
    service.home_chat.done_grace_s = 0
    run_id, _ = start_held(server, service, hermes)
    key = service.store.key_for_run(run_id)
    service.store.settle(key, "completed", result={
        "spoken": "Two cafes fit.", "question": {"question": "Do you want outdoor seating?",
                                                 "options": [{"text": "Yes"}, {"text": "No"}]}})
    lines = wait_for(lambda: finish_lines(service, run_id))
    assert [r["text"] for r in lines] == ["One question before it goes on: Do you want outdoor seating?"]


def test_a_stopped_job_or_one_started_elsewhere_gets_no_line(server, service, hermes):
    service.home_chat.done_grace_s = 0
    run_id, _ = start_held(server, service, hermes)
    key = service.store.key_for_run(run_id)
    service.store.settle(key, "cancelled", result=None)        # you stopped it: the reply already said so
    hermes.hold = False
    status, other = http(server.base_url, "POST", "/voice/work", {"text": "Plan the tomato beds"}, server.token)
    assert status in (200, 202), other
    other_key = wait_for(lambda: service.store.key_for_run(other["run_id"]))
    wait_for(lambda: service.store.run_status(other_key) == "completed")
    time.sleep(0.3)
    with service.store._lock:
        n = service.store._db.execute("SELECT count(*) FROM home_done").fetchone()[0]
        posted = service.store._db.execute("SELECT count(*) FROM home_messages WHERE part='done'").fetchone()[0]
    assert posted == 0 and n == 0


def test_a_retried_send_still_gets_the_coordinators_reply_not_the_finish_line(server, service, hermes):
    service.home_chat.done_grace_s = 0
    run_id, _ = start_held(server, service, hermes)
    finish(service, hermes, run_id)
    wait_for(lambda: finish_lines(service, run_id))
    again = post(server, {"text": "find me a quiet cafe near the river, early", "client_id": "c_cafe"})[1]
    assert again["replayed"] is True and "Riverside" not in again["reply"]["text"]


def test_finish_line_words():
    assert HC.finish_line("completed", {"spoken": "A. B. C."}) == "A. B."
    assert HC.finish_line("completed", {}, "The summary.") == "The summary."
    assert HC.finish_line("completed", {"headline": "Three found"}) == "Three found"
    assert HC.finish_line("completed", {}, title="Cafe search") == "Cafe search is done."
    assert HC.finish_line("completed", {"spoken": "x"}, asking="Which day?") == "One question before it goes on: Which day?"
    assert HC.finish_line("failed", {}, reason="The disk was full.") == "It stopped before finishing. The disk was full."
    from speakeasy.failures import NO_REASON
    assert HC.finish_line("failed", {}, reason=NO_REASON) == "It stopped before finishing."
    assert HC.finish_line("cancelled", {"spoken": "x"}) is None
    assert len(HC.finish_line("completed", {"spoken": "word " * 200}) or "") <= HC.DONE_CHARS


def _state(service, run_id):
    with service.store._lock:
        row = service.store._db.execute("SELECT state FROM home_done WHERE run_id=?", (run_id,)).fetchone()
    return row[0] if row else None
