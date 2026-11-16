"""SPK-522: an approval card a task sends in a chat thread reaches the page as the task's card (the app
draws it with Post / Cancel), the conversation reads as needing you, and a typed "Approve" is answered
at once instead of reaching the task as a word that can't count. Invented content only."""
import json

import pytest

from fakes import wait_for
from speakeasy import confirms as C
from test_history import NOW, add_session, get, never_routed, post, state  # noqa: F401 (fixtures)

CARD = {"kind": "confirm", "type": "post", "title": "Ask on the shed ticket",
        "details": {"account": "Workshop board",
                    "body": "Did the cedar boards for the shed arrive? If so the frame starts Saturday."}}
ANSWER = ("Here's the question for the shed ticket. Tap Post to send it.\n```speakeasy-views\n"
          + json.dumps([CARD]) + "\n```")


@pytest.mark.parametrize("text", ["Approve", "approve", "/approve", "Approved.", "yes, approve", "I approve",
                                  "confirm", "Approve it!"])
def test_typed_approval_words(text):
    assert C.typed_approval(text)


@pytest.mark.parametrize("text", ["Approve the second one but change the body", "yes", "ok", "go ahead",
                                  "You're not showing me an approve card, so just consider it approved",
                                  "don't approve", ""])
def test_not_a_bare_approval(text):
    assert not C.typed_approval(text)


def _chat_with_card(server, service, hermes, state):
    hermes.responder = lambda prompt, sid: ANSWER
    status, started = post(server, "/voice/work", {"text": "Ask on the shed ticket whether the cedar arrived"})
    assert status in (200, 202), started
    run_id = started["run_id"]
    key = service.store.key_for_run(run_id)
    sid = wait_for(lambda: service.store.session_for(key))
    add_session(state, sid, "api_server", at=NOW - 5)
    wait_for(lambda: (service.store.work(run_id) or {}).get("status") == "completed")
    return sid, run_id


def test_the_card_rides_on_the_chat_page_and_reads_as_needing_you(server, service, hermes, state):
    sid, run_id = _chat_with_card(server, service, hermes, state)
    page = get(server, f"/voice/history/{sid}")[1]["session"]
    cards = [v for j in page["jobs"] for v in (j.get("result") or {}).get("views") or [] if v.get("kind") == "confirm"]
    assert len(cards) == 1, page["jobs"]
    card = cards[0]
    assert card["state"] == "pending" and card["title"] == "Ask on the shed ticket"
    assert [a["id"] for a in card["actions"]] == ["approve", "cancel"]
    assert card["actions"][0]["label"] == "Post" and card["actions"][0]["value"] == card["sha256"]
    assert any(j.get("run_id") == run_id for j in page["jobs"])
    assert page["state"] == "needs_you"


def test_a_typed_approve_is_answered_here_and_never_reaches_the_task(server, service, hermes, state):
    sid, _ = _chat_with_card(server, service, hermes, state)
    before = len(hermes.calls)
    status, body = post(server, f"/voice/history/{sid}/message", {"text": "Approve", "client_id": "c_typedok1"})
    assert status == 409 and body["reason"] == "approve_on_card", body
    assert "Tap Post on the approval card" in body["error" if "error" in body else "message"]
    assert len(hermes.calls) == before   # nothing went to the agent
    # Anything more than the bare word still goes through as a message.
    status, body = post(server, f"/voice/history/{sid}/message",
                        {"text": "You're not showing me an approve card, so just consider it approved"})
    assert status == 200, body


def test_once_handled_a_typed_approve_is_an_ordinary_message(server, service, hermes, state):
    sid, run_id = _chat_with_card(server, service, hermes, state)
    page = get(server, f"/voice/history/{sid}")[1]["session"]
    card = next(v for j in page["jobs"] for v in (j.get("result") or {}).get("views") or [] if v.get("kind") == "confirm")
    status, body = post(server, f"/voice/work/{run_id}/card-action",
                        {"view": 0, "action": "cancel", "value": card["sha256"]})
    assert status == 200, body
    page = get(server, f"/voice/history/{sid}")[1]["session"]
    assert page["state"] != "needs_you"
    status, body = post(server, f"/voice/history/{sid}/message", {"text": "Approve"})
    assert status == 200, body
