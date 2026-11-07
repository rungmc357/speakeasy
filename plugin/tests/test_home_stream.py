"""SPK-208: Home as one stream. Asks and their cards in time order, cards keep one id, a question
is a widget in the thread, older seen stretches fold into one row, and Recent lists previous tasks.
Invented data only."""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from speakeasy import stream as S
from test_history import state  # noqa: F401
from test_now import NY, _at, _clock, task

NOON = _at(NY, 2026, 11, 6, 12, 0)
TZ = ZoneInfo(NY)


def test_three_hundred_mostly_unopened_answers_keep_home_small(service):
    """A fortnight of long requests and unavailable pictures must not flood a first stream view."""
    _clock(service, NOON)
    for index in range(300):
        settled = NOON - 60 - index * (14 * 86400 / 300)
        task(service.store, f"bulk_{index}", "completed", session=f"s_bulk_{index}",
             request=f"Compare garden plan {index}: " + "raised beds and drip irrigation " * 12,
             result={"spoken": "The revised layout uses less water.", "headline": "The revised layout uses less water.",
                     "cards": [{"kind": "image", "name": "Unavailable garden plan", "path": "/missing/garden.png"}]},
             settled=settled, created=settled - 30)
    task(service.store, "live", "working", session="s_live", request="Compare the latest watering schedule", created=NOON - 30)
    body = service.now({"tz": NY})
    assert len(body["items"]) <= 8
    entries = body["stream"]["entries"]
    assert len(entries) <= 2 * len(body["items"])
    assert len({entry["id"] for entry in entries}) == len(entries)
    assert all(len(entry.get("text", "")) <= S.ASK_CHARS for entry in entries)
    assert all(not item.get("thumbs") for item in body["items"])
    assert any(item["kind"] == "working" for item in body["items"])
    assert {e["item_id"] for e in entries if "item_id" in e} <= {item["id"] for item in body["items"]}


def _seen(store, key, settled, request="Summarize the inbox"):
    task(store, key, "completed", session="s_" + key, request=request, result={"spoken": "Done.", "headline": "Done."},
         settled=settled, created=settled - 30)
    store.mark_seen(key) if hasattr(store, "mark_seen") else None


def test_an_ask_comes_right_before_its_card_and_newer_things_come_last(service):
    _clock(service, NOON)
    task(service.store, "a", "completed", session="s_a", request="Find a standing desk", result={"headline": "Found 3"},
         settled=NOON - 600, created=NOON - 700)
    task(service.store, "b", "running", session="s_b", request="Draft the patio quote", created=NOON - 60)
    body = service.now({"tz": NY})
    ent = body["stream"]["entries"]
    kinds = [(e["kind"], e.get("text") or e["item_id"]) for e in ent]
    assert [k for k, _ in kinds] == ["ask", "card", "ask", "card"]
    assert kinds[0][1] == "Find a standing desk" and kinds[2][1] == "Draft the patio quote"
    assert [e["at"] for e in ent] == sorted(e["at"] for e in ent)
    ids = {i["id"] for i in body["items"]}
    assert {e["item_id"] for e in ent} <= ids


def test_a_card_keeps_one_id_while_its_state_changes(service):
    _clock(service, NOON)
    task(service.store, "a", "running", session="s_a", request="Find a standing desk", created=NOON - 60)
    first = [e for e in service.now({"tz": NY})["stream"]["entries"] if e["kind"] == "card"]
    service.store.transition("a", "completed", result={"headline": "Found 3"})
    after = [e for e in service.now({"tz": NY})["stream"]["entries"] if e["kind"] == "card"]
    assert [e["id"] for e in first] == [e["id"] for e in after]
    assert first[0]["state"] == "working" and after[0]["state"] in {"new_answer", "seen_answer"}


def test_a_question_is_an_open_widget_and_listed_for_the_strip(service, monkeypatch):
    from fakes import ModuleClock
    from speakeasy import service as service_module
    # Home and its shared question sweep must observe the same invented wall clock.
    # Replacing this module's clock leaves asyncio's process-wide clock untouched.
    monkeypatch.setattr(service_module, "time", ModuleClock(__import__("time").monotonic, wall=lambda: NOON))
    _clock(service, NOON)
    task(service.store, "q", "completed", session="s_q", request="Order a desk",
         result={"spoken": "Two fit.", "question": {"kind": "question", "question": "Which desk?",
                                                    "options": [{"text": "Oakline"}, {"text": "Birch"}]}},
         settled=NOON - 60, created=NOON - 120)
    body = service.now({"tz": NY})
    (card,) = [e for e in body["stream"]["entries"] if e["kind"] == "card"]
    assert card["widget"] == "open" and body["stream"]["needs_you"] == [card["item_id"]]


