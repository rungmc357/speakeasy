"""Automated work in the history (SPK-70): Kanban board workers and scheduled jobs can be hidden with one
switch, are never unread or needs-you, and a worker's session opens on its card and its updates, not
on the dispatch prompt."""
import json

from speakeasy import history as H
from test_history import NOW, add_msg, add_session, call, get, make_db, post, state  # noqa: F401

SKILL = ('[IMPORTANT: The user launched this CLI session with the "lantern-worker" skill preloaded. Treat its '
         "instructions as active guidance for the duration of this session unless the user overrides them.]\n\n"
         "---\nname: lantern-worker\n---\n# Lantern worker\nPost a start comment, then do the work.\n\n"
         "[Skill directory: /tmp/skills/lantern-worker]\nResolve any relative paths in this skill (e.g. "
         "`scripts/foo.js`, `templates/config.yaml`) against that directory, then run them with the terminal "
         "tool using the absolute path.\n\n")
CARD = ("# Kanban task t_9f2a: LAN-12: Lantern wick burns too fast\n\nAssignee: default\nStatus:   running\n"
        "Workspace: scratch @ /tmp/ws/t_9f2a\n\n## Body\nTracker issue LAN-12: Lantern wick burns too fast\n"
        "https://tracker.example/acme/issue/LAN-12/lantern-wick-burns-too-fast\n\n"
        "The demo lantern's wick is gone in ten minutes. Make it last an hour. Keep the flame the same size.\n\n"
        "Integration bases: from `main`.\n\n--- Team board at dispatch ---\n# Board\n- LAN-9 Glass cleaning\n\n\n"
        "work kanban task t_9f2a")


def worker(path, sid="kan1", at=NOW - 20):
    add_session(path, sid, "kanban", title="LAN-12: Lantern wick burns too fast", at=at)
    add_msg(path, sid, "user", SKILL + CARD, at=at - 600)
    add_msg(path, sid, "assistant", "", calls=[call("k1", "kanban_show", {})], at=at - 590)
    add_msg(path, sid, "tool", '{"task": {"id": "t_9f2a", "body": "lots of card json"}}', call_id="k1",
            tool="kanban_show", at=at - 589)
    add_msg(path, sid, "assistant", "", calls=[call("c1", "terminal", {
        "command": 'L="tracker"; $L comment LAN-12 "Starting: making the wick last an hour at the same flame."'})],
        at=at - 580)
    add_msg(path, sid, "tool", '{"output": "ok"}', call_id="c1", tool="terminal", at=at - 579)
    add_msg(path, sid, "assistant", "Now the rewrite.", calls=[call("r1", "read_file", {"path": "/repo/wick.py"})],
            at=at - 500)
    add_msg(path, sid, "tool", "def burn(): ...", call_id="r1", tool="read_file", at=at - 499)
    add_msg(path, sid, "assistant", "", calls=[call("h1", "kanban_heartbeat", {"note": "still going"})], at=at - 300)
    add_msg(path, sid, "tool", '{"ok": true}', call_id="h1", tool="kanban_heartbeat", at=at - 299)
    add_msg(path, sid, "assistant", "", calls=[call("d1", "kanban_complete", {
        "summary": "The wick now lasts an hour; flame size unchanged. 14 tests pass."})], at=at - 10)
    add_msg(path, sid, "tool", '{"ok": true}', call_id="d1", tool="kanban_complete", at=at - 9)
    add_msg(path, sid, "assistant", "Done.", at=at)


# -- the list -------------------------------------------------------------------------------------


def test_kanban_sessions_list_by_default_with_their_own_filter(server, state):
    worker(state)
    page = get(server, "/voice/history")[1]
    row = next(s for s in page["sessions"] if s["id"] == "kan1")
    assert row["source"] == "kanban" and row["source_label"] == "Kanban" and row["automated"] is True
    assert {"id": "kanban", "label": "Kanban"} in page["filters"]
    assert {"source": "kanban", "count": 1} in page["sources"]
    assert page["automated"] == {"count": 2, "hidden": False}   # the worker + the scheduled run
    only = get(server, "/voice/history?source=kanban")[1]["sessions"]
    assert [s["id"] for s in only] == ["kan1"]


