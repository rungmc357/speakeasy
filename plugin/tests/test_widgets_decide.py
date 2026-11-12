"""Widgets you can decide from (QA 74 lane D): approvable asks drawn as approvals, flight times
ready to print, untitled runs named from what they found, every card saying when its data is from,
and a finished answer that still asks something never reading as plain Done. Invented data only."""
from __future__ import annotations

import json

from fakes import wait_for
from speakeasy import now as N
from speakeasy import views as V
from test_history import NOW, add_msg, add_session, post, state  # noqa: F401
from test_now import _clock, task

GRAB = {"question": "Grab the remux?", "approvable": True, "recommended": 0,
        "context": "Cleanup is paused, so nothing gets deleted.", "why": "Best quality.",
        "options": [{"text": "Grab the big one", "detail": "80 GB, 1,000 seeders", "risk": "low", "reversible": True},
                    {"text": "Hold off", "detail": "Nothing gets grabbed", "risk": "low", "reversible": True}]}


def test_an_approvable_yes_no_ask_is_drawn_as_an_approval_with_its_details_kept():
    q = N.public_question(GRAB, "run_x", 100.0)
    assert q["shape"] == "approval" and q["approvable"] is True
    assert q["approval"] == {"choices": ["once", "deny"]}
    assert q["approve"] == {"option_id": "o1", "text": "Grab the big one"}
    assert q["options"][0]["detail"] == "80 GB, 1,000 seeders" and q["why"] == "Best quality." and q["context"]


def test_a_pick_or_a_risky_ask_stays_a_choice():
    for prompt in ("Which remix should we build first?", "Find the cause first, or build anyway?",
                   "How should it sign back in?"):
        assert N.public_question({**GRAB, "question": prompt}, "run_x", 1.0)["shape"] == "words", prompt
    assert N.public_question({**GRAB, "approvable": False}, "run_x", 1.0)["shape"] == "words"
    no_rec = {k: v for k, v in GRAB.items() if k != "recommended"}
    assert N.public_question(no_rec, "run_x", 1.0)["shape"] == "words"


def test_an_ask_with_a_second_way_to_say_yes_stays_a_choice():
    two = {**GRAB, "options": [GRAB["options"][0], {"text": "Grab the small one", "detail": "30 GB, faster"},
                               GRAB["options"][1]]}
    q = N.public_question(two, "run_x", 1.0)
    assert q["shape"] != "approval" and "approve" not in q and len(q["options"]) == 3
    for no in ("Not now", "Don't grab it", "Skip it", "No, leave it", "Keep waiting"):
        assert N.public_question({**GRAB, "options": [GRAB["options"][0], {"text": no}]}, "r", 1.0)["shape"] == "approval"


def _approvable_question(server, service, hermes, state):
    hermes.responder = lambda prompt, sid: "Found two.\n\n```speakeasy-question\n" + json.dumps(GRAB) + "\n```"
    _, started = post(server, "/voice/work", {"text": "Find the movie release"})
    key = service.store.key_for_run(started["run_id"])
    sid = wait_for(lambda: service.store.session_for(key))
    add_session(state, sid, "api_server", at=NOW - 5)
    add_msg(state, sid, "assistant", "Found two.", at=NOW - 5)
    wait_for(lambda: service.store.work(started["run_id"])["status"] == "completed")
    item = next(i for i in service.now({})["items"] if i["kind"] == "question")
    return key, item


def test_allow_on_an_approvable_ask_answers_with_the_recommended_option(server, service, hermes, state):
    key, item = _approvable_question(server, service, hermes, state)
    assert item["question"]["shape"] == "approval"
    sent = []
    hermes.responder = lambda prompt, sid: sent.append(prompt) or "Grabbing it."
    status, got = post(server, f"/voice/now/{item['id']}/approval", {"choice": "once"})
    assert status == 200 and got["choice"] == "once" and got["via"] == "conversation", got
    assert service.store.work(idem_key=key).get("answered") is True
    wait_for(lambda: sent)
    assert "Grab the big one" in sent[-1]


