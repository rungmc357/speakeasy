"""Home (SPK-124, now.py): one ordered list of what's live and new, its header sentence, the 4:00 AM
rollover in the phone's zone, one read state shared with the conversation list, Later, the answer
router and Earlier's filter menu. Invented data only."""
from __future__ import annotations

import datetime as dt
import json
import time
from zoneinfo import ZoneInfo

import pytest

from fakes import wait_for
from speakeasy import history as H
from speakeasy import now as N
from test_history import NOW, add_msg, add_session, get, make_db, never_routed, post, state  # noqa: F401

QUESTION = {"kind": "question", "question": "Which desk should I order?",
            "options": [{"text": "Oakline 60 in.", "link": "https://desks.example/oakline", "facts": ["$389", "60 in. wide"]},
                        {"text": "Birch 55 in.", "link": "javascript:alert(1)"}], "recommended": 0, "images": [1, 0]}


def task(store, key, status="completed", *, session=None, result=None, settled=None, created=None,
         request="Find me a standing desk under $400", interaction="vi_typed"):
    store.reserve_run(key, interaction, "d_" + key, 0)
    store.progress(key, "request", request)
    if session:
        store.set_session(key, session)
    if status in {"running", "working"}:
        store.update_run(key, "run_" + key, "running")
        if status == "working":
            store.update_run(key, None, "working")
    elif status != "admitting":
        store.update_run(key, "run_" + key, "running")
        store.transition(key, status, result=result)
    with store._lock, store._db:
        if settled is not None:
            store._db.execute("UPDATE runs SET settled_at=? WHERE idem_key=?", (settled, key))
        if created is not None:
            store._db.execute("UPDATE runs SET created=? WHERE idem_key=?", (created, key))
    return "run_" + key


def kinds(body):
    return [(i["kind"], i["title"]) for i in body["items"]]


# -- the sentence -----------------------------------------------------------------------------------

def _items(**n):
    out = []
    for kind, count in n.items():
        out += [{"kind": kind, "title": "Desk hunt", "id": f"s:{kind}{n}"} for n in range(count)]
    return out


@pytest.mark.parametrize("counts,text", [
    ({}, "All quiet."),
    ({"question": 1}, "Desk hunt has a question for you."),
    ({"question": 1, "new_answer": 1}, "One question for you, and one new answer."),
    ({"question": 2, "working": 4}, "Two questions for you, and four things in the works."),
    ({"stopped": 1, "working": 2}, "One thing stopped, and two things in the works."),
    ({"working": 2}, "Two things in the works."),
    ({"seen_answer": 3}, "All caught up."),
    ({"waiting": 1}, "Waiting on one reply."),
    ({"working": 12}, "12 things in the works."),
    ({"new_answer": 1, "working": 1, "waiting": 3}, "One thing in the works, one new answer, and three waiting on replies."),
    ({"question": 1, "stopped": 1, "working": 2, "new_answer": 2, "waiting": 1},
     "One question for you, one thing stopped, two things in the works, two new answers, and one waiting on a reply."),
    ({"working": 1, "waiting": 1}, "One thing in the works, and one waiting on a reply."),
])
def test_the_header_sentence_follows_the_grammar(counts, text):
    assert N.sentence(_items(**counts)) == text


def test_the_sentence_mentions_every_kind_of_card_shown():
    words = {"question": "question", "stopped": "stopped", "working": "in the works",
             "new_answer": "new answer", "waiting": "waiting"}
    for combo in [{"question": 1, "stopped": 1, "working": 2}, {"stopped": 2, "new_answer": 1, "waiting": 2},
                  {"question": 1, "stopped": 1, "working": 2, "new_answer": 2, "waiting": 1}]:
        text = N.sentence(N.order(_items(**combo), {})).lower()
        for kind in combo:
            assert words[kind] in text, (combo, text)


def test_the_sentence_always_describes_the_first_card():
    for combo in [{"question": 1, "working": 2}, {"stopped": 2, "new_answer": 1}, {"new_answer": 2, "waiting": 1}]:
        items = N.order(_items(**combo), {})
        first = items[0]["kind"]
        word = {"question": "question", "stopped": "stopped", "new_answer": "answer"}[first]
        assert word in N.sentence(items).lower()


