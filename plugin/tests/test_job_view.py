"""Job view: jobs on tasks, decision briefs, the new visual cards, focus as a routing prior, and two
result fixes (a reply stored twice, a spoken summary lifted out of context)."""
import json
import sqlite3

from speakeasy import router, text, views
from speakeasy.store import StateStore as Store, job_name


# -- jobs ---------------------------------------------------------------------------------------

def _run(store, key, session=None):
    store.reserve_run(key, "int_1", "call_" + key[-4:], 0)
    if session:
        store.set_session(key, session)


def test_a_job_is_the_session_and_is_named_once_from_its_first_task(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    _run(store, "se_first", "sess_garden")
    store.set_title("se_first", "Plan the spring garden beds")
    _run(store, "se_second", "sess_garden")
    store.set_title("se_second", "Order compost for the beds")
    first, second = store.work(idem_key="se_first")["job"], store.work(idem_key="se_second")["job"]
    assert first == second == {"id": "sess_garden", "name": "Plan the spring garden"}


def test_a_thread_title_never_names_the_job(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    _run(store, "se_thread")
    store.set_title("se_thread", "Fix the porch light timer")
    store.set_continued("se_thread", "sess_porch", "a thread in #home")
    store.set_title("se_thread", "Porch stuff and other things", thread=True)
    work = store.work(idem_key="se_thread")
    assert work["title"] == "Porch stuff and other things"
    assert work["job"] == {"id": "sess_porch", "name": "Fix the porch light"}


def test_the_titler_refines_the_job_name_and_it_is_never_empty(tmp_path):
    store = Store(tmp_path / "s.sqlite3")
    _run(store, "se_early")
    store.progress("se_early", "request", "um can you look at why the backups stopped")
    early = store.work(idem_key="se_early")["job"]
    assert early["id"] == "se_early" and early["name"]           # a stand-in before any title
    store.set_title("se_early", "Look at why the")                 # the instant word label
    assert store.work(idem_key="se_early")["job"]["name"] == "Look at why"
    store.set_title("se_early", "Backup failure investigation")     # the titler answers
    assert store.work(idem_key="se_early")["job"]["name"] == "Backup failure investigation"
    _run(store, "se_blank")
    assert store.work(idem_key="se_blank")["job"]["name"]


def test_a_follow_up_that_names_nothing_does_not_name_the_job(tmp_path):
    """A vague follow-up ("any luck?") titled "Check the earlier task status" named the
    session's job, so a real request later in that session showed under that name."""
    store = Store(tmp_path / "s.sqlite3")
    _run(store, "se_check", "sess_mixed")
    store.set_title("se_check", "Check the earlier task status")
    assert store.work(idem_key="se_check")["job"]["name"] == "Check the earlier task"  # nothing better yet
    _run(store, "se_bug", "sess_mixed")
    store.set_title("se_bug", "Fix the flaky checkout test in CI")
    for key in ("se_check", "se_bug"):
        assert store.work(idem_key=key)["job"]["name"] == "Fix the flaky checkout"
    _run(store, "se_more", "sess_mixed")
    store.set_title("se_more", "Redesign the session header colors")
    assert store.work(idem_key="se_more")["job"]["name"] == "Fix the flaky checkout"  # a real name sticks


def test_job_names_are_two_to_four_words():
    assert job_name("Architect combined voice and generation system") == "Architect combined voice"
    assert job_name("Build rules engine implementation") == "Build rules engine implementation"
    assert job_name('"Night shift"') == "Night shift"
    assert job_name("") is None and job_name(None) is None


def test_old_databases_gain_the_job_table(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE runs (idem_key TEXT PRIMARY KEY, interaction_id TEXT NOT NULL,
        delegation_id TEXT NOT NULL, revision INTEGER NOT NULL, run_id TEXT, status TEXT NOT NULL, updated REAL NOT NULL,
        short_status TEXT, detail TEXT, progress_updated REAL, status_source TEXT, authored_at REAL, result_json TEXT,
        settled_at REAL, title TEXT, dismissed INTEGER, session_id TEXT, summary TEXT, continued TEXT)""")
    db.execute("INSERT INTO runs (idem_key,interaction_id,delegation_id,revision,status,updated,title,session_id) "
               "VALUES ('se_old','i','d',0,'completed',1,'Old task title here','sess_old')")
    db.commit()
    db.close()
    store = Store(path)
    work = store.work(idem_key="se_old")
    assert work["job"] == {"id": "sess_old", "name": "Old task title here"}
    assert work["title"] == "Old task title here"


# -- decision briefs ----------------------------------------------------------------------------

def _ask(body):
    return text.split_result(f"Checked it.\n\n```speakeasy-question\n{json.dumps(body)}\n```\nSPOKEN: Checked it.", ())


BRIEF = {
    "question": "When should the router stop sending work to a node?",
    "context": "Two nodes share local-model work; one stalls when memory runs low.",
    "why": "Keeps the swap protection and halves the both-down window.",
    "affects": "1 file on the server, all local-model jobs",
    "approvable": True,
    "options": [
        {"text": "1 error, 30s bench", "detail": "Keeps the swap protection.", "risk": "LOW", "reversible": True,
         "tags": [{"text": "Low risk", "tone": "good"}, {"text": "Odd", "tone": "purple"}, {"tone": "bad"}]},
        {"text": "3 errors, 30s bench", "detail": "Swap errors may return.", "risk": "medium", "reversible": True},
        "Leave it",
    ],
    "recommended": 0,
}


def test_a_decision_brief_keeps_its_explanation_and_option_objects():
    q = _ask(BRIEF)["question"]
    assert q["context"].startswith("Two nodes") and q["why"] and q["affects"]
    first = q["options"][0]
    assert first == {"text": "1 error, 30s bench", "detail": "Keeps the swap protection.", "risk": "low",
                     "reversible": True, "tags": [{"text": "Low risk", "tone": "good"}, {"text": "Odd"}]}
    assert q["options"][2] == "Leave it"
    assert q["approvable"] is True


def test_the_voice_and_the_written_answer_read_option_text():
    r = _ask(BRIEF)
    assert "1 error, 30s bench, 3 errors, 30s bench, or Leave it" in r["spoken"]
    assert "I'd go with 1 error, 30s bench." in r["spoken"]
    assert "1. 1 error, 30s bench (recommended)" in r["full"] and "{" not in r["full"]


def test_approve_is_the_plugins_call_never_the_models():
    def approvable(**change):
        body = json.loads(json.dumps(BRIEF))
        for key, value in change.items():
            if key in ("risk", "reversible", "text", "detail"):
                body["options"][0][key] = value
            else:
                body[key] = value
        return _ask(body)["question"].get("approvable", False)
    assert approvable() is True
    assert approvable(risk="medium") is False
    assert approvable(reversible=False) is False
    assert approvable(recommended=None) is False
    assert approvable(text="Pay the $40 upgrade") is False                 # money
    assert approvable(detail="Then I send the email from your account") is False  # speaking as the user
    assert approvable(question="Publish the new page now?") is False       # publishing
    assert approvable(text="Ship the plugin now") is False                 # publishing, said as shipping
    assert approvable(detail="Deletes the old cache folder") is False      # deleting
    plain = _ask({"question": "Which one?", "options": ["A", "B"], "recommended": 0, "approvable": True})
    assert "approvable" not in plain["question"]


def test_bad_options_are_dropped_and_a_question_needs_two():
    _, q = views.extract_question('x\n```speakeasy-question\n{"question": "Pick", "options": [{"detail": "no text"}, '
                                  '"A", {"text": "B", "risk": "extreme", "reversible": "yes"}]}\n```')
    assert q["options"] == ["A", "B"]
    _, q = views.extract_question('x\n```speakeasy-question\n{"question": "Pick", "options": [{"detail": "x"}, "A"]}\n```')
    assert q is None


def test_answering_with_an_object_options_text_reaches_the_task(server, service, hermes):
    from fakes import http, wait_for
    from test_routing import start_call, tasks
    ask = ("Voice: tune the router\nChecked the logs.\n\n```speakeasy-question\n" + json.dumps(BRIEF)
           + "\n```\nDONE: router\nSPOKEN: Checked the logs.")
    hermes.responder = lambda prompt, sid: (ask if "1 error, 30s bench" not in prompt
                                            else "Set.\nDONE: router\nSPOKEN: Set to one error.")
    session, worker = start_call(server, service)
    worker.delegate("call_q", "Tune when the router benches a node")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    option = done["result"]["question"]["options"][0]
    assert done["result"]["question"]["approvable"] is True and done["job"]["name"]
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/answer",
                        {"task_id": done["task_id"], "text": option["text"]}, server.token)
    assert status == 200 and body["sent"], body
    wait_for(lambda: len(hermes.calls) >= 2)
    assert "1 error, 30s bench" in hermes.calls[1]["input"]


# -- new visual cards ---------------------------------------------------------------------------

def test_lanes_clamp_segments_and_drop_backwards_ones():
    v = views.clean({"kind": "lanes", "title": "Overnight jobs", "start_label": "22:00", "end_label": "06:00",
                     "ticks": ["22", "00", "02", "04", "06"],
                     "lanes": [{"label": "Node A", "segments": [
                         {"from": -0.2, "to": 0.4, "tone": "good", "label": "ran"},
                         {"from": 0.5, "to": 0.5, "tone": "bad"},
                         {"from": 0.9, "to": 0.7},
                         {"from": 0.8, "to": 1.6, "tone": "sparkly"}]},
                         {"label": "Node B", "segments": [{"from": 0.6, "to": 0.2}]},
                         {"segments": []}],
                     "note": "Node A stalled at 03:00"})
    a, b = v["lanes"]
    assert a["segments"] == [{"from": 0.0, "to": 0.4, "tone": "good", "label": "ran"}, {"from": 0.8, "to": 1.0}]
    assert b == {"label": "Node B"}
    assert v["note"] and v["ticks"][0] == "22"


def test_flow_devices_and_before_after_shapes():
    flow = views.clean({"kind": "flow", "title": "Where requests go",
                        "nodes": [{"label": f"Step {i}", "tone": "INFO", "detail": "x"} for i in range(7)]})
    assert len(flow["nodes"]) == 5 and flow["nodes"][0]["tone"] == "info"
    dev = views.clean({"kind": "devices", "title": "Fleet", "items": [
        {"name": "Node A", "value": "OK", "tone": "good", "history": ["1", "2", "3", "4", "5", "6", "7"]},
        {"name": "Node B", "tone": "on fire", "extra": 1}]})
    assert len(dev["items"][0]["history"]) == 6 and dev["items"][1] == {"name": "Node B"}
    ba = views.clean({"kind": "before_after", "title": "Header",
                      "before": {"label": "Before", "text": "Crowded", "image": "http://insecure.example/a.png"},
                      "after": {"label": "After", "image": 2}})
    assert ba["before"] == {"label": "Before", "text": "Crowded"} and ba["after"]["image"] == 2
    assert views.clean({"kind": "before_after", "title": "Nothing to compare"}) is None


def test_before_after_screenshots_become_card_numbers(tmp_path):
    shot = tmp_path / "after.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    block = json.dumps([{"kind": "before_after", "title": "Header",
                         "before": {"label": "Before", "image": "https://example.com/before.png"},
                         "after": {"label": "After", "image": str(shot)}}])
    r = text.split_result(f"Done.\n```speakeasy-views\n{block}\n```\nSPOKEN: Done.", (tmp_path,))
    view = r["views"][0]
    assert view["before"]["image"] == "https://example.com/before.png"
    assert isinstance(view["after"]["image"], int) and r["cards"][view["after"]["image"] - 1]["path"] == str(shot)
    # A path outside the image roots never reaches a client.
    r = text.split_result(f"Done.\n```speakeasy-views\n{block}\n```\nSPOKEN: Done.", ())
    assert "image" not in r["views"][0]["after"] and str(shot) not in json.dumps(text.public_result(r))


# -- focus is a prior, not an order -------------------------------------------------------------

TASKS = [router.OpenTask("call_a", "Plan a four day trip to Lisbon", "running"),
         router.OpenTask("call_b", "Draft the launch email for the newsletter", "running")]


def test_the_router_is_told_what_the_user_is_looking_at():
    seen = []

    def model(messages):
        seen.append(messages[-1]["content"])
        return '{"follow_up_task_id": "call_a", "parts": ["x"], "channel": null}'
    focus = router.Focus("call_a", "Lisbon trip", "day two")
    d = router.decide("make it lighter, fewer museums", TASKS, None, [], model, focus=focus)
    assert d.parts[0].task_id == "call_a" and "looking at task call_a (Lisbon trip)" in seen[0]


def test_an_unrelated_request_while_focused_goes_elsewhere():
    model = lambda messages: '{"follow_up_task_id": null, "parts": ["check the score of the game"], "channel": null}'
    d = router.decide("check the score of the game", TASKS, None, [], model, focus=router.Focus("call_a", "Lisbon trip"))
    assert d.parts[0].kind == "new" and d.source == "model"


def test_without_a_router_answer_the_focus_wins():
    def boom(messages):
        raise RuntimeError("no model")
    for call in (boom, lambda m: None, lambda m: "not json"):
        d = router.decide("check the score of the game", TASKS, None, [], call, focus=router.Focus("call_b"))
        assert d.parts[0] == router.Part("follow_up", "check the score of the game", "call_b") and d.source == "focus"


def test_a_focus_on_a_closed_task_is_ignored():
    d = router.decide("check the score", TASKS, None, [], lambda m: None, focus=router.Focus("call_gone"))
    assert d.source != "focus"


def test_an_unrelated_request_in_a_call_leaves_the_focused_task(home, hermes):
    from fakes import FakeLiveWorker, FakeTransport, http, wait_for
    from speakeasy.server import SpeakeasyServer
    from speakeasy.service import VoiceService
    from test_routing import start_call, tasks

    def model(messages):
        if "Request: check the score" in messages[-1]["content"]:
            return '{"follow_up_task_id": null, "parts": ["check the score of the game"], "channel": null}'
        return None
    workers = []
    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport, route_call=model,
                       openai_negotiate=lambda key, payload: {"session": {"id": "sess_fake"}, "transport": {"sdp": "v=0\r\n"}},
                       openai_worker=lambda rt, i: workers.append(FakeLiveWorker(rt, i)) or workers[-1])
    svc.settings.patch({"voice": {"provider": "openai"}})
    svc.workers = workers
    srv = SpeakeasyServer(svc, "127.0.0.1", 0)
    srv.start()
    try:
        status, paired = http(srv.base_url, "POST", "/voice/pair", {"code": svc.devices.new_pairing_code(),
                                                                    "device_name": "Test Mac"})
        srv.token = paired["token"]
        hermes.hold = True
        session, worker = start_call(srv, svc)
        worker.delegate("call_a", "Plan a four day trip to Lisbon")
        wait_for(lambda: len(hermes.calls) >= 1)
        lisbon = tasks(srv)[0]["task_id"]
        assert http(srv.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/focus",
                    {"task_id": lisbon}, srv.token)[0] == 200
        worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 3, "end_ms": 4})
        worker.delegate("call_c", "check the score of the game")
        wait_for(lambda: len(hermes.calls) >= 2)
        assert "score of the game" in hermes.calls[1]["input"] and not hermes.steers
    finally:
        srv.httpd.shutdown()
        srv.httpd.server_close()
        svc.close()


# -- result fixes -------------------------------------------------------------------------------

REPORT = ("I wrote the plan and a first draft, and saved both next to the idea.\n\n"
          "The system has three layers:\n\n1. The voice.\n2. The engine.\n3. The director.\n\n"
          "**Should I start building the engine now?**\n1. Yes, start the engine (recommended)\n2. Review the draft")


def test_a_reply_that_arrives_twice_is_kept_once():
    for doubled in (REPORT + "\n" + REPORT, REPORT + "\n\n" + REPORT + "\n"):
        r = text.split_result(doubled, ())
        assert r["full"] == REPORT and r["full"].count("I wrote the plan") == 1
    short = "Done.\nDone."
    assert text.split_result(short, ())["full"] == short           # short repeats are left alone
    once = REPORT + "\n\nI wrote the plan and a first draft."
    assert text.split_result(once, ())["full"] == once


def test_the_spoken_summary_never_lifts_a_sentence_out_of_context():
    full = ("Tomorrow is a full umbrella day: heavy rain and a high of 66. The transcription work is finished.\n\n"
            "Where it ended last night:\n\n"
            "The new model won the test on 23 real notes. Its one repeat error is hearing a name wrong.\n\n"
            "If you meant the other app instead, that ended at build 40.")
    said = text.spoken_from(full)
    assert said == "Tomorrow is a full umbrella day: heavy rain and a high of 66. The transcription work is finished."
    twice = "The engine is built.\n\nYou roll and you fail the check.\n\nYou roll and you fail the check."
    assert text.spoken_from(twice).count("you fail") <= 1
    later = "The report is ready.\n\n## Backups\n\nThe nightly backup failed after the disk filled up."
    assert "nightly backup failed" in text.spoken_from(later)
