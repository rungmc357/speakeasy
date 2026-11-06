"""Clear done must clear every finished task, including ones Hermes ran without a run id."""
from __future__ import annotations

import sqlite3

from speakeasy.store import StateStore, local_run_id


def test_quick_answer_without_run_id_gets_a_clearable_id(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.reserve_run("se_quick", "vi_1", "item_quick", 1)
    store.update_run("se_quick", None, "running")
    assert store.work(idem_key="se_quick")["run_id"] is None  # still running: no id needed yet
    store.update_run("se_quick", None, "completed")
    run_id = store.work(idem_key="se_quick")["run_id"]
    assert run_id == local_run_id("se_quick")
    assert store.dismiss([run_id]) == [run_id]
    assert store.work(idem_key="se_quick")["dismissed"]


def test_a_finished_run_is_recorded_whole_and_once(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    result = {"spoken": "Short or long?", "question": {"question": "Short or long?"}}
    assert store.record_finished_run("se_pick", "vi_1", "choice_1", "Short or long?", result)
    work = store.work(idem_key="se_pick")
    assert work is not None and work["status"] == "completed" and work["run_id"] == local_run_id("se_pick")
    assert work["title"] == "Short or long?" and work["result"]["question"]["question"] == "Short or long?"
    assert not store.record_finished_run("se_pick", "vi_1", "choice_1", "Other", None)
    assert store.work(idem_key="se_pick")["title"] == "Short or long?"


def test_real_run_id_is_kept(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    store.reserve_run("se_real", "vi_1", "item_real", 1)
    store.update_run("se_real", "run_abc", "running")
    store.update_run("se_real", None, "completed")
    assert store.work(idem_key="se_real")["run_id"] == "run_abc"


def test_existing_finished_rows_are_backfilled_on_open(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = StateStore(path)
    store.reserve_run("se_old", "vi_1", "item_old", 1)
    store.reserve_run("se_live", "vi_1", "item_live", 1)
    store.update_run("se_live", None, "running")
    with sqlite3.connect(path) as db:  # a row written by an older version
        db.execute("UPDATE runs SET run_id=NULL, status='completed' WHERE idem_key='se_old'")
    reopened = StateStore(path)
    assert reopened.work(idem_key="se_old")["run_id"] == local_run_id("se_old")
    assert reopened.work(idem_key="se_live")["run_id"] is None
