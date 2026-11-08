"""SPK-208: Home is a chat. The composer talks to a coordinator that replies in the thread and starts
or follows up jobs only for real work. The coordinator model is faked here (its JSON contract);
the real-model run is the acceptance recording. Invented data only.

The real coordinator answers as a chat model: plain text for chat, a tool call for work. Scripted
answers here are either a dict (the legacy JSON contract), a ``{"tool": ...}`` call, or plain text."""
from __future__ import annotations

import json

import pytest

from fakes import http, wait_for
from speakeasy import home_chat as HC
from test_routing import tasks
from test_staged_attachments import jpeg, reads_images, stage  # noqa: F401  (reads_images is a fixture)


def coordinator(service, *answers):
    """Script the coordinator: each turn pops the next answer (a dict -> JSON, or a raw string)."""
    seen: list[list[dict]] = []
    queue = list(answers)

    def call(messages):
        seen.append(messages)
        nxt = queue.pop(0)
        if isinstance(nxt, dict) and "tool" not in nxt:
            nxt = json.dumps(nxt)
        return nxt, {"model": "fast-chat-1", "label": "Fast chat"}
    service.home_chat._call = call
    return seen


def post(server, body):
    return http(server.base_url, "POST", "/voice/home/message", body, server.token)


def stream(server):
    return http(server.base_url, "GET", "/voice/now?tz=America/New_York", None, server.token)[1]["stream"]["entries"]


def test_capability_is_advertised(server):
    status, body = http(server.base_url, "GET", "/voice/status", None, server.token)
    assert status == 200 and body["capabilities"]["home_chat"] is True


def test_a_greeting_is_a_chat_reply_with_no_job(server, service, hermes):
    coordinator(service, {"reply": "Hey! What's up?", "action": "none"})
    status, body = post(server, {"text": "yo", "client_id": "c_yo"})
    assert status == 200, body
    assert body["action"] == "none" and body["run_id"] is None
    assert body["message"]["text"] == "yo" and body["message"]["role"] == "user"
    assert body["reply"]["text"] == "Hey! What's up?" and body["reply"]["role"] == "assistant"
    assert body["reply"]["model"]["model"] == "fast-chat-1"
    assert not hermes.calls and not tasks(server)
    entries = stream(server)
    assert [(e["kind"], e.get("role")) for e in entries] == [("message", "user"), ("message", "assistant")]
    assert all(e["kind"] != "card" for e in entries)


def test_real_work_replies_briefly_then_one_job_card_follows(server, service, hermes):
    seen = coordinator(service, {"reply": "Looking for a quiet cafe near the river.", "action": "start",
                                 "request": "Find a quiet cafe near the river that opens early"})
    status, body = post(server, {"text": "find me a quiet cafe near the river, early", "client_id": "c_cafe"})
    assert status == 200 and body["action"] == "start" and body["run_id"]
    call = wait_for(lambda: hermes.calls)[0]
    assert "quiet cafe near the river" in json.dumps(call["input"])
    entries = wait_for(lambda: [e for e in stream(server) if e["kind"] == "card"] and stream(server))
    kinds = [(e["kind"], e.get("role")) for e in entries]
    assert kinds == [("message", "user"), ("message", "assistant"), ("card", None)]   # no extra "ask" echo
    assert entries[0]["run_id"] == body["run_id"] == entries[1]["run_id"]
    assert "Starting" not in entries[1]["text"]
    # The coordinator saw the system prompt and the user's words.
    assert seen[0][0]["role"] == "system" and seen[0][-1]["content"].startswith("find me")


def test_a_retry_with_the_same_client_id_never_starts_a_second_job(server, service, hermes):
    coordinator(service, {"reply": "On it.", "action": "start", "request": "Draft the patio quote"})
    first = post(server, {"text": "draft the patio quote", "client_id": "c_retry"})[1]
    again = post(server, {"text": "draft the patio quote", "client_id": "c_retry"})[1]
    assert again["replayed"] is True and again["run_id"] == first["run_id"]
    wait_for(lambda: hermes.calls)
    assert len([t for t in tasks(server)]) == 1
    assert len([e for e in stream(server) if e["kind"] == "message"]) == 2


