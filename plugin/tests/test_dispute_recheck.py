"""SPK-555: "no it's not" right after a task answered re-checks that task. It is never a correction of
the request's words, never a new task and never a new Home card; a stand-in rename also renames the
card Home's widget stack kept."""
from __future__ import annotations

import pytest

from fakes import SDP, wait_for
from speakeasy import router as R
from test_home_control import STATES, FakeHA, _planner, _svc, st  # noqa: F401  (shared fixtures)

GARAGE = "Close the garage door. Garage door closed."


@pytest.mark.parametrize("said", [
    "No it's not.", "It's still open, I can see it.", "No, they're not They're totally not off",
    "That didn't work", "You didn't do it", "Nothing happened", "no it isn't", "The garage is still open",
    "nope, it\u2019s still open", "they're still on", "That's wrong", "It didn't close"])
def test_a_protest_about_the_last_answer_is_a_dispute(said):
    assert R.disputes(said, GARAGE)


@pytest.mark.parametrize("said", [
    "No, turn them on", "no thanks", "Our guest room, sorry", "The kitchen lights are still on too",
    "what's the weather", "no, the dashboard one", "It's not urgent", "no", "close the garage door",
    "they are not coming to dinner", "No, it's the other report", "I don't think so"])
def test_a_new_ask_or_answer_is_not_a_dispute(said):
    assert not R.disputes(said, GARAGE)


def test_a_dispute_is_never_spliced_into_the_request_as_a_correction():
    # The bug: "Turn off the hall lamps (correction: they're not, ...)".
    assert R.correction("No, they're not") is None
    assert R.correction("No it's not") is None
    # Real corrections still are.
    assert R.correction("Our guest room, sorry") == ("phrase", "guest room")
    assert R.correction("no, turn them on") == ("replace", "turn them on")


def _say(worker, text, at):
    worker.feed({"type": "session.output_transcript.delta", "delta": text, "start_ms": at, "end_ms": at + 1})


def test_disputing_a_home_answer_rechecks_the_same_task(home, hermes):
    """The issue's replay on home control: close the blinds, "No it's not.", "It's still open, I can see it." ->
    one task, one card, its title unchanged; the house is checked and asked again each time."""
    ha = FakeHA([st("cover.blinds", "Living Room Blinds", "open", current_position=100)])
    close = {"calls": [{"service": "cover.close_cover", "entity_id": ["cover.blinds"]}]}
    svc, workers = _svc(home, ha, [
        {**close, "say": "Blinds closed."},
        {**close, "say": "It was still open. Closing it again now."},
        {**close, "say": "Sent the close again; it shows as closing."}])
    try:
        svc.put_home({"enabled": True, "entities": ["cover.blinds"]})
        created = svc.create_session({"sdp": SDP}, "req_dispute_1")
        worker = workers[-1]
        worker.delegate("call_blinds_1", "Close the living room blinds.")
        wait_for(lambda: any("Blinds closed." in str(x) for x in worker.sent), timeout=10)
        _say(worker, "Blinds closed.", 3)
        worker.delegate("call_blinds_2", "No it's not.")
        wait_for(lambda: any("Closing it again now." in str(x) for x in worker.sent), timeout=10)
        _say(worker, "It was still open. Closing it again now.", 6)
        worker.delegate("call_blinds_3", "It's still open, I can see it.")
        wait_for(lambda: any("it shows as closing" in str(x) for x in worker.sent), timeout=10)

        assert hermes.calls == []  # nothing went to a new Hermes task
        assert len(ha.calls) == 3  # the house was asked again each time
        # Every re-check was planned from the original request, with what they said beside it.
        recheck = _planner.prompts[1][-1]["content"]
        assert recheck.startswith("Close the living room blinds.") and "No it's not." in recheck
        assert "It's still open, I can see it." in _planner.prompts[2][-1]["content"]

        iid = created["interaction_id"]
        with svc.store._lock:
            rows = svc.store._db.execute(
                "SELECT delegation_id, title FROM runs WHERE interaction_id=? AND status != 'rejected'", (iid,)).fetchall()
        assert [r[0] for r in rows] == ["call_blinds_1"]  # one task
        title = rows[0][1]
        assert title and "still open" not in title.lower() and "not" not in title.lower().split()
        with svc.store._lock:
            key = svc.store._db.execute("SELECT idem_key FROM runs WHERE delegation_id='call_blinds_1'").fetchone()[0]
        assert "(correction:" not in (svc.store.request_text(key) or "")
        assert (svc.store.work(idem_key=key) or {}).get("result", {}).get("spoken", "").startswith("Sent the close again")

        feed = svc.interaction(iid).feed
        shown = wait_for(lambda: [t for t in feed.last.get("tasks") or [] if t.get("status") not in ("rejected",)])
        assert [t["task_id"] for t in shown] == ["call_blinds_1"]  # one card on the call
        assert shown[0].get("title") == title
        stack = svc.home_stack({})
        runs = {i.get("run_id") for i in stack.get("items", []) if i.get("run_id")}
        assert len(runs) <= 1  # at most the one card, never a card per protest
    finally:
        svc.close()