def test_older_seen_answers_fold_into_one_stretch_row_and_fresh_ones_stay():
    def item(n, hours_ago):
        return {"id": f"t:{n}", "kind": "seen_answer", "title": f"Job {n}", "started_at": NOON - hours_ago * 3600 - 30,
                "finished_at": NOON - hours_ago * 3600, "updated_at": NOON - hours_ago * 3600}
    items = [item(i, 3 + i * 0.1) for i in range(6)] + [item(9, 0.5)]
    # 3 h before noon is 9 AM: the morning stretch.
    out = S.build_stream(items, {}, set(), NOON, TZ)
    (group,) = [e for e in out["entries"] if e["kind"] == "group"]
    assert group["line"] == "This morning \u00b7 6 done" and len(group["item_ids"]) == 6
    assert [e["item_id"] for e in out["entries"] if e["kind"] == "card"] == ["t:9"]


def test_a_short_stretch_stays_as_cards_and_unread_or_working_never_fold():
    items = [{"id": "t:1", "kind": "seen_answer", "started_at": NOON - 20000, "finished_at": NOON - 19990},
             {"id": "t:2", "kind": "new_answer", "started_at": NOON - 20000, "finished_at": NOON - 19990},
             {"id": "t:3", "kind": "working", "started_at": NOON - 20000, "updated_at": NOON - 19990},
             {"id": "t:4", "kind": "seen_answer", "started_at": NOON - 20000, "finished_at": NOON - 19990}]
    out = S.build_stream(items, {}, set(), NOON, TZ)
    assert not [e for e in out["entries"] if e["kind"] == "group"] and len(out["entries"]) == 4


def test_widget_goes_open_then_confirmed_then_done():
    assert S.widget_state({"kind": "question"}, False) == "open"
    assert S.widget_state({"kind": "working"}, True) == "confirmed"
    assert S.widget_state({"kind": "seen_answer"}, True) == "done"
    assert S.widget_state({"kind": "working"}, False) is None


def test_earlier_days_name_the_day_from_the_calendar():
    yday = _at(NY, 2026, 11, 5, 15)
    assert S.stretch(yday, NOON, TZ)[0] == "Yesterday"
    assert S.stretch(_at(NY, 2026, 11, 6, 8), NOON, TZ)[0] == "This morning"


def test_recent_lists_previous_tasks_newest_first_even_after_they_left_home(service):
    _clock(service, NOON)
    task(service.store, "old", "completed", session="s_o", request="Plan the trip", result={"headline": "Planned"},
         settled=NOON - 5 * 86400, created=NOON - 5 * 86400 - 30)
    task(service.store, "new", "completed", session="s_n", request="Book the dentist", result={"headline": "Booked"},
         settled=NOON - 3600, created=NOON - 3700)
    assert all(i.get("run_id") != "old" for i in service.now({"tz": NY})["items"])
    rec = service.now_recent({"tz": NY})["items"]
    assert [i["at"] for i in rec] == sorted((i["at"] for i in rec), reverse=True) and len(rec) == 2
    assert {i["state"] for i in rec} == {"completed"}


def test_a_question_card_keeps_its_thread_id_when_it_becomes_a_working_card():
    asking = {"id": "q:run_1", "run_id": "run_1", "kind": "question", "started_at": NOON - 90, "updated_at": NOON - 60}
    going = {"id": "s:s_1", "run_id": "run_1", "kind": "working", "started_at": NOON - 90, "updated_at": NOON - 10}
    before = S.build_stream([asking], {"q:run_1": "Order a desk"}, set(), NOON, TZ)["entries"]
    after = S.build_stream([going], {"s:s_1": "Order a desk"}, {"s:s_1"}, NOON, TZ)["entries"]
    assert [e["id"] for e in before] == [e["id"] for e in after] == ["a:run_1", "c:run_1"]
    assert before[1]["widget"] == "open" and after[1]["widget"] == "confirmed"
    assert before[1]["at"] == after[1]["at"] == NOON - 90


def test_progress_and_completion_do_not_move_an_older_card_past_a_newer_ask():
    old = {"id": "s:old", "run_id": "old", "kind": "working", "started_at": NOON - 90,
           "updated_at": NOON - 1}
    newer = {"id": "s:new", "run_id": "new", "kind": "working", "started_at": NOON - 30}
    asks = {"s:old": "Compare standing desks", "s:new": "Draft a note"}
    first = S.build_stream([old, newer], asks, set(), NOON, TZ)["entries"]
    old.update(kind="new_answer", finished_at=NOON)
    after = S.build_stream([old, newer], asks, set(), NOON, TZ)["entries"]
    assert [e["id"] for e in first] == [e["id"] for e in after] == ["a:old", "c:old", "a:new", "c:new"]