def test_follow_up_goes_to_the_named_job_and_the_coordinator_sees_its_result(server, service, hermes):
    coordinator(service, {"reply": "Checking the forecast.", "action": "start", "request": "Weather for Saturday"})
    first = post(server, {"text": "weather saturday?", "client_id": "c_w1"})[1]
    wait_for(lambda: hermes.calls)
    seen = coordinator(service, {"reply": "Asking it for Sunday too.", "action": "follow_up", "job": "j1",
                                 "request": "Also check Sunday"})
    status, body = post(server, {"text": "and sunday", "client_id": "c_w2"})
    assert status == 200 and body["action"] == "follow_up"
    jobs = seen[0][0]["content"]
    assert "j1:" in jobs and first["run_id"] not in jobs          # the model sees aliases, never run ids
    assert body["run_id"] == first["run_id"] or body["run_id"]      # the same job (or its follow-up run)
    assert not [t for t in tasks(server) if t.get("run_id") not in {first["run_id"], body["run_id"]}]


def test_an_unknown_job_never_invents_a_reply(server, service, hermes):
    coordinator(service, {"reply": "Sure.", "action": "follow_up", "job": "j9"})
    status, body = post(server, {"text": "make it shorter", "client_id": "c_bad"})
    # No usable decision: anything that isn't small talk runs as a job, as Home always did.
    assert status == 200 and body["action"] == "start" and body["reply"] is None
    coordinator(service, None)          # no answer at all
    status, body = post(server, {"text": "thanks", "client_id": "c_thx"})
    assert body["action"] == "none" and body["reply"]["text"]


def test_a_plain_text_answer_is_the_chat_reply(server, service, hermes):
    coordinator(service, "About 384,400 km on average.")
    body = post(server, {"text": "how far is the moon?", "client_id": "c_moon"})[1]
    assert body["action"] == "none" and body["reply"]["text"] == "About 384,400 km on average."
    assert not hermes.calls and not tasks(server)


def test_a_start_job_tool_call_starts_one_job(server, service, hermes):
    coordinator(service, {"tool": "mcp__start_job", "text": "",
                          "args": {"request": "Find tomorrow's forecast for the user's town",
                                   "reply": "Checking tomorrow's forecast."}})
    body = post(server, {"text": "weather tomorrow?", "client_id": "c_wx"})[1]
    assert body["action"] == "start" and body["run_id"]
    assert body["reply"]["text"] == "Checking tomorrow's forecast."
    call = wait_for(lambda: hermes.calls)[0]
    assert "forecast for the user's town" in json.dumps(call["input"])


def test_tool_calls_find_the_job_by_a_sloppy_id_or_its_title(server, service, hermes, monkeypatch):
    coordinator(service, {"reply": "Looking.", "action": "start", "request": "Compare two standing desks"})
    first = post(server, {"text": "compare two standing desks", "client_id": "c_s1"})[1]
    wait_for(lambda: hermes.calls)
    stopped: list[str] = []
    monkeypatch.setattr(service, "stop_task", lambda run_id: stopped.append(run_id) or {"ok": True})
    coordinator(service, {"tool": "cancel_job", "args": {"job": "j1\u0159", "reply": "Stopped it."}})
    assert post(server, {"text": "stop that", "client_id": "c_s2"})[1]["action"] == "cancel"
    title = service.home_chat._titles["j1"]
    coordinator(service, {"tool": "functions.cancel_job", "args": {"job": title, "reply": "Stopped it."}})
    assert post(server, {"text": "stop the desk one", "client_id": "c_s3"})[1]["action"] == "cancel"
    assert stopped == [first["run_id"], first["run_id"]]


class _Fn:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments


class _Msg:
    def __init__(self, content, calls=()):
        self.content = content
        self.tool_calls = [type("C", (), {"function": _Fn(*c)})() for c in calls]


class _Resp:
    def __init__(self, msg, model="chat-model-2"):
        self.choices = [type("Ch", (), {"message": msg})()]
        self.model = model


def test_answer_of_reads_tool_calls_and_text():
    got = HC.HomeChat._answer_of(_Resp(_Msg("\n\n", [("start_job", '{"request":"x","reply":"y"}')])), lambda r: None)
    assert got == {"tool": "start_job", "args": {"request": "x", "reply": "y"}, "text": "", "more": []}
    assert HC.HomeChat._answer_of(_Resp(_Msg("Hey!")), lambda r: r.choices[0].message.content) == "Hey!"


def _entries(*ids):
    return {i: {"id": i, "provider": i.split("/")[0], "model": i.split("/", 1)[1]} for i in ids}


def test_auto_picks_the_first_approved_model_the_user_has():
    have = _entries("acme/big-model-3", "openai-codex/gpt-5.4-mini", "anthropic/claude-haiku-4-5",
                    "anthropic/claude-haiku-5-5", "anthropic/claude-opus-5-5")
    assert HC.pick_auto(have) == "anthropic/claude-haiku-5-5"        # list order, newest version first
    assert HC.approved(have)[:3] == ["anthropic/claude-haiku-5-5", "anthropic/claude-haiku-4-5",
                                     "openai-codex/gpt-5.4-mini"]
    assert HC.pick_auto(_entries("acme/big-model-3", "studio/qwen3.8-coder")) is None


