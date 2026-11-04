"""Live job activity (docs/API.md "Live job activity", capability job_activity). Invented data only."""
import json
import threading
import time

import pytest

from fakes import http, wait_for
from speakeasy import activity as A
from speakeasy.store import StateStore
from test_project_fence import run_of, say
from test_routing import start_call


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "state.sqlite3", tmp_path)


def _run(events, start=1000.0):
    job = A.JobActivity()
    for i, event in enumerate(events):
        job.observe(event, now=start + i)
    return job, A.public(job.state)


def started(tool, preview, call_id=None):
    event = {"event": "tool.started", "tool": tool, "preview": preview}
    if call_id:
        event["tool_call_id"] = call_id
    return event


# -- humanizer: one row per tool kind ---------------------------------------------------------------

@pytest.mark.parametrize("tool,preview,kind,text", [
    ("read_file", "checkout.spec.ts L10-40", "read", "Read checkout.spec.ts"),
    ("read_file", "/srv/projects/shop/tests/e2e/checkout.spec.ts", "read", "Read checkout.spec.ts"),
    ("write_file", "/srv/projects/shop/src/mock.ts", "edit", "Wrote mock.ts"),
    ("patch", "...hop/tests/e2e/checkout.spec.ts", "edit", "Edited checkout.spec.ts"),
    ("search_files", "waitForPayment", "search", "Searched the files for “waitForPayment”"),
    ("web_search", "playwright wait for network idle", "search", "Searched the web for “playwright wait for network idle”"),
    ("web_extract", "https://docs.example.com/guide/waiting?ref=abc", "browse", "Read docs.example.com/guide/waiting"),
    ("browser_navigate", "https://www.example.org/", "browse", "Opened example.org"),
    ("browser_click", "@e12", "browse", "Clicked on the page"),
    ("browser_type", "correct horse battery staple", "browse", "Typed into the page"),
    ("terminal", "npx playwright test checkout.spec.ts --repeat-each=50", "run", "Ran the tests"),
    ("terminal", "pytest -q tests/test_cart.py", "run", "Ran the tests"),
    ("terminal", "git status", "run", "Checked git status"),
    ("terminal", "git diff --stat", "run", "Looked at the changes"),
    ("terminal", "npm run build", "run", "Built the project"),
    ("terminal", "pip install requests", "run", "Installed packages"),
    ("terminal", "rg -n waitForPayment src", "search", "Searched the code"),
    ("terminal", "ls -la", "search", "Looked through the folders"),
    ("terminal", "node scripts/seed.js", "run", "Ran node seed.js"),
    ("execute_code", "import json; print(1)", "run", "Ran a script"),
    ("vision_analyze", "what's on screen", "read", "Looked at an image"),
    ("delegate_task", "Compare the two mocks", "tool", "Handed part of the work to a helper"),
    ("todo_list", "3 items", "think", "Updated its to-do list"),
    ("skill_view", "release-notes", "read", "Read its notes on release-notes"),
    ("clarify", "Which branch?", "message", "Asked you a question"),
    ("mcp__tracker__create_issue", "{\"title\": \"x\"}", "tool", "Used tracker create issue"),
])
def test_each_tool_kind_becomes_one_plain_sentence(tool, preview, kind, text):
    out = A.humanize(tool, preview)
    assert (out["kind"], out["text"]) == (kind, text)
    assert out["kind"] in A.KINDS and len(out["text"]) <= A.TEXT_CHARS


def test_no_tool_name_or_thinking_is_no_item():
    assert A.humanize(None, "x") is None and A.humanize("", "x") is None
    assert A.humanize("_thinking", "secret plan") is None


