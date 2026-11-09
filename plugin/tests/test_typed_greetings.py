"""A hello typed on Home / New chat is plain chat, never a job (build 70 was withdrawn for "hi" showing
as a started task). The typed path (POST /voice/work with `chat_replies`) and the call's path (a handoff
the voice makes on /voice/sessions) use the same rule: greetings and how-are-you small talk start
nothing; a real ask still starts its job. Apps that don't send the flag (the Mac, older phones) get
the job they always did."""
import time

import pytest

from fakes import http, wait_for
from test_routing import spoken, start_call, tasks
from speakeasy.calls import is_greeting
from speakeasy.prompt import builder as P

GREETINGS = ["hi", "Hi!", "hello there", "hey", "how's it going", "how’s it going?",
             "hey, how's it going?", "Good morning! How are you?", "hi how are you doing today",
             "what's up", "how's your day going"]
REAL_ASKS = ["Find me a standing desk under $400", "hi can you check the parcel",
             "hey how's it going, can you check my flight", "how's the weather in Lisbon",
             "Hey, dim the porch light", "good morning, what's on my calendar"]


def work(server, body, chats=True):
    if chats:
        body = {**body, "chat_replies": True}
    return http(server.base_url, "POST", "/voice/work", body, server.token)


@pytest.mark.parametrize("text", GREETINGS)
def test_greetings_and_small_talk_are_not_work(text):
    assert is_greeting(text), text


@pytest.mark.parametrize("text", REAL_ASKS)
def test_a_real_ask_is_still_work(text):
    assert not is_greeting(text), text


# -- the new path: POST /voice/work answers small talk itself ------------------------------------

def test_status_advertises_chat_replies(server):
    caps = http(server.base_url, "GET", "/voice/status", token=server.token)[1]["capabilities"]
    assert caps["chat_replies"] is True and caps["start_jobs"] is True


@pytest.mark.parametrize("text", ["hi", "how’s it going"])
def test_a_typed_greeting_starts_no_job(server, service, hermes, text):
    status, body = work(server, {"text": text, "source": "typed", "client_id": "hello_1"})
    assert status == 202, body
    assert body["ok"] and body["delivered_as"] == "chat" and body["reply"] == P.GREETING_TYPED
    assert body["run_id"] is None and body["task_id"] is None and body["task"] is None
    time.sleep(0.3)
    assert hermes.calls == []                         # Hermes never saw it: no run, no model call
    assert tasks(server) == []                        # nothing on the board
    assert not [i for i in service.now({})["items"] if i.get("source") == "speakeasy"]  # nor on Home


def test_a_typed_greeting_retry_gets_the_same_chat_reply(server, hermes):
    first = work(server, {"text": "hey", "client_id": "hello_twice"})[1]
    again = work(server, {"text": "hey", "client_id": "hello_twice"})[1]
    assert again["delivered_as"] == "chat" and again["reply"] == first["reply"]
    assert again["receipt_id"] == first["receipt_id"]
    assert hermes.calls == []


def test_a_typed_real_ask_still_starts_its_job(server, hermes):
    status, body = work(server, {"text": "Find me a standing desk under $400", "client_id": "ask_1"})
    assert status == 202, body
    assert body["delivered_as"] == "new_run" and body["run_id"] and body["task_id"].startswith("job_")
    wait_for(lambda: hermes.calls)
    assert "standing desk" in str(hermes.calls[0]["input"])


def test_without_the_flag_a_greeting_is_still_the_job_older_apps_expect(server, hermes):
    status, body = work(server, {"text": "hi", "client_id": "old_app_1"}, chats=False)
    assert status == 202, body
    assert body["delivered_as"] == "new_run" and body["run_id"] and body["task_id"].startswith("job_")
    wait_for(lambda: hermes.calls)


def test_the_flag_must_be_a_bool(server, hermes):
    status, body = work(server, {"text": "hi", "client_id": "bad_flag", "chat_replies": "yes"}, chats=False)
    assert status == 400, body
    assert hermes.calls == []


def test_small_talk_then_a_real_ask_starts_exactly_one_job(server, hermes):
    assert work(server, {"text": "hi", "client_id": "seq_1"})[1]["delivered_as"] == "chat"
    assert work(server, {"text": "how's it going", "client_id": "seq_2"})[1]["delivered_as"] == "chat"
    assert work(server, {"text": "Check if my flight on Friday is on time", "client_id": "seq_3"})[1][
        "delivered_as"] == "new_run"
    wait_for(lambda: hermes.calls)
    time.sleep(0.3)
    assert len(hermes.calls) == 1 and "flight" in str(hermes.calls[0]["input"])


# -- the call's path (/voice/sessions): the voice handing off small talk starts nothing ----------

@pytest.mark.parametrize("words", ["hi", "hey, how's it going?"])
def test_a_handed_off_greeting_on_a_call_starts_nothing(server, service, hermes, words):
    _, worker = start_call(server, service, key="req_hello_" + str(len(words)))
    worker.delegate("call_hello", words)
    wait_for(lambda: P.GREETING_SPOKEN in spoken(worker))
    time.sleep(0.3)
    assert hermes.calls == []
    assert not [t for t in tasks(server) if t.get("run_id")]


def test_a_handed_off_real_ask_on_a_call_still_starts(server, service, hermes):
    _, worker = start_call(server, service, key="req_real_ask")
    worker.delegate("call_ask", "Find me a dentist near the office")
    wait_for(lambda: hermes.calls)
    assert "dentist" in str(hermes.calls[0]["input"])