# -- the 4:00 AM rollover, in the phone's zone ------------------------------------------------------

def _at(zone, y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=ZoneInfo(zone)).timestamp()


def test_rollover_is_four_am_in_the_phones_zone_not_the_computers():
    la = "America/Los_Angeles"
    assert N.rollover(_at(la, 2026, 11, 6, 3, 59), N.zone(la)) == _at(la, 2026, 11, 5, 4)
    assert N.rollover(_at(la, 2026, 11, 6, 4, 0), N.zone(la)) == _at(la, 2026, 11, 6, 4)
    # 1:00 AM in Los Angeles is 4:00 AM in New York: a traveller's Home doesn't clear yet.
    t = _at(la, 2026, 11, 6, 1)
    assert N.rollover(t, N.zone(la)) < N.rollover(t, N.zone("America/New_York"))
    assert N.zone("Not/AZone") is not None


def test_a_seen_answer_opened_in_the_six_hours_before_stays_one_more_day():
    edge = _at("America/New_York", 2026, 11, 6, 4)
    assert N.seen_stays(edge + 10, None, edge)
    assert not N.seen_stays(edge - 3600, edge - 7 * 3600, edge)
    assert N.seen_stays(edge - 3600, edge - 2 * 3600, edge)


# -- the list ---------------------------------------------------------------------------------------

def test_home_orders_questions_stopped_working_new_then_seen(server, service, state):
    store = service.store
    now = time.time()
    task(store, "w1", "working", session="s_w1", request="Sort the garden photos")
    task(store, "f1", "failed", session="s_f1", request="Book the cabin", settled=now - 50)
    store.set_failure("f1", "billing", None)
    task(store, "a1", "completed", session="s_a1", request="Compare tomato seeds",
         result={"spoken": "Done.", "headline": "Brandywine wins for your shade."}, settled=now - 40)
    task(store, "q1", "completed", session="s_q1", request="Desk hunt",
         result={"spoken": "Four desks fit.", "question": QUESTION, "cards": [{"kind": "image", "name": "oak"}]},
         settled=now - 30)
    task(store, "s1", "completed", session="s_s1", request="Check the weather",
         result={"spoken": "Sunny.", "headline": "Sunny, 72°F."}, settled=now - 20)
    store.mark_seen("s1")
    status, body = get(server, "/voice/now?tz=America/New_York")
    assert status == 200, body
    assert [i["kind"] for i in body["items"]] == ["question", "stopped", "working", "new_answer", "seen_answer"]
    assert [i["tier"] for i in body["items"]] == [1, 2, 3, 4, 6]
    assert body["sentence"] == "One question for you, one thing stopped, one thing in the works, and one new answer."
    assert body["counts"] == {"needs_you": 1, "stopped": 1, "working": 1, "waiting": 0, "finished_unread": 1,
                              "on_its_own_running": 0}
    q = body["items"][0]
    assert q["id"] == "q:run_q1" and q["line"] == "Which desk should I order?"
    opts = q["question"]["options"]
    assert [o["id"] for o in opts] == ["o1", "o2"]
    assert opts[0]["link"] == "https://desks.example/oakline" and opts[0]["facts"] == ["$389", "60 in. wide"]
    assert "link" not in opts[1], "only https links"
    assert opts[0]["image"] == "/voice/card-image/run_q1/1" and q["question"]["shape"] == "things"
    stopped = body["items"][1]
    assert stopped["fix"] == {"kind": "credits"} and "credits" in stopped["line"]
    assert body["items"][3]["line"] == "Brandywine wins for your shade." and body["items"][3]["unread"] is True
    assert isinstance(body["rev"], int) and body["as_of"] > 0 and body["call"] is None
    # No pins anywhere on Home.
    assert not any("pinned" in i for i in body["items"])
    # The same state twice: the same rev.
    assert get(server, "/voice/now")[1]["rev"] == body["rev"]


def test_a_working_card_never_moves_while_it_keeps_working(service):
    store = service.store
    task(store, "w1", "working", session="s1", request="First job", created=time.time() - 300)
    first = service.now({})
    task(store, "w2", "working", session="s2", request="Second job", created=time.time() - 900)
    ids = [i["id"] for i in service.now({})["items"]]
    assert [i["id"] for i in first["items"]] == ["s:s1"]
    # The new card goes on top by when it first showed, whatever its start time says; s1 stays put.
    assert ids == ["s:s2", "s:s1"]
    assert [i["id"] for i in service.now({})["items"]] == ids


