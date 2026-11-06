"""The voice never cuts a thought off, takes back what it sent too early, answers questions about a
job from the job itself, names jobs properly, and each job page shows only its own work.

Reproduces one real call's sequence with invented wording: a result's question spoken over a thought
in progress, "wait, I wasn't done", "no, don't add that", then "what is it checking?" / "for what?".
"""
from __future__ import annotations

import time

import pytest

from fakes import SDP, http, wait_for
from speakeasy import router
from speakeasy import store as store_mod
from speakeasy.prompt import builder as P


def start_call(server, service, key="req_cf_1"):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def tasks(server):
    return http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]


def spoken(worker):
    return [c for k, _, c in worker.sent if k == "session.commentary.append"]


def notes(worker):
    return [c for k, _, c in worker.sent if k == "session.thinking.append"]


def voice(worker, words, at):
    worker.feed({"type": "session.output_transcript.delta", "delta": words, "start_ms": at, "end_ms": at + 1})


def say(worker, delegation_id, words, at):
    """The user says ``words`` and the voice hands them off (each turn after a line from the voice)."""
    worker.feed({"type": "session.input_transcript.delta", "delta": words, "start_ms": at, "end_ms": at + 1})
    worker.feed({"type": "session.delegation.created", "offset_ms": at + 5,
                 "delegation": {"id": delegation_id, "target": "client"}})


# -- the router: talk about the conversation is never work --------------------------------------

@pytest.mark.parametrize("said, kind", [
    ("I wasn't done talking about the other thing", "hold"),
    ("hold on, let me finish my thought", "hold"),
    ("Wait", "hold"),
    ("No, don't send that yet, I want to finish before you send incomplete context", "undo"),
    ("For what", "confused"),
    ("Which one?", "confused"),
    ("I don't know what build you're talking about", "confused"),
])
def test_meta_remarks_are_recognized(said, kind):
    assert router.meta_kind(said) == kind


@pytest.mark.parametrize("said", [
    "wait, what time is the game tonight", "hold on, also check the weather", "Fix and ship",
    "Don't send that email to Dana", "let me finish, and then book the table at 8",
    "what do you mean by the pricing page, look up their pricing",
])
def test_real_requests_are_not_meta(said):
    assert router.meta_kind(said) is None


def test_questions_about_a_running_job():
    assert router.asks_about_task("What are you testing")
    assert router.asks_about_task("tell me what it's checking, can't you look at the task and see")
    assert not router.asks_about_task("what are you doing tonight")
    assert not router.asks_about_task("Fix and ship")


def test_a_thought_that_trails_off_is_not_finished():
    assert router.trails_off("And I want a way to see old calls, my sister said she never uses channels because she")
    assert not router.trails_off("Book a table for two at eight tonight")
    assert not router.trails_off("and the")  # a scrap: handled as one elsewhere
    assert not router.trails_off("Make it seventy-three instead (following up: what is the schedule for the")


# -- the call: the exact sequence -----------------------------------------------------------------

def test_the_cut_off_call_sequence(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service)
    say(worker, "call_1", "Look into why the pricing page still shows the old plan names", 10)
    first = wait_for(lambda: [t for t in tasks(server) if t.get("run_id")])[0]
    voice(worker, "On it.", 20)

    # Mid-thought (not handed off), and it trails off: a result with a question must not be spoken now.
    worker.feed({"type": "session.input_transcript.delta", "start_ms": 30, "end_ms": 31,
                 "delta": "Also I want a way to look back at old calls, my sister never sends them to chats because she"})
    for run in hermes.runs.values():
        run["done"].set()
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    time.sleep(1.5)
    assert spoken(worker) == [], "a result was spoken over a thought in progress"

    # "Wait, I wasn't done": no task, no follow-up; the voice is told to stop and listen.
    before = len(tasks(server))
    say(worker, "call_2", "Wait, I wasn't done talking about the other thing", 40)
    time.sleep(0.6)
    assert len(tasks(server)) == before
    assert any("isn't finished" in n for n in notes(worker))

    # "No, don't add that": nothing new starts.
    voice(worker, "Go ahead.", 50)
    say(worker, "call_3", "No, don't add it, I want to finish my thought before you send incomplete context", 60)
    time.sleep(0.6)
    assert len(tasks(server)) == before
    assert first["run_id"] in {t.get("run_id") for t in tasks(server)}


