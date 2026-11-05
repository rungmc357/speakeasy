"""History pages as one source of truth (deep pass): answered questions close everywhere, a reply on
a session page always continues that session (never a new row), no Speakeasy plumbing in what the
apps show, rename/pin/archive/read from the panel, the live history feed, and fast cached reads.
Invented sessions in an invented Hermes state DB; nothing is ever routed."""
import json
import sqlite3
import threading
import time
import urllib.request

import pytest

from fakes import wait_for
from speakeasy import history as H
from test_history import NOW, add_msg, add_session, get, make_db, never_routed, post, state  # noqa: F401

QUESTION = {"question": "Ship the shelf as version two?", "options": ["Ship it now", "Hold for next batch"],
            "recommended": 0}


def asks(prompt, session_id):
    return "Built and tested.\n\n```speakeasy-question\n" + json.dumps(QUESTION) + "\n```"


def plain(prompt, session_id):
    return "Done: " + prompt[-40:]


@pytest.fixture
def pinned_col(state):
    db = sqlite3.connect(state)
    db.execute("ALTER TABLE sessions ADD COLUMN pinned INTEGER DEFAULT 0")
    db.commit()
    db.close()
    return state


def _row(server, sid, query=""):
    _, listing = get(server, "/voice/history" + query)
    return next((r for r in listing["sessions"] if r["id"] == sid), None), listing


# -- (B)(C)(D) what the apps show: never plumbing --------------------------------------------------

def test_assistant_text_drops_question_json_status_lines_and_the_voice_echo():
    raw = ('Voice: or using the menu bar (Geo is looking at: Find previous sessions)\n\nThe clock button opens it.'
           '\n\n```speakeasy-question\n{"question": "Ship it?", "options": ["Yes", "No"]}\n```')
    assert H.assistant_text(raw) == "The clock button opens it."
    assert H.assistant_text("STATUS: Checking the tests\nDETAIL: Two still fail.") == "Checking the tests — Two still fail"
    assert "speakeasy-views" not in H.assistant_text('Done.\n```speakeasy-views\n[{"kind":"fact"}]\n```')


def test_relayed_requests_show_only_the_persons_words():
    raw = ('[rungmc] [Voice request from Geo, relayed by Speakeasy] or using the menu bar\n\n'
           '(Geo is looking at: Find previous sessions)\n\nBegin your reply with exactly one line: "Voice: x"')
    assert H.user_words(raw) == ("user", "or using the menu bar")


def test_previews_are_plain_and_titles_tidy():
    assert H.preview_text("**Where:** the [clock](https://x.example/a) button") == "Where: the clock button"
    assert H.tidy_title("telegram general") == "Telegram general"
    assert H.tidy_title("Fix iOS build") == "Fix iOS build"


def test_the_list_and_page_never_show_plumbing(server, state):
    add_session(state, "d9", "discord", title="find previous sessions", at=NOW - 10)
    add_msg(state, "d9", "user", "[rungmc] [Voice request from Geo, relayed by Speakeasy] where are they\n\n"
            "(Geo is looking at: History)\n\nBegin your reply with exactly one line: \"Voice: where are they\"", at=NOW - 12)
    add_msg(state, "d9", "assistant", "Voice: where are they (Geo is looking at: History)\n\n**Here:** the clock icon."
            "\n\n```speakeasy-question\n" + json.dumps(QUESTION) + "\n```", at=NOW - 10)
    row, _ = _row(server, "d9")
    assert row["title"] == "Find previous sessions"
    assert row["preview"] == "Here: the clock icon."
    _, page = get(server, "/voice/history/d9")
    texts = [e.get("text") for e in page["session"]["entries"]]
    assert "where are they" in texts
    assert not any("speakeasy-question" in (t or "") or "Voice:" in (t or "") or "looking at" in (t or "") for t in texts)


# -- (A) one truth for which question still waits ----------------------------------------------------

def test_an_answered_question_closes_everywhere_and_shows_once(server, service, hermes, state):
    hermes.responder = asks
    _, started = post(server, "/voice/work", {"text": "Build the shelf page"})
    run_id = started["run_id"]
    key = service.store.key_for_run(run_id)
    sid = wait_for(lambda: service.store.session_for(key))
    add_session(state, sid, "api_server", at=NOW - 5)
    add_msg(state, sid, "assistant", "Built and tested.", at=NOW - 5)
    wait_for(lambda: service.store.work(run_id)["status"] == "completed")
    row, _ = _row(server, sid)
    assert row["state"] == "needs_you"
    _, page = get(server, f"/voice/history/{sid}")
    assert [j.get("question_open") for j in page["session"]["jobs"]] == [True]
    # The reply on the page answers it: no Needs you, no open card, on any device.
    hermes.responder = plain
    status, body = post(server, f"/voice/history/{sid}/message", {"text": "Ship it now"})
    assert status == 200 and body["via"] == "task", body
    wait_for(lambda: all(w["status"] == "completed" for w in [service.store.work(body["run_id"]) or {"status": ""}]))
    row, _ = _row(server, sid)
    assert row["state"] != "needs_you"
    _, page = get(server, f"/voice/history/{sid}")
    assert not any(j.get("question_open") for j in page["session"]["jobs"])
    assert service.store.work(idem_key=key).get("answered") is True


