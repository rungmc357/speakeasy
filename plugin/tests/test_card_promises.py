"""SPK-398: the call promises a card only once the server has placed it on Home, and a job that is
waiting says why and roughly for how long instead of a bare "Queued.". Invented data only."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fakes import wait_for
from speakeasy import calls
from speakeasy.prompt import builder as P
from test_call_pulse import clock, setup  # noqa: F401  (clock is a fixture)
from test_routing import spoken, start_call


def _notes(worker):
    return [c for k, _, c in worker.sent if k == "session.thinking.append"]


VIEWS = [{"kind": "list", "title": "Garden plan", "items": [{"text": "Tomatoes in bed 2"},
                                                             {"text": "Basil in bed 1"}]}]


# -- wording ---------------------------------------------------------------------------------------

def test_a_status_line_says_the_card_is_coming_until_it_is_placed():
    pending = P.status_note("Drawing the garden plan")
    assert "isn't on their screen yet" in pending and "never present it as already there" in pending
    assert "here it is" not in pending.lower()  # never quoted: the voice would parrot it
    assert "Drawing the garden plan" in pending
    placed = P.status_note("Drawing the garden plan", card_up=True)
    assert "isn't on their screen" not in placed and "Drawing the garden plan" in placed


def test_here_it_is_is_dropped_from_a_line_about_an_unplaced_card():
    assert P.without_here_it_is("Here it is: the garden plan has five beds.") == "The garden plan has five beds."
    assert P.without_here_it_is("Okay, here's the plan: five beds.") == "Five beds."
    assert P.without_here_it_is("It's on your screen. Five beds.") == "Five beds."
    assert P.without_here_it_is("Here it is.") is None  # nothing left to say
    assert P.without_here_it_is("Measuring the beds.") == "Measuring the beds."
    assert P.without_here_it_is("Where is it at? Still measuring.") == "Where is it at? Still measuring."


def test_the_result_note_says_whether_a_card_is_on_home():
    assert "on their Home now (1 card)" in P.card_on_home_note(1)
    none = P.card_on_home_note(0)
    assert "no card on their Home" in none and "Never present anything as showing" in none


def test_a_wait_has_a_reason_and_an_estimate():
    note = P.follow_up_queued_note("the garden plan", "Measuring the beds", 70)
    assert note.startswith("Lined up, not read yet:")
    assert "in the middle of a step (measuring the beds)" in note and "likely in about a minute" in note
    assert "Never answer with a one-word status" in note and "Queued" not in note
    unknown = P.follow_up_queued_note("the garden plan")
    assert "isn't known yet, so don't guess a time" in unknown
    assert [P.wait_estimate(s) for s in (5, 30, 70, 150, 300, 2000)] == [
        "a few seconds", "about 30 seconds", "about a minute", "a minute or two", "about 5 minutes",
        "several minutes or more"]
    assert P.wait_estimate(None) is None


def test_a_status_answer_carries_the_card_and_the_wait():
    text = P.status_answer("Garden plan", "running", ["Measuring the beds"], 200, None, card_up=False,
                           waiting="what they added hasn't been read yet. It's mid-step, likely in about a minute.")
    assert "Its card isn't on their screen yet" in text
    assert "Why it's waiting: what they added hasn't been read yet" in text and "one-word status" in text
    done = P.status_answer("Garden plan", "completed", [], 200, "Five beds.", card_up=True, waiting="x")
    assert "isn't on their screen" not in done and "Why it's waiting" not in done


def test_show_me_with_no_picture_yet_says_what_it_is_on_and_when():
    line = P.show_me_requested("Drawing the garden plan", 40)
    assert line.startswith("There's no picture yet; it's still drawing the garden plan.")
    assert line.endswith("likely in about 30 seconds.") and "here it is" not in line.lower()
    assert P.show_me_requested() == P.SHOW_ME_REQUESTED


def test_the_voice_rules_never_point_at_an_unplaced_card():
    rules = Path(P.__file__).with_name("rules.md").read_text()
    assert "never present a card or picture as already there" in rules
    assert '"Lined up" means not read yet' in rules and "on your screen" not in rules.lower()


# -- a fake-action call -------------------------------------------------------------------------

def test_the_live_step_and_its_pace_give_the_wait(service, clock):
    worker, backend = setup(service, clock, key="idem_g1", request="Draw the garden plan")
    for text in ("Reading the bed sizes", "Measuring the beds", "Drawing the garden plan"):
        service.store.progress(backend.idem_key, "milestone", text)
        clock.t += 60
    service.store.progress(backend.idem_key, "milestone", "You added: make it bigger")  # theirs, not a step
    asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim",
                                                      "text": "STATUS: Drawing the garden plan\nDETAIL: Five beds so far"}))
    clock.t += 20
    doing, eta = worker.step_wait(backend.idem_key)
    assert doing == "Drawing the garden plan"
    assert eta is not None and 30 <= eta <= 60  # steps ~60 s apart, the newest one 20 s ago
    assert worker.card_on_home(backend.idem_key) == 0  # still running: nothing on Home yet


def test_a_progress_line_never_says_here_it_is_while_the_card_is_pending(service, clock):
    worker, backend = setup(service, clock, key="idem_g2", request="Draw the garden plan")
    clock.t += calls.PROGRESS_AFTER_S + 1
    asyncio.run(worker._handle_hermes_event(backend, {
        "event": "message.interim", "text": "STATUS: Planning five beds\nDETAIL: Here it is: tomatoes go in bed 2"}))
    said = spoken(worker)
    assert said and not any("here it is" in s.lower() for s in said), said
    assert said[-1] == "Planning five beds. Tomatoes go in bed 2."


def test_a_task_lined_up_behind_another_says_who_what_and_when(service, clock):
    """A task waiting its turn in a chat behind another one (the card's "Queued behind …"): a status
    answer gets who it waits behind, what that is doing and roughly when it starts, never "Queued."."""
    worker, first = setup(service, clock, key="idem_q1", request="Draw the garden plan", title="Garden plan")
    _, second = setup(service, clock, key="idem_q2", delegation="t_p2", request="Add a herb bed",
                      title="Herb bed")
    worker.interaction.runs[second.delegation_id] = second
    for key in ("idem_q1", "idem_q2"):
        service.store._db.execute("UPDATE runs SET session_id='sess_garden' WHERE idem_key=?", (key,))
    for text in ("Reading the bed sizes", "Measuring the beds"):
        service.store.progress("idem_q1", "milestone", text)
        clock.t += 60
    asyncio.run(worker._handle_hermes_event(first, {"event": "message.interim",
                                                    "text": "STATUS: Measuring the beds\nDETAIL: Two of five done"}))
    clock.t += 20
    why = worker.waiting_text("idem_q2", "Garden plan")
    assert why and "Queued" not in why, why
    assert why.lower().startswith("it's lined up behind the garden plan work, which is measuring the beds")
    assert "should start in about" in why
    text = P.status_answer("Herb bed", "waiting_on_task", [], 30, None, queued_behind="Garden plan",
                           card_up=False, waiting=why)
    assert "why it's waiting: it's lined up behind the garden plan work" in text.lower()
    assert worker.waiting_text("idem_q1", None) is None  # running, nothing of theirs unread: no wait to explain


def test_here_it_is_only_after_the_card_is_placed(server, service, hermes):
    """End to end: while the task runs its status notes say the card is coming; the result note says the
    card is on Home only when the finished answer carried one, and says the opposite when it didn't."""
    answer = ("Five beds, tomatoes in bed 2.\n```speakeasy-views\n" + json.dumps(VIEWS)
              + "\n```\nDONE: Garden plan\nSPOKEN: Here it is: five beds, tomatoes in bed 2.")
    hermes.hold = True
    hermes.live_events = [{"event": "message.interim", "text": "STATUS: Drawing the garden plan\nDETAIL: Five beds so far"}]
    hermes.responder = lambda prompt, sid: answer
    # the task's status is in the note the voice gets while it runs; nothing has reached Home yet
    session, worker = start_call(server, service)
    worker.HOLD_QUIET_NOTES = False  # every note goes out as it is made, so the test can read each
    worker.delegate("call_garden", "Draw me a garden plan")
    run = wait_for(lambda: next((r for r in worker.interaction.runs.values() if r.run_id), None))
    try:
        pending = wait_for(lambda: [n for n in _notes(worker) if "Five beds so far" in n])
    except AssertionError:
        raise AssertionError(f"no status note: {worker.sent}")
    assert all("isn't on their screen yet" in n for n in pending)
    assert worker.card_on_home(run.idem_key) == 0
    assert not any("on their Home now" in n for n in _notes(worker))
    hermes.runs[run.run_id]["done"].set()
    wait_for(lambda: any("Here it is" in s for s in spoken(worker)), timeout=30)
    assert worker.card_on_home(run.idem_key) == 1
    placed = [n for n in _notes(worker) if "on their Home now" in n]
    assert placed and "(1 card)" in placed[-1]


def test_an_answer_with_no_card_says_none_is_on_home(server, service, hermes):
    hermes.responder = lambda prompt, sid: "Bed 2 gets the most sun.\nDONE: Sunny bed\nSPOKEN: Bed 2 gets the most sun."
    session, worker = start_call(server, service)
    worker.HOLD_QUIET_NOTES = False
    worker.delegate("call_sun", "Which garden bed gets the most sun")
    wait_for(lambda: any("most sun" in s for s in spoken(worker)), timeout=30)
    assert any("put no card on their Home" in n for n in _notes(worker))
    assert not any("on their Home now" in n for n in _notes(worker))


def test_a_lined_up_follow_up_gives_a_reason_not_a_bare_queued(server, service, hermes):
    hermes.hold = True
    hermes.live_events = [{"event": "message.interim", "text": "STATUS: Measuring the beds\nDETAIL: Two of five done"}]
    session, worker = start_call(server, service)
    worker.HOLD_QUIET_NOTES = False
    worker.delegate("call_beds", "Plan the garden beds")
    run = wait_for(lambda: next((r for r in worker.interaction.runs.values()
                                 if r.delegation_id == "call_beds" and r.run_id), None))
    wait_for(lambda: (service.store.work(idem_key=run.idem_key) or {}).get("short_status") == "Measuring the beds")
    worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_beds_more", "make the beds wider", task_id="call_beds")
    note = wait_for(lambda: [n for n in _notes(worker) if n.startswith("Lined up, not read yet:")])[0]
    assert "in the middle of a step (measuring the beds)" in note
    assert "Never answer with a one-word status" in note
    hermes.runs[run.run_id]["done"].set()
