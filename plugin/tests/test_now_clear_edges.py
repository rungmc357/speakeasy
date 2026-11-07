"""Home's Clear (SPK-212): it clears an active call's answers too, and it never touches stopped work.
Invented data only."""
from __future__ import annotations

import re
import time

from test_history import get, state  # noqa: F401
from test_now import _delete, post, task
from test_now_call import live_call, on_call


def _ids(body):
    return sorted(i["id"] for i in body["items"])


def test_clear_moves_a_live_calls_answer_off_home_and_undo_brings_it_back(server, service, state):
    store, now = service.store, time.time()
    call = live_call(service, started=now - 120)
    task(store, "k_a", "completed", session="s_a", request="Compare tomato seeds", interaction="vi_call",
         result={"spoken": "Brandywine.", "headline": "Brandywine wins."}, settled=now - 20)
    on_call(call, "d_k_a", "k_a")
    assert _ids(get(server, "/voice/now")[1]) == ["s:s_a"]
    # Without Clear, a heard answer stays under the call card (intentional).
    store.mark_seen("k_a")
    assert _ids(get(server, "/voice/now")[1]) == ["s:s_a"]
    status, moved = post(server, "/voice/now/clear-seen", {})
    assert status == 200 and moved["moved"] == 1
    body = get(server, "/voice/now")[1]
    assert body["items"] == [], "the call is still on, yet Clear moved its answer"
    assert get(server, "/voice/now")[1]["items"] == [], "a refresh must not bring it back"
    assert _delete(server, "/voice/now/clear-seen")[0] == 200
    assert _ids(get(server, "/voice/now")[1]) == ["s:s_a"], "Undo restores it"


def test_clear_leaves_unread_stopped_work_in_another_conversation(server, service, state):
    store, now = service.store, time.time()
    task(store, "k_ok", "completed", session="s_ok", request="Check ferry times",
         result={"spoken": "x", "headline": "8:15 from Pier 11."}, settled=now - 30)
    task(store, "k_bad", "failed", session="s_bad", request="Move the photo archive", settled=now - 40)
    assert _ids(get(server, "/voice/now")[1]) == ["s:s_bad", "s:s_ok"]
    assert post(server, "/voice/now/clear-seen", {})[1]["moved"] == 1
    assert _ids(get(server, "/voice/now")[1]) == ["s:s_bad"], "Clear is answers-only"
    assert _ids(get(server, "/voice/now")[1]) == ["s:s_bad"], "and stays so on refresh"
    assert _delete(server, "/voice/now/clear-seen")[0] == 200
    assert _ids(get(server, "/voice/now")[1]) == ["s:s_bad", "s:s_ok"]


def test_the_dismiss_path_a_client_builds_reaches_the_routes(server, service, state):
    now = time.time()
    task(service.store, "k_a", "completed", session="s_a", request="Desk hunt", result={"spoken": "x"},
         settled=now - 20)
    # NowItemPath.segment("s:s_a") == "s:s_a": prefix colon literal, rest percent-encoded.
    assert post(server, "/voice/now/s:s_a/dismiss", {})[0] == 200
    assert _delete(server, "/voice/now/s:s_a/dismiss")[0] == 200
    # What the old client sent (an encoded colon) never matched a route.
    assert post(server, "/voice/now/s%3As_a/dismiss", {})[0] != 200
