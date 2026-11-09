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


# -- build 73: the email card (recipients, thread, unresolved names, edits, its own buttons) -----------

RICH = {**DRAFT, "to": ["Pat Doe <pat@example.org>"],
        "cc": ["Robin Vale <robin.vale@example.net>", "kit@example.net"],
        "subject": "RE: Spring schedule", "reply_to_message_id": "m-123",
        "thread_subject": "Spring schedule", "thread_count": 4,
        "thread_last": {"from": "Pat Doe <pat@example.org>", "when": "Mon 9:12", "text": "Does Friday work?"},
        "unresolved": ["Lark & Finch"], "notes": ["Left out 'next week': the calendar shows this week"]}


def test_card_context_parsed_but_never_hashed_or_sent():
    _, drafts = extract_email_drafts(answer_with(RICH))
    d = drafts[0]
    assert d["context"]["thread_count"] == 4 and d["context"]["unresolved"] == ["Lark & Finch"]
    assert draft_sha256(d) == draft_sha256({k: v for k, v in d.items() if k != "context"})
    assert "context" not in canonical_json(d) and "Lark" not in canonical_json(d)


def test_malformed_context_is_dropped_not_fatal():
    _, drafts = extract_email_drafts(answer_with({**DRAFT, "thread_count": "four", "unresolved": [3, ""],
                                                  "thread_last": "nope", "notes": "One note"}))
    assert drafts and drafts[0]["context"] == {"notes": ["One note"]}


def test_email_view_shows_every_recipient_with_name_and_address():
    from speakeasy.emails import email_view, with_context
    d = {**with_context(normalize_draft(RICH), RICH), "draft_id": "ed_" + "a" * 24, "sha256": "f" * 64,
         "status": "pending"}
    v = email_view(d)
    assert v["kind"] == "draft" and v["channel"] == "email" and v["to"] == "Pat Doe"
    assert v["recipients"]["to"] == [{"name": "Pat Doe", "address": "pat@example.org"}]
    assert v["recipients"]["cc"] == [{"name": "Robin Vale", "address": "robin.vale@example.net"},
                                     {"name": "", "address": "kit@example.net"}]
    assert "bcc" not in v["recipients"]
    assert v["thread"] == "Spring schedule" and v["thread_count"] == 4 and v["thread_last"]["text"]
    assert v["unresolved"] == ["Lark & Finch"] and v["notes"] and v["draft_id"] and v["sha256"]


def test_edit_validates_and_clears_a_resolved_name():
    from speakeasy.emails import apply_edit, with_context
    d = with_context(normalize_draft(RICH), RICH)
    edited = apply_edit(d, {"cc": ["Robin Vale <robin.vale@example.net>", "Lark & Finch <desk@larkfinch.example>"],
                            "body": "Hi Pat,\n\nFriday works."})
    assert edited["context"]["edited"] is True and "unresolved" not in edited["context"]
    assert edited["body"] == "Hi Pat,\n\nFriday works." and edited["subject"] == RICH["subject"]
    with pytest.raises(DraftInvalid):
        apply_edit(d, {"to": []})
    with pytest.raises(DraftInvalid):
        apply_edit(d, {"from": "else@example.com"})


def test_prompts_carry_every_named_recipient_and_detail():
    prompt = P.build_task_prompt(P.Names("Hermes", "Sam"), 1, "User: email Pat and Lark & Finch")
    assert "unresolved" in prompt and "never drop one silently" in prompt and "thread_count" in prompt
    assert "never describe the draft instead of including it" in prompt
    rules = P.build_live_instructions(P.Names("Hermes", "Sam"))
    assert "every person, company and detail" in rules and "shows as a card by itself" in rules


def _call_with_rich_draft(server, service, hermes):
    hermes.responder = lambda prompt, sid: answer_with(RICH)
    status, _ = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                     {"Idempotency-Key": "req_email_rich"})
    assert status == 201
    service.workers[-1].delegate("call_email_rich", "Email Pat and Lark & Finch about Friday")
    tasks = wait_for(lambda: (lambda t: t if t and t[0].get("email_drafts") else None)(
        http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]))
    return tasks[0]


def test_finished_draft_job_answers_with_the_email_card(server, service, hermes):
    task = _call_with_rich_draft(server, service, hermes)
    status, body = http(server.base_url, "GET", f"/voice/work/{task['run_id']}", token=server.token)
    views = body["work"]["result"]["views"]
    assert views[-1]["kind"] == "draft" and views[-1]["draft_id"] == task["email_drafts"][0]["draft_id"]
    assert views[-1]["recipients"]["cc"][0]["address"] == "robin.vale@example.net"
    assert sum(1 for v in views if v.get("kind") == "draft") == 1


def test_email_card_never_moves_another_cards_index():
    from speakeasy.emails import with_email_views
    drafts = [{**normalize_draft(DRAFT), "draft_id": "ed_" + "b" * 24, "sha256": "e" * 64, "status": "pending"}]
    confirm, hand = {"kind": "confirm", "title": "x"}, {"kind": "draft", "channel": "email", "to": "Pat"}
    assert [v["kind"] for v in with_email_views([confirm], drafts)] == ["confirm", "draft"]
    out = with_email_views([confirm, hand, {"kind": "fact"}], drafts)
    assert [v["kind"] for v in out] == ["confirm", "draft", "fact"] and out[1]["draft_id"]
    assert with_email_views([confirm], []) == [confirm]


