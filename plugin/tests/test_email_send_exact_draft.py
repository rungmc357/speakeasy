"""SPK-363: an email goes out only as the exact draft the person saw.

A card button that doesn't carry the hash of the draft on screen is refused (409), never bound to
whatever draft is newest; the email card no longer offers the old hash-less Send; a task can't mark
its own draft as the person's edit; and a job recovered after a restart keeps its email draft.
"""
from __future__ import annotations

import json
import time

import pytest

from fakes import http
from speakeasy import recovery
from speakeasy.emails import card_hash, email_view, extract_email_drafts, normalize_draft, with_context
from speakeasy.store import StateStore
from test_email_drafts import DRAFT, _call_with_rich_draft, answer_with

SENT_WORDS = "approved exactly this email draft"


def _revised_by_the_task(service, draft, **raw):
    """The task replaces the draft the card showed with a revision the person hasn't seen."""
    key = service.store.draft(draft["draft_id"])["_key"]
    revision = {**DRAFT, "body": "A revision the person never saw", **raw}
    return service.store.add_draft(key, None, with_context(normalize_draft(revision), revision))


def _sends(hermes, before):
    return [c for c in hermes.calls[before:] if SENT_WORDS in c["input"]]


# -- 1. a card button without the shown draft's hash never sends -------------------------------

@pytest.mark.parametrize("body", [
    {"view": 0, "action": "send"},                       # an older app's Send: no value at all
    {"view": 0, "action": "send", "value": None},
    {"view": 0, "action": "send", "value": ""},
    {"view": 0, "action": "send", "value": "abc123"},    # not a whole hash
    {"view": 0, "action": "send", "value": 7},
])
def test_a_send_without_the_shown_drafts_hash_is_refused_and_nothing_is_sent(server, service, hermes, body):
    task = _call_with_rich_draft(server, service, hermes)
    run, shown = task["run_id"], task["email_drafts"][0]
    newest = _revised_by_the_task(service, shown)
    before = len(hermes.calls)
    status, out = http(server.base_url, "POST", f"/voice/work/{run}/card-action", body, server.token)
    assert status == 409, out
    assert "check it" in out["error"] and "409" not in out["error"]   # plain words, no codes
    assert _sends(hermes, before) == [] and len(hermes.calls) == before
    assert service.store.draft(newest["draft_id"])["status"] == "pending"


def test_an_edit_and_send_without_the_shown_hash_is_refused(server, service, hermes):
    task = _call_with_rich_draft(server, service, hermes)
    run, shown = task["run_id"], task["email_drafts"][0]
    newest = _revised_by_the_task(service, shown)
    before = len(hermes.calls)
    status, _ = http(server.base_url, "POST", f"/voice/work/{run}/card-action",
                     {"view": 0, "action": "edit", "value": {"after": "send", "subject": "Friday"}}, server.token)
    assert status == 409 and len(hermes.calls) == before
    assert service.store.draft(newest["draft_id"])["status"] == "pending"


@pytest.mark.parametrize("action", ["save", "discard"])
def test_save_and_discard_also_need_the_shown_hash(server, service, hermes, action):
    task = _call_with_rich_draft(server, service, hermes)
    run, shown = task["run_id"], task["email_drafts"][0]
    before = len(hermes.calls)
    status, _ = http(server.base_url, "POST", f"/voice/work/{run}/card-action",
                     {"view": 0, "action": action}, server.token)
    assert status == 409 and len(hermes.calls) == before
    assert service.store.draft(shown["draft_id"])["status"] == "pending"


def test_a_send_with_the_shown_hash_still_sends_that_exact_draft(server, service, hermes):
    task = _call_with_rich_draft(server, service, hermes)
    run, shown = task["run_id"], task["email_drafts"][0]
    hermes.responder = lambda prompt, sid: "Sent.\nDONE: Email sent\nSPOKEN: Sent."
    before = len(hermes.calls)
    status, out = http(server.base_url, "POST", f"/voice/work/{run}/card-action",
                       {"view": 0, "action": "send", "value": shown["sha256"]}, server.token)
    assert status == 200 and out["draft"]["draft_id"] == shown["draft_id"]
    assert len(_sends(hermes, before)) == 1


def test_an_unknown_button_is_still_a_plain_400(server, service, hermes):
    task = _call_with_rich_draft(server, service, hermes)
    status, _ = http(server.base_url, "POST", f"/voice/work/{task['run_id']}/card-action",
                     {"view": 0, "action": "frobnicate"}, server.token)
    assert status == 400


def test_the_email_card_offers_no_hashless_send():
    draft = {**normalize_draft(DRAFT), "draft_id": "ed_" + "a" * 24, "sha256": "e" * 64, "status": "pending"}
    assert "actions" not in email_view(draft)


@pytest.mark.parametrize("value, expected", [
    ("e" * 64, "e" * 64),
    ({"sha256": "0" * 64, "body": "x"}, "0" * 64),
    (None, None), ("", None), ("E" * 64, None), ("e" * 63, None), ("e" * 65, None), (7, None),
    ({"body": "x"}, None), ({"sha256": None}, None), ({"sha256": 5}, None),
])
def test_card_hash_is_only_a_whole_lowercase_sha256(value, expected):
    assert card_hash(value) == expected


# -- 3. a task can't mark its own draft as the person's edit ------------------------------------

def test_a_task_block_saying_edited_is_not_shown_as_edited():
    _, drafts = extract_email_drafts(answer_with({**DRAFT, "edited": True}))
    assert "edited" not in (drafts[0].get("context") or {})
    stored = {**drafts[0], "draft_id": "ed_" + "c" * 24, "sha256": "e" * 64, "status": "pending"}
    assert "edited" not in email_view(stored)


