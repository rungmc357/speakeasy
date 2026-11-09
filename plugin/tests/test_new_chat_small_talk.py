"""SPK-290: small talk that opens a new chat looks like chat.

A zero-tool reply to "yo" is a message, not a task: its work carries ``chat: true`` so the apps drop
the task header ("Started · Took 4 s") and the "Looked into it" card (older apps ignore the field).
A chat that opens with small talk is "New chat" (in the app and on its Hermes session) until a real
ask names it, never the titler's guess or Hermes' own name made from the brief.
"""
from __future__ import annotations

import pytest

from fakes import http, wait_for
from speakeasy import session_titles


@pytest.fixture(autouse=True)
def _fresh_titles():
    session_titles._ours.clear()
    yield
    session_titles._ours.clear()


BRIEF_TITLE = "Handle the user's task in Speakeasy app #24"  # Hermes' own name, made from the brief


def _job(server, text):
    status, body = http(server.base_url, "POST", "/voice/work", {"text": text}, server.token)
    assert status == 202, body
    return body


def _reply_only(hermes, run_id):
    """Finish a held run as a plain reply: no tool call on its stream."""
    run = hermes.runs[run_id]
    run["events"] = [e for e in run["events"] if not e["event"].startswith("tool.")]
    run["status"] = "completed"
    run["done"].set()


def _session_of(service, key):
    return service.store.session_for(key)


def test_small_talk_in_a_new_chat_is_new_chat_and_marked_as_chat(server, service, hermes):
    named = []
    service.rt.title_call = lambda text: named.append(text) or "Greet user in voice session"
    hermes.auto_title = BRIEF_TITLE
    hermes.hold = True
    _job(server, "yo")
    wait_for(lambda: hermes.calls)
    key, run_id = hermes.calls[0]["idem"], hermes.calls[0]["run_id"]
    _reply_only(hermes, run_id)
    wait_for(lambda: service.store.run_status(key) == "completed")
    assert service.store.title(key) == "New chat"
    session = _session_of(service, key)
    wait_for(lambda: hermes.session_titles.get(session) == "New chat", timeout=10)
    assert named == []                                   # no guessed topic ("Greet user")
    work = service.store.work(idem_key=key) or {}
    assert work.get("chat") is True
    detail = service.store.work(run_id, activity_detail=True) or {}
    assert detail.get("chat") is True


def test_the_first_real_ask_names_the_chat(server, service, hermes):
    service.rt.title_call = lambda text: "Spring tomato bed plan"
    hermes.auto_title = BRIEF_TITLE
    hermes.hold = True
    first = _job(server, "hey")
    wait_for(lambda: hermes.calls)
    key, run_id = hermes.calls[0]["idem"], hermes.calls[0]["run_id"]
    _reply_only(hermes, run_id)
    wait_for(lambda: service.store.run_status(key) == "completed")
    session = _session_of(service, key)
    wait_for(lambda: hermes.session_titles.get(session) == "New chat", timeout=10)
    status, body = http(server.base_url, "POST", f"/voice/work/{first.get('run_id') or run_id}/message",
                        {"text": "Plan the tomato beds for spring"}, server.token)
    assert status == 200, body
    wait_for(lambda: len(hermes.calls) == 2)
    assert hermes.calls[1]["session_id"] == session      # the same chat
    hermes.runs[hermes.calls[1]["run_id"]]["done"].set()
    wait_for(lambda: hermes.session_titles.get(session) not in (None, "New chat"), timeout=10)
    assert hermes.session_titles[session] in ("Spring tomato bed plan", "Plan the tomato beds for spring")


def test_work_with_a_tool_call_is_not_marked_as_chat(server, service, hermes):
    _job(server, "Find a quiet cafe near the office")
    wait_for(lambda: hermes.calls)
    key = hermes.calls[0]["idem"]
    wait_for(lambda: service.store.run_status(key) == "completed")
    work = service.store.work(idem_key=key) or {}
    assert "chat" not in work                             # the fake's run used web_search
    assert service.store.title(key) != "New chat"


def test_a_new_chat_name_is_only_replaced_while_it_is_still_new_chat():
    class Sessions:
        def __init__(self, title):
            self.title, self.sets = title, []

        def session_title(self, sid):
            return self.title

        def set_session_title(self, sid, title):
            self.sets.append(title)
            self.title = title

    renamed = Sessions("My own name")
    assert session_titles.push(renamed, "s1", lambda: "Tomato beds", only_if="New chat", retry_s=0.01) is None
    assert renamed.sets == []
    fresh = Sessions("New chat (Nov 8)")                  # "New chat" was taken: Hermes titles are unique
    assert session_titles.push(fresh, "s2", lambda: "Tomato beds", only_if="New chat", retry_s=0.01) == "Tomato beds"