def test_old_seen_answers_leave_at_the_rollover_and_clear_seen_moves_the_rest(server, service):
    store = service.store
    now = time.time()
    edge = N.rollover(now, N.zone("UTC"))
    task(store, "old", "completed", session="s_old", request="Old one", result={"spoken": "x", "headline": "Old."},
         settled=edge - 3 * 3600)
    store.mark_seen("old")
    task(store, "new", "completed", session="s_new", request="New one", result={"spoken": "y", "headline": "New."},
         settled=max(edge + 1, now - 5))
    store.mark_seen("new")
    body = service.now({"tz": "UTC"})
    assert [i["title"] for i in body["items"]] == ["New one"]
    assert body["notice"] == {"kind": "rollover", "moved": 1, "text": "Moved one seen answer to Earlier."}
    assert service.now({"tz": "UTC"})["notice"] is None, "shown once"
    status, moved = post(server, "/voice/now/clear-seen", {})
    assert status == 200 and moved["moved"] == 1
    assert service.now({"tz": "UTC"})["items"] == []
    assert service.now({"tz": "UTC"})["sentence"] == "All quiet."


def test_dismissed_tasks_and_cancelled_ones_are_not_on_home(service):
    store = service.store
    task(store, "c1", "cancelled", session="s_c", request="Cancelled", settled=time.time())
    task(store, "d1", "completed", session="s_d", request="Cleared", result={"spoken": "x"}, settled=time.time())
    store.dismiss(["run_d1"])
    assert service.now({})["items"] == []


# -- one read state ---------------------------------------------------------------------------------

def test_reading_a_conversation_marks_its_answer_seen_on_home(server, service, state):
    add_session(state, "s_r1", "api_server", title="Seeds", at=NOW - 5)
    add_msg(state, "s_r1", "assistant", "Brandywine.", at=NOW - 5)
    task(service.store, "r1", "completed", session="s_r1", request="Compare seeds",
         result={"spoken": "Done.", "headline": "Brandywine."}, settled=time.time() - 5)
    assert service.now({})["items"][0]["kind"] == "new_answer"
    assert post(server, "/voice/history/s_r1/read", {})[0] == 200
    item = service.now({})["items"][0]
    assert item["kind"] == "seen_answer" and item["unread"] is False
    assert service.store.is_seen("r1")


def test_opening_an_answer_reads_its_conversation_in_the_list(server, service, state):
    add_session(state, "s_r2", "api_server", title="Weather", at=NOW - 5)
    add_msg(state, "s_r2", "assistant", "Sunny.", at=NOW - 5)
    task(service.store, "r2", "completed", session="s_r2", request="Weather",
         result={"spoken": "Sunny.", "headline": "Sunny."}, settled=time.time() - 5)
    assert post(server, "/voice/tasks/run_r2/seen", {})[0] == 200
    assert (service.store.session_marks().get("s_r2") or {}).get("read_at")
    assert service.now({})["items"][0]["kind"] == "seen_answer"


# -- Later ------------------------------------------------------------------------------------------

def test_later_puts_an_item_off_until_its_time(server, service, monkeypatch):
    task(service.store, "l1", "working", session="s_l1", request="Sort photos")
    status, got = post(server, "/voice/now/s:s_l1/later", {"until": time.time() + 3600})
    assert status == 200, got
    assert service.now({})["items"] == []
    real = time.time
    service.now_model.clock = lambda: real() + 3700
    item = service.now({})["items"][0]
    assert item["provenance"]["kind"] == "later"


def test_later_on_change_wakes_when_the_item_changes(server, service):
    store = service.store
    task(store, "l2", "working", session="s_l2", request="Sort photos")
    assert post(server, "/voice/now/s:s_l2/later", {"on_change": True})[0] == 200
    assert service.now({})["items"] == []
    store.transition("l2", "completed", result={"spoken": "Sorted.", "headline": "Sorted 50 photos."})
    item = service.now({})["items"][0]
    assert item["kind"] == "new_answer" and item["provenance"]["kind"] == "later"


