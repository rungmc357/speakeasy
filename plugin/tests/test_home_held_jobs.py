"""A Home job held behind another job editing the same project is visible at once (its card says
"Waiting · after <job>"), the coordinator's reply says it will start after, "Start now anyway" starts
it in parallel, and plain questions never wait. Invented data only."""
from __future__ import annotations

import time

import pytest

from fakes import http, wait_for
from speakeasy import calls, fence
from test_home_chat import coordinator, post


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)
    monkeypatch.setattr(calls, "FENCE_POLL_S", 0.05)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "shopfront"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    return root


def items(server):
    return http(server.base_url, "GET", "/voice/now?tz=America/New_York", None, server.token)[1].get("items") or []


def cards(server):
    entries = http(server.base_url, "GET", "/voice/now?tz=America/New_York", None, server.token)[1]["stream"]["entries"]
    return [e for e in entries if e["kind"] == "card"]


def home_job(server, service, words, said, cid, reply="Fixing it."):
    coordinator(service, {"tool": "start_job", "text": "", "args": {"request": words, "reply": reply}})
    t0 = time.monotonic()
    status, body = post(server, {"text": said, "client_id": cid})
    assert status == 200, body
    return body, time.monotonic() - t0


def two(server, service, hermes, repo, said=None, words=None):
    hermes.hold = True
    first, _ = home_job(server, service, f"Fix the checkout bug in {repo}", f"fix the checkout bug in {repo}",
                        "c_first", reply="Fixing the checkout bug.")
    wait_for(lambda: len(hermes.calls) == 1)
    second, took = home_job(server, service, words or f"Refactor the settings page in {repo}/src",
                            said or f"refactor the settings page in {repo}/src", "c_second",
                            reply="Refactoring the settings page.")
    return first, second, took


def test_a_held_home_job_shows_waiting_after_the_other_job_at_once(server, service, hermes, repo):
    first, second, took = two(server, service, hermes, repo)
    assert took < 5                                   # never stuck waiting for a run id that can't come yet
    assert len(hermes.calls) == 1                     # held: not started on top of the first
    assert second["run_id"]                           # the thread can draw its card now
    held = wait_for(lambda: next((i for i in items(server) if i.get("run_id") == second["run_id"]), None))
    assert held["kind"] == "waiting"
    assert held["line"].startswith("Waiting \u00b7 after ") and "checkout" in held["line"].lower()
    assert held.get("can_start_now") is True
    card = next(c for c in cards(server) if c["id"] == "c:" + second["run_id"])
    assert card["state"] == "waiting"     # its card is in the Home thread
    counts = http(server.base_url, "GET", "/voice/now?tz=America/New_York", None, server.token)[1].get("counts") or {}
    assert counts.get("waiting", 0) >= 1


def test_the_reply_says_it_starts_after_the_other_job(server, service, hermes, repo):
    _, second, _ = two(server, service, hermes, repo)
    text = second["reply"]["text"]
    assert "as soon as" in text and "wraps up" in text, text
    assert second.get("held") is True


def test_start_now_anyway_starts_it_in_parallel(server, service, hermes, repo):
    _, second, _ = two(server, service, hermes, repo)
    wait_for(lambda: next((i for i in items(server) if i.get("run_id") == second["run_id"]
                           and i["kind"] == "waiting"), None))
    status, body = http(server.base_url, "POST", f"/voice/work/{second['run_id']}/start-now", {}, server.token)
    assert status == 200 and body["ok"] is True, body
    wait_for(lambda: len(hermes.calls) == 2)
    assert "settings page" in str(hermes.calls[1]["input"])
    assert hermes.runs[next(iter(hermes.runs))]["status"] == "running"        # the first keeps going
    # Again: nothing is waiting any more.
    status, _ = http(server.base_url, "POST", f"/voice/work/{second['run_id']}/start-now", {}, server.token)
    assert status in (200, 409)
    assert http(server.base_url, "POST", "/voice/work/run_nope/start-now", {}, server.token)[0] == 404


def test_a_plain_question_about_the_same_repo_never_waits(server, service, hermes, repo):
    # The coordinator words it as work ("Fix ..."), but the user asked a question: it never waits.
    _, second, _ = two(server, service, hermes, repo, said=f"why is the settings page in {repo} slow?",
                       words=f"Fix the slow settings page in {repo}/src")
    wait_for(lambda: len(hermes.calls) == 2)
    assert not second.get("held")


@pytest.mark.parametrize("said", ["Propose a fix for the checkout bug in the shopfront repo",
                                  "Research how to fix the shopfront repo's checkout",
                                  "Plan the refactor of the shopfront repo"])
def test_proposals_plans_and_research_are_not_edits(said):
    assert not fence.is_edit_request(said)
    assert fence.is_edit_request("Fix the checkout bug in the shopfront repo")


@pytest.mark.parametrize("after, said", [
    ("Paint search", "I'll start that as soon as the paint search wraps up."),
    ("Fix the checkout bug", "I'll start that as soon as \u201cFix the checkout bug\u201d wraps up."),
    ("iOS release notes", "I'll start that as soon as the iOS release notes wraps up."),
    (None, "I'll start that as soon as the other job wraps up."),
])
def test_the_held_line(after, said):
    from speakeasy.home_chat import held_line
    assert held_line(after) == said


def test_once_it_starts_its_messages_follow_the_real_run(server, service, hermes, repo):
    _, second, _ = two(server, service, hermes, repo)
    assert second["run_id"].startswith("lr_")
    http(server.base_url, "POST", f"/voice/work/{second['run_id']}/start-now", {}, server.token)
    wait_for(lambda: len(hermes.calls) == 2)
    key = service.store.key_for_run(second["run_id"])
    real = wait_for(lambda: (service.store.work(idem_key=key) or {}).get("run_id") not in (None, second["run_id"])
                    and service.store.work(idem_key=key)["run_id"])
    entries = http(server.base_url, "GET", "/voice/now?tz=America/New_York", None, server.token)[1]["stream"]["entries"]
    linked = {e.get("run_id") for e in entries if e["kind"] == "message"}
    assert real in linked and second["run_id"] not in linked
    assert [c for c in entries if c["kind"] == "card" and c["id"] == "c:" + real]
