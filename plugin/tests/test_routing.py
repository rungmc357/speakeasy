"""Spoken acknowledgements (A), channel routing and new threads (B), continuity switch (C),
the routing model (one auxiliary call per handoff), and Suggest channels."""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import time

import pytest

from fakes import SDP, http, wait_for
from speakeasy import channels, continuity, router, suggest, threads
from speakeasy.prompt import builder as P
from speakeasy import settings as S
from speakeasy.prompt import builder as P


def start_call(server, service, key="req_call_1"):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def tasks(server):
    return http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]


def spoken(worker):
    return [c for k, _, c in worker.sent if k == "session.commentary.append"]


WORK = {"target": "discord:111", "label": "#work", "topic": "my job: meetings, email, projects",
         "new_thread": False}
RESEARCH = {"target": "discord:222", "label": "#research", "topic": "reading up on a subject, comparing options",
            "new_thread": False}


# -- (A) spoken acknowledgement -------------------------------------------------------------------

def test_server_adds_no_scripted_acknowledgement(server, service, hermes):
    """The voice model acknowledges on its own; a plain task gets no server-spoken line."""
    hermes.hold = True
    _, worker = start_call(server, service)
    worker.delegate("call_a", "Find me a dentist near the office")
    wait_for(lambda: [t for t in tasks(server) if t.get("run_id")])
    time.sleep(0.2)
    assert spoken(worker) == []


def test_legacy_acknowledge_setting_is_dropped(service):
    service.settings.patch({"speech": {"progress": False}})
    raw = json.loads(service.settings.path.read_text())
    raw["speech"]["acknowledge"] = True
    service.settings.path.write_text(json.dumps(raw))
    assert service.settings.get()["speech"] == {"progress": False}


def test_spoken_lines_can_be_turned_off(server, service, hermes, monkeypatch):
    import asyncio
    from speakeasy.calls import BackendRun
    service.settings.patch({"speech": {"progress": False}})
    _, worker = start_call(server, service)
    backend = BackendRun("t9", 1, "idem_9", status="running")
    backend.started -= 25
    asyncio.run(worker.maybe_speak_progress(backend, "Pulling this week's events"))
    assert spoken(worker) == []
    status, _ = http(server.base_url, "PATCH", "/voice/settings", {"speech": {"progress": "yes"}}, server.token)
    assert status == 400


def test_both_transports_route_commentary_to_speech():
    from speakeasy import codex_transport
    source = open(codex_transport.__file__).read()
    assert '"thread/realtime/appendSpeech" if kind == "session.commentary.append"' in source
    from speakeasy import openai_live
    live = open(openai_live.__file__).read()
    assert '"type": kind' in live  # session.commentary.append goes out as-is: the live API speaks it


def _progress_worker(service, writer=None):
    from speakeasy.calls import Interaction
    from fakes import FakeLiveWorker
    if writer is not None:
        service.rt.progress_call = writer
    return FakeLiveWorker(service.rt, Interaction("int_x", "sess_x"))


def _step(worker, backend, clock, at, detail):
    import asyncio
    clock[0] = backend.started + at
    event = {"event": "message.interim", "text": f"STATUS: Working step {int(at)}\nDETAIL: {detail}"}
    asyncio.run(worker._handle_hermes_event(backend, event))


def _run_with_clock(fn):
    from speakeasy import calls
    clock = [0.0]
    real = calls.time.monotonic
    calls.time.monotonic = lambda: clock[0]
    try:
        fn(clock)
    finally:
        calls.time.monotonic = real


def test_progress_says_what_the_task_is_actually_doing(service):
    """Live report: updates were 'still on it, I'm checking the repo'. The spoken line is written from
    the agent's recent concrete steps, and the writer is told what was already said."""
    from speakeasy.calls import BackendRun
    seen = []

    def writer(request, steps, told):
        seen.append((request, list(steps), list(told)))
        return f"I found {steps[-1].lower()}"
    worker = _progress_worker(service, writer)
    backend = BackendRun("t1", 1, "idem_1", status="running")

    def go(clock):
        _step(worker, backend, clock, 10, "Cloned the speakeasy repo")
        _step(worker, backend, clock, 50, "Three failing tests in test_routing.py")
    _run_with_clock(go)
    assert spoken(worker) == ["I found three failing tests in test_routing.py"]
    assert seen[0][1] == ["Cloned the speakeasy repo", "Three failing tests in test_routing.py"]


def test_updates_are_capped_across_the_call_and_need_new_work(service):
    import asyncio
    from speakeasy import calls
    from speakeasy.calls import BackendRun
    n = [0]

    def writer(request, steps, told):
        n[0] += 1
        return f"Update {n[0]} about {steps[-1]}"
    worker = _progress_worker(service, writer)
    a = BackendRun("t1", 1, "idem_a", status="running")
    b = BackendRun("t2", 1, "idem_b", status="running")
    b.started = a.started

    def go(clock):
        for at in range(0, 181, 5):  # two chatty tasks, a status line every 5 s each, for 3 minutes
            _step(worker, a, clock, at, f"Task A reading file number {at}")
            _step(worker, b, clock, at, f"Task B checking source number {at}")
    _run_with_clock(go)
    said = spoken(worker)
    assert 1 <= len(said) <= 180 // calls.PROGRESS_EVERY_S + 1
    # nothing new since the last update: quiet even once the cap allows it
    worker.sent.clear()
    a.told_count = a.activity_count

    def later(clock):
        clock[0] = a.started + 999
        asyncio.run(worker.maybe_speak_progress(a, "x"))
    _run_with_clock(later)
    assert spoken(worker) == []