def test_touched_comes_from_files_urls_and_commands():
    _, out = _run([started("read_file", "cart.ts"), started("patch", "/srv/app/cart.ts"),
                   started("web_extract", "https://docs.example.com/a"), started("terminal", "npm test"),
                   started("read_file", "cart.ts")])
    assert [(t["kind"], t["label"]) for t in out["touched"]] == [   # most recently touched last
        ("url", "docs.example.com/a"), ("command", "npm test"), ("file", "cart.ts")]
    cart = next(t for t in out["touched"] if t["label"] == "cart.ts")
    assert cart["detail"] == "edited"                       # edited, counts not known
    url = next(t for t in out["touched"] if t["kind"] == "url")
    assert url["url"] == "https://docs.example.com/a"
    assert len({(t["kind"], t["label"]) for t in out["touched"]}) == len(out["touched"])   # de-duplicated


# -- redaction ------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,gone,kept", [
    ("OPENAI_API_KEY=sk-test1234567890abcdef npx playwright test", ["sk-test", "1234567890abcdef"], ["OPENAI_API_KEY", "playwright"]),
    ("export GITHUB_TOKEN=ghp_FAKEFAKEFAKEFAKEFAKE12 && make", ["ghp_"], ["GITHUB_TOKEN"]),
    ("curl -H 'Authorization: Bearer abc.def.ghi' https://api.example.com/v1/items?api_key=zzz", ["abc.def", "zzz", "api_key="],
     ["api.example.com/v1/items"]),
    ("deploy --token s3cr3tvalue --region east", ["s3cr3tvalue"], ["--region east"]),
    ("password: hunter2 for the staging box", ["hunter2"], ["staging box"]),
    ("cat /home/someone/work/shop/src/payments/mock_server.py", ["/home", "someone", "payments/"], ["mock_server.py"]),
    ("vim ~/Code/shop/.env", ["~/Code", ".env"], ["a private file"]),
    ("email pat.doe@example.com about it", ["pat.doe@example.com"], ["an email address"]),
    ("session d41d8cd98f00b204e9800998ecf8427e", ["d41d8cd98f00b204e9800998ecf8427e"], ["session"]),
    ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl", ["eyJhbGci"], []),
    ("aws AKIAABCDEFGHIJKLMNOP s3 ls", ["AKIAABCDEFGHIJKLMNOP"], ["s3 ls"]),
])
def test_secrets_tokens_env_values_and_long_paths_are_redacted(raw, gone, kept):
    out = A.redact(raw)
    for g in gone:
        assert g not in out, out
    for k in kept:
        assert k in out, out


def test_redacted_commands_still_reach_the_feed_but_nothing_secret_does():
    secret = "sk-live-FAKE0123456789abcdef"
    _, out = _run([started("terminal", f"STRIPE_KEY={secret} npm test -- checkout"),
                   {"event": "message.interim", "text": f"The key {secret} was wrong. The mock needs a fixed port."}])
    dumped = json.dumps(out)
    assert secret not in dumped and "FAKE0123" not in dumped
    assert out["activity"][0]["text"] == "Ran the tests"
    assert "STRIPE_KEY=…" in out["activity"][0]["detail"]


def test_paths_never_reach_the_feed_whole():
    _, out = _run([started("read_file", "/Users/someone/Code/private-thing/src/deep/a/b/c/file_with_a_long_name.ts")])
    dumped = json.dumps(out)
    assert "/Users" not in dumped and "someone" not in dumped and "private-thing" not in dumped
    assert out["activity"][0]["text"] == "Read file_with_a_long_name.ts"


def test_raw_json_and_long_text_are_kept_out():
    _, out = _run([started("web_search", '{"query": "x", "nested": {"a": 1}}'),
                   started("terminal", "python3 -c 'print(" + "x" * 300 + ")'")])
    for item in out["activity"]:
        assert '{"' not in item["text"] and len(item["text"]) <= A.TEXT_CHARS
        assert len(item.get("detail") or "") <= A.DETAIL_CHARS


# -- caps -----------------------------------------------------------------------------------------

def test_sizes_are_capped():
    events = [started("read_file", f"file_{i}.ts") for i in range(100)]
    events += [{"event": "message.interim", "text": f"The cache entry {i} expires after ten minutes, not one hour as documented "
                                                    f"in the readme which is wrong and has been for a while now honestly {i}."}
               for i in range(30)]
    _, out = _run(events)
    assert len(out["activity"]) == A.MAX_ACTIVITY == 40
    assert out["activity"][-1]["text"] == "Read file_99.ts"          # newest last
    assert len(out["findings"]) == A.MAX_FINDINGS == 8
    assert all(len(f["text"]) <= A.FINDING_CHARS for f in out["findings"])
    assert len(out["touched"]) == A.MAX_TOUCHED == 20
    assert len(json.dumps(out)) < 12000                                  # stays small


