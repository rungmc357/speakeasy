"""A task's own STATUS/DETAIL lines reach its live line even when the run stream never carries them.

Hermes' admitted API runs (``/v1/runs`` turns through the session runtime) stream tool events but
not the agent's interim commentary, so the lines the task brief asks for were written, paid for and
never shown: the live line only ever came from tool steps. Hermes stores the assistant message
that carries a tool call (commentary included) before the tool runs, so Speakeasy reads the task's
own lines from its transcript when the tool starts (threads.SessionCommentary). Everything invented.
"""
import json

from fakes import wait_for
from speakeasy import history as H
from speakeasy import threads
from speakeasy.text import interim_progress
from test_history import add_msg, add_session, make_db
from test_run_failures import start_call
from test_task_messages import one_task, tasks

PAIR = "STATUS: Adding the composer to Home\nDETAIL: Found the Home layout; the composer goes under the header"


def _transcript_writer(service, hermes, text=PAIR):
    """The fake Hermes writes the turn's tool-call message (with its commentary) into the task's
    session, as Hermes does before the tool runs; its run stream carries the tool event only."""
    db = service.home / "state.db"
    if not db.exists():
        make_db(db)

    def respond(prompt, sid):
        add_session(db, sid, "api_server")
        add_msg(db, sid, "user", prompt)
        add_msg(db, sid, "assistant", text, calls=[{"id": "c1", "type": "function", "function": {
            "name": "web_search", "arguments": json.dumps({"query": "home layout"})}}])
        return "Added it.\nDONE: Composer added\nSPOKEN: The composer is on Home."
    hermes.responder = respond
    return db


def test_a_status_pair_in_the_transcript_becomes_the_tasks_own_live_line(server, service, hermes):
    _transcript_writer(service, hermes)
    hermes.hold = True  # still running: the tool start below streams while the run works
    hermes.live_events = [{"event": "tool.started", "tool": "web_search", "preview": "home layout"}]
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Add the composer to the Home page")
    work = wait_for(lambda: (lambda w: w if w and w.get("short_status") == "Adding the composer to Home" else None)(
        service.store.work(idem_key=service.store.key_for_run(task["run_id"]))))
    assert work["status_source"] == "authored"
    assert work["detail"] == "Found the Home layout; the composer goes under the header"


def test_the_same_lines_from_the_stream_and_the_transcript_count_once(server, service, hermes):
    """A Hermes that streams the commentary too: the pair is handled once, never said twice."""
    _transcript_writer(service, hermes)
    hermes.live_events = [{"event": "message.interim", "text": PAIR},
                          {"event": "tool.started", "tool": "web_search", "preview": "home layout"}]
    hermes.hold = True
    seen = []
    real = service.store.user_progress
    service.store.user_progress = lambda key, status, detail: (seen.append(status), real(key, status, detail))[1]
    _, worker = start_call(server, service)
    task = one_task(server, worker, "Add the composer to the Home page")
    key = service.store.key_for_run(task["run_id"])
    wait_for(lambda: (service.store.work(idem_key=key) or {}).get("status_source") == "authored" or None)
    import time
    time.sleep(0.3)
    assert seen == ["Adding the composer to Home"]


def test_the_commentary_reader_only_reads_this_runs_new_rows(tmp_path):
    db = tmp_path / "state.db"
    make_db(db)
    add_session(db, "s1", "api_server")
    add_msg(db, "s1", "assistant", "STATUS: Reading the old notes\nDETAIL: An earlier turn")
    reader = threads.SessionCommentary(db, "s1")      # the run starts here
    add_msg(db, "s2", "assistant", "STATUS: Elsewhere\nDETAIL: Another session")
    add_msg(db, "s1", "tool", "result", call_id="c0", tool="web_search")
    add_msg(db, "s1", "assistant", PAIR)
    assert reader.new() == [PAIR]
    assert reader.new() == []                         # read once
    assert threads.SessionCommentary(None, "s1").new() == []


def test_a_detail_that_restates_its_status_shows_once():
    assert H.assistant_text("STATUS: Checking the wiki\nDETAIL: Checking the wiki.") == "Checking the wiki"
    assert H.assistant_text("STATUS: Searching notes\nDETAIL: Searching notes for the trip") == "Searching notes for the trip"
    assert H.assistant_text("STATUS: Checking the tests\nDETAIL: Two still fail.") == "Checking the tests — Two still fail"


def test_talk_about_a_hiccup_is_never_the_live_line():
    for text in ("Using patch instead", "Trying again with a smaller file", "Hmm, wrong file",
                 "That didn't work, retrying"):
        assert interim_progress({"event": "message.interim", "text": text}) is None, text
    assert interim_progress({"event": "message.interim", "text": "Checking the order totals"}) == (
        "Checking the order totals", "Checking the order totals")


def test_another_sessions_floor_skips_hiccup_talk_and_prefers_the_status(tmp_path):
    import time
    db = tmp_path / "state.db"
    make_db(db)
    now = time.time()
    add_session(db, "w1", "cli", at=now)
    add_msg(db, "w1", "user", "Fix the cart test", at=now - 60)
    calls = [{"id": "c1", "type": "function", "function": {"name": "patch", "arguments": "{}"}}]
    add_msg(db, "w1", "assistant", "Using patch instead.", calls=calls, at=now - 5)
    entries = H.session_messages(db, "w1")["entries"]
    live = H.session_live(db, "w1", entries, now=now)
    assert live is None or "instead" not in live["now"].lower()
    add_msg(db, "w1", "tool", "ok", call_id="c1", tool="patch", at=now - 4)
    add_msg(db, "w1", "assistant", "STATUS: Fixing the cart total\nDETAIL: Found the rounding bug", calls=[
        {"id": "c2", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}], at=now - 2)
    entries = H.session_messages(db, "w1")["entries"]
    live = H.session_live(db, "w1", entries, now=now)
    assert live and live["now"] == "Fixing the cart total"
