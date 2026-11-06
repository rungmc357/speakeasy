"""Task names and headlines say what the task was and what came of it, recognizable a day later:
never the voice's opener ("Okay, I'm dropping it."), never a name with no subject ("Check work
status"), and a spoken pick or a twin of the same ask never lists as its own item."""
import time

from fakes import http, wait_for
from speakeasy import router
from speakeasy.store import fold_tasks
from speakeasy.text import clean_headline, is_opener, outcome_headline, split_result, vague_title
from test_follow_through_promises import said
from test_routing import start_call


def test_the_headline_is_the_outcome_never_the_opener():
    assert outcome_headline("Okay, I'm dropping it. Nothing changed, and nothing goes into build 66.") == \
        "Nothing changed, and nothing goes into build 66."
    assert outcome_headline("Got it — the kitchen lights are off.") == "The kitchen lights are off."
    assert outcome_headline("Done. Three board bugs fixed; the push waits on your go.") == \
        "Three board bugs fixed; the push waits on your go."
    assert outcome_headline("Tomorrow in Boston: 54°F and rain after 3.") == "Tomorrow in Boston: 54°F and rain after 3."
    for nothing in ("Okay.", "Sure thing. Let me check.", "Want me to keep going?", ""):
        assert outcome_headline(nothing) is None, nothing


def test_the_written_answer_wins_when_it_says_more():
    assert outcome_headline("Okay. Nothing changed.", "Okay. Nothing changed, and the release is untouched.") == \
        "Nothing changed, and the release is untouched."


def test_openers_and_model_headlines():
    assert is_opener("Okay, I'm dropping it.") and is_opener("Let me check.") and is_opener("Sure thing.")
    assert not is_opener("I'm out of space on the backup drive, 12 GB left.")
    assert clean_headline("Okay, I'm dropping it") is None
    assert clean_headline("Which one do you want?") is None
    assert clean_headline("Build 66 fix dropped; nothing shipped") == "Build 66 fix dropped; nothing shipped"


def test_a_result_carries_its_headline_and_a_question_does_not():
    result = split_result("Okay, I'm dropping it. Nothing changed.\nSPOKEN: Okay, I'm dropping it. Nothing changed.", ())
    assert result["headline"] == "Nothing changed."
    asked = split_result('Pick one.\n```speakeasy-question\n{"question": "Short or long?", "options": ["Short", "Long"]}\n```', ())
    assert "headline" not in asked


def test_names_with_no_subject_are_vague():
    for name in ("Check work status", "Decide on holding", "Confirm the earlier task", "Review the update"):
        assert vague_title(name), name
    for name in ("Fix duplicate selection showing", "Speakeasy build 66: why task cards freeze"):
        assert vague_title(name) is None, name


def test_an_older_vague_name_is_renamed_when_the_list_is_read(server, service, hermes):
    session, worker = start_call(server, service)
    service.rt.title_call = lambda text: None
    worker.delegate("call_a", "is the build 66 card fix still going in")
    wait_for(lambda: hermes.calls, timeout=5)
    key = next(r.idem_key for t, r in worker.interaction.runs.items() if t == "call_a")
    wait_for(lambda: service.store.work(idem_key=key)["status"] in {"completed", "failed"}, timeout=10)
    service.store.set_title(key, "Check work status")
    service.rt.title_call = lambda text: "Build 66 task card fix status"
    service.__dict__.pop("_title_tried", None)
    http(server.base_url, "GET", "/voice/work/latest", token=server.token)
    wait_for(lambda: service.store.title(key) == "Build 66 task card fix status", timeout=5)


def test_the_task_says_what_was_asked_even_after_many_steps(server, service, hermes):
    _, worker = start_call(server, service)
    worker.delegate("call_a", "find why the checkout test fails")
    wait_for(lambda: hermes.calls, timeout=5)
    key = next(r.idem_key for t, r in worker.interaction.runs.items() if t == "call_a")
    for i in range(40):
        service.store.progress(key, "milestone", f"Step number {i}")
    work = service.store.work(idem_key=key)
    assert not any(e["kind"] == "request" for e in work["events"])  # fell off the newest 30
    assert "checkout test" in work["request"]