def test_ids_are_unique_and_stable():
    _, out = _run([started("read_file", f"f{i}.ts") for i in range(50)])
    ids = [i["id"] for i in out["activity"]]
    assert len(set(ids)) == len(ids) and ids[-1] == "a50"


# -- statuses ---------------------------------------------------------------------------------------

def test_a_tool_runs_then_finishes_or_fails_matched_by_call_id():
    job, out = _run([started("terminal", "npm test", "c1"), started("read_file", "a.ts", "c2")])
    assert [i["status"] for i in out["activity"]] == ["running", "running"]
    job.observe({"event": "tool.completed", "tool": "terminal", "tool_call_id": "c1", "error": True})
    job.observe({"event": "tool.completed", "tool": "read_file", "tool_call_id": "c2"})
    assert [i["status"] for i in A.public(job.state)["activity"]] == ["failed", "done"]


def test_unfinished_items_settle_with_the_run():
    job, _ = _run([started("terminal", "npm test")])
    job.observe({"event": "run.completed"})
    assert A.public(job.state)["activity"][0]["status"] == "done"
    job2, _ = _run([started("terminal", "npm test")])
    job2.observe({"event": "run.cancelled"})
    assert A.public(job2.state)["activity"][0]["status"] == "failed"


def test_thinking_shows_as_one_item_without_its_text():
    _, out = _run([{"event": "reasoning.available", "text": "private chain of thought about the secret plan"},
                   {"event": "reasoning.available", "text": "more"}])
    assert [i["text"] for i in out["activity"]] == ["Thinking it over"]
    assert "secret plan" not in json.dumps(out)


def test_status_lines_are_messages_and_intent_is_not_a_finding():
    _, out = _run([{"event": "message.interim", "text": "STATUS: Rerunning the checkout test\nDETAIL: 50 runs to measure it"},
                   {"event": "message.interim", "text": "Let me look at the payment mock next."}])
    assert out["activity"][0] == {**out["activity"][0], "kind": "message", "text": "Rerunning the checkout test",
                                  "detail": "50 runs to measure it"}
    assert "findings" not in out


def test_findings_come_only_from_the_agents_own_words():
    _, out = _run([{"event": "message.interim",
                    "text": "Found it. The test doesn't wait for the payment mock, so it fails about 1 in 6 runs."},
                   {"event": "message.interim", "text": "FINDING: The mock server starts on a random port"}])
    assert [f["text"] for f in out["findings"]] == [
        "The test doesn't wait for the payment mock, so it fails about 1 in 6 runs", "The mock server starts on a random port"]
    _, none = _run([started("read_file", "a.ts")])
    assert "findings" not in none                                         # no commentary, no findings


# -- hero ---------------------------------------------------------------------------------------------

def test_trials_hero_from_a_reported_repeated_run():
    _, out = _run([started("terminal", "npx playwright test --repeat-each=50", "c1"),
                   {"event": "message.interim", "text": "31/50 passed so far, 0 failed."}])
    assert out["hero"] == {"type": "trials", "total": 50, "passed": 31, "failed": 0}
    job, _ = _run([started("terminal", "for i in $(seq 50); do npm test; done", "c1")])
    job.observe({"event": "tool.completed", "tool": "terminal", "tool_call_id": "c1",
                 "preview": json.dumps({"output": "...\nRun 50 of 50: 46/50 passed, 4 failed\n", "exit_code": 1})})
    assert A.public(job.state)["hero"] == {"type": "trials", "total": 50, "passed": 46, "failed": 4}


@pytest.mark.parametrize("text", ["= 12 passed, 1 failed in 3.2s =", "Step 3 of 5", "I read 2 of the 9 files",
                                  "60/50 passed", "1/1 passed"])
