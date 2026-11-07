"""Plugin 0.3.46: every id GET /voice/now hands out can be swiped away and undone, sent the way the
apps send it (percent-encoded: build 69's NowClient turns "s:abc" into "s%3Aabc").

Build 69 got "That didn't go through" on every swipe: the route wanted a literal ':' after the
prefix, so the encoded id never matched it, and "call:" / "g:stopped:" ids and answers folded into
"Earlier today" were never matched at all. Every fixture here is invented."""
from __future__ import annotations

import time
from urllib.parse import quote

from test_history import get, post, state  # noqa: F401
from test_now import NY, QUESTION, _clock, _delete, task
from test_now_call import _ended_call
from test_now_stopped_fold import NOON, _done, _fail


def _app(item_id: str) -> str:
    """NowClient.esc: alphanumerics plus -_.~ stay; everything else is %-encoded."""
    return quote(item_id, safe="-_.~")


def _ids(body) -> list[str]:
    ids = [i["id"] for i in body["items"]]
    ids += [i["id"] for i in ((body.get("earlier") or {}).get("today") or {}).get("items") or []]
    if body.get("call_summary"):
        ids.append(body["call_summary"]["id"])
    return ids


def _round_trip(server, ids: list[str]) -> None:
    for item_id in ids:
        for path in (f"/voice/now/{_app(item_id)}/dismiss", f"/voice/now/{item_id}/dismiss"):
            status, body = post(server, path, {})
            assert status == 200 and body == {"ok": True, "id": item_id, "dismissed": True}, (path, status, body)
            assert item_id not in _ids(get(server, "/voice/now?tz=" + quote(NY, safe=""))[1]), path
            status, body = _delete(server, path)
            assert status == 200 and body["dismissed"] is False, (path, status, body)
            assert item_id in _ids(get(server, "/voice/now?tz=" + quote(NY, safe=""))[1]), f"undo brings {path} back"


def test_every_card_id_on_a_busy_home_dismisses_and_undoes(server, service, state):
    _clock(service, NOON)
    store = service.store
    _done(store, "a1", session="speakeasy_task_4d168a86d0b4f1e790674b26", request="List ramen spots in Kyoto")
    _done(store, "a2", session="20261107_015217_11136419", request="Pack list for the cabin", settled=NOON - 90)
    _done(store, "a3", session="s+odd@name=1", request="Odd session id", settled=NOON - 120)
    # One conversation, three answers: one card, folded:2.
    for n in range(3):
        _done(store, f"r{n}", session="s_repeat", request=f"Lamp {n}", settled=NOON - 600 + n,
              interaction="vi_lamps")
    # Three stopped with one cause: one g:stopped: card.
    for n in range(3):
        _fail(store, f"f{n}", "hermes_unreachable", session=f"s_f{n}", request=f"Sync job {n}", settled=NOON - 300 + n)
    task(store, "q1", "completed", session="s_q", request="Order a desk", result={"spoken": "?", "question": QUESTION},
         settled=NOON - 50)
    task(store, "w1", "running", session="s_w", request="Still going")
    # Answers more than 4 h old: the "Earlier today" row.
    _done(store, "e1", session="s_e1", request="Five hours ago", settled=NOON - 5 * 3600)
    _done(store, "e2", session="s_e2", request="Six hours ago", settled=NOON - 6 * 3600)
    body = get(server, "/voice/now?tz=" + quote(NY, safe=""))[1]
    ids = _ids(body)
    prefixes = {i.split(":", 1)[0] for i in ids}
    assert {"s", "q", "g"} <= prefixes, ids
    assert any(i.get("folded") for i in body["items"]), "a folded card is in the payload"
    assert body["earlier"]["today"]["count"] == 2, "earlier-today ids are in the payload"
    _round_trip(server, ids)


def test_the_call_card_and_the_answers_under_it_dismiss_and_undo(server, service, state):
    store, now = service.store, time.time()
    call = _ended_call(service, store, now)
    with call.lock:
        call.call_closed, call.ended_at = True, now - 30
    body = get(server, "/voice/now")[1]
    ids = _ids(body)
    assert any(i.startswith("call:") for i in ids), ids
    assert any(i.get("call_id") for i in body["items"]), "a call-child card is in the payload"
    _round_trip(server, ids)


def test_ids_that_are_not_home_ids_are_a_plain_404(server, service, state):
    _clock(service, NOON)
    for path in ("/voice/now/s%3Anope/dismiss", "/voice/now/x%3Aabc/dismiss", "/voice/now/%00/dismiss",
                 "/voice/now/s:nope/dismiss"):
        status, body = post(server, path, {})
        assert status == 404, (path, status, body)
    assert post(server, "/voice/now/s%3Anope/dismiss", {})[1].get("error") == "That card isn't on Home."
