"""Job progress fields and a plain failure reason on every task (docs/API.md "Job progress")."""
import time

import pytest

from speakeasy import failures
from speakeasy.store import StateStore, job_progress


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.sqlite3", tmp_path)


def _task(store, key="k1", status="running", plan=None, milestones=(), error_kind=None, hint=None):
    store.reserve_run(key, "vi_x", "call_" + key, 1)
    if plan:
        store.set_plan(key, plan)
    for text in milestones:
        store.progress(key, "milestone", text)
    store.update_run(key, None, status)
    if status == "failed":
        store.set_failure(key, error_kind, hint)
    return store.work(idem_key=key)


def test_a_running_task_reports_its_start_never_a_plan_step(store):
    # The opening plan is a handoff guess: at most "Started with", never a step number or a plan to tick.
    work = _task(store, plan=["Find PRs", "Read reviews", "Summarize"], milestones=["Started working", "Found 21 PRs"])
    assert work["started_at"] and abs(work["started_at"] - time.time()) < 5
    assert work["finished_at"] is None
    assert (work["step_index"], work["step_count"]) == (None, None) and work["plan"] == []
    assert work["started_with"] == "Find PRs; Read reviews; Summarize"
    assert work["eta_seconds"] is None                  # one step done is no basis for a guess
    assert work["failure_reason"] is None


def test_a_finished_task_has_its_end_and_no_step_counts(store):
    work = _task(store, status="completed", plan=["Find PRs", "Read reviews", "Summarize"])
    assert work["finished_at"] and work["step_index"] is None and work["step_count"] is None


def test_no_plan_means_no_step_numbers(store):
    work = _task(store)
    assert work["step_index"] is None and work["step_count"] is None and work["eta_seconds"] is None


def test_eta_only_from_a_measured_pace():
    events = [{"kind": "milestone", "text": "one"}, {"kind": "milestone", "text": "two"}]
    out = job_progress("running", 1000.0, None, ["A", "B", "C", "D"], events, now=1060.0)
    assert out["step_index"] == 3 and out["eta_seconds"] == 60   # 2 steps in 60 s, 2 to go


@pytest.mark.parametrize("kind,hint,expected", [
    ("billing", None, "Your Hermes's AI account is out of credits."),
    (failures.UNKNOWN, None, failures.NO_REASON),
    (failures.UNKNOWN, "timeout", "It timed out waiting on Hermes's model provider."),
    (None, "provider_error", "Hermes's model provider had a server error."),
    (failures.UNKNOWN, "start", "Speakeasy couldn't start it on Hermes."),
])
def test_a_failed_task_says_why_in_one_plain_sentence(store, kind, hint, expected):
    work = _task(store, status="failed", error_kind=kind, hint=hint)
    assert work["failure_reason"] == expected
    assert "unknown" not in work["failure_reason"].lower()


def test_error_shapes_map_to_hints_without_keeping_their_words():
    assert failures.failure_hint("HTTP 500: Internal server error") == "provider_error"
    assert failures.failure_hint("Request timed out after 600s") == "timeout"
    assert failures.failure_hint("HTTP 403 — Attention Required! | Cloudflare") == "blocked"
    assert failures.failure_hint("Hermes can't reach the model provider. You may be offline.") == "offline"
    assert failures.failure_hint("agent run failed") is None


def test_a_task_lost_to_a_restart_says_so(store):
    work = _task(store, status="interrupted")
    assert work["failure_reason"] == failures.INTERRUPTED_REASON