def test_an_older_answer_gets_its_headline_on_read(server, service, hermes):
    hermes.hold = True  # the real run stays open, so its own ending never overwrites the stored answer
    _, worker = start_call(server, service)
    worker.delegate("call_a", "drop the card fix from build 66")
    wait_for(lambda: hermes.calls, timeout=5)
    running = wait_for(lambda: (lambda r: r if r and r.run_id else None)(worker.interaction.runs.get("call_a")))
    key = running.idem_key
    try:
        service.store.update_run(key, None, "completed")
        service.store.set_result(key, {"spoken": "Okay, I'm dropping it.", "full": "Okay, I'm dropping it. Nothing goes into build 66."})
        assert service.store.work(idem_key=key)["result"]["headline"] == "Nothing goes into build 66."
    finally:
        hermes.runs[running.run_id]["done"].set()


def _choice_rows(server):
    tasks = http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]
    return [t for t in tasks if t["task_id"].startswith("choice_")]


def test_a_spoken_pick_sits_in_its_task_and_folds_away_once_answered(server, service, hermes):
    session, worker = start_call(server, service)
    worker.delegate("call_a", "should the card fix hold for build 67")
    wait_for(lambda: hermes.calls, timeout=5)
    parent = next(r.idem_key for t, r in worker.interaction.runs.items() if t == "call_a")
    said(worker, "Hold it for build 67, ")
    said(worker, "or ship it now?", at=12)
    choice = wait_for(lambda: (_choice_rows(server) or [None])[0], timeout=5)
    parent_job = service.store.work(idem_key=parent)["job"]["id"]
    assert choice["job"]["id"] == parent_job                       # inside the task it's about
    status, _ = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/answer",
                     {"task_id": choice["task_id"], "text": "Hold it for build 67"}, token=server.token)
    assert status == 200
    wait_for(lambda: not _choice_rows(server), timeout=5)            # never its own "Set aside" item
    events = [e["text"] for e in service.store.work(idem_key=parent)["events"]]
    assert "You picked: Hold it for build 67" in events


def _row(task_id, ask, status, started, **extra):
    return {"task_id": task_id, "request": ask, "status": status, "started_at": started, "job": {"id": task_id},
            **extra}


def test_twins_of_one_ask_fold_into_the_better_one():
    now = time.time()
    rows = [_row("a", "Find a good oolong", "failed", now), _row("b", "find a good oolong.", "completed", now + 4),
            _row("c", "Find a good oolong", "completed", now + 3600)]
    assert [r["task_id"] for r in fold_tasks(rows)] == ["b", "c"]          # an hour later is a new ask
    running = [_row("a", "Plan the trip", "completed", now), _row("b", "Plan the trip", "running", now + 5)]
    assert [r["task_id"] for r in fold_tasks(running)] == ["b"]
    waiting = [_row("a", "Plan the trip", "completed", now, result={"question": {"question": "Which?"}}),
               _row("b", "Plan the trip", "completed", now + 5)]
    assert [r["task_id"] for r in fold_tasks(waiting)] == ["a", "b"]       # a question still waiting stays


def test_the_headline_model_never_returns_an_opener(monkeypatch, real_result_headline):
    class Reply:
        def __init__(self, text):
            self.choices = [type("C", (), {"message": type("M", (), {"content": text})()})()]
    import sys
    import types
    aux = types.ModuleType("agent.auxiliary_client")
    replies = iter(['{"headline": "Okay, I\'m dropping it"}', '{"headline": "Card fix dropped from build 66"}'])
    aux.call_llm = lambda **kw: Reply(next(replies))
    monkeypatch.setitem(sys.modules, "agent.auxiliary_client", aux)
    assert real_result_headline("drop it", "Okay, I'm dropping it.") is None
    assert real_result_headline("drop it", "Okay, I'm dropping it.") == "Card fix dropped from build 66"