def test_status_question_is_answered_from_the_job(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service, "req_cf_2")
    say(worker, "call_1", "Fix the pricing page so it shows the new plan names and ship it", 10)
    task = wait_for(lambda: [t for t in tasks(server) if t.get("run_id")])[0]
    key = service.store.key_for_run(task["run_id"])
    service.store.set_activity(key, {"v": 1, "items": [
        {"id": "a1", "at": time.time(), "kind": "run", "text": "Edited pricing.html", "status": "done", "tool": "patch"},
        {"id": "a2", "at": time.time(), "kind": "run", "text": "Ran the full test suite", "status": "running",
         "tool": "terminal"}], "findings": [{"id": "f1", "at": time.time(), "text": "Plan names come from plans.json"}]})
    voice(worker, "On it.", 20)
    before = len(tasks(server))
    say(worker, "call_2", "What is it testing", 30)
    answer = wait_for(lambda: spoken(worker))[-1]
    assert "pricing page" in answer and "Ran the full test suite" in answer and "plans.json" in answer
    # It starts from what the task's card says right now, the same words the app shows.
    card = service.store.work(idem_key=key)
    assert f'Its card in the app says: "{card["short_status"]}.' in answer
    assert len(tasks(server)) == before
    voice(worker, "It's running the tests.", 40)
    say(worker, "call_3", "For what", 50)
    wait_for(lambda: len(spoken(worker)) >= 2)
    assert "don't follow" in spoken(worker)[-1] and len(tasks(server)) == before


def test_a_thought_that_trails_off_is_asked_about_not_sent(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service, "req_cf_3")
    say(worker, "call_1", "I want a way to browse old sessions in the app, my sister said she never uses chats because she", 10)
    wait_for(lambda: any("Want me to send that" in n for n in notes(worker)))
    assert not [t for t in tasks(server) if t.get("run_id")]
    voice(worker, "Want me to send that?", 20)
    say(worker, "call_2", "Yes, send it", 30)
    wait_for(lambda: [t for t in tasks(server) if t.get("run_id")])
    asked = [service.store.request_text(r.idem_key) or "" for r in worker.interaction.runs.values()]
    assert any("browse old sessions" in a for a in asked), asked


# -- names, pulses and status wording ---------------------------------------------------------------

def test_spoken_names_never_end_mid_phrase():
    assert P.spoken_name("Speakeasy app responding as") == "Speakeasy app responding"
    assert P.spoken_name("Fix the pricing page") == "Fix the pricing page"
    long = "Check why the nightly backup job keeps failing on the second drive and fix it for good"
    assert not P.spoken_name(long).split()[-1] in {"the", "on", "and", "for"}


def test_pulse_names_the_job_when_needed():
    line = P.named_pulse("Still running the tests, about a minute in.", "Pricing page fix", True)
    assert line.startswith("Pricing page fix:")
    assert P.named_pulse("Still running the tests.", "Pricing page fix", False) == "Still running the tests."


def test_status_answer_carries_request_tools_and_findings():
    text = P.status_answer("Pricing fix", "running", [], 120, None, request="Fix the plan names on the pricing page",
                           tools=["Edited pricing.html", "Ran the full test suite"], findings=["Names come from plans.json"])
    assert "Fix the plan names" in text and "Ran the full test suite" in text and "plans.json" in text


def test_a_tapped_answer_carries_its_question(server, service, hermes):
    _, worker = start_call(server, service, "req_cf_4")
    say(worker, "call_1", "Check whether setup finds the app", 10)
    task = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    key = service.store.key_for_run(task["run_id"])
    import json
    with service.store._lock, service.store._db:
        row = service.store._db.execute("SELECT result_json FROM runs WHERE idem_key=?", (key,)).fetchone()
        result = json.loads(row[0]) if row and row[0] else {"spoken": "x", "full": "x"}
        result["question"] = {"kind": "question", "question": "Fix the setup gaps?", "options": ["Fix and ship", "Leave it"]}
        service.store._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?", (json.dumps(result), key))
    assert worker.picked_request(task["task_id"] if "task_id" in task else "call_1", "Fix and ship") == \
        "Fix and ship: Fix the setup gaps?"


