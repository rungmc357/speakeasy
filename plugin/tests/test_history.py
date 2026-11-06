"""History (docs/API.md "History"): every Hermes session listed, read and written to by id from the
app, with no call. Invented sessions in an invented Hermes state DB; nothing is ever routed."""
import json
import sqlite3
import time

import pytest

from fakes import http, wait_for
from speakeasy import history as H

NOW = time.time()


def make_db(path, *, fts=True):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, display_name TEXT,
                  chat_type TEXT, thread_id TEXT, origin_json TEXT, parent_session_id TEXT, started_at REAL NOT NULL,
                  ended_at REAL, message_count INTEGER DEFAULT 0, last_activity_at REAL, hidden INTEGER DEFAULT 0,
                  archived INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
                  content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL,
                  active INTEGER NOT NULL DEFAULT 1, display_kind TEXT)""")
    if fts:
        db.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content, tool_name, tool_calls)")
    db.commit()
    db.close()


def add_session(path, sid, source, *, title=None, display=None, chat_type=None, parent=None, at=None, **flags):
    db = sqlite3.connect(path)
    at = NOW - 3600 if at is None else at
    db.execute("INSERT INTO sessions (id, source, title, display_name, chat_type, parent_session_id, started_at, "
               "message_count, last_activity_at, hidden, archived) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
               (sid, source, title, display, chat_type, parent, at - 60, 1, at, flags.get("hidden", 0),
                flags.get("archived", 0)))
    db.commit()
    db.close()


def add_msg(path, sid, role, content="", *, calls=None, call_id=None, tool=None, at=None):
    db = sqlite3.connect(path)
    cur = db.execute("INSERT INTO messages (session_id, role, content, tool_call_id, tool_calls, tool_name, timestamp) "
                     "VALUES (?,?,?,?,?,?,?)", (sid, role, content, call_id, json.dumps(calls) if calls else None,
                                                tool, NOW - 3000 if at is None else at))
    try:
        db.execute("INSERT INTO messages_fts (rowid, content, tool_name, tool_calls) VALUES (?,?,?,?)",
                   (cur.lastrowid, content, tool or "", ""))
    except sqlite3.Error:
        pass
    db.commit()
    db.close()


def call(cid, name, args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.fixture
def state(service):
    path = service.rt.state_db
    make_db(path)
    # A Discord thread, a Telegram chat, a terminal session, a cron run, a helper, a hidden one.
    add_session(path, "d1", "discord", title="Shelf brackets", display="Workshop / #build / Shelf brackets",
                chat_type="thread", at=NOW - 100)
    add_msg(path, "d1", "user", "Can you size the walnut shelf brackets?", at=NOW - 200)
    add_msg(path, "d1", "assistant", "Three brackets at 12 in spacing will hold it.", at=NOW - 100)
    add_session(path, "t1", "telegram", title=None, display="Sam", chat_type="dm", at=NOW - 7200)
    add_msg(path, "t1", "user", "Remind me which seeds to start in March", at=NOW - 7300)
    add_session(path, "c1", "cli", title="Fix the lantern build", at=NOW - 90000)
    add_msg(path, "c1", "user", "the lantern build fails on the marigold step", at=NOW - 90100)
    add_session(path, "cron1", "cron", title="Morning digest", at=NOW - 50)
    add_msg(path, "cron1", "assistant", "Digest ready", at=NOW - 50)
    add_session(path, "sub1", "subagent", title="helper", at=NOW - 40)
    add_session(path, "h1", "discord", title="Hidden one", at=NOW - 30, hidden=1)
    # A compression chain: only the newest segment is listed.
    add_session(path, "c2root", "cli", title="Garden plan", at=NOW - 5000)
    add_msg(path, "c2root", "user", "Plan the tomato beds", at=NOW - 5100)
    add_session(path, "c2tip", "cli", title="Garden plan", parent="c2root", at=NOW - 4000)
    add_msg(path, "c2tip", "assistant", "Beds planned along the fence.", at=NOW - 4000)
    return path


def get(server, path):
    return http(server.base_url, "GET", path, token=server.token)


def post(server, path, body):
    return http(server.base_url, "POST", path, body, server.token)


@pytest.fixture(autouse=True)
def never_routed(monkeypatch):
    """Nothing sent from a history page may reach the call router or continuity matching."""
    from speakeasy import continuity, router

    def boom(*a, **k):
        raise AssertionError("a history message went through routing")
    monkeypatch.setattr(router, "aux_call", boom)
    monkeypatch.setattr(continuity, "conversations_with_context", boom)


# -- the list -------------------------------------------------------------------------------------

def test_list_newest_first_with_labels_and_no_internal_sessions(server, state):
    status, body = get(server, "/voice/history")
    assert status == 200, body
    ids = [s["id"] for s in body["sessions"]]
    assert ids == ["d1", "c2tip", "t1", "c1"]          # no cron, helper, hidden, or chain root
    d1 = body["sessions"][0]
    assert d1["source_label"] == "Discord · #build" and d1["title"] == "Shelf brackets"
    assert d1["preview"].startswith("Three brackets") and d1["state"] == "idle"
    t1 = body["sessions"][2]
    assert t1["source_label"] == "Telegram" and t1["title"].startswith("Remind me which seeds")
    assert {f["id"] for f in body["filters"]} >= {"speakeasy", "discord", "scheduled"}


def test_sources_lists_only_what_the_person_has_with_counts(server, state):
    """The filter chips: only sources with sessions, counted over everything visible (every page,
    any search or filter), so Slack never shows for someone with no Slack sessions."""
    add_session(state, "speakeasy_task_x1", "api_server", title="Bake sale flyer", at=NOW - 20)
    add_msg(state, "speakeasy_task_x1", "user", "Draft the flyer", at=NOW - 20)
    add_session(state, "empty1", "slack", title="Never used", at=NOW - 10)   # no messages: not listed
    db = sqlite3.connect(state)
    db.execute("UPDATE sessions SET message_count=0 WHERE id='empty1'")
    db.commit(); db.close()
    want = [{"source": "speakeasy", "count": 1}, {"source": "discord", "count": 1},
            {"source": "telegram", "count": 1}, {"source": "terminal", "count": 2},
            {"source": "scheduled", "count": 1}]
    for path in ("/voice/history", "/voice/history?limit=1", "/voice/history?source=discord",
                 "/voice/history?q=marigold", "/voice/history?q=zzznothing"):
        status, body = get(server, path)
        assert status == 200 and body["sources"] == want, (path, body.get("sources"))


def test_list_pages_with_a_cursor(server, state):
    _, first = get(server, "/voice/history?limit=2")
    assert [s["id"] for s in first["sessions"]] == ["d1", "c2tip"] and first["next_cursor"]
    _, second = get(server, f"/voice/history?limit=2&cursor={first['next_cursor']}")
    assert [s["id"] for s in second["sessions"]] == ["t1", "c1"]
    assert second["next_cursor"] is None


def test_source_filter(server, state):
    _, body = get(server, "/voice/history?source=scheduled")
    assert [s["id"] for s in body["sessions"]] == ["cron1"]
    _, body = get(server, "/voice/history?source=terminal")
    assert [s["id"] for s in body["sessions"]] == ["c2tip", "c1"]
    assert get(server, "/voice/history?source=nope")[0] == 400


def test_search_covers_message_text_through_fts(server, state):
    _, body = get(server, "/voice/history?q=marigold")
    assert body["search"] == "fts"
    assert [s["id"] for s in body["sessions"]] == ["c1"]
    assert "marigold" in body["sessions"][0]["match"]
    # A word only in the chain's root still finds the conversation (listed as its newest segment).
    _, body = get(server, "/voice/history?q=tomato")
    assert [s["id"] for s in body["sessions"]] == ["c2tip"]
    _, body = get(server, "/voice/history?q=Shelf")  # title match
    assert [s["id"] for s in body["sessions"]] == ["d1"]


def test_search_falls_back_to_like_without_fts(tmp_path):
    path = tmp_path / "state.db"
    make_db(path, fts=False)
    add_session(path, "x1", "discord", title="Other", at=NOW - 10)
    add_msg(path, "x1", "user", "the heron nest moved", at=NOW - 10)
    got = H.list_sessions(path, q="heron")
    assert got["search"] == "like" and [s["id"] for s in got["sessions"]] == ["x1"]


def test_history_needs_a_device_token(server, state):
    assert http(server.base_url, "GET", "/voice/history")[0] == 401
    assert http(server.base_url, "GET", "/voice/history/d1")[0] == 401
    assert http(server.base_url, "POST", "/voice/history/d1/message", {"text": "hi"})[0] == 401


def test_status_advertises_history():
    from speakeasy.service import CAPABILITIES
    assert CAPABILITIES["history"] is True


# -- one session ----------------------------------------------------------------------------------

def test_session_page_collapses_tool_calls_and_redacts_them(server, state):
    path = state
    add_session(path, "d2", "discord", title="Pantry", display="Workshop / #home / Pantry", at=NOW - 20)
    add_msg(path, "d2", "user", "Check the pantry sheet", at=NOW - 30)
    add_msg(path, "d2", "assistant", "", calls=[call("c_1", "terminal",
                                                    {"command": "curl -H 'Authorization: Bearer sk-live-abcdefghijk' https://example.com/sheet?key=zzz"})],
            at=NOW - 29)
    add_msg(path, "d2", "tool", '{"output": "token=sk-live-abcdefghijk rows: 12", "exit_code": 0}', call_id="c_1",
            tool="terminal", at=NOW - 28)
    add_msg(path, "d2", "user", "[SYSTEM: background helper finished]", at=NOW - 27)
    add_msg(path, "d2", "assistant", "The pantry sheet has 12 rows.", at=NOW - 26)
    status, body = get(server, "/voice/history/d2")
    assert status == 200, body
    page = body["session"]
    assert page["source_label"] == "Discord · #home" and page["send_via"] == "linked"
    kinds = [e["kind"] for e in page["entries"]]
    assert kinds == ["user", "tools", "note", "assistant"]
    tools = page["entries"][1]["items"]
    assert tools[0]["tool"] == "terminal" and tools[0]["status"] == "done" and tools[0]["started_at"]
    blob = json.dumps(page)
    assert "sk-live-abcdefghijk" not in blob and "key=zzz" not in blob


def test_session_page_shows_the_words_not_the_speakeasy_brief(server, state):
    path = state
    sid = H.SPEAKEASY_PREFIX + "a" * 24
    add_session(path, sid, "api_server", title=None, at=NOW - 10)
    brief = ("You are Hermes, handling one task Sam gave you by voice from Speakeasy on their Mac. " + "x" * 3000
             + "\n\nRecent timestamped voice transcript (revision 0):\nUser: Find a quiet cafe near the river")
    add_msg(path, sid, "user", brief, at=NOW - 12)
    add_msg(path, sid, "assistant", "River Bean is quiet before ten.", at=NOW - 10)
    _, body = get(server, f"/voice/history/{sid}")
    entries = body["session"]["entries"]
    assert entries[0] == {**entries[0], "kind": "user", "text": "Find a quiet cafe near the river"}
    _, listing = get(server, "/voice/history?source=speakeasy")
    row = next(s for s in listing["sessions"] if s["id"] == sid)
    assert row["source_label"] == "Speakeasy" and row["title"] == "Find a quiet cafe near the river"


def test_unknown_session_is_404(server, state):
    assert get(server, "/voice/history/nope")[0] == 404
    assert post(server, "/voice/history/nope/message", {"text": "hi"})[0] == 404


# -- sending --------------------------------------------------------------------------------------

def test_send_to_a_speakeasy_task_session_uses_the_task_by_id(server, service, hermes, state):
    status, started = post(server, "/voice/work", {"text": "Draft the bake sale flyer"})
    assert status == 202, started
    run_id = started["run_id"]
    sid = wait_for(lambda: service.store.session_for(service.store.key_for_run(run_id)))
    add_session(state, sid, "api_server", at=NOW - 5)
    add_msg(state, sid, "user", "User: Draft the bake sale flyer", at=NOW - 6)
    add_msg(state, sid, "assistant", "Flyer drafted.", at=NOW - 5)
    wait_for(lambda: service.store.work(run_id)["status"] == "completed")
    _, listing = get(server, "/voice/history")
    row = next(s for s in listing["sessions"] if s["id"] == sid)
    assert row["source"] == "speakeasy" and run_id in row["run_ids"]
    calls_before = len(hermes.calls)
    status, body = post(server, f"/voice/history/{sid}/message", {"text": "Add the date too", "client_id": "c_h1"})
    assert status == 200, body
    assert body["via"] == "task" and body["run_id"]
    wait_for(lambda: len(hermes.calls) > calls_before)
    assert hermes.calls[-1]["session_id"] == sid and "Add the date too" in json.dumps(hermes.calls[-1]["input"])
    # The same client_id is the same send.
    again = post(server, f"/voice/history/{sid}/message", {"text": "Add the date too", "client_id": "c_h1"})[1]
    assert again["run_id"] == body["run_id"] and len(hermes.calls) == calls_before + 1
    _, page = get(server, f"/voice/history/{sid}")
    assert page["session"]["send_via"] == "task" and page["session"]["jobs"]
    # A job page links to its full session.
    assert get(server, f"/voice/work/{run_id}")[1]["work"]["session_id"] == sid


def test_send_to_another_api_session_starts_a_job_in_that_session(server, hermes, state):
    add_session(state, "api_other", "api_server", title="Recipe scaling", at=NOW - 5)
    add_msg(state, "api_other", "user", "Scale the soup recipe", at=NOW - 6)
    status, body = post(server, "/voice/history/api_other/message", {"text": "Now for twelve people"})
    assert status == 200, body
    assert body["via"] == "session_job" and body["run_id"]
    assert hermes.calls[-1]["session_id"] == "api_other"


def test_send_to_a_discord_thread_starts_a_linked_task_and_never_posts_back(server, service, hermes, state):
    status, body = post(server, "/voice/history/d1/message", {"text": "Order them please", "source": "voice_message"})
    assert status == 200, body
    assert body["via"] == "linked" and body["run_id"]
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["session_id"] != "d1"                      # never into the chat's own session
    sent = json.dumps(call["input"])
    assert "Order them please" in sent and "walnut shelf brackets" in sent   # seeded with the chat
    assert "never posted to that chat" in sent
    key = service.store.key_for_run(body["run_id"])
    assert service.store.history_links("d1") == [key]
    linked_sid = wait_for(lambda: service.store.session_for(key))
    add_session(state, linked_sid, "api_server", at=NOW - 1)
    add_msg(state, linked_sid, "assistant", "Ordered three brackets.", at=NOW - 1)
    _, page = get(server, "/voice/history/d1")
    s = page["session"]
    assert s["send_via"] == "linked" and s["source"] == "discord"
    texts = [e.get("text") for e in s["entries"]]
    assert "From Speakeasy · not posted to Discord" in texts and "Ordered three brackets." in texts
    assert s["jobs"] and s["jobs"][0]["run_id"] == body["run_id"]
    # The next message goes to the same linked task, not a new one.
    wait_for(lambda: service.store.work(body["run_id"])["status"] == "completed")
    before = len(hermes.calls)
    status, again = post(server, "/voice/history/d1/message", {"text": "And the screws"})
    assert status == 200 and again["via"] == "linked", again
    assert service.store.history_links("d1") == [key]
    wait_for(lambda: len(hermes.calls) > before)
    assert hermes.calls[-1]["session_id"] == linked_sid


def test_the_chat_row_shows_its_linked_task_running(server, service, hermes, state):
    hermes.hold = True
    post(server, "/voice/history/t1/message", {"text": "Which of those can go outside now?"})
    wait_for(lambda: hermes.calls)
    _, listing = get(server, "/voice/history")
    row = next(r for r in listing["sessions"] if r["id"] == "t1")
    assert row["state"] == "running" and row["source"] == "telegram"


def test_bad_message_bodies(server, state):
    assert post(server, "/voice/history/d1/message", {"text": "  "})[0] == 400
    assert post(server, "/voice/history/d1/message", {"text": "x", "extra": 1})[0] == 400
    assert post(server, "/voice/history/d1/message", {"text": "x" * 5000})[0] == 400
    assert post(server, "/voice/history/d1/message", {"text": "x", "source": "routed"})[0] == 400


def test_a_speakeasy_job_waiting_on_approval_is_answerable_from_its_session(server, service, hermes, state):
    hermes.approval_first = True
    status, started = post(server, "/voice/work", {"text": "Tidy the old garden photos"})
    assert status == 202, started
    run_id = started["run_id"]
    sid = wait_for(lambda: service.store.session_for(service.store.key_for_run(run_id)))
    add_session(state, sid, "api_server", at=NOW - 5)
    add_msg(state, sid, "user", "User: Tidy the old garden photos", at=NOW - 6)
    page = wait_for(lambda: (lambda b: b if b["session"]["approvals"] else None)(get(server, f"/voice/history/{sid}")[1]))
    approval = page["session"]["approvals"][0]
    assert approval["run_id"] == run_id and approval["description"] == "Delete an old file"
    assert "always" not in approval["choices"]
    assert post(server, f"/voice/history/{sid}/approval", {"run_id": run_id, "choice": "always"})[0] == 400
    assert post(server, "/voice/history/d1/approval", {"run_id": run_id, "choice": "once"})[0] == 404
    status, body = post(server, f"/voice/history/{sid}/approval", {"run_id": run_id, "choice": "once"})
    assert status == 200 and body["choice"] == "once", body
    assert hermes.approvals[-1]["choice"] == "once"


# -- attachments on a history-page message (same claim/give-back as /voice/work[/{id}/message]) ----

@pytest.fixture
def reads_images(service):
    service.hermes.image_support = {"images": True, "vision": "native"}


def test_a_linked_chat_message_carries_a_picture_and_never_posts_back(server, service, hermes, state, reads_images):
    from test_staged_attachments import images_in, jpeg, stage, text_of
    picture = jpeg(b"invented-bracket-sketch ")
    a = stage(server, picture, name="bracket.jpg")[1]["id"]
    status, body = post(server, "/voice/history/d1/message",
                        {"text": "Use this sketch", "attachments": [a], "client_id": "c_hatt1"})
    assert status == 200 and body["via"] == "linked", body
    assert body["attachments"] == [{"id": a, "kind": "picture", "name": "bracket.jpg"}]
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["session_id"] != "d1" and images_in(call["input"]) == [picture]
    assert "Use this sketch" in text_of(call["input"])
    # Claimed: the same id can't go twice, but the same client_id replays the first reply.
    assert post(server, "/voice/history/d1/message", {"text": "Again", "attachments": [a]})[0] == 404
    again = post(server, "/voice/history/d1/message", {"text": "Use this sketch", "attachments": [a],
                                                     "client_id": "c_hatt1"})[1]
    assert again["run_id"] == body["run_id"]


def test_an_api_session_message_carries_a_file_by_its_saved_copy(server, hermes, state, home, reads_images):
    from test_staged_attachments import stage, text_of
    add_session(state, "api_other", "api_server", title="Recipe scaling", at=NOW - 5)
    b = stage(server, b"%PDF-1.7 invented menu", "file", "application/pdf", "menu.pdf")[1]["id"]
    status, body = post(server, "/voice/history/api_other/message", {"text": "", "attachments": [b],
                                                                    "source": "voice_message"})
    assert status == 200 and body["via"] == "session_job", body
    call = hermes.calls[-1]
    prompt = text_of(call["input"])
    assert call["session_id"] == "api_other" and "menu.pdf" in prompt
    assert "Take a look at what I attached." in prompt
    assert str(home / "cache" / "speakeasy" / "shared") in prompt


def test_a_speakeasy_task_session_follow_up_carries_the_picture(server, service, hermes, state, reads_images):
    from test_staged_attachments import images_in, jpeg, stage
    status, started = post(server, "/voice/work", {"text": "Plan the herb garden"})
    run_id = started["run_id"]
    sid = wait_for(lambda: service.store.session_for(service.store.key_for_run(run_id)))
    add_session(state, sid, "api_server", at=NOW - 5)
    wait_for(lambda: service.store.work(run_id)["status"] == "completed")
    picture = jpeg(b"invented-herb-layout ")
    a = stage(server, picture)[1]["id"]
    status, body = post(server, f"/voice/history/{sid}/message", {"text": "Use this layout", "attachments": [a]})
    assert status == 200 and body["via"] == "task", body
    follow = wait_for(lambda: [c for c in hermes.calls if images_in(c["input"])])[0]
    assert follow["session_id"] == sid and images_in(follow["input"]) == [picture]


def test_history_attachments_are_all_or_nothing_and_per_device(server, service, hermes, state):
    from test_staged_attachments import jpeg, second_device, stage
    a = stage(server, jpeg())[1]["id"]
    status, body = post(server, "/voice/history/d1/message", {"text": "x", "attachments": [a, "att_gone"]})
    assert status == 404 and body["reason"] == "unknown"
    other = second_device(server, service)
    status, body = http(server.base_url, "POST", "/voice/history/d1/message", {"text": "x", "attachments": [a]}, other)
    assert status == 404 and body["reason"] == "unknown"
    assert post(server, "/voice/history/d1/message", {"text": "x", "attachments": "att_x"})[0] == 400
    assert not hermes.calls
    assert a in service.staged._items[next(iter(service.staged._items))]   # still waiting to be sent


def test_a_failed_history_send_gives_the_attachments_back(server, service, state, monkeypatch):
    from test_staged_attachments import jpeg, stage
    a = stage(server, jpeg())[1]["id"]

    def boom(*_a, **_k):
        raise RuntimeError("hermes down")
    monkeypatch.setattr(service, "_start_job", boom)
    device = next(iter(service.staged._items))
    with pytest.raises(Exception):
        service.history_message("d1", {"text": "x", "attachments": [a]}, device)
    assert a in service.staged._items[device]