def test_no_trials_hero_without_a_clear_repeated_count(text):
    _, out = _run([{"event": "message.interim", "text": text}])
    assert (out.get("hero") or {}).get("type") != "trials"


def test_diff_hero_from_counted_changes():
    diff = "--- a/cart.ts\n+++ b/cart.ts\n@@ -1,3 +1,4 @@\n-await pay()\n+await mock.ready()\n+await pay()\n"
    job, _ = _run([started("patch", "/srv/shop/cart.ts", "p1")])
    job.observe({"event": "tool.completed", "tool": "patch", "tool_call_id": "p1",
                 "preview": json.dumps({"success": True, "diff": diff, "files_modified": ["/srv/shop/cart.ts"]})})
    out = A.public(job.state)
    assert out["hero"]["type"] == "diff" and (out["hero"]["files"], out["hero"]["added"], out["hero"]["removed"]) == (1, 2, 1)
    assert out["hero"]["preview"].splitlines()[0] == "- await pay()" and len(out["hero"]["preview"].splitlines()) <= 6
    assert next(t for t in out["touched"] if t["label"] == "cart.ts")["detail"] == "+2 \u22121"


def test_files_changed_events_give_cumulative_counts():
    _, out = _run([{"event": "files.changed", "files": [{"path": "/w/a.py", "added": 3, "removed": 1}]},
                   {"event": "files.changed", "files": [{"path": "/w/a.py", "added": 5, "removed": 2},
                                                        {"path": "/w/b.py", "added": 1, "removed": 0}]}])
    assert out["hero"] == {"type": "diff", "files": 2, "added": 6, "removed": 2}


def test_a_cut_patch_result_gives_no_counts():
    job, _ = _run([started("patch", "/srv/shop/cart.ts", "p1")])
    cut = json.dumps({"success": True, "diff": "+x\n" * 400})[:497] + "..."
    job.observe({"event": "tool.completed", "tool": "patch", "tool_call_id": "p1", "preview": cut})
    out = A.public(job.state)
    assert out.get("hero", {}).get("type") != "diff"
    assert next(t for t in out["touched"] if t["label"] == "cart.ts")["detail"] == "edited"


def test_sources_hero_for_reading_without_edits():
    _, out = _run([started("web_search", "best mock pattern"), started("web_extract", "https://docs.example.com/a"),
                   started("web_extract", "https://blog.example.net/b")])
    assert out["hero"] == {"type": "sources", "items": [{"title": "docs.example.com/a", "site": "docs.example.com"},
                                                        {"title": "blog.example.net/b", "site": "blog.example.net"}]}
    _, edited = _run([started("web_extract", "https://docs.example.com/a"), started("web_extract", "https://blog.example.net/b"),
                      started("write_file", "notes.md")])
    assert edited["hero"] == {"type": "steps"}


def test_steps_or_nothing_otherwise():
    assert "hero" not in _run([started("read_file", "a.ts")])[1]
    assert _run([started("read_file", f"{n}.ts") for n in "abc"])[1]["hero"] == {"type": "steps"}
    assert A.public(None) == {} and A.public({"v": 99}) == {}


# -- throttle -------------------------------------------------------------------------------------

class CountingStore:
    def __init__(self):
        self.saves, self.state = [], None

    def activity_state(self, key):
        return self.state

    def set_activity(self, key, state):
        self.saves.append(time.monotonic())
        self.state = json.loads(json.dumps(state))


def test_saves_are_held_to_one_a_second_with_a_trailing_save(monkeypatch):
    monkeypatch.setattr(A, "FLUSH_EVERY_S", 0.3)
    store, flushed = CountingStore(), threading.Event()
    for i in range(20):
        A.observe(store, "k", started("read_file", f"f{i}.ts"), on_flush=flushed.set)
    assert len(store.saves) == 1                        # the first at once, the burst held back
    assert flushed.wait(2)                               # the held changes land, and get published
    assert len(store.saves) == 2 and len(store.state["items"]) == 20
    assert store.saves[1] - store.saves[0] >= 0.25