# -- the job page: its own status, plan and work ----------------------------------------------------

@pytest.fixture
def st(tmp_path):
    return store_mod.StateStore(tmp_path / "state.sqlite3", tmp_path)


def _run(st, key, session, status, request, plan=None, continued='{"session_id": "s_thread", "label": "Discord"}'):
    st.reserve_run(key, "vi_x", "call_" + key, 1)
    st.progress(key, "request", request)
    st.progress(key, "milestone", 'Continuing in Discord "Old thread"')
    with st._lock, st._db:
        st._db.execute("UPDATE runs SET session_id=?, continued=? WHERE idem_key=?", (session, continued, key))
    if plan:
        st.set_plan(key, plan)
    st.update_run(key, None, status)


def test_a_queued_thread_job_says_queued_not_working(st):
    _run(st, "k1", "s_thread", "running", "Fix the setup text and ship it")
    st.set_title("k1", "Fix the setup text")
    _run(st, "k2", "s_thread", "admitting", "Add a history page", plan=["Map the history page", "Build it", "Test it"])
    st.progress("k2", "milestone", "You added: and search too")
    work = st.work(idem_key="k2")
    assert work["status"] == "waiting_on_task" and "Fix the setup text" in work["short_status"]
    assert work["step_index"] is None  # nothing done on a job that hasn't started


def test_plumbing_milestones_are_not_steps_done():
    out = store_mod.job_progress("running", time.time() - 60, None, ["Map it", "Build it", "Test it"],
                                 [{"kind": "milestone", "text": 'Continuing in Discord "x"'},
                                  {"kind": "milestone", "text": "You added: more"}])
    assert out["step_index"] == 1


def test_generic_plans_are_not_shown(st):
    _run(st, "k1", "s_other", "running", "I wasn't done", plan=["Recall prior points", "Continue the topic",
                                                                  "Add your input"])
    assert st.work(idem_key="k1")["plan"] == [] and "started_with" not in st.work(idem_key="k1")
    _run(st, "k2", "s_other2", "running", "Fix the setup", plan=["Find the issue", "Fix and test", "Ship it"])
    # Stock steps that fit any task: not shown, not even as "Started with".
    assert st.work(idem_key="k2")["plan"] == [] and "started_with" not in st.work(idem_key="k2")
    # A concrete plan is only ever "Started with": never a list of steps to tick off.
    _run(st, "k3", "s_other3", "running", "Fix the setup", plan=["Find why setup misses the app",
                                                                  "Point setup at the app", "Try setup again"])
    assert st.work(idem_key="k3")["plan"] == []
    assert st.work(idem_key="k3")["started_with"].startswith("Find why setup misses the app")
    # Four stock steps planned for a bug report: not even "Started with".
    _run(st, "k4", "s_other4", "running", "Fix the flaky checkout test",
         plan=["Find the cause", "Trace the stuck state", "Fix and test", "Your review"])
    assert st.work(idem_key="k4")["plan"] == [] and "started_with" not in st.work(idem_key="k4")


def test_unrelated_requests_in_one_chat_thread_are_separate_jobs(st):
    _run(st, "k1", "s_thread", "completed", "Check whether setup finds the app")
    _run(st, "k2", "s_thread", "running", "Fix and ship")
    _run(st, "k3", "s_thread", "admitting", "Add a history page")
    st.link_job("k1", None)
    st.link_job("k2", "k1")  # the answer to k1's question: same job
    st.link_job("k3", None)  # a new request placed into the same chat: its own job
    j1, j2, j3 = (st.work(idem_key=k)["job"]["id"] for k in ("k1", "k2", "k3"))
    assert j1 == j2 and j3 != j1
