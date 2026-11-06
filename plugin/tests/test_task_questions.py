"""A question about the running work itself ("what is it testing?", "which files?") gets a real answer:
from the task's own steps when they say, else the task itself is asked (one steer) and its one-line
STATUS/DETAIL reply is spoken. "How's it going?" keeps the status-from-steps path. Invented data only."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from speakeasy import activity as A
from speakeasy import calls
from speakeasy import router
from speakeasy.calls import BackendRun
from speakeasy.prompt import builder as P
from test_routing import _progress_worker, spoken


# -- telling a content question from "how's it going?" --------------------------------------------

@pytest.mark.parametrize("said", [
    "What are you testing", "what is it testing?", "Which tests?", "which files did it change",
    "why is it running the tests again", "what's it checking for", "what are the tests for",
    "what tests are those", "testing what?", "what is the test suite for",
    "can't you look at the task and see what it's checking",
])
def test_content_questions_about_the_work_are_recognized(said):
    assert router.asks_about_task(said)
    assert not router.is_status_question(said)


@pytest.mark.parametrize("said", [
    "How's it going", "any update?", "where are you at", "is it done yet",
])
def test_how_is_it_going_stays_a_status_question(said):
    assert router.is_status_question(said) and not router.asks_about_task(said)


@pytest.mark.parametrize("said", [
    "Which files should I delete from my downloads", "which page can you book the table on",
    "what's the weather", "Which restaurant is open late", "what tests should we add for the cart",
])
def test_new_asks_are_not_questions_about_running_work(said):
    assert not router.asks_about_task(said)


def test_side_model_reply_is_read_safely():
    assert router.clean_answer('{"answer": "The sign-in tests for the iPhone app."}') == \
        "The sign-in tests for the iPhone app."
    assert router.clean_answer('{"answer": null}') == ""
    assert router.clean_answer("sure!") is None
    # Never a path, a flag or a command read aloud: treated as "the facts don't say".
    assert router.clean_answer('{"answer": "It runs pytest -q on tests/test_cart.py"}') == ""
    assert router.clean_answer('{"answer": "It reads ~/shop/cart.py"}') == ""


def test_the_steer_asks_for_one_status_detail_pair_and_carries_on():
    text = P.ask_task_text(P.Names("Ava", "Sam"), "what is it testing?")
    assert text.startswith("Sam asks: what is it testing?")
    assert "STATUS/DETAIL" in text and "carry on" in text and "do not start over" in text
    assert "typed:" in P.ask_task_text(P.Names("Ava", "Sam"), "which files?", typed=True)
    for phrase in ("thinking", "reasoning", "thought"):  # never reads as a reasoning request
        assert phrase not in text.lower()


# -- answering on a call -------------------------------------------------------------------------

def setup(service, model, key="idem_q1", delegation="t_q1", title="Checkout page timeout fix"):
    service.rt.answer_call = model
    worker = _progress_worker(service)
    service.store.reserve_run(key, "int_x", "call_" + key, 1)
    service.store.progress(key, "request", "Find why the checkout page times out and fix it")
    service.store.set_title(key, title)
    service.store.update_run(key, "run_" + key, "running")
    backend = BackendRun(delegation, 1, key, run_id="run_" + key, status="running")
    worker.interaction.runs[backend.delegation_id] = backend
    worker.interaction.latest_delegation_id = delegation
    return worker, backend


def tool(service, backend, name, preview, call_id):
    now = time.time()
    A.observe(service.store, backend.idem_key, {"event": "tool.started", "tool": name, "preview": preview,
                                                "tool_call_id": call_id}, now=now)


class Steers:
    def __init__(self, accept=True):
        self.sent: list[tuple[str, str]] = []
        self.accept = accept

    def __call__(self, run_id, text):
        self.sent.append((run_id, text))
        return self.accept


def test_what_is_it_testing_is_answered_from_the_steps(service, monkeypatch):
    seen = {}

    def model(question, facts):
        seen["q"], seen["facts"] = question, facts
        return "The checkout timeout fix: it's running the payment and cart tests."
    worker, backend = setup(service, model)
    steers = Steers()
    monkeypatch.setattr(worker.hermes, "steer", steers)
    tool(service, backend, "terminal", "pytest -q tests/test_payment.py tests/test_cart.py", "c1")
    asyncio.run(worker.answer_task_question("call_2", backend.delegation_id, "What are you testing"))
    # The side model saw the tool log with each call's own detail (the suite names) and the request.
    assert "test_payment.py" in seen["facts"] and "checkout page times out" in seen["facts"]
    said = spoken(worker)[-1]
    assert "payment and cart tests" in said and "What are you testing" in said
    assert "Checkout page timeout fix" in said
    assert steers.sent == []  # answered from the steps: the task was not interrupted


def test_a_question_the_steps_cant_answer_asks_the_task_once_and_speaks_its_reply(service, monkeypatch):
    worker, backend = setup(service, lambda q, facts: "")
    steers = Steers()
    monkeypatch.setattr(worker.hermes, "steer", steers)
    tool(service, backend, "terminal", "make check", "c1")

    async def scenario():
        await worker.answer_task_question("call_3", backend.delegation_id, "why is it running the checks again")
        assert len(steers.sent) == 1
        run_id, text = steers.sent[0]
        assert run_id == backend.run_id and "why is it running the checks again" in text and "STATUS/DETAIL" in text
        assert "ask it" in spoken(worker)[-1]
        # A line already on its way the instant the question went out isn't its answer.
        question, handoff, _ = backend.asked or ("", "", 0.0)
        backend.asked = (question, handoff, time.monotonic() - 2)
        await worker._handle_hermes_event(backend, {
            "event": "message.interim",
            "text": "STATUS: Rerunning the checks\nDETAIL: The first run hit a flaky network test, so I'm confirming the fix"})
    asyncio.run(scenario())
    said = spoken(worker)[-1]
    assert "flaky network test" in said and "answered their question" in said
    assert backend.asked is None
    assert any("You asked: why is it running the checks again" in e.get("text", "")
               for e in service.store.work(idem_key=backend.idem_key)["events"])
    # The next status line is ordinary progress again, not a second "answer".
    before = len(spoken(worker))
    asyncio.run(worker.task_replied(backend, "Patched the retry"))
    assert len(spoken(worker)) == before


def test_no_steps_at_all_asks_the_task(service, monkeypatch):
    worker, backend = setup(service, lambda q, facts: None)
    steers = Steers()
    monkeypatch.setattr(worker.hermes, "steer", steers)
    asyncio.run(worker.answer_task_question("call_4", backend.delegation_id, "which page is it on?"))
    assert len(steers.sent) == 1 and "ask it" in spoken(worker)[-1]


def test_no_side_model_lets_the_voice_answer_the_exact_question_from_the_steps(service, monkeypatch):
    worker, backend = setup(service, lambda q, facts: None)
    steers = Steers()
    monkeypatch.setattr(worker.hermes, "steer", steers)
    tool(service, backend, "terminal", "pytest -q tests/test_payment.py", "c1")
    asyncio.run(worker.answer_task_question("call_5", backend.delegation_id, "what is it testing"))
    said = spoken(worker)[-1]
    assert steers.sent == []
    assert 'They asked about the "Checkout page timeout fix" task: "what is it testing"' in said
    assert "test_payment.py" in said or "Ran the tests" in said
    assert "never just \"the test run\"" in said


def test_a_task_that_wont_take_the_question_falls_back_to_its_steps(service, monkeypatch):
    worker, backend = setup(service, lambda q, facts: "")
    monkeypatch.setattr(calls, "STEER_RETRY_S", 0.0)
    steers = Steers(accept=False)
    monkeypatch.setattr(worker.hermes, "steer", steers)
    tool(service, backend, "terminal", "make check", "c1")
    asyncio.run(worker.answer_task_question("call_6", backend.delegation_id, "what is it checking for"))
    assert len(steers.sent) == calls.STEER_RETRIES + 1
    assert backend.asked is None
    assert "what is it checking for" in spoken(worker)[-1] and "ask it" not in spoken(worker)[-1]


def test_no_answer_yet_is_said_once_then_forgotten(service, monkeypatch):
    worker, backend = setup(service, lambda q, facts: "")
    monkeypatch.setattr(worker.hermes, "steer", Steers())
    monkeypatch.setattr(calls, "ASK_REPLY_WAIT_S", 0.05)
    monkeypatch.setattr(calls, "ASK_REPLY_KEEP_S", 0.1)
    monkeypatch.setattr(worker, "call_connected", lambda: True)

    async def scenario():
        await worker.answer_task_question("call_7", backend.delegation_id, "which files did it change")
        await asyncio.sleep(0.3)
    asyncio.run(scenario())
    assert "hasn't answered" in spoken(worker)[-1]
    assert backend.asked is None


def test_how_is_it_going_keeps_the_status_path(service, monkeypatch):
    asked = []
    worker, backend = setup(service, lambda q, facts: asked.append(q) or "x")
    steers = Steers()
    monkeypatch.setattr(worker.hermes, "steer", steers)
    asyncio.run(worker.answer_status("call_8", backend.delegation_id))
    assert asked == [] and steers.sent == []
    assert "how the \"Checkout page timeout fix\" task is going" in spoken(worker)[-1]


def test_voice_rules_split_content_questions_from_status():
    rules = (Path(P.__file__).parent / "rules.md").read_text()
    assert "never delegate a status question" in rules
    assert "what it's testing" in rules and "hand it off as a follow-up to that task" in rules
    assert "never just \"the test run\"" in rules