def test_without_the_wording_model_it_still_names_the_step(service):
    from speakeasy.calls import BackendRun
    worker = _progress_worker(service, lambda r, s, t: None)
    backend = BackendRun("t1", 1, "idem_1", status="running")
    _run_with_clock(lambda clock: _step(worker, backend, clock, 50, "Comparing prices on three hotel sites"))
    assert spoken(worker) == ["Quick update: comparing prices on three hotel sites."]


def test_the_wording_model_never_gets_away_with_still_on_it():
    assert router.clean_progress('{"say": "Still on it."}') is None
    assert router.clean_progress('{"say": "Still checking the repo."}') is None
    assert router.clean_progress('{"say": "I found the bug in the router; fixing it now."}') == \
        "I found the bug in the router; fixing it now."



# -- routing model ---------------------------------------------------------------------------------

OPEN = [router.OpenTask("t1", "Plan the Rome trip itinerary", "running")]
TOPICS = [router.Topic("#work", WORK["topic"]), router.Topic("#research", RESEARCH["topic"])]


def test_routing_model_splits_a_compound_request():
    call = lambda messages: json.dumps({"follow_up_task_id": None, "channel": None,
                                        "parts": ["Check the weather in Lisbon", "Book a table for two tonight"]})
    d = router.decide("Check the weather in Lisbon and book a table for two tonight", [], None, [], call)
    assert d.source == "model" and [p.request for p in d.parts] == ["Check the weather in Lisbon",
                                                                    "Book a table for two tonight"]


def test_routing_model_attaches_a_follow_up():
    call = lambda messages: '{"follow_up_task_id": "t1", "parts": [], "channel": null}'
    d = router.decide("Add a day in Florence", OPEN, None, [], call)
    assert d.parts[0].kind == "follow_up" and d.parts[0].task_id == "t1"


def test_routing_model_picks_a_channel_and_rejects_unknown_ones():
    pick = lambda label: (lambda messages: json.dumps({"follow_up_task_id": None, "parts": ["x"], "channel": label}))
    assert router.decide("Move my 3pm meeting to Thursday", [], None, TOPICS, pick("work")).channel == "#work"
    assert router.decide("Move my 3pm meeting to Thursday", [], None, TOPICS, pick("#nope")).channel is None


def test_routing_model_timeout_and_garbage_fall_back_to_rules():
    def slow(messages):
        time.sleep(1)
        return '{"follow_up_task_id": "t1", "parts": [], "channel": null}'
    started = time.monotonic()
    d = router.decide("What's the capital of Peru?", OPEN, None, [], slow, timeout=0.2)
    assert d.source == "fallback" and d.parts[0].kind == router.NEW and time.monotonic() - started < 0.8
    assert router.decide("x and y", [], None, [], lambda m: "not json").source == "fallback"
    assert router.decide("x and y", [], None, [], lambda m: '{"follow_up_task_id": "ghost"}').source == "fallback"
    boom = lambda m: (_ for _ in ()).throw(RuntimeError("no provider"))
    assert router.decide("x and y", [], None, [], boom).source == "fallback"


def test_routing_skips_the_model_when_nothing_to_decide():
    calls = []
    d = router.decide("What time is it in Tokyo?", [], None, [], lambda m: calls.append(m) or "{}")
    assert calls == [] and d.parts[0].kind == router.NEW


def test_routing_latency_is_stored_with_the_task(home, hermes):
    from speakeasy.service import VoiceService
    from fakes import FakeLiveWorker, FakeTransport
    workers = []
    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                       openai_negotiate=lambda key, payload: {"session": {"id": "sess_fake"}, "transport": {"sdp": "v=0\r\n"}},
                       openai_worker=lambda rt, i: workers.append(FakeLiveWorker(rt, i)) or workers[-1],
                       route_call=lambda m: '{"follow_up_task_id": null, "parts": ["a", "b"], "channel": null}')
    svc.settings.patch({"voice": {"provider": "openai"}})
    try:
        svc.create_session({"sdp": SDP}, "req_lat")
        workers[-1].delegate("call_ab", "Check the weather and also book a table")
        wait_for(lambda: len(hermes.calls) == 2)
        keys = [r.idem_key for r in workers[-1].interaction.runs.values()]
        assert all(svc.store.timings(k).get("routing_ms") is not None for k in keys)
        assert any("split into separate tasks" in c for _, _, c in workers[-1].sent)
    finally:
        svc.close()


# -- (B) channels ---------------------------------------------------------------------------------

def _settings(**delivery):
    return S.validate({"delivery": {"target": "telegram:555", "channels": [WORK, RESEARCH], **delivery}})


def test_explicit_channel_naming_wins_and_never_asks():
    s = _settings()
    assert channels.explicit("put this in work: a menu bar timer", s).channel.label == "#work"
    assert channels.explicit("put it in #research please", s).channel.label == "#research"
    both = channels.explicit("post it in #work and #research", s, P.clarify_channel)
    assert not both.clarify and both.channel.label == "#work"      # the first one named
    assert channels.explicit("put it in #cooking", s, P.clarify_channel) is None  # unknown: topic/default decide
    assert channels.explicit("put it in writing for me", s) is None  # ordinary phrase, not a channel


def test_topical_pick_or_default():
    s = _settings(mode="topic")
    assert channels.resolve(s, "Telegram", "#work").channel.target == "discord:111"
    fallback = channels.resolve(s, "Telegram", None)
    assert fallback.channel.default and fallback.channel.target == "telegram:555"


