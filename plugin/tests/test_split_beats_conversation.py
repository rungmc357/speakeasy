"""Several asks in one breath become separate tasks, even when one of them matches an existing chat."""

import json

from speakeasy import router

REQUEST = ("Where'd we leave off with my PRs, did any get upstream? And then how's my fantasy team looking "
           "this weekend? And then anything else we left hanging for the next couple days")
CHATS = [router.Chat("c1", "Discord #dev / PR work", ("open upstream PRs and cherry-picks",), "PR status")]


def model_says(conversation, parts):
    return json.dumps({"status_task_id": None, "follow_up_task_id": None, "conversation": conversation,
                       "parts": parts, "channel": None, "show": False})


def test_split_asks_are_not_swallowed_by_a_matching_conversation():
    parts = ["Where did we leave off with my PRs?", "How is my fantasy team looking this weekend?",
             "What else did we leave hanging for the next couple days?"]
    decision = router.parse_decision(model_says("c1", parts), REQUEST, [], [], CHATS)
    assert decision is not None
    assert [p.request for p in decision.parts] == parts
    assert all(p.kind == router.NEW for p in decision.parts)
    assert decision.conversation is None


def test_a_single_ask_still_continues_its_conversation():
    decision = router.parse_decision(model_says("c1", ["Where did we leave off with my PRs?"]),
                                     "where'd we leave off with my PRs", [], [], CHATS)
    assert decision is not None
    assert decision.conversation == "c1"
    assert len(decision.parts) == 1
