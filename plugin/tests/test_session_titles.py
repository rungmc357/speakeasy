"""The Hermes session behind a voice task is named after the task, not the prompt's preamble."""
from __future__ import annotations

import threading

import pytest

from fakes import wait_for
from speakeasy import session_titles
from speakeasy.calls import task_session_id
from speakeasy.hermes_api import HermesError


@pytest.fixture(autouse=True)
def _fresh_state():
    session_titles._ours.clear()
    yield
    session_titles._ours.clear()


def _task_sessions(hermes):
    with hermes.lock:
        return {sid: t for sid, t in hermes.session_titles.items() if sid.startswith("speakeasy_task_")}


def test_a_task_session_gets_the_stand_in_then_the_titler_name(server, service, hermes):
    from test_routing import start_call
    gate = threading.Event()

    def titler(text):
        gate.wait(10)
        return "Mac mini storage usage"

    service.rt.title_call = titler
    _, worker = start_call(server, service)
    worker.delegate("call_s", "Can you check how much storage is left on the Mac mini")
    wait_for(lambda: any(t == "Check how much storage is left on the Mac mini"
                         for t in _task_sessions(hermes).values()), timeout=10)
    gate.set()
    wait_for(lambda: list(_task_sessions(hermes).values()) == ["Mac mini storage usage"], timeout=10)
    assert all(not (t or "").startswith("Handle voice task") for _, t in hermes.title_patches)
    assert not any("#" in (t or "") for _, t in hermes.title_patches)


def test_a_title_another_session_holds_gets_its_day_not_a_number(server, service, hermes):
    from test_routing import start_call
    with hermes.lock:
        hermes.session_titles["older_session"] = "Mac mini storage usage"
    service.rt.title_call = lambda text: "Mac mini storage usage"
    _, worker = start_call(server, service)
    worker.delegate("call_d", "Check the storage on the Mac mini")
    wait_for(lambda: any((t or "").startswith("Mac mini storage usage (") for t in _task_sessions(hermes).values()),
             timeout=10)
    title = next(iter(_task_sessions(hermes).values()))
    assert "#" not in title


def test_a_conversation_the_user_named_never_takes_a_later_task_name(server, service, hermes):
    """SPK-320: a rename from the app always wins, even over the titler's name arriving later."""
    from test_routing import start_call
    gate = threading.Event()

    def titler(text):
        gate.wait(10)
        return "Porch lamp wiring plan"

    service.rt.title_call = titler
    _, worker = start_call(server, service)
    worker.delegate("call_u", "Can you plan the wiring for the porch lamps")
    wait_for(lambda: _task_sessions(hermes), timeout=10)
    sid = next(iter(_task_sessions(hermes)))
    service.store.set_user_title(sid, "Lamp project")
    with hermes.lock:
        hermes.session_titles[sid] = "Lamp project"
    sent = len(hermes.title_patches)
    gate.set()
    wait_for(lambda: any(service.store.title(k) == "Porch lamp wiring plan"
                         for k, _, _ in service.store.keys_for_session(sid)), timeout=10)
    import time as _t
    _t.sleep(0.5)
    assert hermes.session_titles[sid] == "Lamp project"
    assert len(hermes.title_patches) == sent


def test_names_never_reach_the_session_title(server, service, hermes):
    from test_routing import start_call
    service.settings.patch({"user_name": "Priya", "assistant_name": "Juniper"})
    service.rt.title_call = lambda text: "Push all five of Priya's PRs"
    _, worker = start_call(server, service)
    worker.delegate("call_n", "Juniper, push all five of the open pull requests on the garden repo")
    wait_for(lambda: "Push all five of my PRs" in _task_sessions(hermes).values(), timeout=10)
    assert not any("Priya" in (t or "") or "Juniper" in (t or "") for _, t in hermes.title_patches)


class FakeSessions:
    """``session_title`` / ``set_session_title`` like HermesAPI, without HTTP."""

    def __init__(self, rows=None, missing_for=0):
        self.rows = dict(rows or {})
        self.missing_for = missing_for  # GETs that 404 before the row appears
        self.sets: list[tuple[str, str]] = []

    def session_title(self, sid):
        if self.missing_for:
            self.missing_for -= 1
            raise HermesError(404, "Hermes HTTP 404")
        if sid not in self.rows:
            raise HermesError(404, "Hermes HTTP 404")
        return self.rows[sid]

    def set_session_title(self, sid, title):
        if any(t == title for s, t in self.rows.items() if s != sid):
            raise HermesError(400, "Hermes HTTP 400")
        self.rows[sid] = title
        self.sets.append((sid, title))


def test_push_waits_for_the_row_hermes_creates_when_the_run_starts():
    api = FakeSessions({"s1": "Handle voice task from Speakeasy #3"}, missing_for=2)
    assert session_titles.push(api, "s1", lambda: "Office lamp brightness", retry_s=0.01) == "Office lamp brightness"
    assert api.rows["s1"] == "Office lamp brightness"


def test_push_gives_up_quietly_when_the_row_never_appears():
    api = FakeSessions()
    assert session_titles.push(api, "gone", lambda: "Anything", wait_s=0.05, retry_s=0.01) is None
    assert api.sets == []


def test_a_rename_by_someone_else_is_never_overwritten():
    api = FakeSessions({"s1": None})
    assert session_titles.push(api, "s1", lambda: "Office lamp brightness", retry_s=0.01)
    api.rows["s1"] = "My lamp thing"  # renamed in the dashboard
    assert session_titles.push(api, "s1", lambda: "Set office lamp to maximum brightness", retry_s=0.01) is None
    assert api.rows["s1"] == "My lamp thing"


def test_an_unchanged_name_is_not_sent_again():
    api = FakeSessions({"s1": None})
    session_titles.push(api, "s1", lambda: "Office lamp brightness", retry_s=0.01)
    session_titles.push(api, "s1", lambda: "Office lamp brightness", retry_s=0.01)
    assert len(api.sets) == 1


def test_a_backend_without_sessions_is_left_alone():
    assert session_titles.push(object(), "s1", lambda: "Anything") is None


def test_candidates_fit_hermes_title_limit():
    long = "word " * 40
    assert all(len(c) <= session_titles.MAX_TITLE for c in session_titles.candidates(long, now=0))
    assert session_titles.candidates("  ") == []


def test_a_continued_chat_session_is_not_the_task_session():
    assert task_session_id("idem_1").startswith("speakeasy_task_")
    assert task_session_id("idem_1") != task_session_id("idem_2")