def test_later_undo_and_bad_requests(server, service):
    task(service.store, "l3", "working", session="s_l3", request="Sort photos")
    assert post(server, "/voice/now/s:s_l3/later", {})[0] == 400
    assert post(server, "/voice/now/s:s_l3/later", {"until": time.time() - 5})[0] == 400
    assert post(server, "/voice/now/s:nothing/later", {"on_change": True})[0] == 404
    assert post(server, "/voice/now/s:s_l3/later", {"on_change": True})[0] == 200
    from fakes import http
    assert http(server.base_url, "DELETE", "/voice/now/s:s_l3/later", None, server.token)[0] == 200
    assert [i["id"] for i in service.now({})["items"]] == ["s:s_l3"]


# -- one way to answer ------------------------------------------------------------------------------

def test_answering_a_question_off_a_call_goes_to_the_task_that_asked(server, service, hermes, state):
    hermes.responder = lambda prompt, sid: ("Four fit.\n\n```speakeasy-question\n" + json.dumps(
        {"question": "Which desk?", "options": ["Oakline", "Birch"], "recommended": 0}) + "\n```")
    _, started = post(server, "/voice/work", {"text": "Find me a standing desk under $400"})
    run_id = started["run_id"]
    key = service.store.key_for_run(run_id)
    sid = wait_for(lambda: service.store.session_for(key))
    add_session(state, sid, "api_server", at=NOW - 5)
    add_msg(state, sid, "assistant", "Four fit.", at=NOW - 5)
    wait_for(lambda: service.store.work(run_id)["status"] == "completed")
    item = next(i for i in service.now({})["items"] if i["kind"] == "question")
    assert post(server, f"/voice/now/{item['id']}/answer", {"option_id": "o9"})[0] == 400
    hermes.responder = lambda prompt, sid: "Ordered the Oakline."
    status, got = post(server, f"/voice/now/{item['id']}/answer", {"option_id": "o1"})
    assert status == 200 and got["via"] == "conversation", got
    assert service.store.work(idem_key=key).get("answered") is True
    assert not any(i["kind"] == "question" for i in service.now({})["items"])


