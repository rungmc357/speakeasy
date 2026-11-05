"""Email approval cards: parse, hash binding (409), idempotent approve, revise, deny, voice rules."""
from __future__ import annotations

import json

import pytest

from fakes import SDP, http, wait_for
from speakeasy.emails import (DraftInvalid, canonical_json, draft_sha256, extract_email_drafts, normalize_draft)
from speakeasy.prompt import builder as P
from speakeasy.text import split_result

DRAFT = {"from": "me@example.com", "to": ["Pat Doe <pat@example.org>"], "cc": [], "bcc": [],
         "subject": "Dinner on Friday", "body": "Hi Pat,\n\nAre we still on for Friday at 7?\n\nThanks"}


def answer_with(draft: dict, spoken: str = "I drafted the email to Pat.") -> str:
    return (f"Here is the draft.\n```email-draft\n{json.dumps(draft)}\n```\n"
            f"DONE: Email drafted\nSPOKEN: {spoken}")


# -- parsing ------------------------------------------------------------------------------------

def test_parse_strips_block_and_validates():
    text, drafts = extract_email_drafts(answer_with(DRAFT))
    assert "email-draft" not in text and "Are we still on" not in text
    assert len(drafts) == 1
    d = drafts[0]
    assert d["to"] == ["Pat Doe <pat@example.org>"] and d["subject"] == "Dinner on Friday"
    assert set(d) == {"from", "to", "cc", "bcc", "subject", "body"}


def test_split_result_keeps_draft_out_of_spoken_and_full():
    result = split_result(answer_with(DRAFT), ())
    assert result["spoken"] == "I drafted the email to Pat."
    assert "email-draft" not in result["full"] and "Are we still on" not in result["full"]
    assert result["_email_drafts"][0]["subject"] == "Dinner on Friday"


@pytest.mark.parametrize("bad", [
    {**DRAFT, "to": []},
    {**DRAFT, "to": ["not-an-address"]},
    {**DRAFT, "subject": "two\nlines"},
    {**DRAFT, "body": "<html><body>hi</body></html>"},
    {**DRAFT, "body": "x" * 20_001},
    {**DRAFT, "cc": ["a@example.com"] * 51},
    {**DRAFT, "account": "a\nb"},
])
def test_invalid_drafts_rejected(bad):
    with pytest.raises(DraftInvalid):
        normalize_draft(bad)


def test_invalid_block_is_stripped_not_shown():
    text, drafts = extract_email_drafts("Hi\n```email-draft\n{not json}\n```\nSPOKEN: ok")
    assert drafts == [] and "email-draft" not in text


def test_hash_is_of_canonical_json():
    d = normalize_draft(DRAFT)
    shuffled = normalize_draft(dict(reversed(list(DRAFT.items()))))
    assert canonical_json(d) == canonical_json(shuffled)
    assert draft_sha256(d) == draft_sha256(shuffled)


# -- prompt contract ------------------------------------------------------------------------------

def test_task_prompt_requires_draft_block_and_forbids_sending():
    prompt = P.build_task_prompt(P.Names("Hermes", "Sam"), 1, "User: email Pat about dinner")
    assert "email-draft" in prompt and "do NOT send" in prompt
    assert '"to": [' in prompt and "{" in prompt  # literal JSON braces survive templating


def test_voice_rules_say_spoken_approve_does_not_send():
    text = P.build_live_instructions(P.Names("Hermes", "Sam"))
    assert "does NOT send an email" in text and "press Send on the card" in text
    note = P.draft_waiting_note(P.Names("Hermes", "Sam"), "Dinner", ["pat@example.org"])
    assert "Send is theirs to press" in note and "spoken approval does not send it" in note


# -- end to end over HTTP with a fake Hermes ------------------------------------------------------------