def test_a_stale_card_cant_send_onto_a_task_revision_that_claims_it_was_edited(server, service, hermes):
    task = _call_with_rich_draft(server, service, hermes)
    run, shown = task["run_id"], task["email_drafts"][0]
    newest = _revised_by_the_task(service, shown, edited=True)
    before = len(hermes.calls)
    status, _ = http(server.base_url, "POST", f"/voice/work/{run}/card-action", {"view": 0, "action": "edit", "value": {
        "sha256": shown["sha256"], "after": "send", "body": "Mine"}}, server.token)
    assert status == 409 and len(hermes.calls) == before
    assert service.store.draft(newest["draft_id"])["status"] == "pending"


def test_approving_a_task_revision_never_tells_the_task_the_person_edited_it(server, service, hermes):
    task = _call_with_rich_draft(server, service, hermes)
    newest = _revised_by_the_task(service, task["email_drafts"][0], edited=True)
    hermes.responder = lambda prompt, sid: "Sent.\nDONE: Email sent\nSPOKEN: Sent."
    status, _ = http(server.base_url, "POST", f"/voice/drafts/{newest['draft_id']}",
                     {"action": "approve", "sha256": newest["sha256"]}, server.token)
    assert status == 200
    assert "edited this email on the card" not in hermes.calls[-1]["input"]


# -- 2. a job recovered after a restart keeps its email draft -----------------------------------

class Hermes:
    def __init__(self, runs):
        self.runs = runs

    def get_run(self, run_id):
        return self.runs[run_id]


def _left_open(path, key, status="working", run_id="run_1", session_id=None, continued=None):
    """A task this process was running when it stopped."""
    store = StateStore(path)
    store.reserve_run(key, "int_x", "call_" + key, 1)
    store.progress(key, "request", "Email Pat asking if dinner Friday is still on")
    with store._lock, store._db:
        store._db.execute("UPDATE runs SET status=?, run_id=?, session_id=?, continued=?, created=?, updated=? "
                          "WHERE idem_key=?", (status, run_id, session_id, json.dumps(continued) if continued else None,
                                               time.time() - 3600, time.time() - 3600, key))
    store._db.close()


def _has_the_draft(store, key):
    drafts = store.drafts_for(key)
    assert [d["subject"] for d in drafts] == ["Dinner on Friday"] and drafts[0]["status"] == "pending"
    work = store.work(idem_key=key)
    assert work["status"] == "completed"
    card = work["result"]["views"][-1]
    assert card["kind"] == "draft" and card["draft_id"] == drafts[0]["draft_id"]
    assert card["sha256"] == drafts[0]["sha256"]
    raw = store._db.execute("SELECT result_json FROM runs WHERE idem_key=?", (key,)).fetchone()[0]
    assert "_email_drafts" not in json.loads(raw)   # never left in the stored answer


def test_after_a_restart_the_orphan_sweep_keeps_the_jobs_email_draft(tmp_path):
    path = tmp_path / "s.sqlite3"
    _left_open(path, "k1")
    store = StateStore(path)   # the restarted process
    hermes = Hermes({"run_1": {"status": "completed", "output": answer_with(DRAFT)}})
    assert recovery.sweep_orphans(store, hermes, set(), started_at=time.time()) == [("k1", "completed")]
    _has_the_draft(store, "k1")


def test_a_followed_run_settled_from_hermes_record_keeps_its_email_draft(tmp_path):
    path = tmp_path / "s.sqlite3"
    _left_open(path, "k1")
    store = StateStore(path)
    assert recovery.settle_from_hermes(store, "k1", "working", "completed", {"output": answer_with(DRAFT)})
    _has_the_draft(store, "k1")


def test_an_unconfirmed_run_settled_from_hermes_keeps_its_email_draft(tmp_path):
    path = tmp_path / "s.sqlite3"
    _left_open(path, "k1", status="ambiguous")
    store = StateStore(path)
    hermes = Hermes({"run_1": {"status": "completed", "output": answer_with(DRAFT)}})
    assert recovery.sweep(store, tmp_path / "state.db", tuple, {}, hermes=hermes) == [("k1", "completed")]
    _has_the_draft(store, "k1")


def test_an_unconfirmed_run_settled_from_its_session_keeps_its_email_draft(tmp_path, monkeypatch):
    path = tmp_path / "s.sqlite3"
    _left_open(path, "k1", status="ambiguous", session_id="sess_1")
    store = StateStore(path)
    monkeypatch.setattr(recovery.T, "session_last_at", lambda db, sid: time.time() - 3600)
    monkeypatch.setattr(recovery.T, "session_answer", lambda db, sid, since=None: answer_with(DRAFT))
    assert recovery.sweep(store, tmp_path / "state.db", tuple, {}) == [("k1", "completed")]
    _has_the_draft(store, "k1")


def test_a_thread_task_settled_after_a_restart_keeps_its_email_draft(tmp_path, monkeypatch):
    path = tmp_path / "s.sqlite3"
    _left_open(path, "k1", status="running", run_id=None,
               continued={"session_id": "sess_1", "platform": "discord", "thread_id": "t1", "label": "a thread"})
    store = StateStore(path)
    monkeypatch.setattr(recovery.T, "session_answer", lambda db, sid, since=None: answer_with(DRAFT))
    assert recovery.sweep(store, tmp_path / "state.db", tuple, {}) == [("k1", "completed")]
    _has_the_draft(store, "k1")