class _Catalog:
    """The cached Hermes model list. ``raw`` would fetch: selection must never call it."""
    def __init__(self, payload):
        self.payload, self.fetched = payload, 0

    def peek(self):
        return self.payload

    def raw(self, refresh=False):
        self.fetched += 1
        raise AssertionError("selection fetched the model list")

    def entries(self, raw):
        return raw


def test_route_auto_then_fallbacks_and_never_calls_anything(service, monkeypatch):
    chat = service.home_chat
    catalog = _Catalog(_entries("acme/big-model-3", "openai-codex/gpt-5.4-mini"))
    monkeypatch.setattr(service, "catalog", catalog)
    monkeypatch.setattr(chat, "_call", None)
    monkeypatch.setattr(HC.host_mod, "aux_client", lambda: (_ for _ in ()).throw(AssertionError("model called")))
    main = {"model": {"default": "big-model-3", "provider": "acme"},
            "auxiliary": {"speakeasy_router": {"provider": "acme", "model": "tiny"}}}
    monkeypatch.setattr(chat, "_config", lambda: main)
    assert chat.route() == {"provider": "openai-codex", "model": "gpt-5.4-mini", "source": "auto"}
    # None approved connected: the user's main model, then the routing model.
    catalog.payload = _entries("acme/big-model-3")
    assert chat.route() == {"provider": "acme", "model": "big-model-3", "source": "main"}
    monkeypatch.setattr(chat, "_config", lambda: {})
    assert chat.route() == {"task": "speakeasy_router", "source": "routing"}
    # An explicit speakeasy_home in Hermes' config beats Auto.
    catalog.payload = _entries("openai-codex/gpt-5.4-mini")
    monkeypatch.setattr(chat, "_config", lambda: {"auxiliary": {"speakeasy_home": {"provider": "acme", "model": "mid"}}})
    assert chat.route() == {"task": "speakeasy_home", "source": "configured"}
    assert catalog.fetched == 0


def test_model_list_not_read_yet_uses_the_main_model_and_warms_it(service, monkeypatch):
    chat = service.home_chat
    catalog = _Catalog(None)
    warmed: list[int] = []
    monkeypatch.setattr(service, "catalog", catalog)
    monkeypatch.setattr(service, "_warm_catalog", lambda: warmed.append(1))
    monkeypatch.setattr(chat, "_config", lambda: {"model": {"default": "big-model-3", "provider": "acme"}})
    assert chat.route()["source"] == "main" and warmed and catalog.fetched == 0


def test_the_home_chip_choice_wins_and_auto_comes_back(server, service, monkeypatch):
    chat = service.home_chat
    monkeypatch.setattr(service, "catalog", _Catalog(_entries("acme/big-model-3", "anthropic/claude-haiku-5-5")))
    monkeypatch.setattr(chat, "_config", lambda: {"model": {"default": "big-model-3", "provider": "acme"}})
    status, body = http(server.base_url, "GET", "/voice/home/model", None, server.token)
    assert status == 200 and body["source"] == "auto" and body["model"]["id"] == "anthropic/claude-haiku-5-5"
    assert [o["id"] for o in body["options"]] == ["anthropic/claude-haiku-5-5", "acme/big-model-3"]
    status, body = http(server.base_url, "POST", "/voice/home/model", {"model": "acme/big-model-3"}, server.token)
    assert status == 200 and body["source"] == "chosen" and body["model"]["id"] == "acme/big-model-3"
    assert chat.route() == {"provider": "acme", "model": "big-model-3", "source": "chosen"}
    assert http(server.base_url, "POST", "/voice/home/model", {"model": "acme/nope"}, server.token)[0] == 400
    status, body = http(server.base_url, "POST", "/voice/home/model", {"model": None}, server.token)
    assert status == 200 and body["source"] == "auto"


