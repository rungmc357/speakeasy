"""Thread tasks orphaned by a hang-up, restart or reload get settled from the thread itself."""
import sqlite3
import time

from speakeasy import recovery
from speakeasy.store import StateStore


def hermes_db(path, sessions):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT, source TEXT, thread_id TEXT, started_at REAL, ended_at REAL, title TEXT)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_calls TEXT)")
    for sid, thread, messages in sessions:
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,NULL,NULL)", (sid, "discord", thread, time.time()))
        for role, content, tools in messages:
            db.execute("INSERT INTO messages (session_id, role, content, tool_calls) VALUES (?,?,?,?)",
                       (sid, role, content, tools))
    db.commit()
    db.close()


def thread_task(store, key, request, age_s=0.0):
    store.reserve_run(key, "call1", key, 1)
    store.progress(key, "request", request)
    store.update_run(key, None, "running")
    if age_s:
        with store._lock, store._db:
            store._db.execute("UPDATE runs SET updated=? WHERE idem_key=?", (time.time() - age_s, key))


def status(store, key):
    return store._db.execute("SELECT status FROM runs WHERE idem_key=?", (key,)).fetchone()[0]


def test_a_finished_thread_settles_the_stuck_task(tmp_path):
    """Live bug: 'How's my league team doing' answered in its thread within a minute, but the call
    had ended, so the task showed 'running' for two days."""
    db = tmp_path / "state.db"
    hermes_db(db, [("s1", "111", [("user", "How's my league team doing", None),
                                  ("assistant", "", '[{"id": "x"}]'), ("tool", "{}", None),
                                  ("assistant", "Voice: How's my league team\n\nYou're 2-0 and in second place.", None),
                                  ("session_meta", "", None)])])
    store = StateStore(tmp_path / "se.sqlite3")
    thread_task(store, "k1", "How's my league team doing")
    store.set_continued("k1", "s1", "a thread in Discord")
    assert recovery.sweep(store, db, lambda: (), {}) == [("k1", "completed")]
    assert status(store, "k1") == "completed"
    work = store.work(idem_key="k1")
    assert "second place" in work["result"]["spoken"] and "Voice:" not in work["result"]["full"]
    assert recovery.sweep(store, db, lambda: (), {}) == []  # settled once


def test_a_thread_known_only_by_id_is_found(tmp_path):
    db = tmp_path / "state.db"
    hermes_db(db, [("s2", "222", [("user", "weather?", None), ("assistant", "Sunny, 72.", None)])])
    store = StateStore(tmp_path / "se.sqlite3")
    thread_task(store, "k2", "weather tomorrow")
    store.set_thread("k2", "discord", "222", "a thread in #main")
    assert recovery.sweep(store, db, lambda: (), {}) == [("k2", "completed")]
    assert store.continued_for("k2")["session_id"] == "s2"
    assert store.continued_for("k2")["thread_id"] == "222"


def test_live_waits_are_left_alone_until_they_are_orphaned(tmp_path):
    db = tmp_path / "state.db"
    hermes_db(db, [("s3", "333", [("assistant", "Done.", None)])])
    store = StateStore(tmp_path / "se.sqlite3")
    thread_task(store, "k3", "x")
    store.set_continued("k3", "s3", "t")
    assert recovery.sweep(store, db, lambda: (), {"k3": 100.0}, mono=200.0) == []
    assert recovery.sweep(store, db, lambda: (), {"k3": 100.0}, mono=100.0 + 36 * 60) == [("k3", "completed")]


def test_tasks_that_never_answer_are_closed_after_a_while(tmp_path):
    db = tmp_path / "state.db"
    hermes_db(db, [("s4", "444", [("user", "still going", None)])])
    store = StateStore(tmp_path / "se.sqlite3")
    thread_task(store, "fresh", "a", age_s=60)
    store.set_thread("fresh", "discord", "999", "t")
    thread_task(store, "no_turn", "b", age_s=31 * 60)
    store.set_thread("no_turn", "discord", "999", "t")
    thread_task(store, "unfinished", "c", age_s=4 * 3600)
    store.set_continued("unfinished", "s4", "t")
    thread_task(store, "unknown", "d", age_s=4 * 3600)  # from before threads were recorded
    out = dict(recovery.sweep(store, db, lambda: (), {}))
    assert out == {"no_turn": "interrupted", "unfinished": "interrupted", "unknown": "interrupted"}
    assert status(store, "fresh") == "running"
    events = [e["text"] for e in store.work(idem_key="no_turn")["events"]]
    assert recovery.NEVER_STARTED in events
    assert recovery.INTERRUPTED_NOTE in [e["text"] for e in store.work(idem_key="unknown")["events"]]