def _call_with_draft(server, service, hermes):
    hermes.responder = lambda prompt, sid: answer_with(DRAFT)
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": "req_email_1"})
    assert status == 201, session
    worker = service.workers[-1]
    worker.delegate("call_email_1", "Email Pat asking if dinner Friday is still on")
    tasks = wait_for(lambda: (lambda t: t if t and t[0].get("email_drafts") else None)(
        http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]))
    return session, worker, tasks[0]["email_drafts"][0]


def test_draft_card_flow_approve_sends_once(server, service, hermes):
    session, worker, draft = _call_with_draft(server, service, hermes)
    assert draft["status"] == "pending" and draft["subject"] == "Dinner on Friday"
    assert set(draft) >= {"draft_id", "from", "to", "cc", "bcc", "subject", "body", "status", "sha256"}
    assert not any(k.startswith("_") for k in draft)
    # the live call was told a draft is waiting, and that spoken approval does not count
    wait_for(lambda: any("Send is theirs to press" in c for _, _, c in worker.sent))
    # SSE-visible state carries the draft
    interaction = service.interaction(session["interaction_id"])
    assert interaction.feed.last["email_drafts"][0]["draft_id"] == draft["draft_id"]

    runs_before = len(hermes.calls)
    hermes.responder = lambda prompt, sid: "Sent.\nDONE: Email sent\nSPOKEN: I sent the email to Pat."
    url = f"/voice/drafts/{draft['draft_id']}"
    status, first = http(server.base_url, "POST", url, {"action": "approve", "sha256": draft["sha256"]}, server.token)
    assert status == 200 and first["draft"]["status"] in {"approved", "sent"}
    status, again = http(server.base_url, "POST", url, {"action": "approve", "sha256": draft["sha256"]}, server.token)
    assert status == 200 and again["draft"]["draft_id"] == draft["draft_id"]
    wait_for(lambda: service.store.draft(draft["draft_id"])["status"] == "sent")
    status, third = http(server.base_url, "POST", url, {"action": "approve", "sha256": draft["sha256"]}, server.token)
    assert status == 200 and third["draft"]["status"] == "sent"

    sends = hermes.calls[runs_before:]
    assert len(sends) == 1, "approve must reach Hermes exactly once"
    send = sends[0]
    task_session = service.store.draft(draft["draft_id"])["_session_id"]
    assert send["session_id"] == task_session  # the SAME Hermes session as the task
    assert "approved exactly this email draft" in send["input"]
    sent_json = send["input"][send["input"].index("{"):send["input"].rindex("}") + 1]
    assert draft_sha256(json.loads(sent_json)) == draft["sha256"]


def test_hash_mismatch_is_409_and_nothing_sent(server, service, hermes):
    _, _, draft = _call_with_draft(server, service, hermes)
    before = len(hermes.calls)
    status, body = http(server.base_url, "POST", f"/voice/drafts/{draft['draft_id']}",
                        {"action": "approve", "sha256": "0" * 64}, server.token)
    assert status == 409 and "changed" in body["error"]
    assert len(hermes.calls) == before
    assert service.store.draft(draft["draft_id"])["status"] == "pending"


def test_deny_tells_session_not_to_send(server, service, hermes):
    _, _, draft = _call_with_draft(server, service, hermes)
    hermes.responder = lambda prompt, sid: "Discarded.\nSPOKEN: I discarded the draft."
    status, body = http(server.base_url, "POST", f"/voice/drafts/{draft['draft_id']}",
                        {"action": "deny", "sha256": draft["sha256"]}, server.token)
    assert status == 200 and body["draft"]["status"] == "denied"
    assert "Do not send it" in hermes.calls[-1]["input"]
    # approving a denied draft is refused
    status, _ = http(server.base_url, "POST", f"/voice/drafts/{draft['draft_id']}",
                     {"action": "approve", "sha256": draft["sha256"]}, server.token)
    assert status == 409


