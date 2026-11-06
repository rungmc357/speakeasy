"""Setting aside a question waiting on the user ("Not needed"): from the apps, or by saying so."""
from __future__ import annotations

import pytest

from fakes import http, wait_for
from speakeasy import router
from speakeasy.prompt import builder as P
from test_routing import start_call, tasks

ASK = """Voice: turn on the office lamp
I found two lamps that could be it.

```speakeasy-question
{"question": "Which one?", "options": ["Living room lights", "All the lights"], "recommended": 0}
```
DONE: lamp
SPOKEN: Which one? Living room lights or all the lights."""


def _asked(server, service, hermes):
    hermes.responder = lambda prompt, sid: ASK
    session, worker = start_call(server, service)
    worker.delegate("call_lamp", "Turn on my office lamp")
    # Wait for the question itself, not just the finished status.
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"
                             and (t.get("result") or {}).get("question")])[0]
    return session, worker, done


def _say(worker, delegation_id, words):
    """The voice spoke in between (it asked the question), so the next words are their own line."""
    worker.feed({"type": "session.output_transcript.delta", "delta": "Which one?", "start_ms": 3, "end_ms": 4})
    worker.delegate(delegation_id, words)


def _notes(worker):
    return [c for k, _, c in worker.sent if k == "session.thinking.append"]


@pytest.mark.parametrize("said", ["It's already handled", "never mind that one", "skip that question",
                                  "I already took care of it", "not needed", "close it out", "pass on that one"])
def test_set_aside_phrases_are_strong(said):
    assert router.set_aside_kind(said) == "strong"


@pytest.mark.parametrize("said", ["never mind", "forget it", "skip it for now", "don't worry about it"])
def test_plain_never_mind_is_weak(said):
    assert router.set_aside_kind(said) == "weak"


@pytest.mark.parametrize("said", ["the living room ones", "never mind, turn on the den lamp instead",
                                  "go with all the lights", "what's the weather", "skip the intro and book it"])
def test_answers_and_new_work_are_not_setting_aside(said):
    assert router.set_aside_kind(said) is None


def test_the_route_is_authenticated_and_validated(server, service, hermes):
    _, _, done = _asked(server, service, hermes)
    url = f"/voice/tasks/{done['run_id']}/set-aside"
    assert http(server.base_url, "POST", url, {})[0] == 401
    assert http(server.base_url, "POST", url, {"why": "x"}, server.token)[0] == 400
    assert http(server.base_url, "POST", "/voice/tasks/run_nope/set-aside", {}, server.token)[0] == 404


def test_not_needed_closes_the_question_without_new_work(server, service, hermes):
    _, worker, done = _asked(server, service, hermes)
    calls_before = len(hermes.calls)
    status, body = http(server.base_url, "POST", f"/voice/tasks/{done['run_id']}/set-aside", {}, server.token)
    assert status == 200 and body == {"run_id": done["run_id"], "set_aside": True}
    task = wait_for(lambda: [t for t in tasks(server) if t["task_id"] == done["task_id"] and t.get("set_aside")])[0]
    assert task["status"] == "completed"                       # settled as not needed, never failed
    assert any(e["text"] == "Set aside" for e in task["events"])
    assert worker.waiting_on_user() == []
    note = wait_for(lambda: [c for c in _notes(worker) if "set aside the question" in c])[0]
    assert "Don't ask it again" in note                          # quiet: a background note, not spoken
    assert not any("set aside" in c for c in [c for k, _, c in worker.sent if k == "session.commentary.append"])
    assert len(hermes.calls) == calls_before                      # a finished question starts no new run
    # Twice is fine; it stays set aside, and the next call isn't told it's still waiting.
    assert http(server.base_url, "POST", f"/voice/tasks/{done['run_id']}/set-aside", {}, server.token)[0] == 200
    assert all(a["run_id"] != done["run_id"] for a in service.store.away())


def test_running_work_has_nothing_to_set_aside(server, service, hermes):
    hermes.hold = True
    session, worker = start_call(server, service)
    worker.delegate("call_run", "Research standing desks")
    running = wait_for(lambda: [t for t in tasks(server) if t.get("run_id") and t["status"] in {"running", "working"}])[0]
    status, _ = http(server.base_url, "POST", f"/voice/tasks/{running['run_id']}/set-aside", {}, server.token)
    assert status == 409


def test_setting_aside_an_approval_declines_it_and_tells_the_run(server, service, hermes):
    hermes.approval_first = True
    session, worker = start_call(server, service)
    worker.delegate("call_risky", "Clean up my downloads folder")
    iid = session["interaction_id"]
    snap = wait_for(lambda: (lambda b: b if b.get("approval") else None)(
        http(server.base_url, "GET", f"/voice/interactions/{iid}", token=server.token)[1]))
    run_id = snap["approval"]["run_id"]
    status, body = http(server.base_url, "POST", f"/voice/tasks/{run_id}/set-aside", {}, server.token)
    assert status == 200 and body["set_aside"]
    assert hermes.approvals[-1]["choice"] == "deny"
    assert any("set this aside" in text and "wrap up" in text for _, text in hermes.steers)
    assert service.store.work(run_id=run_id)["set_aside"] is True


def test_saying_its_handled_sets_the_question_aside(server, service, hermes):
    _, worker, done = _asked(server, service, hermes)
    calls_before = len(hermes.calls)
    _say(worker, "call_aside", "It's already handled, never mind that one")
    task = wait_for(lambda: [t for t in tasks(server) if t["task_id"] == done["task_id"] and t.get("set_aside")])[0]
    assert task["status"] == "completed"
    note = wait_for(lambda: [c for c in _notes(worker) if "set aside the question" in c])[0]
    assert "dropped it" in note
    assert len(hermes.calls) == calls_before                       # no new task for "never mind"
    assert not [t for t in tasks(server) if t["status"] not in {"completed", "rejected"}]


def test_plain_never_mind_after_a_newer_request_does_not_set_aside(server, service, hermes):
    _, worker, done = _asked(server, service, hermes)
    hermes.hold = True
    _say(worker, "call_new", "Research standing desks")
    wait_for(lambda: [t for t in tasks(server) if t["status"] in {"running", "working"}])
    _say(worker, "call_nm", "never mind")
    wait_for(lambda: len(worker.sent) > 0)
    assert not service.store.work(run_id=done["run_id"]).get("set_aside")


def test_the_voice_is_told_it_can_set_questions_aside():
    names = P.Names("Ava", "Sam")
    rules = P.rules_text(names)
    assert "already handled" in rules and "sets that question aside" in rules
    assert "Sam set this aside" in P.set_aside_answer(names)
    resumed = P.resume_block([{"status": "completed", "set_aside": True,
                               "events": [{"kind": "request", "text": "Turn on the lamp"}]}], names)
    assert "set aside" in resumed and "don't ask" in resumed