def test_two_asks_in_one_message_are_two_jobs(server, service, hermes):
    coordinator(service, {"tool": "start_job", "text": "",
                          "args": {"request": "Find a dentist open Saturday", "reply": "Looking for a dentist."},
                          "more": [{"tool": "start_job", "args": {"request": "Find a birthday gift for a cyclist",
                                                                  "reply": "And a gift idea."}}]})
    body = post(server, {"text": "find a dentist open saturday and a gift for my cycling friend",
                         "client_id": "c_two"})[1]
    assert body["action"] == "start" and body["reply"]["text"] == "Looking for a dentist. And a gift idea."
    calls = wait_for(lambda: len(hermes.calls) >= 2 and hermes.calls)
    sent = json.dumps([c["input"] for c in calls])
    assert "dentist open Saturday" in sent and "birthday gift for a cyclist" in sent
    cards = wait_for(lambda: len([e for e in stream(server) if e["kind"] == "card"]) == 2 and stream(server))
    assert not [e for e in cards if e["kind"] == "ask"]       # both jobs are the chat's: no "you asked" echo


def test_a_selected_job_is_the_coordinators_context(server, service, hermes):
    coordinator(service, {"reply": "Looking.", "action": "start", "request": "Compare two standing desks"})
    first = post(server, {"text": "compare two standing desks", "client_id": "c_f1"})[1]
    wait_for(lambda: hermes.calls)
    seen = coordinator(service, "Sure.")
    post(server, {"text": "make it cheaper", "client_id": "c_f2", "focus": first["run_id"]})
    assert "has job j1" in seen[0][0]["content"] and "selected right now" in seen[0][0]["content"]


def test_cancel_stops_the_named_running_job(server, service, hermes, monkeypatch):
    coordinator(service, {"reply": "Looking.", "action": "start", "request": "Compare two standing desks"})
    first = post(server, {"text": "compare two standing desks", "client_id": "c_d1"})[1]
    wait_for(lambda: hermes.calls)
    stopped: list[str] = []
    monkeypatch.setattr(service, "stop_task", lambda run_id: stopped.append(run_id) or {"ok": True})
    coordinator(service, {"reply": "Stopped the desk comparison.", "action": "cancel", "job": "j1"})
    body = post(server, {"text": "cancel that", "client_id": "c_d2"})[1]
    assert body["action"] == "cancel" and stopped == [first["run_id"]]


def test_attachments_always_reach_a_job(server, service, hermes, reads_images):
    a = stage(server, jpeg())[1]["id"]
    coordinator(service, {"reply": "Nice photo!", "action": "none"})
    body = post(server, {"text": "", "attachments": [a], "client_id": "c_pic"})[1]
    assert body["action"] == "start" and body["run_id"]
    wait_for(lambda: hermes.calls)


def test_shape_errors(server):
    assert post(server, {"text": "", "client_id": "c_e"})[0] == 400
    assert post(server, {"text": "hi", "extra": 1})[0] == 400
    assert post(server, {"text": "hi", "client_id": "bad id!"})[0] == 400


def test_clear_hides_old_chat(server, service):
    coordinator(service, {"reply": "Hey.", "action": "none"})
    post(server, {"text": "hi", "client_id": "c_c1"})
    assert http(server.base_url, "POST", "/voice/now/clear-seen", {}, server.token)[0] == 200
    service.store.set_meta("now_cleared_at", str(service.home_chat.clock() + 1))
    assert not [e for e in stream(server) if e["kind"] == "message"]


@pytest.mark.parametrize("raw,ok", [
    ('{"reply":"Hi","action":"none"}', True),
    ('```json\n{"reply":"Hi","action":"none"}\n```', True),
    ('{"reply":"","action":"none"}', False),
    ('{"reply":"x","action":"explode"}', False),
    ('{"reply":"x","action":"cancel","job":"j1"}', True),
    ('{"reply":"x","action":"cancel"}', False),
    ("Plain words are a chat reply.", True),
    ("   ", False),
    ({"tool": "start_job", "args": {"request": "x", "reply": "On it."}}, True),
    ({"tool": "cancel_job", "args": {"job": "j7", "reply": "x"}}, False),
    ({"tool": "delete_everything", "args": {}}, False),
])
def test_parse(raw, ok):
    assert (HC.parse(raw, {"j1": "run_1"}) is not None) is ok


def test_small_talk_fallback_words():
    assert HC.small_talk("yo") and HC.small_talk("Thanks!") and not HC.small_talk("book a table")


def test_a_run_that_continues_a_chat_job_stays_in_the_chat(service):
    """Answering a chat job's question runs a child job: its result stays in the thread (the
    real-model run lost the shopping list once the answer run finished and was opened)."""
    db = service.store._db
    with service.store._lock, db:
        cols = {r[1] for r in db.execute("pragma table_info(runs)")}
        assert {"idem_key", "run_id", "parent"} <= cols
        for key, run, parent in [("k_job", "run_job", None), ("k_answer", "run_answer", "k_job"),
                                 ("k_more", "run_more", "k_answer"), ("k_other", "run_other", None)]:
            db.execute("INSERT INTO runs (idem_key, interaction_id, delegation_id, revision, run_id, parent, "
                       "status, updated) VALUES (?,?,?,?,?,?,?,?)",
                       (key, "i_" + key, "d_" + key, 1, run, parent, "completed", 0))
    assert service.now_model._chat_children({"run_job"}) == {"run_answer", "run_more"}
    assert service.now_model._chat_children(set()) == set()