def test_legacy_settings_migrate():
    s = S.validate({"delivery": {"target": "discord:123", "new_thread_per_task": True}})
    assert s["delivery"] == {"target": "discord:123", "new_thread": True, "channels": [], "mode": "home"}


def test_channel_settings_are_validated(server):
    bad = [{"target": "rm -rf", "label": "#x"}, {"target": "discord:1", "label": "{oops}"},
           {"target": "discord:1", "label": "#a", "topic": "x" * 200}]
    for channel in bad:
        status, _ = http(server.base_url, "PATCH", "/voice/settings", {"delivery": {"channels": [channel]}}, server.token)
        assert status == 400
    dup = [WORK, dict(WORK, label="#other")]
    assert http(server.base_url, "PATCH", "/voice/settings", {"delivery": {"channels": dup}}, server.token)[0] == 400
    status, body = http(server.base_url, "PATCH", "/voice/settings", {"delivery": {"channels": [WORK]}}, server.token)
    assert status == 200 and body["settings"]["delivery"]["channels"][0]["label"] == "#work"


def test_named_channel_runs_here_and_posts_the_result_there(server, service, hermes):
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [WORK]}})
    posted = []
    service.notices.post = lambda key, text, limit=600, target=None: posted.append((key, target)) or True
    _, worker = start_call(server, service)
    worker.delegate("call_b", "Put this in work: move my 3pm meeting to Thursday")
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    wait_for(lambda: posted)
    assert posted[0][1] == "discord:111"
    assert any("#work" in line for line in spoken(worker))
    assert "#work" in hermes.calls[0]["input"]


def test_unknown_channel_starts_anyway_without_asking(server, service, hermes):
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [WORK]}})
    _, worker = start_call(server, service)
    worker.delegate("call_c", "put it in #cooking: find a pasta recipe")
    wait_for(lambda: hermes.calls)
    assert not any("channel" in line.lower() and "?" in line for line in spoken(worker))


class FakeThreads:
    def __init__(self, answer="Voice: x\nThe timer now has dark mode.", fail=False):
        self.answer, self.fail, self.opened = answer, fail, []

    def available(self, target):
        return True

    def open(self, target, *, message, title, delivery_id):
        if self.fail:
            raise threads.ThreadError("no")
        self.opened.append((target, message, title))
        return threads.Opened("speakeasy-x", "999", "discord")

    def wait(self, opened, on_session, on_title=None):
        on_session("thread_session_1")
        if on_title:
            on_title("Hermes named this")
        return self.answer


def test_new_thread_channel_runs_the_task_in_a_thread(server, service, hermes):
    runner = FakeThreads()
    service.rt.threads = runner
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [dict(WORK, new_thread=True)]}})
    _, worker = start_call(server, service)
    worker.delegate("call_t", "Put this in work: move my 3pm meeting to Thursday")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    assert runner.opened and runner.opened[0][0] == "discord:111" and hermes.calls == []
    assert "Sending that to #work." in spoken(worker)  # named by the user: a short confirmation
    assert "dark mode" in done["result"]["full"] and not done["result"]["full"].startswith("Voice:")
    key = next(iter(worker.interaction.runs.values())).idem_key
    assert service.store.continued_for(key)["session_id"] == "thread_session_1"  # follow-ups go there
    milestones = [p["text"] for p in done.get("progress", []) if p.get("kind") == "milestone"]
    assert not milestones or "Started in a new thread in #work" in milestones


def test_thread_destination_wording_reads_as_a_place():
    """Labels are places ("your Discord", "#voice on Discord"), never adjectives: the old template
    produced "Started in a new your Discord thread" and spoke "a new your thread"."""
    from speakeasy.prompt import builder as P
    for label, spoken_as in (("your Discord", "your Discord"), ("#voice on Discord", "#voice")):
        assert P.new_thread_in(label) == f"a new thread in {label}"
        assert P.ack_channel_thread(label) == f"Sending that to {spoken_as}."
    assert P.ack_channel_post("your Telegram") == "Sending that to your Telegram."


def thinking(worker):
    return [c for k, _, c in worker.sent if k == "session.thinking.append"]


def test_a_thread_picked_by_topic_is_not_announced(server, service, hermes):
    """Where work goes is plumbing: unless the user named the place, nothing is spoken about it;
    the voice gets it as a silent note for "where did that go?"."""
    runner = FakeThreads()
    service.rt.threads = runner
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [dict(WORK, new_thread=True)]}})
    service.rt.topical_channel = lambda label: channels.Choice(channels.Channel(**{k: WORK[k] for k in ("target", "label", "topic")}, new_thread=True), "topic")
    _, worker = start_call(server, service)
    worker.delegate("call_q", "move my 3pm meeting to Thursday")
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    assert runner.opened
    assert not any("#work" in line or "thread" in line.lower() for line in spoken(worker))
    assert any("#work" in note and "do not say" in note for note in thinking(worker))


def test_continuing_a_conversation_never_reads_its_title():
    note = P_builder().continuing_in_note("Discord thread: Refactor the lisbon itinerary into days")
    assert "do not read this aloud" in note and "never read the conversation's name" in note


def test_the_prompt_no_longer_asks_to_announce_where_work_went():
    from speakeasy.prompt import builder as P
    text = P.delivery_clause("your Discord", [WORK])
    assert "say in one short line where" not in text and "only if" in text


