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


def test_the_side_model_is_off_unless_the_user_picked_one():
    """Left on "auto" the side model is the user's MAIN model: no summaries at all, whatever reads."""
    calls = []
    s = L.Summarizer(lambda m: calls.append(m) or None, clock=lambda: 1000.0, spawn=lambda fn: fn())
    st = state(item(1, "Read SessionPage.swift", "read", 990.0))
    for _ in range(5):
        assert s.line("k", st, request="x", started=900.0, ask=True)["source"] == "activity"
    assert calls == []
    from speakeasy.router import side_model_picked
    assert side_model_picked({}) is False
    assert side_model_picked({"auxiliary": {"speakeasy_router": {"provider": "auto"}}}) is False
    assert side_model_picked({"auxiliary": {"speakeasy_router": {"provider": "auto", "model": " "}}}) is False
    assert side_model_picked({"auxiliary": {"speakeasy_router": {"provider": "openrouter", "model": "small"}}}) is True
    assert side_model_picked({"auxiliary": {"speakeasy_router": {"model": "local-small"}}}) is True


def test_background_reads_never_ask_the_side_model():
    """The app's liveness loop and every push read the card: only a real open (ask=True) may ask."""
    calls = []
    clock = [1000.0]
    s = L.Summarizer(lambda m: calls.append(m) or None, clock=lambda: clock[0], spawn=lambda fn: fn(),
                     enabled=lambda: True)
    st = state(item(1, "Read SessionPage.swift", "read", 990.0))
    for i in range(30):  # ten minutes of a 20 s liveness loop, steps piling up
        clock[0] += 20
        st["items"].append(item(i + 2, f"Read file{i}.swift", "read", clock[0] - 1))
        s.line("k", st, request="x", started=900.0)
    assert calls == []


def test_the_side_model_runs_once_per_phase_and_under_an_hourly_cap():
    calls = []
    clock = [1000.0]
    s = L.Summarizer(lambda m: calls.append(m) or None, clock=lambda: clock[0], spawn=lambda fn: fn(),
                     enabled=lambda: True)
    st = state(item(1, "Read SessionPage.swift", "read", 990.0))
    s.line("k", st, request="x", started=900.0, ask=True)
    assert len(calls) == 1
    clock[0] += 120
    st["items"].append(item(2, "Read History.swift", "read", clock[0] - 1))
    s.line("k", st, request="x", started=900.0, ask=True)
    assert len(calls) == 1                     # one new step is the same phase
    for n in (3, 4):
        st["items"].append(item(n, f"Edited file{n}.swift", "edit", clock[0] - 1))
    s.line("k", st, request="x", started=900.0, ask=True)
    assert len(calls) == 2                     # three new steps since the last ask: a new phase
    # Many tasks opened over and over: never more than HOURLY_CAP asks in any hour, all tasks together.
    at = []
    s.call = lambda m: at.append(clock[0]) or None
    for t in range(200):
        clock[0] += 61
        st2 = state(*[item(n, f"Read f{n}.swift", "read", clock[0] - 1) for n in range(1, 4 + 3 * t)])
        s.line(f"t{t % 9}", st2, request="x", started=clock[0] - 50, ask=True)
    assert at and max(sum(1 for b in at if a <= b < a + 3600) for a in at) <= L.HOURLY_CAP


def test_the_side_model_words_it_rate_limited_and_vetted():
    calls = []
    clock = [1000.0]

    def model(messages):
        calls.append(messages)
        return '{"now": "Reading the session page code to find why old messages show last", "so_far": "Found the cause; fixing it"}'

    s = L.Summarizer(model, clock=lambda: clock[0], spawn=lambda fn: fn(), enabled=lambda: True)
    st = state(item(1, "Read SessionPage.swift", "read", 990.0))
    first = s.line("k", st, request="Why do old messages show last?", started=900.0, ask=True)
    assert len(calls) == 1 and first["source"] == "activity"        # asked; this read uses the steps
    again = s.line("k", st, request="Why do old messages show last?", started=900.0)
    assert again["now"].startswith("Reading the session page code") and again["source"] == "model"
    assert len(calls) == 1                                           # nothing new: not asked again
    for n in (2, 3, 4):
        st["items"].append(item(n, f"Read History{n}.swift", "read", 1010.0))
    clock[0] = 1020.0
    s.line("k", st, request="", started=900.0, ask=True)
    assert len(calls) == 1                                           # under a minute since the last ask
    clock[0] = 1065.0
    s.line("k", st, request="", started=900.0, ask=True)
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


# -- Home's step capsule (build 73 home-fixes) ----------------------------------------------------

def test_step_kind_names_what_the_call_touches_then_the_kind_of_work():
    k = L.step_kind
    assert k({"text": "Ran gws gmail search flight", "kind": "run", "tool": "terminal"}) == "mail"
    assert k({"text": "Used google calendar list events", "kind": "tool", "tool": "mcp__gcal__list"}) == "calendar"
    assert k({"text": "Read www.example.com", "kind": "browse", "tool": "web_extract"}) == "web"
    assert k({"text": "Searched the web for “cabins”", "kind": "search", "tool": "web_search"}) == "web"
    assert k({"text": "Ran the tests", "kind": "run", "tool": "terminal"}) == "terminal"
    assert k({"text": "Read beds.md", "kind": "read", "tool": "read_file"}) == "doc"
    assert k({"text": "Edited beds.py", "kind": "edit", "tool": "patch"}) == "edit"
    assert k({"text": "Searched the files for “seed”", "kind": "search", "tool": "search_files"}) == "search"
    assert k({"text": "Thinking it over", "kind": "think"}) == "think"
    assert k({"text": "Handed part of the work to a helper", "kind": "tool", "tool": "delegate_task"}) == "helper"
    # Reading notes ABOUT email, or a file called calendar.md, is still reading a file.
    assert k({"text": "Read its notes on email-operations", "kind": "read", "tool": "skill_view"}) == "doc"
    assert k({"text": "Read calendar.md", "kind": "read", "tool": "read_file"}) == "doc"
    assert k({"text": "", "kind": "read"}) is None and k(None) is None


def test_home_steps_are_the_newest_plain_words_oldest_first_without_arguments():
    acts = [{"id": f"a{n}", "text": f"Read file{n}.md", "kind": "read", "tool": "read_file", "at": 100.0 + n,
             "args": "secret-arg", "status": "done"} for n in range(12)]
    acts[-1].update(status="running", started_at=111.5)
    steps = L.home_steps(acts)
    assert len(steps) == L.HOME_STEPS and steps[0]["text"] == "Read file4.md" and steps[-1]["text"] == "Read file11.md"
    assert steps[-1] == {"text": "Read file11.md", "kind": "doc", "at": 111.5, "running": True}
    assert all(set(s) <= {"text", "kind", "at", "running"} for s in steps)
    assert L.home_steps(None) == [] and L.home_steps([{"kind": "read"}]) == []