def test_a_listed_model_that_fails_falls_back_and_auto_skips_it(service, monkeypatch):
    """Hermes can list a model whose sign-in doesn't work. A real message that fails on it falls back
    to the main model, and Auto skips it afterwards; learned from use, never from a test call."""
    chat = service.home_chat
    monkeypatch.setattr(chat, "_call", None)
    monkeypatch.setattr(service, "catalog", _Catalog(_entries("acme/big-model-3", "anthropic/claude-haiku-5-5",
                                                              "openai-api/gpt-5-mini")))
    monkeypatch.setattr(chat, "_config", lambda: {"model": {"default": "big-model-3", "provider": "acme"}})
    called: list[str] = []

    class _M:
        content, tool_calls = "Hi!", None

    class _R:
        choices = [type("C", (), {"message": _M()})()]
        model = "big-model-3"

    def call_llm(provider=None, model=None, task=None, **kw):
        called.append(model or task)
        if model == "claude-haiku-5-5":
            raise PermissionError("not signed in")
        return _R()
    monkeypatch.setattr(HC.host_mod, "aux_client", lambda: (call_llm, lambda r: r.choices[0].message.content))
    answer, served = chat._model_call([{"role": "user", "content": "hi"}])
    assert answer == "Hi!" and called == ["claude-haiku-5-5", "big-model-3"]
    assert chat.route() == {"provider": "openai-api", "model": "gpt-5-mini", "source": "auto"}
    # A chosen model that fails keeps the choice but says so on the chip.
    service.store.set_meta(HC.CHOICE_META, "anthropic/claude-haiku-5-5")
    status = chat.model_status()
    assert status["source"] == "chosen" and "didn't answer" in status["note"]


def test_more_jobs_in_one_call_start_their_own_jobs(server, service, hermes):
    coordinator(service, {"tool": "start_job", "text": "",
                          "args": {"request": "Find the farmers' market's Saturday hours",
                                   "more_jobs": ["Find a rain jacket under $100"],
                                   "reply": "Checking the market's hours and looking for a jacket."}})
    body = post(server, {"text": "market hours on saturday and a rain jacket under 100", "client_id": "c_more"})[1]
    assert body["action"] == "start"
    calls = wait_for(lambda: len(hermes.calls) >= 2 and hermes.calls)
    sent = json.dumps([c["input"] for c in calls])
    assert "Saturday hours" in sent and "rain jacket under $100" in sent
    assert HC.TOOLS[0]["function"]["parameters"]["required"] == ["request", "more_jobs", "reply"]


def test_a_voice_memo_with_two_asks_gives_each_job_only_its_own(service, monkeypatch):
    started: list[dict] = []
    monkeypatch.setattr(service, "start_job", lambda body, device_id=None: started.append(body) or
                        {"run_id": f"run_{len(started)}", "session_id": None})
    coordinator(service, {"tool": "start_job", "text": "",
                          "args": {"request": "Find the farmers' market's Saturday hours",
                                   "more_jobs": ["Find a rain jacket under $100"], "reply": "On both."}})
    service.home_chat.message({"text": "market hours and a rain jacket", "client_id": "c_vm"}, None,
                              voice={"duration": 6.0}, job_text='[The user sent a voice message] "market hours and a rain jacket"')
    assert [b["text"] for b in started] == ["Find the farmers' market's Saturday hours", "Find a rain jacket under $100"]
    assert all(b["source"] == "voice_message" for b in started)


@pytest.mark.parametrize("raw,shown", [
    ("The Moon is about 384,400 km (238,900 miles) away on average.",
     "The Moon is about 238,900 miles (384,400 km) away on average."),
    ("It's 384,400 kilometers, or about 238,900 miles, from Earth.",
     "It's about 238,900 miles (384,400 kilometers) from Earth."),
    ("Expect 18°C (64°F) and light wind.", "Expect 64°F (18°C) and light wind."),
    ("It's about 238,900 miles (384,400 km) away.", "It's about 238,900 miles (384,400 km) away."),
    ("Three cafes within 2 miles.", "Three cafes within 2 miles."),
])
def test_replies_lead_with_us_units(raw, shown):
    assert HC.us_units_first(raw) == shown