def P_builder():
    from speakeasy.prompt import builder as P
    return P


def test_thread_failure_falls_back_to_posting(server, service, hermes):
    service.rt.threads = FakeThreads(fail=True)
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [dict(WORK, new_thread=True)]}})
    _, worker = start_call(server, service)
    worker.delegate("call_f", "Put this in work: move my 3pm meeting to Thursday")
    wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])
    assert len(hermes.calls) == 1


def test_rules_list_the_opted_in_channels():
    text = P.rules_text(P.Names.from_settings(S.validate({})), "Telegram", [WORK, dict(RESEARCH, new_thread=True)])
    assert "#work (my job: meetings" in text and "own new thread" in text
    assert "{" not in text.split("# Backchannel")[0]


# -- threads: routes, secret, state DB ------------------------------------------------------------

def _state_db(path, rows):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_type TEXT, thread_id TEXT, "
               "origin_json TEXT, title TEXT, started_at REAL, ended_at REAL)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
               "content TEXT, tool_calls TEXT, timestamp REAL)")
    for sid, source, chat_type, thread_id, origin in rows:
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,NULL)",
                   (sid, source, chat_type, thread_id, json.dumps(origin), "t", time.time()))
    db.commit()
    return db


def test_routes_are_generated_per_channel_with_a_private_secret(tmp_path):
    (tmp_path / "webhook_subscriptions.json").write_text(json.dumps({"mine": {"secret": "keep"}}))
    _state_db(tmp_path / "state.db", [("s1", "discord", "group", None,
                                       {"platform": "discord", "chat_id": "111", "chat_type": "group",
                                        "user_id": "42", "user_name": "sam"})]).close()
    made = threads.sync_routes(tmp_path, [dict(WORK, new_thread=True), RESEARCH], supported=True)
    subs = json.loads((tmp_path / "webhook_subscriptions.json").read_text())
    route = subs[made["discord:111"]]
    assert set(made) == {"discord:111"} and subs["mine"] == {"secret": "keep"}
    assert route["source_new_thread"] is True and route["source_chat_id"] == "111" and route["source_user_id"] == "42"
    secret_file = tmp_path / "speakeasy" / "webhook_secret"
    assert stat.S_IMODE(os.stat(secret_file).st_mode) == 0o600 and route["secret"] == secret_file.read_text()
    threads.sync_routes(tmp_path, [], supported=True)  # channel removed: its route goes, others stay
    assert json.loads((tmp_path / "webhook_subscriptions.json").read_text()) == {"mine": {"secret": "keep"}}


def test_thread_answer_is_read_from_the_state_db(tmp_path):
    db = _state_db(tmp_path / "state.db", [("s_thread", "discord", "thread", "999", {"platform": "discord"})])
    opened = threads.Opened("r", "999", "discord")
    seen = []
    assert threads.wait_for_answer(tmp_path / "state.db", opened, 0, sleep=lambda s: None, on_session=seen.append) is None
    db.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES ('s_thread','assistant','Done here',NULL,1)")
    db.commit()
    assert threads.wait_for_answer(tmp_path / "state.db", opened, 0, sleep=lambda s: None) == "Done here"
    assert seen == ["s_thread"]


def test_thread_answer_skips_hermes_bookkeeping_rows_and_follows_the_title(tmp_path):
    # A finished Hermes turn can end with a session_meta row after the answer.
    db = _state_db(tmp_path / "state.db", [("s_thread", "discord", "thread", "999", {"platform": "discord"})])
    db.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES ('s_thread','assistant','All set',NULL,1)")
    db.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES ('s_thread','session_meta','',NULL,2)")
    db.execute("UPDATE sessions SET title='Check league team' WHERE id='s_thread'")
    db.commit()
    titles = []
    answer = threads.wait_for_answer(tmp_path / "state.db", threads.Opened("r", "999", "discord"), 0,
                                     sleep=lambda s: None, on_title=titles.append)
    assert answer == "All set" and titles == ["Check league team"]


def test_thread_capability_needs_the_webhook_platform(tmp_path):
    assert threads.capability(tmp_path, True)["supported"] is False
    (tmp_path / "config.yaml").write_text("platforms:\n  webhook:\n    enabled: true\n    extra:\n      port: 8650\n")
    assert threads.capability(tmp_path, True)["supported"] is True
    assert threads.webhook_base(tmp_path) == "http://127.0.0.1:8650"
    assert threads.capability(tmp_path, False)["supported"] is False