def test_the_same_question_from_two_tasks_is_one_decision(service):
    store = service.store
    for key, rowid_delay in (("k1", 0), ("k2", 0.01)):
        store.reserve_run(key, "vi_x", "d_" + key, 0)
        store.set_session(key, "s_same")
        store.update_run(key, "run_" + key, "completed")
        store.set_result(key, {"spoken": "Built.", "question": {"kind": "question", **QUESTION}}) \
            if hasattr(store, "set_result") else store._db.execute(
                "UPDATE runs SET result_json=? WHERE idem_key=?",
                (json.dumps({"spoken": "Built.", "question": {"kind": "question", **QUESTION}}), key))
        time.sleep(rowid_delay)
    assert service._open_question(["k1", "k2"]) == "k2"
    assert store.mark_answered("k2")
    assert service._open_question(["k1", "k2"]) is None
    assert store.work(idem_key="k1").get("answered") is True, "the duplicate closes with it"


# -- (H) a reply always continues in place ------------------------------------------------------------

def test_new_chat_reply_names_its_session_at_once(server, service, state):
    status, started = post(server, "/voice/work", {"text": "Plan the tomato beds"})
    assert status == 202, started
    assert started["session_id"] and started["session_id"] == service.store.session_for(
        service.store.key_for_run(started["run_id"]))


def test_linked_chat_replies_continue_one_session_and_fold_into_the_chat_row(server, service, hermes, state):
    status, first = post(server, "/voice/history/d1/message", {"text": "Order them please"})
    assert status == 200 and first["via"] == "linked"
    key = service.store.key_for_run(first["run_id"])
    linked = wait_for(lambda: service.store.session_for(key))
    add_session(state, linked, "api_server", at=NOW - 2)
    add_msg(state, linked, "assistant", "Ordered three brackets.", at=NOW - 2)
    wait_for(lambda: service.store.work(first["run_id"])["status"] == "completed")
    for n, words in enumerate(("And the screws", "And wall plugs")):
        before = len(hermes.calls)
        status, again = post(server, "/voice/history/d1/message", {"text": words})
        assert status == 200 and again["via"] == "linked", again
        wait_for(lambda: len(hermes.calls) > before)
        assert hermes.calls[-1]["session_id"] == linked, f"reply {n + 2} continued the same session"
        wait_for(lambda: (service.store.work(again["run_id"]) or {}).get("status") == "completed")
        add_msg(state, linked, "assistant", f"Added {words[4:]}.", at=time.time())
    _, listing = get(server, "/voice/history")
    ids = [r["id"] for r in listing["sessions"]]
    assert linked not in ids, "the linked task's session never shows as its own row"
    assert ids[0] == "d1", "the chat moved to the top"
    _, page = get(server, "/voice/history/d1")
    texts = [e.get("text") for e in page["session"]["entries"]]
    assert "Added the screws." in texts and "Added wall plugs." in texts


def test_a_linked_chat_whose_task_was_cleared_still_continues_its_session(server, service, hermes, state):
    _, first = post(server, "/voice/history/t1/message", {"text": "Which can go outside?"})
    key = service.store.key_for_run(first["run_id"])
    linked = wait_for(lambda: service.store.session_for(key))
    wait_for(lambda: service.store.work(first["run_id"])["status"] == "completed")
    service.store._db.execute("UPDATE runs SET status='cancelled' WHERE idem_key=?", (key,))
    before = len(hermes.calls)
    status, again = post(server, "/voice/history/t1/message", {"text": "And the basil?"})
    assert status == 200 and again["via"] == "linked", again
    wait_for(lambda: len(hermes.calls) > before)
    assert hermes.calls[-1]["session_id"] == linked
    assert service.store.history_links("t1")[0] == key


def test_a_finished_task_session_follow_up_stays_in_place(server, service, hermes, state):
    _, started = post(server, "/voice/work", {"text": "Draft the flyer"})
    sid = started["session_id"]
    add_session(state, sid, "api_server", at=NOW - 5)
    add_msg(state, sid, "assistant", "Flyer drafted.", at=NOW - 5)
    wait_for(lambda: service.store.work(started["run_id"])["status"] == "completed")
    _, before_list = get(server, "/voice/history")
    for words in ("Add the date", "Make it blue"):
        n = len(hermes.calls)
        status, body = post(server, f"/voice/history/{sid}/message", {"text": words})
        assert status == 200 and body["via"] == "task"
        wait_for(lambda: len(hermes.calls) > n)
        assert hermes.calls[-1]["session_id"] == sid
        wait_for(lambda: (service.store.work(body["run_id"]) or {}).get("status") == "completed")
    _, after_list = get(server, "/voice/history")
    assert len(after_list["sessions"]) == len(before_list["sessions"]), "no new row"


# -- (I)(J) rename, pin, archive, read; the live feed ----------------------------------------------

