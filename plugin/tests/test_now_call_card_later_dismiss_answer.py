"""SPK-221: the 0.3.46 hotfix let Home's item routes take the call card's id, which left three gaps in
the shared lookup.

1. Later on the call card said it worked and the card stayed: Home applied Later to its ordinary items
   only, and built the call card separately, checking dismissal alone.
2. A dismissed call card never came back: the come-back signature hashed kind/finished_at/run_id/
   question prompt, and a call card has none of them (the same four nulls forever), so an answer that
   landed a few seconds after hang-up left it hidden.
3. Sending an answer to a call card's id was a 500 (no ``kind`` key): the route indexed it before
   checking the card was a question.

Each test fails on 0.3.46 and passes with the fix. Every fixture is invented.
"""
from __future__ import annotations

import time
from urllib.parse import quote

from test_history import add_session, get, post, state  # noqa: F401
from test_now import _delete, task
from test_now_call import live_call, on_call


def _app(item_id: str) -> str:
    """NowClient.esc: alphanumerics plus -_.~ stay; everything else is %-encoded."""
    return quote(item_id, safe="-_.~")


def _recap(server):
    return get(server, "/voice/now")[1]["call_summary"]


def _held_call(service, store, now):
    """A call that just hung up: one answer was heard, and one task from the call is still landing
    (the worker keeps waiting on outstanding dispatches after hang-up, and the recap reads the
    answer that comes back)."""
    call = live_call(service, interaction_id="vi_paint", started=now - 120)
    add_session(service.rt.state_db, "s_paint", "api_server", title="Hall paint", at=now - 60)
    task(store, "h1", "completed", session="s_paint", request="Which paint for the hall",
         interaction="vi_paint", result={"spoken": "Oakline flat.", "headline": "Oakline flat goes on best."},
         settled=now - 90)
    task(store, "h2", "running", session="s_paint", request="Check the paint store hours",
         interaction="vi_paint")
    on_call(call, "d_h1", "h1")
    on_call(call, "d_h2", "h2", "running")
    with call.lock:
        call.spoken_tasks.update({"d_h1": now - 85})
        call.call_closed, call.ended_at = True, now - 2
    store.mark_seen("h1")
    return call


# -- 1. Later on the call card ----------------------------------------------------------------

def test_the_call_card_respects_later_raw_and_encoded_and_undo(server, service, state):
    now = time.time()
    call = _held_call(service, service.store, now)
    card = _recap(server)
    assert card and card["id"] == "call:vi_paint"
    item_id = card["id"]
    # Set through the encoded id the apps send, undo through the raw one, then the other way round:
    # the card is off Home while it's put off, and back the moment it's undone.
    for set_id, undone_id in ((_app(item_id), item_id), (item_id, _app(item_id))):
        status, body = post(server, f"/voice/now/{set_id}/later", {"until": time.time() + 3600})
        assert status == 200 and body["later"]["until"], (set_id, status, body)
        assert _recap(server) is None, f"put off through {set_id}"
        status, body = _delete(server, f"/voice/now/{undone_id}/later")
        assert status == 200, (undone_id, status, body)
        back = _recap(server)
        assert back and back["id"] == item_id, f"undo through {undone_id}"
    # Due: it comes back on its own once its time arrives.
    until = time.time() + 2
    assert post(server, f"/voice/now/{_app(item_id)}/later", {"until": until})[0] == 200
    assert _recap(server) is None
    time.sleep(max(0.0, until - time.time()) + 0.3)
    woke = _recap(server)
    assert woke and woke["id"] == item_id
    assert (woke.get("provenance") or {}).get("kind") == "later"


def test_the_call_card_put_off_comes_back_when_a_late_answer_lands(server, service, state):
    now = time.time()
    _held_call(service, service.store, now)
    item_id = _recap(server)["id"]
    assert post(server, f"/voice/now/{_app(item_id)}/later", {"on_change": True})[0] == 200
    assert _recap(server) is None
    service.store.transition("h2", "completed",
                             result={"spoken": "The paint store closes at 7.",
                                     "headline": "Paint store closes at 7 PM."})
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE runs SET settled_at=? WHERE idem_key=?", (time.time(), "h2"))
    card = _recap(server)
    assert card and card["id"] == item_id, "a changed recap comes back"
    assert any("7 PM" in a for a in card["answers"]), card["answers"]
    assert (card.get("provenance") or {}).get("kind") == "later"


# -- 2. Dismiss and come back ------------------------------------------------------------------

def test_a_dismissed_call_card_comes_back_only_when_its_content_changes(server, service, state):
    now = time.time()
    _held_call(service, service.store, now)
    item_id = _recap(server)["id"]
    for spelling in (_app(item_id), item_id):
        assert post(server, f"/voice/now/{spelling}/dismiss", {})[0] == 200
        assert _recap(server) is None, spelling
        # Unchanged content stays hidden across rebuilds.
        assert _recap(server) is None
        assert _delete(server, f"/voice/now/{spelling}/dismiss")[0] == 200
        assert _recap(server)["id"] == item_id
    assert post(server, f"/voice/now/{_app(item_id)}/dismiss", {})[0] == 200
    assert _recap(server) is None
    # The answer from the call's outstanding task lands inside the window the recap reads: same card,
    # new content, so it comes back (the documented return-on-new-answer contract).
    service.store.transition("h2", "completed",
                             result={"spoken": "The paint store closes at 7.",
                                     "headline": "Paint store closes at 7 PM."})
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE runs SET settled_at=? WHERE idem_key=?", (time.time(), "h2"))
    card = _recap(server)
    assert card and card["id"] == item_id
    assert any("7 PM" in a for a in card["answers"]), card["answers"]


# -- 3. Answering a call card's id ---------------------------------------------------------------

def test_answering_a_call_card_id_is_a_clean_404_with_no_side_effects(server, service, state):
    now = time.time()
    call = _held_call(service, service.store, now)
    item_id = _recap(server)["id"]
    runs_before = dict(call.runs)
    for spelling in (_app(item_id), item_id):
        status, body = post(server, f"/voice/now/{spelling}/answer", {"text": "Oakline flat"})
        assert status == 404, (spelling, status, body)
        assert body.get("error") == "That question has closed.", (spelling, body)
    assert call.runs == runs_before, "no task was submitted"
    assert service.store.question_answer("h1") is None, "no answer was recorded"
    assert _recap(server)["id"] == item_id, "the card is untouched"


def test_a_real_question_card_still_answers(server, service, state):
    now = time.time()
    _held_call(service, service.store, now)
    task(service.store, "q1", "completed", session="s_paint", request="Which paint for the hall door",
         result={"spoken": "Two fit.", "question": {
             "kind": "question", "question": "Which paint for the door?",
             "options": [{"text": "Oakline flat", "link": "https://paint.example/oakline"},
                         {"text": "Birch eggshell"}], "recommended": 0}},
         settled=now - 20)
    card = next(i for i in get(server, "/voice/now")[1]["items"] if i["kind"] == "question")
    # The route still accepts a real question card (a bad option is a 400, never the closed 404).
    status, body = post(server, f"/voice/now/{_app(card['id'])}/answer", {"option_id": "o9"})
    assert status == 400, body
    assert body.get("error") != "That question has closed."