def test_the_terminal_event_saves_at_once():
    store = CountingStore()
    A.observe(store, "k", started("terminal", "npm test"))
    A.observe(store, "k", started("read_file", "a.ts"))
    assert len(store.saves) == 1
    A.observe(store, "k", {"event": "run.completed"})
    assert len(store.saves) == 2 and all(i["status"] == "done" for i in store.state["items"])


def test_unknown_events_save_nothing():
    store = CountingStore()
    assert not A.observe(store, "k", {"event": "message.delta", "delta": "hi"})
    assert store.saves == []


# -- persistence and older clients -----------------------------------------------------------------

def test_the_feed_survives_a_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "FLUSH_EVERY_S", 0)
    path = tmp_path / "state.sqlite3"
    first = StateStore(path, tmp_path)
    first.reserve_run("k1", "vi_x", "call_k1", 1)
    first.update_run("k1", "run_0001", "running")
    for event in [started("read_file", "cart.ts"), {"event": "message.interim", "text": "The mock starts after the test does."}]:
        A.observe(first, "k1", event)
    A.observe(first, "k1", {"event": "run.completed"})
    before = {k: first.work(idem_key="k1")[k] for k in ("activity", "findings", "touched")}
    first.close()
    again = StateStore(path, tmp_path)
    work = again.work("run_0001")
    assert {k: work[k] for k in ("activity", "findings", "touched")} == before
    # a resumed tracker continues the same feed, without reusing ids
    A.observe(again, "k1", started("read_file", "b.ts"))
    ids = [i["id"] for i in again.work("run_0001")["activity"]]
    assert ids == ["a1", "a2"]


def test_older_clients_see_the_same_work_object_plus_optional_fields(store):
    store.reserve_run("k1", "vi_x", "call_k1", 1)
    store.update_run("k1", "run_0001", "running")
    plain = store.work(idem_key="k1")
    assert not {"activity", "findings", "touched", "hero"} & set(plain)    # nothing known, nothing sent
    A.observe(store, "k1", started("read_file", "cart.ts"))
    rich = store.work(idem_key="k1")
    added = set(rich) - set(plain)
    assert added <= {"activity", "touched", "findings", "hero"} and "activity" in added
    for key in plain:
        if key not in {"updated", "updated_at"}:
            assert rich[key] == plain[key], key
    # an older client decoding only the fields it knows gets exactly what it got before
    old_view = {k: v for k, v in json.loads(json.dumps(rich)).items() if k in plain}
    assert set(old_view) == set(plain)


# -- end to end through a call ----------------------------------------------------------------------

def test_a_running_task_carries_its_activity_and_status_has_the_flag(server, service, hermes):
    status = http(server.base_url, "GET", "/voice/status", token=server.token)[1]
    assert status["capabilities"]["job_activity"] is True
    hermes.hold = True
    hermes.live_events = [
        {"event": "tool.started", "tool": "read_file", "preview": "checkout.spec.ts", "tool_call_id": "t1"},
        {"event": "tool.completed", "tool": "read_file", "tool_call_id": "t1", "duration": 0.1, "error": False},
        {"event": "message.interim", "text": "The test doesn't wait for the payment mock."},
        {"event": "tool.started", "tool": "terminal", "preview": "API_TOKEN=abc123secretvalue npm test", "tool_call_id": "t2"},
    ]
    _, worker = start_call(server, service)
    say(worker, "call_a", "Fix the flaky checkout test")
    run = run_of(worker, "call_a")

    def task():
        work = http(server.base_url, "GET", f"/voice/work/{run.run_id}", token=server.token)[1].get("work") or {}
        return work if len(work.get("activity") or []) >= 2 else None
    work = wait_for(task, timeout=10)
    assert [i["text"] for i in work["activity"]][:2] == ["Read checkout.spec.ts", "Ran the tests"]
    assert work["findings"][0]["text"] == "The test doesn't wait for the payment mock"
    assert "abc123secretvalue" not in json.dumps(work)
    for r in hermes.runs.values():
        r["done"].set()
