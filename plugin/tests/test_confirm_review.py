"""Consent-critical regression cases, invented previews and fake execution only."""
import asyncio
import json
import sqlite3
import time

import pytest
from speakeasy import confirms as C, confirm_actions as A, confirm_voice as V
from test_confirms import checkout
from test_confirm_actions import service, state, voice_worker


def raw_card(kind):
    if kind == "checkout":
        return checkout()
    details = {key: "Example " + key for key in C.REQUIRED[kind]}
    if kind == "pay":
        details.update(total="12.00", currency="USD", payment_last4="4242")
    if kind in {"subscription", "home", "delete_share"}:
        details["operation"] = {"subscription": "sign_up", "home": "unlock", "delete_share": "share"}[kind]
    raw = {"type": kind, "title": "Example preview", "details": details}
    if kind == "form":
        raw["fields"] = [{"label": "Attendance", "value": "Two", "guessed": True}]
    return raw


@pytest.mark.parametrize("kind", sorted(C.KINDS))
def test_spoken_offer_contains_all_consent_terms(kind):
    card = C.clean(raw_card(kind))
    worker, _ = voice_worker()
    asyncio.run(V.present(worker, "reply", "task", 0, card))
    speech = worker.append.call_args.args[2]
    for key, value in card["details"].items():
        if key not in {"photo_url", "checkout_url", "total_source"}:
            assert value.replace("sign_up", "sign up") in speech
    for item in card.get("items", []):
        assert item["name"] in speech and str(item["quantity"]) in speech
    for field in card.get("fields", []):
        assert field["value"] in speech and "agent guessed" in speech
    assert worker.confirmation_offers[("task", 0)][0] == card["sha256"]


def test_share_and_subscription_exact_identifying_terms():
    for raw, terms in [
        ({"type": "delete_share", "title": "Share samples", "details": {"operation": "share", "items_description": "Two sample files", "recipient": "Example external group", "count": "2"}}, ["Example external group", "Two sample files"]),
        ({"type": "subscription", "title": "Membership", "details": {"operation": "sign_up", "consequences": "Starts recurring billing", "final_charge": "USD 89.00 monthly", "effective": "2030-05-01"}}, ["sign up", "USD 89.00 monthly", "2030-05-01", "Starts recurring billing"]),
    ]:
        worker, _ = voice_worker()
        asyncio.run(V.present(worker, "reply", "task", 0, C.clean(raw)))
        assert all(term in worker.append.call_args.args[2] for term in terms)


@pytest.mark.parametrize("length", [1900, 7000])
def test_long_offer_never_binds_clipped_terms(length):
    raw = raw_card("send"); raw["details"]["body"] = "x" * length
    worker, decisions = voice_worker()
    worker.confirmation_offers = {("task", 0): ("old", time.monotonic())}
    asyncio.run(V.present(worker, "reply", "task", 0, C.clean(raw)))
    assert not worker.confirmation_offers and not decisions
    assert "tap" in worker.append.call_args.args[2]


@pytest.mark.parametrize("count", [None, "0", "-1", "1.5", "01", "1000000", "everything immediately", True, 2])
def test_delete_rejects_invalid_count(count):
    raw = raw_card("delete_share"); raw["details"].update(operation="delete")
    if count is not None:
        raw["details"]["count"] = count
    assert C.clean(raw) is None


@pytest.mark.parametrize("count,label", [("1", "Delete 1 item"), ("2", "Delete 2 items"), ("999999", "Delete 999999 items")])
def test_delete_formats_numeric_label(count, label):
    raw = raw_card("delete_share"); raw["details"].update(operation="delete", count=count)
    assert C.clean(raw)["actions"][0]["label"] == label


@pytest.mark.parametrize("description", ["3 files", "Three sample files"])
def test_delete_rejects_conflicting_count(description):
    raw = raw_card("delete_share"); raw["details"].update(operation="delete", count="2", items_description=description)
    assert C.clean(raw) is None


@pytest.mark.parametrize("mode", ["unoffered", "changed", "expired", "multiple", "unrelated"])
def test_unbound_no_cannot_cancel(mode):
    worker, decisions = voice_worker(2 if mode == "multiple" else 1)
    view = C.clean(checkout())
    if mode in {"changed", "expired", "unrelated"}:
        worker.confirmation_offers = {("task", 0): ("old" if mode == "changed" else view["sha256"], time.monotonic() - (121 if mode == "expired" else 0))}
    asyncio.run(V.answer(worker, "reply", 1, "no", marked="other" if mode == "unrelated" else None))
    assert not decisions


@pytest.mark.parametrize("reopen", [False, True])
def test_a_b_a_is_nonretryable_and_startup_repairs_legacy_pending(tmp_path, monkeypatch, reopen):
    svc, original, starts = service(tmp_path, monkeypatch)
    changed = checkout(); changed["details"].update(subtotal="90.00", total="96.40")
    svc.agent.events = lambda run, cb: cb({"event": "run.completed", "output": '```speakeasy-views\n' + json.dumps([changed]) + '\n```'})
    A.decide(svc, "run_card", 0, original["sha256"], "approve")
    svc.agent.events = lambda run, cb: cb({"event": "run.completed", "output": '```speakeasy-views\n' + json.dumps([checkout()]) + '\n```'})
    A.decide(svc, "run_card", 0, C.clean(changed)["sha256"], "approve")
    assert state(svc)["state"] == "unverified" and state(svc)["actions"] == []
    assert "already used" in state(svc)["error"]
    if reopen:
        # Reproduce an old-version pending replay on disk before reopening.
        with svc.store._db:
            svc.store._db.execute("UPDATE runs SET result_json=?", (json.dumps({"views": [original]}),))
        svc.store._db.close()
        svc.store._db = sqlite3.connect(tmp_path / "cards.sqlite", check_same_thread=False)
        assert C.recover(svc.store) == 1
        assert state(svc)["state"] == "unverified" and not state(svc)["actions"]
        assert C.recover(svc.store) == 0
    A.decide(svc, "run_card", 0, original["sha256"], "approve")
    assert state(svc)["state"] == "unverified" and len(starts) == 2