def test_email_card_buttons_edit_then_send_bound_to_the_hash(server, service, hermes):
    task = _call_with_rich_draft(server, service, hermes)
    run, draft = task["run_id"], task["email_drafts"][0]
    url = f"/voice/work/{run}/card-action"
    # a stale hash is refused and nothing reaches Hermes
    before = len(hermes.calls)
    status, _ = http(server.base_url, "POST", url, {"view": 0, "action": "send", "value": "0" * 64}, server.token)
    assert status == 409 and len(hermes.calls) == before
    # edit: a new pending draft with a new hash, nothing sent
    status, out = http(server.base_url, "POST", url, {"view": 0, "action": "edit", "value": {
        "sha256": draft["sha256"], "to": ["Pat Doe <pat@example.org>", "Lee Moss <lee@example.org>"]}}, server.token)
    assert status == 200 and out["draft"]["status"] == "pending" and out["draft"]["sha256"] != draft["sha256"]
    assert len(hermes.calls) == before
    assert service.store.draft(draft["draft_id"])["status"] == "superseded"
    view = http(server.base_url, "GET", f"/voice/work/{run}", token=server.token)[1]["work"]["result"]["views"][0]
    assert [r["name"] for r in view["recipients"]["to"]] == ["Pat Doe", "Lee Moss"] and view["edited"] is True
    # send the edited one: the task is told it was edited, with the new recipients
    hermes.responder = lambda prompt, sid: "Sent.\nDONE: Email sent\nSPOKEN: Sent."
    status, out = http(server.base_url, "POST", url, {"view": 0, "action": "send", "value": view["sha256"]},
                       server.token)
    assert status == 200 and out["draft"]["status"] in {"approved", "sent"}
    sent = hermes.calls[-1]["input"]
    assert "lee@example.org" in sent and "edited this email on the card" in sent and "Lark" not in sent


def test_email_card_save_and_discard(server, service, hermes):
    task = _call_with_rich_draft(server, service, hermes)
    run, draft = task["run_id"], task["email_drafts"][0]
    url = f"/voice/work/{run}/card-action"
    hermes.responder = lambda prompt, sid: "Saved.\nDONE: Saved\nSPOKEN: It's in your Drafts."
    status, out = http(server.base_url, "POST", url, {"view": 0, "action": "save", "value": draft["sha256"]},
                       server.token)
    assert status == 200 and out["draft"]["status"] == "pending"
    assert "Do NOT send it" in hermes.calls[-1]["input"] and "Drafts" in hermes.calls[-1]["input"]
    hermes.responder = lambda prompt, sid: "Discarded.\nSPOKEN: Discarded."
    status, out = http(server.base_url, "POST", url, {"view": 0, "action": "discard", "value": draft["sha256"]},
                       server.token)
    assert status == 200 and out["draft"]["status"] == "denied"
    status, _ = http(server.base_url, "POST", url, {"view": 0, "action": "frobnicate"}, server.token)
    assert status == 400


def test_card_edit_based_on_an_earlier_hash_after_the_users_own_edit(server, service, hermes):
    task = _call_with_rich_draft(server, service, hermes)
    run, draft = task["run_id"], task["email_drafts"][0]
    url = f"/voice/work/{run}/card-action"
    fields = {"to": ["Pat Doe <pat@example.org>"], "cc": [], "bcc": [], "subject": "RE: Spring schedule",
              "body": "Hi Pat,\n\nFriday works."}
    status, _ = http(server.base_url, "POST", url, {"view": 0, "action": "edit",
                                                     "value": {"sha256": draft["sha256"], **fields}}, server.token)
    assert status == 200
    # the card still holds the first hash: Send with its full fields goes through (only the user edited)
    hermes.responder = lambda prompt, sid: "Sent.\nDONE: Email sent\nSPOKEN: Sent."
    status, out = http(server.base_url, "POST", url, {"view": 0, "action": "edit", "value": {
        "sha256": draft["sha256"], "after": "send", **fields}}, server.token)
    assert status == 200 and out["draft"]["status"] in {"approved", "sent"}
    assert "Friday works." in hermes.calls[-1]["input"] and "robin" not in hermes.calls[-1]["input"]


def test_card_edit_is_refused_after_the_task_revised_the_draft(server, service, hermes):
    task = _call_with_rich_draft(server, service, hermes)
    run, draft = task["run_id"], task["email_drafts"][0]
    key = service.store.draft(draft["draft_id"])["_key"]
    service.store.add_draft(key, None, normalize_draft({**DRAFT, "body": "A revision by the task"}))
    before = len(hermes.calls)
    status, _ = http(server.base_url, "POST", f"/voice/work/{run}/card-action", {"view": 0, "action": "edit", "value": {
        "sha256": draft["sha256"], "after": "send", "body": "Mine"}}, server.token)
    assert status == 409 and len(hermes.calls) == before
