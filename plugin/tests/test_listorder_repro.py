"""SPK-319: the conversations list orders rows by the person's own activity (their messages, a
task starting, a finished reply), never by a running task's progress steps. Invented data."""
import sqlite3
import time

from speakeasy import history as H
from test_history import add_msg, add_session, call, make_db

T0 = time.time() - 30
IDS = ["speakeasy_task_A", "speakeasy_task_B", "speakeasy_task_C"]


def _db(tmp_path):
    path = tmp_path / "state.db"
    make_db(path, routed=True)
    for i, sid in enumerate(IDS):
        add_session(path, sid, "api_server", title=f"Task {sid[-1]}", at=T0 + i)
        add_msg(path, sid, "user", f"Please do task {sid[-1]}", at=T0 + i)
    return path


def _rows(path, **kw):
    return H.list_sessions(path, live=lambda sid: True, now=T0 + 10, **kw)


def _order(path):
    return [r["id"] for r in _rows(path)["sessions"] if r["id"] in IDS]


def _row(path, sid):
    return next(r for r in _rows(path)["sessions"] if r["id"] == sid)


def _heartbeat(path, sid, at):
    db = sqlite3.connect(path)
    db.execute("UPDATE sessions SET last_activity_at=? WHERE id=?", (at, sid))
    db.commit(); db.close()


def test_agent_progress_alone_does_not_reorder_running_rows(tmp_path):
    path = _db(tmp_path)
    before = _order(path)
    assert before == ["speakeasy_task_C", "speakeasy_task_B", "speakeasy_task_A"]
    add_msg(path, "speakeasy_task_A", "assistant", "Looking into it",
            calls=[call("c1", "terminal", {"command": "ls"})], at=T0 + 5)
    add_msg(path, "speakeasy_task_A", "tool", "ok", call_id="c1", tool="terminal", at=T0 + 5)
    after = _order(path)
    assert after == before, f"agent-only progress reordered the list: {before} -> {after}"


def test_control_new_user_message_moves_row_to_top(tmp_path):
    path = _db(tmp_path)
    assert _order(path)[0] == "speakeasy_task_C"
    add_msg(path, "speakeasy_task_A", "user", "Also check the logs", at=T0 + 5)
    assert _order(path)[0] == "speakeasy_task_A"


def test_hermes_heartbeat_during_a_turn_does_not_reorder(tmp_path):
    """Hermes moves ``last_activity_at`` while a turn runs: that is progress, not the person."""
    path = _db(tmp_path)
    _heartbeat(path, "speakeasy_task_A", T0 + 8)
    assert _order(path) == ["speakeasy_task_C", "speakeasy_task_B", "speakeasy_task_A"]


def test_progress_updates_the_preview_where_the_row_sits(tmp_path):
    path = _db(tmp_path)
    add_msg(path, "speakeasy_task_A", "assistant", "Reading the test logs now",
            calls=[call("c1", "terminal", {"command": "ls"})], at=T0 + 5)
    row = _row(path, "speakeasy_task_A")
    assert _order(path)[-1] == "speakeasy_task_A"
    # The row's time is the moment its place comes from (it started), not the step.
    assert row["last_activity"] == round(T0, 3)
    assert row["preview"].startswith("Reading the test logs") or row["preview"]


def test_a_finished_reply_moves_the_row_up(tmp_path):
    """A task settling (its final reply, no tool calls) is something the person wants to see."""
    path = _db(tmp_path)
    add_msg(path, "speakeasy_task_A", "assistant", "Checking",
            calls=[call("c1", "terminal", {"command": "ls"})], at=T0 + 4)
    add_msg(path, "speakeasy_task_A", "tool", "ok", call_id="c1", tool="terminal", at=T0 + 4)
    add_msg(path, "speakeasy_task_A", "assistant", "Found it: the timeout is 30 seconds.", at=T0 + 6)
    assert _order(path)[0] == "speakeasy_task_A"
    assert _row(path, "speakeasy_task_A")["last_activity"] == round(T0 + 6, 3)


def test_the_row_time_is_the_order_time_and_the_cursor_follows_it(tmp_path):
    path = _db(tmp_path)
    for sid in IDS:   # every task keeps working; none of it moves the rows
        add_msg(path, sid, "assistant", "step", calls=[call("c", "terminal", {})], at=T0 + 9)
    page = _rows(path, limit=1)
    assert [r["id"] for r in page["sessions"]] == ["speakeasy_task_C"]
    assert page["sessions"][0]["last_activity"] == round(T0 + 2, 3)
    rest = _rows(path, limit=5, cursor=page["next_cursor"])
    assert [r["id"] for r in rest["sessions"] if r["id"] in IDS] == ["speakeasy_task_B", "speakeasy_task_A"]


def test_a_linked_task_moves_its_chat_only_on_the_persons_activity(tmp_path):
    """A chat continued in Speakeasy takes its linked task's place, under the same rule."""
    path = _db(tmp_path)
    add_session(path, "chat1", "discord", title="Porch lights", chat_type="thread", at=T0 - 20)
    add_msg(path, "chat1", "user", "When does the porch timer start?", at=T0 - 20)
    add_session(path, "speakeasy_task_L", "api_server", title="Porch timer", at=T0 - 10)
    add_msg(path, "speakeasy_task_L", "user", "When does the porch timer start?", at=T0 - 10)
    fold = lambda: {"speakeasy_task_L": "chat1"}  # noqa: E731

    def order():
        rows = H.list_sessions(path, live=lambda sid: True, now=T0 + 10, folded=fold)["sessions"]
        return [r["id"] for r in rows if r["id"] in (*IDS, "chat1")]

    assert order()[-1] == "chat1"
    add_msg(path, "speakeasy_task_L", "assistant", "Checking the schedule",
            calls=[call("c1", "terminal", {})], at=T0 + 7)
    assert order()[-1] == "chat1"
    add_msg(path, "speakeasy_task_L", "assistant", "It starts at dusk.", at=T0 + 8)
    assert order()[0] == "chat1"