def test_automated_work_hides_with_one_switch(server, state):
    worker(state)
    page = get(server, "/voice/history?automated=0")[1]
    ids = [s["id"] for s in page["sessions"]]
    assert "kan1" not in ids and "cron1" not in ids and "d1" in ids
    assert page["automated"] == {"count": 2, "hidden": True}
    assert not {"kanban", "scheduled"} & {c["source"] for c in page["sources"]}
    # Searching hides them too; asking for the Kanban filter by name still lists them.
    assert get(server, "/voice/history?automated=0&q=wick")[1]["sessions"] == []
    assert [s["id"] for s in get(server, "/voice/history?automated=0&source=kanban")[1]["sessions"]] == ["kan1"]
    assert [s["id"] for s in get(server, "/voice/history?automated=0&source=scheduled")[1]["sessions"]] == ["cron1"]


def test_automated_rows_are_never_unread(server, state):
    worker(state)
    assert post(server, "/voice/history/kan1/read", {})[0] == 200
    assert post(server, "/voice/history/d1/read", {})[0] == 200
    add_msg(state, "kan1", "assistant", "More chatter.", at=NOW + 50)
    add_msg(state, "d1", "assistant", "A new reply.", at=NOW + 50)
    import sqlite3
    db = sqlite3.connect(state)
    db.execute("UPDATE sessions SET last_activity_at=? WHERE id IN ('kan1','d1')", (NOW + 50,))
    db.commit(); db.close()
    rows = {s["id"]: s for s in H.list_sessions(state, marks=lambda: {"kan1": {"read_at": NOW}, "d1": {"read_at": NOW}},
                                                 now=NOW + 3000)["sessions"]}
    assert rows["d1"].get("unread") is True
    assert "unread" not in rows["kan1"] and rows["kan1"]["state"] != "needs_you"


def test_needs_you_never_marks_an_automated_row(state):
    worker(state)
    link = H.Linked(keys=["k"], name=None, running=False, needs_you=True, run_ids=[])
    rows = {s["id"]: s for s in H.list_sessions(state, linked=lambda sid: link if sid == "kan1" else None,
                                                 now=NOW + 3000)["sessions"]}
    # A link with keys makes it Speakeasy's own row, so only an unkeyed link stays automated.
    unkeyed = H.Linked(keys=[], name=None, running=False, needs_you=True, run_ids=[])
    rows = {s["id"]: s for s in H.list_sessions(state, linked=lambda sid: unkeyed if sid == "kan1" else None,
                                                 now=NOW + 3000)["sessions"]}
    assert rows["kan1"]["state"] == "idle" and rows["kan1"]["automated"] is True


# -- the session page -----------------------------------------------------------------------------


def test_a_worker_session_opens_on_its_card_not_the_dispatch_prompt(server, state):
    worker(state)
    entries = get(server, "/voice/history/kan1")[1]["session"]["entries"]
    head = entries[0]
    assert head["kind"] == "note" and head["text"] == "LAN-12 \u00b7 Lantern wick burns too fast"
    card = head["task"]
    assert card["issue"] == "LAN-12" and card["title"] == "Lantern wick burns too fast"
    assert card["url"] == "https://tracker.example/acme/issue/LAN-12/lantern-wick-burns-too-fast"
    assert card["goal"] == "The demo lantern's wick is gone in ten minutes."
    assert "Team board at dispatch" in card["instructions"] and "work kanban task" not in card["instructions"]
    # The skill file and the card never show as anyone's words.
    blob = json.dumps(entries)
    assert "lantern-worker" not in blob and "Post a start comment" not in blob
    assert not any(e["kind"] == "user" for e in entries)


