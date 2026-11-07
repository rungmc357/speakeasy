import asyncio
import json

import pytest
from speakeasy import confirms as C, confirm_actions as A, confirm_voice as V
from test_confirms import checkout, store
from test_confirm_actions import service, state, voice_worker


@pytest.mark.parametrize("value", ["é", "", 3, None, "a" * 65])
def test_bad_digest_fails_cleanly(tmp_path, value):
    view = C.clean(checkout()); s = store(tmp_path, view)
    with pytest.raises(C.Conflict):
        C.claim(s, "run_card", 0, value, "approve", source="tap")


def test_process_loss_is_unverified_not_retryable(tmp_path):
    view = C.clean(checkout()); s = store(tmp_path, view)
    C.claim(s, "run_card", 0, view["sha256"], "approve", source="tap")
    assert C.recover(s) == 1
    saved = json.loads(s._db.execute("SELECT result_json FROM runs").fetchone()[0])["views"][0]
    assert saved["state"] == "unverified" and saved["actions"] == []
    assert C.recover(s) == 0
    assert not C.claim(s, "run_card", 0, view["sha256"], "approve", source="tap")[2]


def test_replacement_does_not_merge_stale_checkout_fields(tmp_path, monkeypatch):
    raw = {"kind": "confirm", "type": "post", "title": "An update", "details": {"account": "Example account", "body": "Example post"}}
    svc, view, starts = service(tmp_path, monkeypatch, output='```speakeasy-views\n' + json.dumps([raw]) + '\n```')
    A.decide(svc, "run_card", 0, view["sha256"], "approve")
    current = state(svc)
    assert current["state"] == "pending" and "items" not in current
    assert C.digest(current) == current["sha256"]
    assert svc.store._db.execute("SELECT state FROM confirmation_claims").fetchone()[0] == "replaced"


def test_focus_on_unrelated_task_does_not_approve_only_card():
    worker, decisions = voice_worker()
    worker.focus = lambda: ("unrelated", "")
    assert asyncio.run(V.answer(worker, "reply", 1, "yes"))
    assert decisions == [] and not hasattr(worker, "confirmation_offers")


def test_new_price_is_spoken_again_and_never_spends_old_yes():
    worker, decisions = voice_worker()
    asyncio.run(V.answer(worker, "first", 1, "yes"))
    raw = checkout(); raw["details"].update(subtotal="90.00", total="96.40")
    changed = C.clean(raw)
    worker.store.run_result = lambda run: ("key", {"views": [changed]})
    asyncio.run(V.answer(worker, "old_yes", 2, "yes"))
    assert decisions == []
    assert "preview changed" in worker.append.call_args.args[2].lower()
    assert "96.40" in worker.append.call_args.args[2]
    asyncio.run(V.answer(worker, "new_yes", 3, "yes"))
    assert len(decisions) == 1 and decisions[0][2] == changed["sha256"]


def test_spoken_offer_expires_and_requires_reask():
    worker, decisions = voice_worker()
    asyncio.run(V.answer(worker, "first", 1, "yes"))
    key = ("task", 0); digest, stamp = worker.confirmation_offers[key]
    worker.confirmation_offers[key] = (digest, stamp - 121)
    asyncio.run(V.answer(worker, "late", 2, "yes"))
    assert decisions == []


def test_replayed_result_writes_back_consumed_state(tmp_path):
    view = C.clean(checkout()); s = store(tmp_path, view)
    C.claim(s, "run_card", 0, view["sha256"], "cancel", source="tap")
    with s._db:
        s._db.execute("UPDATE runs SET result_json=?", (json.dumps({"views": [view]}),))
    C.claim(s, "run_card", 0, view["sha256"], "approve", source="tap")
    saved = json.loads(s._db.execute("SELECT result_json FROM runs").fetchone()[0])["views"][0]
    assert saved["state"] == "cancelled" and saved["actions"] == []


def test_edit_changes_hash_and_old_send_cannot_authorize_it(tmp_path):
    raw = {"type": "send", "title": "Reply", "details": {"recipient": "Example support", "channel": "email", "body": "Before", "subject": "Question"}}
    view = C.clean(raw); s = store(tmp_path, view)
    C.edit(s, "run_card", 0, {"sha256": view["sha256"], "body": "After", "subject": "Updated"})
    saved = json.loads(s._db.execute("SELECT result_json FROM runs").fetchone()[0])["views"][0]
    assert saved["details"]["body"] == "After" and saved["state"] == "pending"
    assert saved["sha256"] != view["sha256"]
    with pytest.raises(C.Conflict):
        C.claim(s, "run_card", 0, view["sha256"], "approve", source="tap")
    assert C.claim(s, "run_card", 0, saved["sha256"], "approve", source="tap")[2]
