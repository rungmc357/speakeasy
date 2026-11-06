"""When the voice offers a pick out loud, the options show up as a question card to tap."""
import time

from fakes import http, wait_for
from speakeasy import router
from test_follow_through_promises import said
from test_routing import start_call


def test_choices_are_read_from_what_the_voice_asked():
    assert router.spoken_choice("Got it. Do you want the quick summary or the full report?") == \
        ("Do you want the quick summary or the full report?", ["The quick summary", "The full report"])
    assert router.spoken_choice("Which one do you want: Kayak, Expedia or Google Flights?")[1] == \
        ["Kayak", "Expedia", "Google Flights"]
    assert router.spoken_choice("Den, bedroom, or both?")[1] == ["Den", "Bedroom", "Both"]
    for line in ("Want me to look up what they charge?", "Do you want me to do that or not?",
                 "On it. I'll check flights.", "Should I go ahead?"):
        assert router.spoken_choice(line) is None, line


def _choice_task(server):
    tasks = http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]
    return next((t for t in tasks if t["task_id"].startswith("choice_")), None)


def _set_aside(service, task_id):
    """A pick that was answered or set aside: off the list (it folds into its task), and settled
    as set aside in the store."""
    key = service.store.key_for_task(task_id)
    return bool(key and (service.store.work(idem_key=key) or {}).get("set_aside"))


def test_a_spoken_choice_becomes_a_card_and_a_tap_answers_the_voice(server, service, hermes):
    session, worker = start_call(server, service)
    said(worker, "Do you want the quick summary ")
    said(worker, "or the full report?", at=12)
    task = wait_for(lambda: _choice_task(server), timeout=5)
    question = task["result"]["question"]
    assert [o["text"] if isinstance(o, dict) else o for o in question["options"]] == ["The quick summary", "The full report"]
    assert task["title"] == "Do you want the quick summary or the full report?"
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/answer",
                        {"task_id": task["task_id"], "text": "The full report"}, token=server.token)
    assert status == 200 and body["sent"]
    assert any("The full report" in str(x) and "commentary" in str(x) for x in worker.sent)
    assert not hermes.calls                                     # the voice carries on; no task follows it up
    wait_for(lambda: _set_aside(service, task["task_id"]), timeout=5)
    assert _choice_task(server) is None                         # answered: never its own item


def test_a_choice_card_is_never_seen_without_its_question(server, service, hermes):
    """The board can be fetched between any two store writes: after every write the card must
    either be absent or carry its question (a half-saved card read as a blank finished task)."""
    _, worker = start_call(server, service)
    store, seen = worker.store, []
    for name in ("reserve_run", "set_title", "update_run", "set_result", "record_finished_run"):
        original = getattr(store, name)

        def spy(*args, _original=original, **kwargs):
            out = _original(*args, **kwargs)
            seen.extend(t for t in store.latest_tasks() if t["task_id"].startswith("choice_"))
            return out
        setattr(store, name, spy)
    said(worker, "Coffee or tea?")
    wait_for(lambda: _choice_task(server), timeout=5)
    assert seen and all((t.get("result") or {}).get("question") for t in seen)


def test_the_same_choice_is_offered_once_and_answering_out_loud_clears_it(server, service, hermes):
    _, worker = start_call(server, service)
    said(worker, "Coffee or tea?")
    task = wait_for(lambda: _choice_task(server), timeout=5)
    said(worker, "Coffee or tea?", at=20)
    time.sleep(0.5)
    tasks = http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]
    assert len([t for t in tasks if t["task_id"].startswith("choice_")]) == 1
    worker.delegate("call_tea", "Tea please, and find me a good oolong")
    wait_for(lambda: _set_aside(service, task["task_id"]), timeout=5)
    assert _choice_task(server) is None
    assert task["task_id"] != "call_tea"


def test_a_yes_no_question_makes_no_card(server, service, hermes):
    _, worker = start_call(server, service)
    said(worker, "Want me to look up what they charge?")
    time.sleep(0.5)
    assert _choice_task(server) is None


def test_a_dropped_connection_takes_the_choice_card_with_it(server, service, hermes):
    """No session.closed (Wi-Fi drop, transport EOF): the card must not stay up as needing you."""
    _, worker = start_call(server, service)
    said(worker, "Coffee or tea?")
    task = wait_for(lambda: _choice_task(server), timeout=5)
    assert not task.get("set_aside")
    worker.call_closed()   # what every transport's finally block does on a drop
    wait_for(lambda: _set_aside(service, task["task_id"]), timeout=5)
    assert _choice_task(server) is None
    assert not hermes.calls


def test_a_choice_this_voice_did_not_ask_never_becomes_a_hermes_job(server, service, hermes):
    """After a resume the new leg carries the old leg's runs, but not its spoken choices: a late tap
    on the old card is refused rather than sent to Hermes as a follow-up of a task that never ran."""
    session, worker = start_call(server, service)
    said(worker, "Coffee or tea?")
    task = wait_for(lambda: _choice_task(server), timeout=5)
    worker.choice_tasks.pop(task["task_id"])   # as on the resumed leg's worker
    status, _ = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/answer",
                     {"task_id": task["task_id"], "text": "Tea"}, token=server.token)
    assert status == 404
    time.sleep(0.3)
    assert not hermes.calls
