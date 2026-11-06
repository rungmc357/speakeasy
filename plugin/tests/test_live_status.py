"""The live line: what a running task is doing, from its real steps, never its opening plan (live_status.py)."""
from speakeasy import live_status as L
from speakeasy.store import StateStore


def item(n, text, kind, at, *, status="done", ended=None, started=None):
    out = {"id": f"a{n}", "text": text, "kind": kind, "at": at, "status": status}
    if ended is not None:
        out["ended_at"] = ended
    if started is not None:
        out["started_at"] = started
    return out


def state(*items, findings=()):
    return {"v": 1, "next": len(items) + 1, "next_f": 1, "items": list(items), "findings": list(findings),
            "touched": [], "edits": {}, "sources": [], "trials": None, "diff_preview": None}


def test_plan_mismatch_the_line_follows_the_work_not_the_plan(tmp_path):
    # The plan said "Find why the export stalls / Trace the stuck state / ..."; the task is editing and testing.
    store = StateStore(tmp_path / "s.sqlite3", tmp_path)
    store.reserve_run("k1", "vi_x", "call_k1", 1)
    store.set_plan("k1", ["Find why the export stalls", "Trace the stuck state in the queue", "Fix the queue and rerun the export"])
    store.update_run("k1", "run_0001", "running")
    import time
    now = time.time()
    store.set_activity("k1", state(item(1, "Read a file", "read", now - 50),
                                   item(2, "Edited store.py", "edit", now - 30),
                                   item(3, "Edited router.py", "edit", now - 10)))
    store.handoff_status("k1", "Investigating the stuck state", "Handed over: why is it stuck?")
    work = store.work(idem_key="k1")
    assert work["now_line"] == "Editing store.py and router.py"
    assert work["short_status"] == "Editing store.py and router.py"     # not the handoff guess
    assert "Trace the stuck state" not in work["short_status"] and work["plan"] == []
    assert work["step_index"] is None and work["started_with"].startswith("Find why the export stalls")
    assert work["so_far"] == "Read a file, edited store.py and router.py"


def test_a_long_single_call_says_what_it_waits_on_and_for_how_long():
    now = 10_000.0
    got = L.deterministic(state(item(1, "Edited store.py", "edit", now - 400),
                                item(2, "Ran the tests", "run", now - 250, status="running", started=now - 250)), now=now)
    assert got["now"] == "Waiting on the tests \u00b7 4 min" and got["source"] == "activity"
    assert got["so_far"] == "Edited store.py, ran the tests"


def test_an_idle_spell_is_said_honestly():
    now = 10_000.0
    got = L.deterministic(state(item(1, "Searched the code", "search", now - 300, ended=now - 290)), now=now)
    assert got["now"] == "No new steps \u00b7 5 min" and got["quiet_s"] == 290 and got["source"] == "idle"


def test_no_steps_after_a_minute_stops_showing_the_handoff_guess():
    assert L.deterministic(state(), now=1000.0, started=980.0) is None              # still honest
    got = L.deterministic(state(), now=1000.0, started=850.0)
    assert got["now"] == "No steps reported yet \u00b7 2 min"


def test_recent_tests_after_edits_read_as_one_line():
    now = 5000.0
    got = L.deterministic(state(item(1, "Edited calls.py", "edit", now - 40), item(2, "Ran the tests", "run", now - 5)), now=now)
    assert got["now"] == "Running the tests after editing calls.py"


def test_the_side_model_words_it_rate_limited_and_vetted():
    calls = []
    clock = [1000.0]

    def model(messages):
        calls.append(messages)
        return '{"now": "Reading the session page code to find why old messages show last", "so_far": "Found the cause; fixing it"}'

    s = L.Summarizer(model, clock=lambda: clock[0], spawn=lambda fn: fn())
    st = state(item(1, "Read SessionPage.swift", "read", 990.0))
    first = s.line("k", st, request="Why do old messages show last?", started=900.0)
    assert len(calls) == 1 and first["source"] == "activity"        # asked; this read uses the steps
    again = s.line("k", st, request="Why do old messages show last?", started=900.0)
    assert again["now"].startswith("Reading the session page code") and again["source"] == "model"
    assert len(calls) == 1                                           # nothing new: not asked again
    st["items"].append(item(2, "Read History.swift", "read", 1010.0))
    clock[0] = 1020.0
    s.line("k", st, request="", started=900.0)
    assert len(calls) == 1                                           # under 40 s since the last ask
    clock[0] = 1045.0
    s.line("k", st, request="", started=900.0)
    assert len(calls) == 2
    # no tool names, no step counts, no paths from the model
    assert L.parse_model('{"now": "Running read_file on the code"}') is None
    assert L.parse_model('{"now": "Working on step 2 of 4"}') is None
    assert L.parse_model('{"now": "Reading /Users/someone/x.py"}') is None


def test_the_model_never_words_a_quiet_spell():
    s = L.Summarizer(lambda m: '{"now": "Busy fixing everything", "so_far": ""}', clock=lambda: 10_000.0,
                     spawn=lambda fn: fn())
    got = s.line("k", state(item(1, "Read a file", "read", 9_000.0)), request="", started=8_000.0)
    assert got["source"] == "idle" and got["now"].startswith("No new steps")
