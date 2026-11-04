"""Several asks in one breath: the one that continues an existing chat goes there, the others become
their own tasks. Before, a matching chat swallowed the whole request ("where'd we leave off with my PRs,
how's my fantasy team, anything left hanging" all ran inside the PR thread as one task)."""
from __future__ import annotations

import json

from fakes import SDP, FakeLiveWorker, FakeTransport, wait_for
from speakeasy import continuity, router

REQUEST = ("Where'd we leave off with my PRs, did any get upstream? And then how's my fantasy team looking "
           "this weekend? And then anything else we left hanging for the next couple days")
PARTS = ["Where did we leave off with my PRs?", "How is my fantasy team looking this weekend?",
         "What else did we leave hanging for the next couple days?"]
CHATS = [router.Chat("c1", "Discord #dev / PR work", ("open upstream PRs and cherry-picks",), "PR status")]


def model_says(conversation, parts, part=None):
    return json.dumps({"status_task_id": None, "follow_up_task_id": None, "conversation": conversation,
                       "conversation_part": part, "parts": parts, "channel": None, "show": False})


def test_the_matching_ask_continues_its_chat_and_the_others_split_off():
    decision = router.parse_decision(model_says("c1", PARTS, 0), REQUEST, [], [], CHATS)
    assert decision is not None
    assert [p.request for p in decision.parts] == PARTS
    assert [p.conversation for p in decision.parts] == ["c1", None, None]
    assert decision.conversation is None  # never the whole request into the chat


def test_without_an_index_the_part_that_shares_the_chats_words_continues_it():
    decision = router.parse_decision(model_says("c1", PARTS), REQUEST, [], [], CHATS)
    assert decision is not None
    assert [p.conversation for p in decision.parts] == ["c1", None, None]


def test_a_single_ask_still_continues_its_conversation():
    decision = router.parse_decision(model_says("c1", [PARTS[0]]), "where'd we leave off with my PRs", [], [], CHATS)
    assert decision is not None
    assert decision.conversation == "c1" and len(decision.parts) == 1


def test_the_call_runs_one_part_in_the_chat_and_starts_the_rest_as_tasks(home, hermes, monkeypatch):
    from speakeasy import calls
    from speakeasy.service import VoiceService
    conv = continuity.Conversation("pr_session", "discord", "555", "thread", "", "u1", "", "Dev / PR work",
                                   "PR work", 0.0)
    continued: list[tuple[str, str, str, str | None]] = []

    async def candidates(self, request):
        return [continuity.Candidate(conv, ("open upstream PRs and cherry-picks",))]

    async def continue_in(self, task_id, revision, context, request, c, joins=None, voice_id=None):
        continued.append((task_id, request, c.session_id, voice_id))

    monkeypatch.setattr(calls.SidebandWorker, "conversation_candidates", candidates)
    monkeypatch.setattr(calls.SidebandWorker, "start_continuity_task", continue_in)
    workers: list[FakeLiveWorker] = []
    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                       route_call=lambda m: model_says("c1", PARTS, 0),
                       openai_negotiate=lambda key, payload: {"session": {"id": "s"}, "transport": {"sdp": "v=0\r\n"}},
                       openai_worker=lambda rt, i: workers.append(FakeLiveWorker(rt, i)) or workers[-1])
    svc.settings.patch({"voice": {"provider": "openai"}})
    try:
        svc.create_session({"sdp": SDP}, "req_split_conv")
        workers[-1].delegate("call_split_conv", REQUEST)
        wait_for(lambda: len(hermes.calls) == 2 and continued, timeout=10)
        assert continued == [("call_split_conv-p0", PARTS[0], "pr_session", "call_split_conv")]
        prompts = " ".join(str(c["input"]) for c in hermes.calls)
        assert PARTS[1] in prompts and PARTS[2] in prompts and PARTS[0] not in prompts
    finally:
        svc.close()