def test_rename_and_archive_go_through_hermes_and_pin_read_are_shared(server, service, hermes, pinned_col):
    hermes.state_db = pinned_col
    status, body = post(server, "/voice/history/d1/rename", {"title": "Walnut shelf"})
    assert status == 200, body
    assert hermes.session_patches[-1] == ("d1", {"title": "Walnut shelf"})
    row, _ = _row(server, "d1")
    assert row["title"] == "Walnut shelf"
    assert post(server, "/voice/history/t1/pin", {"pinned": True})[0] == 200
    _, listing = get(server, "/voice/history")
    assert listing["sessions"][0]["id"] == "t1" and listing["sessions"][0]["pinned"] is True
    assert post(server, "/voice/history/c1/archive", {"archived": True})[0] == 200
    assert "c1" not in [r["id"] for r in get(server, "/voice/history")[1]["sessions"]]
    archived = get(server, "/voice/history?archived=1")[1]["sessions"]
    assert [r["id"] for r in archived] == ["c1"] and archived[0]["archived"] is True
    assert post(server, "/voice/history/c1/archive", {"archived": False})[0] == 200
    assert "c1" in [r["id"] for r in get(server, "/voice/history")[1]["sessions"]]
    # Unread until read on any device.
    service.store.set_read("d1", NOW - 10000)
    assert _row(server, "d1")[0].get("unread") is True
    assert post(server, "/voice/history/d1/read", {})[0] == 200
    assert not _row(server, "d1")[0].get("unread")
    assert post(server, "/voice/history/d1/rename", {"title": " "})[0] == 400
    assert post(server, "/voice/history/d1/pin", {"pinned": "yes"})[0] == 400
    assert post(server, "/voice/history/nope/archive", {})[0] == 404


def test_the_live_feed_carries_changes_within_two_seconds(server, service, hermes, pinned_col):
    hermes.state_db = pinned_col
    req = urllib.request.Request(server.base_url + "/voice/history/events",
                                 headers={"Authorization": f"Bearer {server.token}"})
    got: list[str] = []
    ready = threading.Event()

    def listen():
        with urllib.request.urlopen(req, timeout=10) as resp:
            for raw in resp:
                line = raw.decode().strip()
                if line.startswith("event:"):
                    got.append(line.split(":", 1)[1].strip())
                    ready.set()
                if len(got) >= 3:
                    return
    threading.Thread(target=listen, daemon=True).start()
    assert ready.wait(5) and got[0] == "reset"
    t0 = time.monotonic()
    post(server, "/voice/history/d1/pin", {"pinned": True})
    post(server, "/voice/history/c1/archive", {"archived": True})
    wait_for(lambda: len(got) >= 3, timeout=3)
    assert got[1:3] == ["session_updated", "session_archived"] and time.monotonic() - t0 < 2


def test_history_events_watcher_sees_state_db_changes(service, state):
    service.history_events.POLL_S = 0.05
    service.history_events.opened()
    try:
        seq = service.history_events.seq
        add_msg(state, "d1", "assistant", "A reply from Discord", at=time.time())
        wait_for(lambda: any(k == "history_changed" for _, k, _ in service.history_events.since(seq) or []), timeout=3)
    finally:
        service.history_events.closed()


# -- (G) fast reads: cached while nothing changed ---------------------------------------------------

def test_history_reads_are_cached_until_something_changes(server, service, state, monkeypatch):
    calls = {"n": 0}
    real = H.session_messages

    def counted(*a, **k):
        calls["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(H, "session_messages", counted)
    get(server, "/voice/history/d1")
    get(server, "/voice/history/d1")
    assert calls["n"] == 1, "the second read came from the cache"
    add_msg(state, "d1", "assistant", "New words", at=time.time())
    _, page = get(server, "/voice/history/d1")
    assert calls["n"] == 2 and "New words" in [e.get("text") for e in page["session"]["entries"]]


def test_a_client_that_hangs_up_is_logged_once_without_a_traceback(server, state, caplog):
    import socket
    s = socket.create_connection(("127.0.0.1", int(server.base_url.rsplit(":", 1)[1])))
    s.sendall(f"GET /voice/history HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {server.token}\r\n\r\n".encode())
    s.close()
    time.sleep(0.5)
    assert not [r for r in caplog.records if r.levelname == "ERROR" and "history" in r.getMessage()]


def test_search_finds_archived_threads_marked_and_the_list_still_hides_them(server, hermes, pinned_col):
    hermes.state_db = pinned_col
    assert post(server, "/voice/history/d1/archive", {"archived": True})[0] == 200
    assert "d1" not in [r["id"] for r in get(server, "/voice/history")[1]["sessions"]]
    found = get(server, "/voice/history?q=walnut")[1]["sessions"]
    row = next(r for r in found if r["id"] == "d1")
    assert row["archived"] is True
    assert "d1" not in [r["id"] for r in get(server, "/voice/history?q=walnut&include_archived=0")[1]["sessions"]]
    assert [r["id"] for r in get(server, "/voice/history?archived=1")[1]["sessions"]] == ["d1"]
    assert get(server, "/voice/history/d1")[1]["session"]["archived"] is True   # the page offers Unarchive