def test_revise_replaces_draft_and_supersedes_old(server, service, hermes):
    _, _, draft = _call_with_draft(server, service, hermes)
    revised = {**DRAFT, "subject": "Dinner on Saturday", "body": "Hi Pat,\n\nCan we move dinner to Saturday?"}
    hermes.responder = lambda prompt, sid: answer_with(revised, "I revised the draft.")
    status, body = http(server.base_url, "POST", f"/voice/drafts/{draft['draft_id']}",
                        {"action": "revise", "sha256": draft["sha256"], "instructions": "Make it Saturday"},
                        server.token)
    assert status == 200 and body["draft"]["status"] == "revising"
    assert "Make it Saturday" in hermes.calls[-1]["input"] and "email-draft" in hermes.calls[-1]["input"]
    wait_for(lambda: service.store.draft(draft["draft_id"])["status"] == "superseded")
    tasks = http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]
    current = tasks[0]["email_drafts"]
    assert len(current) == 1 and current[0]["subject"] == "Dinner on Saturday"
    assert current[0]["draft_id"] != draft["draft_id"] and current[0]["status"] == "pending"
    # the old hash no longer approves anything
    status, _ = http(server.base_url, "POST", f"/voice/drafts/{draft['draft_id']}",
                     {"action": "approve", "sha256": draft["sha256"]}, server.token)
    assert status == 409


def test_draft_route_validation(server, service, hermes):
    _, _, draft = _call_with_draft(server, service, hermes)
    url = f"/voice/drafts/{draft['draft_id']}"
    assert http(server.base_url, "POST", url, {"action": "send", "sha256": draft["sha256"]}, server.token)[0] == 400
    assert http(server.base_url, "POST", url, {"action": "revise", "sha256": draft["sha256"]}, server.token)[0] == 400
    assert http(server.base_url, "POST", url, {"action": "approve", "sha256": draft["sha256"]})[0] == 401
    assert http(server.base_url, "POST", "/voice/drafts/ed_" + "0" * 24,
                {"action": "approve", "sha256": draft["sha256"]}, server.token)[0] == 404


def test_a_waiting_draft_survives_ending_the_call(service):
    store = service.store
    store.reserve_run("se_old", "vi_old", "item_old", 1)
    store.update_run("se_old", "run_old", "completed")
    store.add_draft("se_old", "sess_old", {"from": "me@example.com", "to": ["me@example.com"], "cc": [], "bcc": [],
                                          "subject": "Portugal trip details", "body": "Overview"})
    store.reserve_run("se_new", "vi_new", "item_new", 1)
    store.update_run("se_new", "run_new", "running")
    listed = store.latest_tasks()
    carried = [t for t in listed if t["task_id"] == "item_old"]
    assert carried and carried[0]["email_drafts"][0]["subject"] == "Portugal trip details"
    draft_id = carried[0]["email_drafts"][0]["draft_id"]
    store.transition_draft(draft_id, {"pending"}, "denied")
    # the finished task stays on the board (the workspace outlives a call) until it's cleared
    left = [t for t in store.latest_tasks() if t["task_id"] == "item_old"]
    assert left and not [d for d in left[0].get("email_drafts") or [] if d.get("state") == "pending"]
    store.dismiss(["run_old"])
    assert not [t for t in store.latest_tasks() if t["task_id"] == "item_old"]


def test_a_cleared_task_with_a_waiting_draft_stays_cleared(service):
    """Clear is authoritative: a task the user cleared is never carried back by its waiting draft."""
    store = service.store
    store.reserve_run("se_old", "vi_old", "item_old", 1)
    store.update_run("se_old", "run_old", "completed")
    store.add_draft("se_old", "sess_old", {"from": "me@example.com", "to": ["me@example.com"], "cc": [], "bcc": [],
                                          "subject": "Portugal trip details", "body": "Overview"})
    store.reserve_run("se_new", "vi_new", "item_new", 1)
    store.update_run("se_new", "run_new", "completed")
    assert [t for t in store.latest_tasks() if t["task_id"] == "item_old"]
    assert service.dismiss_tasks({"run_ids": ["run_old", "run_new"]})["dismissed"] == ["run_old", "run_new"]
    assert store.latest_tasks() == []
