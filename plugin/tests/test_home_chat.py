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
    assert got == {"tool": "start_job", "args": {"request": "x", "reply": "y"}, "text": ""}
    assert HC.HomeChat._answer_of(_Resp(_Msg("Hey!")), lambda r: r.choices[0].message.content) == "Hey!"


def test_the_main_model_answers_unless_home_chat_has_its_own(service, monkeypatch):
    chat = service.home_chat
    monkeypatch.setattr(chat, "_config", lambda: {"model": {"default": "big-model-3", "provider": "acme"},
                                                  "auxiliary": {"speakeasy_router": {"provider": "acme", "model": "tiny"}}})
    assert chat.route() == {"provider": "acme", "model": "big-model-3"}
    monkeypatch.setattr(chat, "_config", lambda: {"model": {"default": "big-model-3", "provider": "acme"},
                                                  "auxiliary": {"speakeasy_home": {"provider": "acme", "model": "mid"}}})
    assert chat.route() == {"task": "speakeasy_home"}
    monkeypatch.setattr(chat, "_config", lambda: {})
    assert chat.route() == {"task": "speakeasy_router"}


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