def test_continuing_a_chat_posts_there_and_never_writes_hermes_config(server, service, hermes, home, monkeypatch):
    """A voice request continued in an existing Discord thread runs in that session and the answer
    is posted into the thread by Speakeasy itself. Hermes' config.yaml is not written at all."""
    conv = continuity.Conversation("s_league", "discord", "777", "thread", "777", "42", "111",
                                   "Server / #work / League", "League team", time.time())
    monkeypatch.setattr(continuity, "conversations_with_context", lambda db, request, **kw: [continuity.Candidate(conv, ())])
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **k: False)
    # the routing model is the only thing that continues a chat
    from speakeasy import router
    monkeypatch.setattr(router, "aux_call", lambda messages, timeout=router.ROUTE_TIMEOUT_S:
                        '{"follow_up_task_id": null, "conversation": "c1", "parts": ["x"], "channel": null}')
    streamed = []

    def fake_stream(base, key, c, message, callback, **k):
        streamed.append(c.session_id)
        callback("run.started", {"run_id": "run_cont1"})
        callback("assistant.completed", {"content": "You're projected to win by 12."})
        callback("run.completed", {})
        return True
    monkeypatch.setattr(continuity, "stream_session_chat", fake_stream)
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [  # the thread's channel is approved
        {"target": "discord:111", "label": "#work", "topic": "work"}]}})
    posted = []
    monkeypatch.setattr(service.rt.notices, "post",
                        lambda key, text, limit=600, target=None: posted.append((key, target, text)) or True)
    config = home / "config.yaml"
    before = config.read_bytes() if config.exists() else None
    mtime = config.stat().st_mtime_ns if config.exists() else None
    _, worker = start_call(server, service)
    worker.delegate("call_c", "In the league team thread, am I going to win this week?")
    wait_for(lambda: [p for p in posted if p[0].startswith("answer:")])
    answer = [p for p in posted if p[0].startswith("answer:")]
    assert streamed == ["s_league"]
    assert answer == [("answer:run_cont1", "discord:777", "You're projected to win by 12.")]
    assert (config.read_bytes() if config.exists() else None) == before
    assert (config.stat().st_mtime_ns if config.exists() else None) == mtime


def test_conversation_targets_match_hermes_send():
    t = time.time()
    thread = continuity.Conversation("s", "discord", "777", "thread", "777", "", "111", "", "", t)
    topic = continuity.Conversation("s", "telegram", "-100123", "group", "17585", "", "", "", "", t)
    dm = continuity.Conversation("s", "telegram", "555", "dm", "", "42", "", "", "", t)
    assert [c.target for c in (thread, topic, dm)] == ["discord:777", "telegram:-100123:17585", "telegram:555"]


# -- (C) continuity ---------------------------------------------------------------------------------

def test_thread_sessions_opened_for_voice_are_continuation_candidates(tmp_path):
    db = _state_db(tmp_path / "state.db", [("s_thr", "discord", "thread", "999",
                                            {"platform": "discord", "chat_id": "999", "chat_type": "thread",
                                             "chat_name": "My Server / #work / Timer dark mode", "user_id": "42",
                                             "thread_id": "999", "parent_chat_id": "111"})])
    db.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES ('s_thr','assistant','ok',NULL,?)",
               (time.time(),))
    db.commit()
    assert "webhook" in continuity.EXCLUDED_SOURCES  # webhook-only runs stay out; threads carry the chat source
    assert [c.session_id for c in continuity.recent_conversations(tmp_path / "state.db")] == ["s_thr"]


def test_continuity_off_stops_matching_end_to_end(server, service, hermes, monkeypatch):
    matched = []
    conv = continuity.Conversation("s_trip", "telegram", "1", "group", "", "42", "", "Travel",
                                   "Lisbon trip planning", time.time())
    monkeypatch.setattr(continuity, "recent_conversations", lambda db, **kw: matched.append(1) or [conv])
    monkeypatch.setattr(continuity, "match", lambda request, convs: None)
    status, _ = http(server.base_url, "PATCH", "/voice/settings", {"continuity": {"enabled": False}}, server.token)
    assert status == 200
    _, worker = start_call(server, service)
    worker.delegate("call_off", "In the Lisbon trip planning chat, also book a museum")
    wait_for(lambda: len(hermes.calls) == 1)
    assert matched == []  # never even looked
    http(server.base_url, "PATCH", "/voice/settings", {"continuity": {"enabled": True}}, server.token)
    worker.delegate("call_on", "In the Lisbon trip planning chat, also book dinner")
    wait_for(lambda: len(hermes.calls) == 2)
    assert matched  # on again: it looks


def test_onboarding_can_turn_continuity_off(server):
    status, body = http(server.base_url, "POST", "/voice/onboarding", {"continuity_enabled": False}, server.token)
    assert status == 200 and body["settings"]["continuity"]["enabled"] is False and body["continuity_enabled"] is False
    assert http(server.base_url, "POST", "/voice/onboarding", {"continuity_enabled": "no"}, server.token)[0] == 400


# -- Suggest channels ------------------------------------------------------------------------------

def _destinations(home):
    (home / "gateway_state.json").write_text(json.dumps({"platforms": {"discord": {"state": "connected"}}}))
    (home / "channel_directory.json").write_text(json.dumps({"platforms": {"discord": [
        {"id": "111", "name": "work", "guild": "My Server", "type": "channel"},
        {"id": "222", "name": "research", "guild": "My Server", "type": "channel"}]}}))


def test_suggest_channels_validates_and_never_saves(server, service, home):
    _destinations(home)
    reply = json.dumps([
        {"target": "discord:111", "label": "#work", "topic": "coding and apps", "new_thread": True},
        {"target": "discord:999", "label": "#ghost", "topic": "not offered"},
        {"target": "discord:222", "label": "{bad}", "topic": "x"},
        {"target": "discord:222", "label": "#research", "topic": "reading up on things"}])
    service._suggest_run = lambda prompt, idem: ("completed", "Here you go:\n```json\n" + reply + "\n```")
    status, body = http(server.base_url, "POST", "/voice/destinations/suggest", {}, server.token)
    assert status == 200
    assert [s["target"] for s in body["suggestions"]] == ["discord:111", "discord:222"]
    assert service.settings.get()["delivery"]["channels"] == []


def test_suggest_prompt_lists_only_labels_and_targets(home):
    _destinations(home)
    from speakeasy import delivery as D
    seen = {}
    with pytest.raises(suggest.SuggestError):
        suggest.suggest(lambda prompt, idem: seen.update(prompt=prompt) or ("completed", "[]"),
                        D.flat_chats(D.destinations(home)), False)
    assert "discord:111" in seen["prompt"] and "My Server / work" in seen["prompt"]