def test_regular_runs_are_not_touched(tmp_path):
    db = tmp_path / "state.db"
    hermes_db(db, [])
    store = StateStore(tmp_path / "se.sqlite3")
    store.reserve_run("k5", "call1", "k5", 1)
    store.progress("k5", "request", "x")
    store.update_run("k5", "run_abc", "running")  # a Hermes API run: its own stream settles it
    with store._lock, store._db:
        store._db.execute("UPDATE runs SET updated=? WHERE idem_key='k5'", (time.time() - 5 * 3600,))
    assert recovery.sweep(store, db, lambda: (), {}) == []


def unconfirmed_run(store, key, run_id, session_id, age_s):
    store.reserve_run(key, "call1", key, 1)
    store.progress(key, "request", "fix the bug")
    store.update_run(key, run_id, "running")
    store.set_session(key, session_id)
    store.update_run(key, None, "ambiguous")  # the stream dropped before the run's final event
    with store._lock, store._db:
        store._db.execute("UPDATE runs SET updated=?, created=? WHERE idem_key=?",
                          (time.time() - age_s, time.time() - age_s - 60, key))


def test_an_unconfirmed_run_settles_from_its_session_once_it_goes_quiet(tmp_path):
    """The stream dropped mid-task and the run finished in its session minutes later, but the board
    showed it Stopped for hours and the x couldn't clear it."""
    db = tmp_path / "state.db"
    hermes_db(db, [("s6", None, [("user", "rename the files", None), ("assistant", "", '[{"id": "x"}]'),
                                ("tool", "{}", None), ("assistant", "Renamed all twelve files.", None)])])
    store = StateStore(tmp_path / "se.sqlite3")
    unconfirmed_run(store, "k6", "run_six", "s6", age_s=60)
    assert recovery.sweep(store, db, lambda: (), {}) == []  # still quiet for under five minutes: leave it
    unconfirmed_run(store, "k6", "run_six", "s6", age_s=10 * 60)
    assert recovery.sweep(store, db, lambda: (), {}) == [("k6", "completed")]
    assert status(store, "k6") == "completed"
    assert "twelve files" in store.work(idem_key="k6")["result"]["full"]
    assert recovery.sweep(store, db, lambda: (), {}) == []


def test_an_unconfirmed_run_without_an_answer_is_closed_and_a_superseded_one_at_once(tmp_path):
    db = tmp_path / "state.db"
    hermes_db(db, [("s7", None, [("user", "go", None), ("assistant", "", '[{"id": "x"}]')]),
                   ("s8", None, [("user", "first", None), ("assistant", "Later reply.", None)])])
    store = StateStore(tmp_path / "se.sqlite3")
    unconfirmed_run(store, "recent", "run_r", "s7", age_s=30 * 60)
    unconfirmed_run(store, "old", "run_o", "s7x", age_s=4 * 3600)
    unconfirmed_run(store, "first", "run_f", "s8", age_s=10 * 60)
    store.reserve_run("second", "call1", "second", 1)  # a follow-up in the same session owns its reply
    store.set_session("second", "s8")
    out = dict(recovery.sweep(store, db, lambda: (), {}))
    assert out == {"old": "interrupted", "first": "interrupted"}
    assert status(store, "recent") == "ambiguous"
    assert recovery.UNCONFIRMED_NOTE in [e["text"] for e in store.work(idem_key="old")["events"]]


def test_an_unconfirmed_run_can_be_dismissed(tmp_path):
    store = StateStore(tmp_path / "se.sqlite3")
    unconfirmed_run(store, "k9", "run_nine", "s9", age_s=60)
    assert store.dismiss(["run_nine"]) == ["run_nine"]


class FakeHermes:
    """Hermes' run records (GET /v1/runs/{id}); a run id not in *runs* raises like an unreachable server."""

    def __init__(self, runs):
        self.runs, self.asked = runs, []

    def get_run(self, run_id):
        self.asked.append(run_id)
        if run_id not in self.runs:
            raise RuntimeError("unreachable")
        return self.runs[run_id]