def test_worker_updates_read_as_messages_and_lifecycle_calls_are_not_steps(server, state):
    worker(state)
    entries = get(server, "/voice/history/kan1")[1]["session"]["entries"]
    updates = [e["text"] for e in entries if e.get("update")]
    assert updates == ["Starting: making the wick last an hour at the same flame.",
                       "The wick now lasts an hour; flame size unchanged. 14 tests pass."]
    steps = [i for e in entries if e["kind"] == "tools" for i in e["items"]]
    assert all("kanban" not in json.dumps(i).lower() for i in steps)
    assert any(i["text"].startswith("Read") for i in steps)
    assert "Now the rewrite." in [e["text"] for e in entries if e["kind"] == "assistant"]


def test_a_retry_shows_the_header_once(state):
    worker(state)
    add_msg(state, "kan1", "user", SKILL + CARD.replace("Body\n", "Body\nResume from your branch.\n"), at=NOW)
    entries = H.session_messages(state, "kan1")["entries"]
    heads = [e for e in entries if e.get("task")]
    assert len(heads) == 1
    assert entries[-1] == {**entries[-1], "kind": "note", "text": "Picked up again"}


def test_a_running_worker_has_a_live_line_from_its_steps(server, state):
    add_session(state, "kan2", "kanban", title="LAN-13: Polish the glass", at=NOW - 5)
    add_msg(state, "kan2", "user", CARD.replace("LAN-12", "LAN-13"), at=NOW - 300)
    add_msg(state, "kan2", "assistant", "", calls=[call("r1", "read_file", {"path": "/repo/glass.py"})], at=NOW - 200)
    add_msg(state, "kan2", "tool", "x = 1", call_id="r1", tool="read_file", at=NOW - 199)
    add_msg(state, "kan2", "assistant", "", calls=[call("t1", "terminal", {"command": "cd app && make test --fast"})],
            at=NOW - 5)
    s = H.session_messages(state, "kan2")
    live = H.session_live(state, "kan2", s["entries"], now=NOW)
    assert live and live["now"] and "&&" not in live["now"] and "make" not in live["now"]
    assert "so_far" in live and "Read" in live["so_far"]
    # Finished (the newest row is an answer): no live line.
    add_msg(state, "kan2", "assistant", "Done.", at=NOW - 1)
    assert H.session_live(state, "kan2", H.session_messages(state, "kan2")["entries"], now=NOW) is None


def test_a_worker_saying_what_it_does_is_the_live_line(state):
    add_session(state, "kan3", "kanban", title="LAN-14: Trim the wick", at=NOW - 5)
    add_msg(state, "kan3", "user", CARD, at=NOW - 300)
    add_msg(state, "kan3", "assistant", "Fixing the flame test now.",
            calls=[call("t1", "terminal", {"command": "pytest -q"})], at=NOW - 10)
    live = H.session_live(state, "kan3", H.session_messages(state, "kan3")["entries"], now=NOW)
    assert live["now"] == "Fixing the flame test now"


def test_the_session_reply_carries_live_and_automated(server, state):
    add_session(state, "kan4", "kanban", title="LAN-15: Clean the glass", at=NOW - 5)
    add_msg(state, "kan4", "user", CARD, at=NOW - 300)
    add_msg(state, "kan4", "assistant", "", calls=[call("r1", "read_file", {"path": "/repo/glass.py"})], at=NOW - 5)
    import time
    session = get(server, "/voice/history/kan4")[1]["session"]
    assert session["automated"] is True
    # The fixture clock is the real one: the turn is pending, so it runs and carries a live line.
    if time.time() - NOW < H.BUSY_WINDOW_S:
        assert session["state"] == "running" and session["live"]["now"]


def test_tracker_update_parsing():
    assert H.tracker_update("terminal", {"command": 'linear comment AB-1 "Fixed the \\"quote\\" case."'}) == \
        'Fixed the "quote" case.'
    assert H.tracker_update("terminal", {"command": "linear.py status AB-2 'Running the tests.'"}) == "Running the tests."
    assert H.tracker_update("terminal", {"command": "git commit -m 'AB-3: thing'"}) is None
    assert H.tracker_update("kanban_block", {"reason": "Needs a decision on colors."}) == "Needs a decision on colors."
    assert H.tracker_update("read_file", {"path": "x"}) is None