def test_suggest_failure_is_plain_text(server, service, home):
    _destinations(home)
    service._suggest_run = lambda prompt, idem: ("failed", "")
    status, body = http(server.base_url, "POST", "/voice/destinations/suggest", {}, server.token)
    assert status == 502 and "suggest" in body["error"].lower()
    service._suggest_run = lambda prompt, idem: ("completed", "I think #work is nice")
    assert http(server.base_url, "POST", "/voice/destinations/suggest", {}, server.token)[0] == 502


def test_status_shows_routing_model_and_tailscale(server):
    status, body = http(server.base_url, "GET", "/voice/status", token=server.token)
    assert status == 200 and body["routing_model"] and "speakeasy_router" in body["routing_hint"]
    assert body["advertised_url"] == "" and "tailscale_name" in body


def test_delivery_is_named_by_its_chat_not_just_the_platform(tmp_path):
    from speakeasy import delivery as D
    (tmp_path / "channel_directory.json").write_text(json.dumps({"platforms": {"discord": [
        {"id": "555", "name": "Home server / #voice", "type": "group"},
        {"id": "555", "name": "voice", "guild": "Home server", "type": "channel"}]}}))
    assert D.target_label("discord:555", tmp_path) == "#voice on Discord"
    assert D.target_label("discord:777", tmp_path) == "your Discord"  # unknown chat: the platform
    assert D.target_label("telegram") == "your Telegram"
    tour = P.tour_block(P.Names("Sam", "Hermes"), {}, D.target_label("discord:555", tmp_path))
    assert "#voice on Discord" in tour and "settings" in tour


def test_routing_call_runs_in_the_plugin_profile_scope(monkeypatch):
    from speakeasy import router
    import contextlib, sys, types
    entered = []

    @contextlib.contextmanager
    def fake_scope(home):
        entered.append(str(home))
        yield
    secret_scope = types.SimpleNamespace(is_multiplex_active=lambda: True, current_secret_scope=lambda: None)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", secret_scope)
    monkeypatch.setitem(sys.modules, "gateway.run", types.SimpleNamespace(_profile_runtime_scope=fake_scope))
    monkeypatch.setattr(router, "_HOME", "/tmp/hermes-home")
    with router._profile_scope():
        pass
    assert entered == ["/tmp/hermes-home"]


# -- a pause mid-request stays one task -----------------------------------------------------------

def test_a_short_tail_right_after_a_task_joins_it():
    running = [router.OpenTask("t1", "How's my league team doing", "running", "", 8.0)]
    for tail in ("gonna win", "and am I gonna win?", "or should I bench someone"):
        decision = router.decide(tail, running, None, [], call=lambda m: (_ for _ in ()).throw(AssertionError))
        assert decision.parts[0].kind == "follow_up" and decision.parts[0].task_id == "t1", tail
    # Too late, not running, or a full request of its own: a new task.
    late = [router.OpenTask("t1", "How's my league team doing", "running", "", 120.0)]
    done = [router.OpenTask("t1", "How's my league team doing", "completed", "", 8.0)]
    assert router.route("gonna win", late)[0].kind == "new"
    assert router.route("gonna win", done)[0].kind == "new"
    assert router.route("book dinner for four on Friday at the Italian place", running)[0].kind == "new"


def test_the_routing_model_sees_how_recently_tasks_started():
    msgs = router.route_messages("gonna win", [router.OpenTask("t1", "league team", "running", "", 8.4)], [])
    assert "started 8s ago" in msgs[1]["content"] and "People pause mid-thought" in msgs[0]["content"]


def test_a_tail_during_a_thread_task_joins_that_thread(server, service, hermes, monkeypatch):
    import threading as _th
    release = _th.Event()

    class SlowThreads(FakeThreads):
        def wait(self, opened, on_session, on_title=None):
            on_session("thread_session_1")
            release.wait(10)
            return self.answer

    conv = continuity.Conversation("thread_session_1", "discord", "999", "thread", "999", "42", "111",
                                   "Server / #work / League", "League team", time.time())
    sent = []
    monkeypatch.setattr(continuity, "conversation_by_session", lambda db, sid: conv if sid == "thread_session_1" else None)
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **k: False)

    def fake_stream(base, key, c, message, callback, **k):
        sent.append((c.session_id, message))
        callback("run.started", {"run_id": "run_tail1"})
        callback("assistant.completed", {"content": "You're projected to win by 12."})
        callback("run.completed", {})
        return True
    monkeypatch.setattr(continuity, "stream_session_chat", fake_stream)
    service.rt.threads = SlowThreads()
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [dict(WORK, new_thread=True)]}})
    _, worker = start_call(server, service)
    worker.delegate("call_a", "Put this in work: how's my league team doing")
    first = wait_for(lambda: next(iter(worker.interaction.runs.values()), None))
    wait_for(lambda: service.store.continued_for(first.idem_key))
    worker.feed({"type": "session.output_transcript.delta", "delta": "On it.", "start_ms": 3, "end_ms": 4})
    worker.delegate("call_b", "gonna win")
    from speakeasy.calls import interaction_tasks
    panel = lambda: interaction_tasks(service.store, worker.interaction)
    time.sleep(0.5)
    assert len(panel()) == 1 and len(tasks(server)) == 1  # the tail joins the task; no second row
    assert "joined" not in str(spoken(worker)) and sent == []  # waits for the first turn
    release.set()
    wait_for(lambda: sent)
    assert sent[0][0] == "thread_session_1" and "gonna win" in sent[0][1]
    final = wait_for(lambda: [t for t in panel() if t["status"] == "completed" and "12" in str(t.get("result"))])
    assert len(panel()) == 1 and len(tasks(server)) == 1 and final