def test_only_a_fresh_answer_without_a_new_subject_is_disputed(home, hermes, monkeypatch):
    from speakeasy import calls as C
    ha = FakeHA([st("cover.blinds", "Living Room Blinds", "open", current_position=100)])
    svc, workers = _svc(home, ha, [{"calls": [{"service": "cover.close_cover", "entity_id": ["cover.blinds"]}],
                                    "say": "Blinds closed."}])
    try:
        svc.put_home({"enabled": True, "entities": ["cover.blinds"]})
        svc.create_session({"sdp": SDP}, "req_dispute_2")
        worker = workers[-1]
        worker.delegate("call_late_1", "Close the living room blinds.")
        wait_for(lambda: any("Blinds closed." in str(x) for x in worker.sent), timeout=10)
        assert worker.dispute_target("No it's not.") == "call_late_1"
        assert worker.dispute_target("The kitchen lights are still on too") is None   # a new subject
        assert worker.dispute_target("No it's not.", marked="call_other") is None     # the voice named another task
        monkeypatch.setattr(C, "DISPUTE_WINDOW_S", -1.0)
        assert worker.dispute_target("No it's not.") is None  # long after the answer: routing decides
    finally:
        svc.close()


def test_a_stand_in_rename_also_renames_the_home_card(service):
    """The run got its real name later; Home's widget stack kept the protest as the card's title."""
    from speakeasy.text import short_title
    store = service.store
    service.now({})  # Home's state exists, with its stack table
    request = "They're not They're totally not off"
    stand_in = short_title(request)
    assert stand_in
    with store._lock:
        store._db.execute("INSERT INTO runs (idem_key, interaction_id, delegation_id, revision, run_id, status, title, updated) "
                          "VALUES ('k_st', 'vi_x', 'call_st', 1, 'lr_st', 'completed', ?, 2)", (stand_in,))
    store.progress("k_st", "request", request)
    for sid, title in (("w:r:lr_st", stand_in), ("w:r:lr_other", "Named by hand")):
        with store._lock:
            store._db.execute("INSERT INTO home_stack (id, kind, run_id, item_id, card_id, title, line, source, added_at, state) "
                              "VALUES (?, 'result', 'lr_st', ?, ?, ?, 'x', '{}', 1, 'open')", (sid, "t:" + sid, "c:" + sid, title))
    assert store.rename_stand_in("k_st", "Bedroom lights check") is True
    with store._lock:
        titles = dict(store._db.execute("SELECT id, title FROM home_stack WHERE run_id='lr_st'").fetchall())
    assert titles["w:r:lr_st"] == "Bedroom lights check"
    assert titles["w:r:lr_other"] == "Named by hand"  # a title given any other way stays


def test_a_titler_rename_also_renames_the_home_card(service):
    store = service.store
    service.now({})
    with store._lock:
        store._db.execute("INSERT INTO runs (idem_key, interaction_id, delegation_id, revision, run_id, status, title, updated) "
                          "VALUES ('k_t2', 'vi_x', 'call_t2', 1, 'lr_t2', 'completed', 'Its still open I', 2)")
        store._db.execute("INSERT INTO home_stack (id, kind, run_id, item_id, card_id, title, line, source, added_at, state) "
                      "VALUES ('w:r:lr_t2', 'result', 'lr_t2', 't:lr_t2', 'c:lr_t2', 'Its still open I', 'x', '{}', 1, 'open')")
    store.set_title("k_t2", "Living room blinds check")
    with store._lock:
        shown = store._db.execute("SELECT title FROM home_stack WHERE id='w:r:lr_t2'").fetchone()[0]
    assert shown == "Living room blinds check"


def test_disputing_a_hermes_answer_continues_that_task(server, service, hermes, monkeypatch):
    """The garage isn't a home-control device, so its task ran in Hermes: "No it's not." continues
    that task's own session as a re-check, never a new task named after the complaint."""
    from speakeasy import calls
    from test_follow_up_turns import record_turns, say
    from test_history import add_session, make_db
    from test_routing import start_call, tasks
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)
    path = service.rt.state_db
    make_db(path)
    _, worker = start_call(server, service)
    say(worker, "call_garage", "Close the garage door.")
    wait_for(lambda: len(hermes.calls) == 1)
    sid = hermes.calls[0]["session_id"]
    add_session(path, sid, "api_server", title="Garage door")
    record_turns(path, hermes, sid)
    wait_for(lambda: any(t.get("status") == "completed" for t in tasks(server)))
    worker.replied_task = "call_garage"  # the voice spoke its answer
    say(worker, "call_protest", "No it's not.")
    wait_for(lambda: len(hermes.calls) == 2)
    assert hermes.calls[1]["session_id"] == sid  # the same task's session, re-checking
    sent = hermes.calls[1]["input"] if isinstance(hermes.calls[1]["input"], str) else str(hermes.calls[1]["input"])
    assert "No it's not." in sent and "(correction:" not in sent
    with service.store._lock:
        key = service.store._db.execute("SELECT idem_key FROM runs WHERE delegation_id='call_protest'").fetchone()
        first = service.store._db.execute("SELECT idem_key FROM runs WHERE delegation_id='call_garage'").fetchone()[0]
        parent = service.store._db.execute("SELECT parent FROM runs WHERE idem_key=?", (key[0],)).fetchone()[0] if key else None
    assert key and parent == first
