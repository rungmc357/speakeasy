"""SPK-560: an existing messaging chat is continued only when the ask clearly refers to it.

Live bug: a fresh question on a call was routed by the model into an old Discord thread about
something else and posted there. Continuing a chat (and so posting into its thread) needs the ask
to name the chat or share a real subject with its title or recent messages; otherwise new task.
"""
from __future__ import annotations

import time

from fakes import SDP, http, wait_for
from speakeasy import continuity, router
from speakeasy.calls import SidebandWorker as CallWorker

PICK_C1 = '{"follow_up_task_id": null, "conversation": "c1", "parts": ["x"], "channel": null}'
FRESH_ASK = "How much were the tickets for the jazz show I bought?"
GROCERY = continuity.Conversation("s_groc", "discord", "777", "thread", "777", "42", "111",
                                  "Server / #home / Weekend grocery list", "Weekend grocery list", time.time())
GROCERY_TALK = ("[sam] eggs, bread and apples for saturday", "[sam] we're out of coffee filters too")


def _cands():
    cands = [continuity.Candidate(GROCERY, GROCERY_TALK)]
    return cands, [router.Chat("c1", GROCERY.where, GROCERY_TALK)]


def test_a_model_pick_is_dropped_when_the_ask_shares_nothing_with_the_chat():
    cands, chats = _cands()
    model = router.Decision([], conversation="c1", source="model")
    assert CallWorker.pick_conversation(FRESH_ASK, model, cands, chats) is None


def test_a_model_pick_stands_when_the_ask_names_or_shares_the_chat_subject():
    cands, chats = _cands()
    model = router.Decision([], conversation="c1", source="model")
    named = CallWorker.pick_conversation("Add oat milk to the weekend grocery list chat", model, cands, chats)
    assert named is not None and named.session_id == "s_groc"
    talk = CallWorker.pick_conversation("did we already get the coffee filters?", model, cands, chats)
    assert talk is not None and talk.session_id == "s_groc"


def test_stopwords_and_time_words_alone_never_make_a_match():
    cands, chats = _cands()
    model = router.Decision([], conversation="c1", source="model")
    assert CallWorker.pick_conversation("what were we doing this weekend?", model, cands, chats) is None


def test_one_shared_word_with_old_talk_is_not_enough():
    # A packing chat that mentioned train tickets is not where a show's ticket price lives.
    packing = continuity.Conversation("s_pack", "discord", "778", "thread", "778", "42", "111",
                                      "Server / #home / Trip packing list", "Trip packing list", time.time())
    talk = ("[sam] passports and the train tickets go in the blue bag",)
    cands = [continuity.Candidate(packing, talk)]
    chats = [router.Chat("c1", packing.where, talk)]
    model = router.Decision([], conversation="c1", source="model")
    assert CallWorker.pick_conversation(FRESH_ASK, model, cands, chats) is None
    named = CallWorker.pick_conversation("add sunscreen to the trip packing list", model, cands, chats)
    assert named is not None and named.session_id == "s_pack"


def _start_call(server, service, key):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def _wire(service, hermes, monkeypatch):
    monkeypatch.setattr(continuity, "conversations_with_context",
                        lambda db, request, **kw: [continuity.Candidate(GROCERY, GROCERY_TALK)])
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **k: False)
    monkeypatch.setattr(router, "aux_call", lambda messages, timeout=router.ROUTE_TIMEOUT_S: PICK_C1)
    streamed, said, posted, prompts = [], [], [], []

    def fake_stream(base, key, c, message, callback, **k):
        streamed.append(c.session_id)
        callback("run.started", {"run_id": "run_groc1"})
        callback("assistant.completed", {"content": "Added oat milk."})
        callback("run.completed", {})
        return True
    monkeypatch.setattr(continuity, "stream_session_chat", fake_stream)

    class Runner:
        def available(self, target): return True
        def post(self, platform, thread_id, *, message, delivery_id):
            said.append((platform, thread_id, message)); return True
        def wait_turn(self, platform, thread_id, after_id, on_activity=None):
            return "Added.\nSPOKEN: Added."
    service.rt.threads = Runner()
    hermes.responder = lambda prompt, sid: prompts.append(prompt) or "About forty dollars.\nSPOKEN: About forty dollars."
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [
        {"target": "discord:111", "label": "#home", "topic": "home"}]}})
    monkeypatch.setattr(service.rt.notices, "post",
                        lambda key, text, limit=600, target=None: posted.append((key, target, text)) or True)
    return streamed, said, posted, prompts


def test_a_fresh_ask_becomes_a_new_task_and_nothing_is_posted_in_the_thread(server, service, hermes, monkeypatch):
    streamed, said, posted, prompts = _wire(service, hermes, monkeypatch)
    _, worker = _start_call(server, service, "req_spk560_new")
    worker.delegate("call_spk560_new", FRESH_ASK)
    wait_for(lambda: prompts)
    assert any("jazz show" in p for p in prompts)  # it ran as its own task
    time.sleep(0.3)
    assert streamed == [] and said == []
    assert not [p for p in posted if p[1] == "discord:777"]


def test_an_ask_that_names_the_chat_continues_it(server, service, hermes, monkeypatch):
    streamed, said, posted, prompts = _wire(service, hermes, monkeypatch)
    _, worker = _start_call(server, service, "req_spk560_cont")
    worker.delegate("call_spk560_cont", "Add oat milk to the weekend grocery list chat")
    wait_for(lambda: streamed)
    assert streamed == ["s_groc"]