def test_task_gets_a_semantic_name_after_the_instant_label(home, hermes):
    from speakeasy.service import VoiceService
    from fakes import FakeLiveWorker, FakeTransport
    workers = []
    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                       openai_negotiate=lambda key, payload: {"session": {"id": "sess_fake"}, "transport": {"sdp": "v=0\r\n"}},
                       openai_worker=lambda rt, i: workers.append(FakeLiveWorker(rt, i)) or workers[-1],
                       route_call=lambda m: None,
                       title_call=lambda request: "Tomorrow's weather in New York",
                       polish_call=lambda request: "Forecast for Chicago on Friday?")
    svc.settings.patch({"voice": {"provider": "openai"}})
    try:
        svc.create_session({"sdp": SDP}, "req_title")
        workers[-1].delegate("call_t", "Chicago forecast for Friday please")
        wait_for(lambda: len(hermes.calls) == 1)
        key = next(iter(workers[-1].interaction.runs.values())).idem_key
        wait_for(lambda: svc.store.title(key) == "Tomorrow's weather in New York")
        wait_for(lambda: (svc.store.work(idem_key=key) or {}).get("summary")
                 == "Forecast for Chicago on Friday?")
        assert svc.store.request_text(key).startswith("Chicago forecast for Friday")  # raw words kept
    finally:
        svc.close()


def test_semantic_name_never_overwrites_a_better_name(home, hermes):
    from speakeasy.service import VoiceService
    from fakes import FakeLiveWorker, FakeTransport
    import threading
    gate = threading.Event()
    workers = []

    def slow_title(request):
        gate.wait(5)
        return "Model title"

    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                       openai_negotiate=lambda key, payload: {"session": {"id": "sess_fake"}, "transport": {"sdp": "v=0\r\n"}},
                       openai_worker=lambda rt, i: workers.append(FakeLiveWorker(rt, i)) or workers[-1],
                       route_call=lambda m: None, title_call=slow_title, polish_call=lambda r: None)
    svc.settings.patch({"voice": {"provider": "openai"}})
    try:
        svc.create_session({"sdp": SDP}, "req_title2")
        workers[-1].delegate("call_t2", "Check the status of my league team")
        wait_for(lambda: len(hermes.calls) == 1)
        key = next(iter(workers[-1].interaction.runs.values())).idem_key
        svc.store.set_title(key, "Thread name from Hermes")
        gate.set()
        time.sleep(0.5)
        assert svc.store.title(key) == "Thread name from Hermes"
    finally:
        svc.close()


def test_polished_request_rejects_answers_and_junk():
    said = "um so like check the forecast friday in chicago I guess"
    assert router.clean_polished('{"request": "Check the Chicago forecast for Friday."}', said) \
        == "Check the Chicago forecast for Friday."
    assert router.clean_polished("```json\n{\"request\": \"Check the weather.\"}\n```", said) == "Check the weather."
    assert router.clean_polished('{"request": ""}', said) is None
    assert router.clean_polished('{"request": "' + "It will be sunny with highs near 70. " * 10 + '"}', said) is None
    assert router.clean_polished("{not json", said) is None


def test_thread_task_asks_for_an_email_draft_card():
    names = P.Names.from_settings(S.validate({}))
    message = P.thread_task_message(names, "write me a note summarizing the Portugal plans", "Portugal email", "")
    assert "`email-draft`" in message and "do NOT send" in message


def test_thread_email_draft_becomes_an_approvable_card(server, service, hermes):
    draft = {"from": "me@example.com", "to": ["me@example.com"], "cc": [], "bcc": [],
             "subject": "Portugal trip details", "body": "Flights and hotels."}
    answer = ("Voice: Portugal email\nHere is the draft.\n\n```email-draft\n" + json.dumps(draft)
              + "\n```\nDONE: drafted\nSPOKEN: I drafted it; approve on the card.")
    service.rt.threads = FakeThreads(answer=answer)
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [dict(WORK, new_thread=True)]}})
    _, worker = start_call(server, service)
    worker.delegate("call_e", "Put this in work: write me a note summarizing the Portugal plans")
    done = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed"])[0]
    assert "email-draft" not in done["result"]["full"]
    key = next(iter(worker.interaction.runs.values())).idem_key
    cards = service.store.drafts_for(key)
    assert len(cards) == 1 and cards[0]["subject"] == "Portugal trip details" and cards[0]["status"] == "pending"
    # Approving goes back into the thread's own session, never a fresh one.
    assert service.store.draft(cards[0]["draft_id"])["_session_id"] == "thread_session_1"


def test_voice_rules_never_claim_a_draft_before_it_exists():
    text = P.rules_text(P.Names.from_settings(S.validate({})), "Telegram", [])
    email = text.split("# Email drafts", 1)[1]
    assert "I drafted it" not in email and "never that it is drafted" in email