def test_a_reloaded_away_run_settles_from_hermes_while_its_thread_goes_on(tmp_path):
    """A plugin reload dropped the follower of a task that kept working; the reload's sweep asked
    Hermes once (still running) and left it unconfirmed. The run finished, the thread it was
    started from went on with unrelated turns, and the card still read Working underneath them:
    the next pass asks Hermes again and settles it with the task's own answer at once."""
    db = tmp_path / "state.db"
    hermes_db(db, [("task_s", None, [("user", "fix the gap", None), ("assistant", "", '[{"id": "x"}]'),
                                     ("tool", "{}", None), ("assistant", "Gap fixed; tests pass.", None)]),
                   ("thread_s", "t1", [("user", "how is build 70?", None), ("assistant", "Build 70 status.", None)])])
    store = StateStore(tmp_path / "se.sqlite3")
    unconfirmed_run(store, "old", "run_old", "task_s", age_s=30)  # the session only just went quiet
    store.link_history("old", "thread_s")
    hermes = FakeHermes({"run_old": {"status": "running"}})
    assert recovery.sweep(store, db, lambda: (), {}, hermes=hermes) == []  # still working: left alone
    assert status(store, "old") == "ambiguous"
    hermes.runs["run_old"] = {"status": "completed", "output": "Gap fixed; tests pass."}
    assert recovery.sweep(store, db, lambda: (), {}, hermes=hermes) == [("old", "completed")]
    work = store.work(idem_key="old")
    assert work["status"] == "completed" and "Gap fixed" in work["result"]["full"]
    assert "Build 70" not in work["result"]["full"]  # never the thread's newer, unrelated reply
    assert not work.get("now_line") and not work.get("quiet_s")  # no "No new steps · N min" any more
    assert recovery.sweep(store, db, lambda: (), {}, hermes=hermes) == []  # settled once


def test_an_unconfirmed_run_takes_each_final_status_from_hermes(tmp_path):
    db = tmp_path / "state.db"
    hermes_db(db, [])
    store = StateStore(tmp_path / "se.sqlite3")
    finals = {"run_f": {"status": "failed", "error": "boom"}, "run_c": {"status": "cancelled"},
              "run_i": {"status": "interrupted"}, "run_d": {"status": "completed", "output": "Done it."}}
    for i, run_id in enumerate(finals):
        unconfirmed_run(store, f"k{i}", run_id, f"s{i}", age_s=30)
    out = dict(recovery.sweep(store, db, lambda: (), {}, hermes=FakeHermes(finals)))
    assert out == {"k0": "failed", "k1": "cancelled", "k2": "interrupted", "k3": "completed"}
    assert store.work(idem_key="k3")["result"]["full"] == "Done it."


def test_a_still_running_or_unreachable_run_falls_back_to_its_session(tmp_path):
    """Hermes says it is still working (a retry, a live worker) or can't be asked: nothing is
    settled from Hermes; the session sweep keeps its own quiet rule."""
    db = tmp_path / "state.db"
    hermes_db(db, [("s_live", None, [("user", "go", None), ("assistant", "", '[{"id": "x"}]')]),
                   ("s_gone", None, [("user", "go", None), ("assistant", "All done.", None)])])
    store = StateStore(tmp_path / "se.sqlite3")
    unconfirmed_run(store, "live", "run_live", "s_live", age_s=10 * 60)
    unconfirmed_run(store, "gone", "run_gone", "s_gone", age_s=10 * 60)
    hermes = FakeHermes({"run_live": {"status": "running"}})  # run_gone: Hermes can't be reached
    assert recovery.sweep(store, db, lambda: (), {}, hermes=hermes) == [("gone", "completed")]
    assert status(store, "live") == "ambiguous"
    assert sorted(hermes.asked) == ["run_gone", "run_live"]


def test_a_run_settled_meanwhile_is_not_overwritten_from_hermes(tmp_path):
    db = tmp_path / "state.db"
    hermes_db(db, [])
    store = StateStore(tmp_path / "se.sqlite3")
    unconfirmed_run(store, "k", "run_k", "s_k", age_s=30)
    store.transition("k", "cancelled", expect={"ambiguous"})  # the user stopped it meanwhile
    hermes = FakeHermes({"run_k": {"status": "completed", "output": "late"}})
    assert recovery.sweep(store, db, lambda: (), {}, hermes=hermes) == []
    assert status(store, "k") == "cancelled"