def _listen(server, query, got, ready):
    import urllib.request
    req = urllib.request.Request(server.base_url + "/voice/history/events" + query,
                                 headers={"Authorization": f"Bearer {server.token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("event:"):
                got.append(line.split(":", 1)[1].strip())
                ready.set()
            if len(got) >= 3:
                return


def test_only_apps_that_ask_get_now_events(server, service):
    import threading
    new, old = [], []
    ready_new, ready_old = threading.Event(), threading.Event()
    threading.Thread(target=_listen, args=(server, "?now=1", new, ready_new), daemon=True).start()
    threading.Thread(target=_listen, args=(server, "", old, ready_old), daemon=True).start()
    assert ready_new.wait(5) and ready_old.wait(5)
    task(service.store, "ev1", "working", session="s_ev1")
    service._now_changed()
    service.history_events.publish("session_updated", {"session_id": "s_ev1"})
    wait_for(lambda: len(new) >= 3 and len(old) >= 2, timeout=5)
    assert new[:3] == ["reset", "now", "session_updated"]
    assert old[:2] == ["reset", "session_updated"]


def test_capability_and_now_event(server, service):
    _, status = get(server, "/voice/status")
    assert status["capabilities"]["now"] is True
    task(service.store, "e1", "working", session="s_e1")
    service._now_changed()
    assert any(kind == "now" for _, kind, _ in service.history_events.events)


# -- Earlier's filter menu --------------------------------------------------------------------------

def test_earlier_filters_by_kind_with_counts(server, service, state):
    add_session(state, "kan1", "kanban", title="Lantern wick", at=NOW - 20)
    add_msg(state, "kan1", "assistant", "Done.", at=NOW - 20)
    add_session(state, "sub2", "subagent", title="Desk helper", parent="d1", at=NOW - 15)
    add_msg(state, "sub2", "assistant", "Found four desks.", at=NOW - 15)
    add_session(state, "arc1", "discord", title="Old thread", at=NOW - 9000, archived=1)
    add_msg(state, "arc1", "user", "old words", at=NOW - 9000)
    _, page = get(server, "/voice/history?kinds=conversations")
    ids = [s["id"] for s in page["sessions"]]
    assert "d1" in ids and "kan1" not in ids and "cron1" not in ids and "sub2" not in ids and "arc1" not in ids
    menu = {k["id"]: k for k in page["kinds"]}
    assert [k["label"] for k in page["kinds"]] == ["Conversations", "Scheduled jobs", "Kanban", "Subagents", "Archived"]
    assert menu["conversations"]["on"] and not menu["kanban"]["on"]
    assert menu["kanban"]["count"] == 1 and menu["scheduled"]["count"] == 1 and menu["archived"]["count"] == 1
    assert menu["subagents"]["count"] >= 1
    _, page = get(server, "/voice/history?kinds=subagents,archived")
    assert {s["id"] for s in page["sessions"]} >= {"sub2", "arc1"} and "d1" not in {s["id"] for s in page["sessions"]}
    assert all(s.get("automated") for s in page["sessions"] if s["id"] == "sub2")
    assert get(server, "/voice/history?kinds=bogus")[0] == 400
    # The old list is unchanged without kinds: no subagents, no kinds facet.
    _, old = get(server, "/voice/history")
    assert "sub2" not in {s["id"] for s in old["sessions"]} and "kinds" not in old


def test_earlier_never_repeats_home(server, service, state):
    add_session(state, "s_h1", "api_server", title="Desk hunt", at=NOW - 5)
    add_msg(state, "s_h1", "assistant", "Working.", at=NOW - 5)
    task(service.store, "h1", "working", session="s_h1", request="Desk hunt")
    service.now({})
    _, page = get(server, "/voice/history?kinds=conversations")
    row = next(s for s in page["sessions"] if s["id"] == "s_h1")
    assert row["on_now"] is True
    _, page = get(server, "/voice/history?kinds=conversations&exclude_now=1")
    assert "s_h1" not in {s["id"] for s in page["sessions"]}


def test_earlier_has_no_pinned_first_order(server, service, state):
    service.store.set_pinned("c1", True)
    _, page = get(server, "/voice/history?kinds=conversations")
    ids = [s["id"] for s in page["sessions"]]
    assert ids[0] != "c1" and not any(s.get("pinned") and s["id"] != "c1" for s in page["sessions"])


# -- the contract the apps decode -------------------------------------------------------------------

CONTRACT = __import__("pathlib").Path(__file__).resolve().parents[2] / "mac/Tests/SpeakeasyCoreTests/Contract/now.json"


def _shape(value):
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


def test_the_now_reply_matches_the_contract_the_apps_decode(server, service, state):
    """mac/.../Contract/now.json is a real /voice/now reply (invented data): SpeakeasyCore's tests decode
    it. Regenerate with SPEAKEASY_WRITE_CONTRACT=1 after changing the shape."""
    import os
    store = service.store
    now = time.time()
    task(store, "c_q", "completed", session="s_cq", request="Desk hunt",
         result={"spoken": "Four fit.", "question": QUESTION, "cards": [{"kind": "image", "name": "oak"}]}, settled=now - 30)
    task(store, "c_f", "failed", session="s_cf", request="Book the cabin", settled=now - 50)
    store.set_failure("c_f", "billing", None)
    task(store, "c_w", "working", session="s_cw", request="Sort the garden photos")
    task(store, "c_a", "completed", session="s_ca", request="Compare tomato seeds",
         result={"spoken": "Done.", "headline": "Brandywine wins for your shade."}, settled=now - 40)
    _, body = get(server, "/voice/now?tz=America/New_York")
    if os.environ.get("SPEAKEASY_WRITE_CONTRACT"):
        CONTRACT.write_text(json.dumps(body, indent=1, sort_keys=True) + "\n")
    saved = json.loads(CONTRACT.read_text())
    assert _shape({k: v for k, v in body.items() if k != "items"}) == _shape({k: v for k, v in saved.items() if k != "items"})
    by_kind = {i["kind"]: i for i in saved["items"]}
    for item in body["items"]:
        assert set(item) <= set(by_kind[item["kind"]]) | {"provenance", "idle_seconds"}, item["kind"]