def test_new_task_shows_what_it_was_handed_instead_of_a_bare_wait(server, service, hermes):
    service.rt.title_call = lambda request: "Draft Portugal trip email"
    service.rt.polish_call = lambda request: "Write me a note summarizing the Portugal plans."
    service.rt.status_call = lambda request: "Drafting your Portugal trip email"
    hermes.hold = True   # Hermes is still working and hasn't reported any progress of its own
    _, worker = start_call(server, service)
    worker.delegate("call_s", "uh, write me a note summarizing the Portugal plans")
    task = wait_for(lambda: [t for t in tasks(server) if t.get("short_status") == "Drafting your Portugal trip email"])[0]
    assert task["title"] == "Draft Portugal trip email"
    assert task["detail"] == "Handed to Hermes: Write me a note summarizing the Portugal plans."


def test_a_handed_off_task_gets_a_plan_for_its_progress_line(server, service, hermes):
    service.rt.title_call = lambda request: "Plan Portugal trip"
    service.rt.plan_call = lambda request: ["Compare flights", "Draft itinerary", "Your review"]
    hermes.hold = True
    _, worker = start_call(server, service)
    worker.delegate("call_p", "plan me a long weekend in Portugal")
    task = wait_for(lambda: [t for t in tasks(server) if t.get("plan")])[0]
    assert task["plan"] == ["Compare flights", "Draft itinerary", "Your review"]


def test_plan_output_is_validated():
    from speakeasy import router
    assert router.clean_plan('{"steps": ["compare flights", "Draft itinerary.", "Your review"]}') == [
        "Compare flights", "Draft itinerary", "Your review"]
    assert router.clean_plan('{"steps": ["Only one", "two"]}') is None   # too few to be a plan
    assert router.clean_plan('{"steps": ["Search", "a step that rambles on for far too many words here", "Done", "Review"]}') == [
        "Search", "Done", "Review"]
    assert router.clean_plan("no json") is None


def test_shape_output_is_validated():
    from speakeasy import router
    raw = json.dumps({"type": "build", "headline": "Dice app is live and tested",
                      "checks": [{"label": "Roll parser", "value": "passed", "good": True}, {"label": "", "value": "x"}],
                      "shipped": ["Dice app is live", ""], "open": ["Public link needs your go-ahead"],
                      "sections": [{"title": "Dice", "points": ["Tap a die to build a roll"]}, {"points": ["no title"]}],
                      "undo": False})
    shape = router.clean_shape("here you go " + raw)
    assert shape["type"] == "build" and shape["checks"] == [{"label": "Roll parser", "value": "passed", "good": True}]
    assert shape["shipped"] == ["Dice app is live"] and [s["title"] for s in shape["sections"]] == ["Dice"]
    assert router.clean_shape(json.dumps({"type": "poem", "open": ["x"]})) is None          # unknown type
    assert router.clean_shape(json.dumps({"type": "answer", "headline": "Nothing else"})) is None   # no body
    assert router.clean_shape('{"type": "build", "open": ["cut off') is None


def test_a_long_finished_report_gets_its_shape(server, service, hermes):
    seen = []
    service.rt.shape_call = lambda request, report: seen.append(report) or {
        "type": "build", "headline": "Fixed and deployed", "checks": [], "shipped": ["Fix is live"],
        "open": ["Reindex one book"], "sections": [], "undo": True}
    hermes.responder = lambda prompt, sid: ("Fixed it and deployed. " + "Details of the fix and the checks I ran. " * 20
                                            + "\nDONE: Fixed\nSPOKEN: Fixed and deployed.")
    _, worker = start_call(server, service)
    worker.delegate("call_shape", "fix the library bug and deploy it")
    task = wait_for(lambda: [t for t in tasks(server) if ((t.get("result") or {}).get("shape") or {}).get("type")])[0]
    assert task["result"]["shape"]["open"] == ["Reindex one book"]
    assert seen and seen[0].startswith("Fixed it and deployed.")


def test_handoff_status_never_overrides_real_progress(service):
    store = service.store
    store.reserve_run("se_x", "vi_x", "item_x", 1)
    store.update_run("se_x", "run_x", "running")
    store.user_progress("se_x", "Searching your inbox", "Looking for the booking emails")
    assert store.handoff_status("se_x", "Drafting your Portugal email", "Handed off") is False
    assert store.work(idem_key="se_x")["short_status"] == "Searching your inbox"


def test_working_status_output_is_validated():
    from speakeasy import router
    assert router.clean_status('{"status": "Drafting your Portugal trip email"}') == "Drafting your Portugal trip email"
    assert router.clean_status('{"status": "Your email is drafted and ready to go now"}') is None
    assert router.clean_status("not json at all, and no ing verb") is None


# -- recall of finished reports ----------------------------------------------------------------

def test_a_report_with_code_still_reaches_the_voice_in_full():
    """Live report: asking about a finished report made the voice go check again. An engineering
    report with a code block (or the word 'token:') used to be dropped from the voice entirely."""
    report = ("Fixed the router crash.\n\n```python\ndef route(self, request, tasks, marked, chats=None):\n"
              "    return decide(request)\n```\n\nRoot cause: Runtime.route took 4 arguments but got 6.\n"
              "The refresh token: abc123 lives in .env\n" + "Details line about the fix. " * 150)
    notes = P.result_notes("Fix router crash", report, "I fixed the router crash.")
    joined = " ".join(notes)
    assert notes and all(len(n) <= 2000 for n in notes)
    assert "def route(self, request, tasks, marked" in joined
    assert "Root cause: Runtime.route took 4 arguments but got 6." in joined
    assert "abc123" not in joined            # the secret-looking line is dropped, not the report
    assert len(notes) > 1 and "part 1 of" in notes[0]