def test_dont_on_an_approvable_ask_says_no_in_its_conversation(server, service, hermes, state):
    key, item = _approvable_question(server, service, hermes, state)
    sent = []
    hermes.responder = lambda prompt, sid: sent.append(prompt) or "Okay, holding off."
    status, got = post(server, f"/voice/now/{item['id']}/approval", {"choice": "deny"})
    assert status == 200 and got["choice"] == "deny", got
    wait_for(lambda: sent)
    assert "don't" in sent[-1].lower()
    assert post(server, f"/voice/now/{item['id']}/approval", {"choice": "always"})[0] in (400, 409)


def test_a_flight_prints_local_times_its_day_and_a_status():
    v = V.flight_display({"kind": "flight", "number": "XX 100", "origin": "TPA", "destination": "LGA",
                          "departs": "2026-11-11T15:14:00-05:00", "arrives": "2026-11-11T17:54:00-05:00"})
    assert v["departs"] == "3:14 PM" and v["arrives"] == "5:54 PM" and v["date"] == "Wed, Nov 11"
    assert v["departs_at"] == "2026-11-11T15:14:00-05:00" and v["status"] == "Status unavailable"
    red_eye = V.flight_display({"kind": "flight", "departs": "2026-11-11T23:40:00-08:00",
                                "arrives": "2026-11-12T07:55:00-05:00", "status": "On time"})
    assert red_eye["arrives"] == "7:55 AM Thu" and red_eye["status"] == "On time"
    plain = {"kind": "flight", "departs": "3:14 PM", "status": "Delayed"}
    assert V.flight_display(plain) == {**plain}
    assert V.flight_display(V.flight_display(dict(red_eye))) == red_eye   # idempotent


def test_stored_views_say_when_their_data_is_from_and_flights_read_ready(service):
    flight = {"kind": "flight", "number": "XX 1", "origin": "TPA", "destination": "LGA",
              "departs": "2026-11-11T15:14:00-05:00", "arrives": "2026-11-11T17:54:00-05:00"}
    run = task(service.store, "f", "completed", session="s_f", result={"spoken": "Your flight.", "views": [flight]},
               settled=NOW - 60)
    views = service.store.work(run)["result"]["views"]
    assert views[0]["departs"] == "3:14 PM" and views[0]["as_of"] == NOW - 60


def test_an_untitled_run_is_named_from_its_headline_never_a_task(service):
    _clock(service, NOW)
    task(service.store, "u", "completed", session="s_u", request="",
         result={"spoken": "Done.", "headline": "Check-in open; one reply needed"}, settled=NOW - 30)
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE runs SET own_title=NULL, title=NULL WHERE idem_key='u'")
    titles = [i["title"] for i in service.now({})["items"]]
    assert "Check-in open; one reply needed" in titles and "A task" not in titles


def test_every_home_item_carries_its_data_time(service):
    _clock(service, NOW)
    task(service.store, "t", "completed", session="s_t", result={"spoken": "Done."}, settled=NOW - 90)
    item = service.now({})["items"][0]
    assert item["as_of"] == NOW - 90


def test_a_done_answer_whose_question_moved_on_still_carries_the_ask(service):
    _clock(service, NOW)
    q = {**GRAB, "question": "Want me to swap the two players?", "kind": "question"}
    task(service.store, "g", "completed", session="s_g", result={"spoken": "Swap?", "question": q, "views": [q]},
         settled=NOW - 30)
    service.store.close_question("g", "superseded")
    item = next(i for i in service.now({})["items"] if i.get("run_id") == "run_g")
    assert item["kind"] in (N.NEW_ANSWER, N.SEEN_ANSWER)
    assert item["asks"]["prompt"] == "Want me to swap the two players?" and item["asks"]["closed"] == "superseded"


def test_a_flight_never_prints_none_for_no_delay():
    out = V.flight_display({"kind": "flight", "departs": "3:18 PM (gate E67)", "status": "En route", "gate": "75",
                            "delay": "None"})
    assert "delay" not in out and out["gate"] == "75" and out["departs"] == "3:18 PM (gate E67)"
    assert V.flight_display({"kind": "flight", "delay": "25 min"})["delay"] == "25 min"
