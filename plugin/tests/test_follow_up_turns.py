"""A task's later turns are just what's new (SPK-127): the full task rules go on a session's first turn
only (again after compaction summarized them away), a follow-up names what it continues once and flat,
and what's on screen sits on its own line, never inside the user's words. Invented tasks only."""
import json

import pytest

from fakes import wait_for
from speakeasy import calls
from speakeasy import history as H
from speakeasy.prompt import builder as P
from test_history import add_msg, add_session, make_db
from test_routing import start_call, tasks


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)


def text_of(call):
    sent = call["input"]
    return sent if isinstance(sent, str) else json.dumps(sent)


def say(worker, delegation_id, words, **extra):
    worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 1, "end_ms": 2})
    worker.delegate(delegation_id, words, **extra)


def record_turns(path, hermes, session_id, since=0):
    """Hermes writes each turn's input into the session as a user row."""
    for call in hermes.calls[since:]:
        if call["session_id"] == session_id:
            add_msg(path, session_id, "user", text_of(call))
            add_msg(path, session_id, "assistant", "DONE: planned\nSPOKEN: Planned.")


def finished_task(server, service, hermes):
    path = service.rt.state_db
    make_db(path)
    session, worker = start_call(server, service)
    say(worker, "call_beds", "Plan the vegetable beds for spring")
    wait_for(lambda: len(hermes.calls) == 1)
    sid = hermes.calls[0]["session_id"]
    add_session(path, sid, "api_server", title="Spring vegetable beds")
    record_turns(path, hermes, sid)
    wait_for(lambda: any(t.get("status") == "completed" for t in tasks(server)))
    return session, worker, path, sid


def test_the_first_turn_carries_the_rules(server, service, hermes):
    _, _, _, _ = finished_task(server, service, hermes)
    first = text_of(hermes.calls[0])
    assert P.TASK_RULES_MARK in first and len(first) > 3000


def test_a_follow_up_turn_has_no_rules_and_no_nested_chain(server, service, hermes):
    _, worker, path, sid = finished_task(server, service, hermes)
    say(worker, "call_more", "make the tomato bed wider", task_id="call_beds")
    wait_for(lambda: len(hermes.calls) == 2)
    second = hermes.calls[1]
    assert second["session_id"] == sid, "a follow-up continues the task's own session"
    sent = text_of(second)
    assert P.TASK_RULES_MARK not in sent and len(sent) < 1500, sent
    assert sent.startswith(P.FOLLOW_UP_HEAD) and "make the tomato bed wider" in sent.splitlines()[0]
    assert "Plan the vegetable beds for spring" in sent and "(following up:" not in sent
    record_turns(path, hermes, sid, since=1)
    wait_for(lambda: sum(t.get("status") == "completed" for t in tasks(server)) >= 1 and
             all(t.get("status") != "running" for t in tasks(server)))
    # A follow-up of the follow-up names the one it continues once, flat.
    say(worker, "call_again", "and add a row of basil", task_id="call_more")
    wait_for(lambda: len(hermes.calls) == 3)
    third = text_of(hermes.calls[2])
    assert hermes.calls[2]["session_id"] == sid and P.TASK_RULES_MARK not in third
    assert "(following up:" not in third and third.count("follows up") <= 1, third
    assert "make the tomato bed wider" in third and "Plan the vegetable beds" not in third
    record_turns(path, hermes, sid, since=2)
    # What the page shows of each turn is the person's words alone.
    entries = H.session_messages(path, sid)["entries"]
    said = [e["text"] for e in entries if e["kind"] == "user"]
    assert said == ["Plan the vegetable beds for spring", "make the tomato bed wider", "and add a row of basil"]


def test_after_compaction_the_rules_come_back(server, service, hermes):
    _, worker, path, sid = finished_task(server, service, hermes)
    # Compaction ended the session and continued it in a child whose context holds only a summary.
    tip = sid + "_c1"
    add_session(path, tip, "api_server", parent=sid)
    add_msg(path, tip, "user", "[CONTEXT COMPACTION] Earlier turns: the beds were planned.")
    say(worker, "call_more", "make the tomato bed wider", task_id="call_beds")
    wait_for(lambda: len(hermes.calls) == 2)
    assert P.TASK_RULES_MARK in text_of(hermes.calls[1]), "the rules fell out, so they go again"


def test_an_unreadable_state_db_sends_the_rules(tmp_path):
    assert H.rules_in_session(tmp_path / "missing.db", "s1", P.TASK_RULES_MARK) is False


def test_what_is_on_screen_is_its_own_line(server, service, hermes):
    session, worker, path, sid = finished_task(server, service, hermes)
    from test_focus import _focus
    beds = next(t["task_id"] for t in tasks(server) if "vegetable" in str(t["events"]))
    assert _focus(server, session, {"task_id": beds, "detail": "the task page for Spring vegetable beds"})[0] == 200
    wait_for(lambda: any("looking at the task" in c for k, _, c in worker.sent if k == "session.thinking.append"))
    say(worker, "call_more", "make it wider")
    wait_for(lambda: len(hermes.calls) == 2)
    sent = text_of(hermes.calls[1])
    lines = sent.splitlines()
    assert "On screen: the task page for Spring vegetable beds" in lines
    assert "Spring vegetable beds" not in lines[0] and "is looking at" not in sent
    # The task's request (what its title and summary are made from) is only the words.
    work = service.store.work(idem_key=service.store.key_for_run(hermes.calls[1]["run_id"]))
    assert "looking at" not in (work.get("request") or "") and "On screen" not in (work.get("request") or "")


def test_a_tapped_answer_is_one_flat_line_naming_the_question(server, service, hermes):
    from fakes import http
    from test_question_cards import ASK
    hermes.responder = lambda prompt, sid: (ASK if "One long bed" not in prompt
                                            else "Ordered.\nDONE: soil\nSPOKEN: Soil is ordered.")
    path = service.rt.state_db
    make_db(path)
    session, worker = start_call(server, service)
    say(worker, "call_q", "Plan the garden beds for spring")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    sid = hermes.calls[0]["session_id"]
    add_session(path, sid, "api_server")
    record_turns(path, hermes, sid)
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/answer",
                        {"task_id": done["task_id"], "text": "One long bed"}, server.token)
    assert status == 200 and body["sent"], body
    wait_for(lambda: len(hermes.calls) >= 2)
    sent = text_of(hermes.calls[1])
    lines = sent.split("\n\n")
    assert P.TASK_RULES_MARK not in sent and lines[0].endswith("answered by tapping: One long bed"), sent
    assert lines[1].startswith('Answering your question "Which layout should I order soil for?"')
    assert sent.count("Which layout should I order soil for?") == 1 and "(following up:" not in sent


def test_follow_up_prompt_shape():
    names = P.Names()
    text = P.build_follow_up_prompt(
        names, "wait for all four", continues=P.continues_line("Ship build 67", None, None,
                                                              "What should build 67 wait for?", tapped=True),
        on_screen="the task page for Build 67", call_lines="Assistant: It is waiting on two fixes.", tapped=True)
    lines = text.split("\n\n")
    assert lines[0].endswith("answered by tapping: wait for all four")
    assert lines[1] == "Answering your question \"What should build 67 wait for?\" (in the earlier request \"Ship build 67\")."
    assert "On screen: the task page for Build 67" in lines and P.TASK_RULES_MARK not in text
    assert H.user_words(text) == ("user", "wait for all four")
