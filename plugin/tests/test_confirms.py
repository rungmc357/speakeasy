"""Invented previews only; no live services or actions."""
import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from speakeasy import confirms as C


def checkout():
    return {"kind": "confirm", "type": "checkout", "title": "Studio headphones", "items": [
        {"name": "Studio headphones", "quantity": 1, "colour": "Sand", "size": "Standard"}],
        "details": {"merchant": "Example Audio", "address": "Home · Northside", "delivery": "Friday",
                    "payment_last4": "4242", "subtotal": "80.00", "tax": "6.40", "shipping": "0.00",
                    "total": "86.40", "currency": "USD", "checkout_url": "https://example.com/checkout",
                    "total_source": "merchant_final"}}


def store(tmp_path, view):
    db = sqlite3.connect(tmp_path / "cards.sqlite", check_same_thread=False)
    db.execute("CREATE TABLE runs (idem_key TEXT PRIMARY KEY,run_id TEXT,result_json TEXT)")
    db.execute("INSERT INTO runs VALUES (?,?,?)", ("key", "run_card", json.dumps({"views": [view]})))
    db.commit()
    return SimpleNamespace(_db=db, _lock=threading.Lock())


def test_exact_checkout_card():
    v = C.clean(checkout())
    assert v["state"] == "pending"
    assert v["actions"][0]["label"] == "Buy for $86.40"
    assert v["actions"][0]["value"] == v["sha256"] == C.digest(v)
    assert v["details"]["payment_last4"] == "4242"


@pytest.mark.parametrize("field,value", [("total_source", "estimate"), ("payment_last4", "4242424242424242"),
    ("total", "86.41"), ("total", 86.40), ("tax", "NaN"), ("shipping", "-1"),
    ("checkout_url", "http://example.com"), ("currency", "dollars")])
def test_checkout_rejects_incomplete_or_untrusted_total(field, value):
    raw = checkout(); raw["details"][field] = value
    assert C.clean(raw) is None


def test_message_is_never_silently_clipped():
    raw = {"type": "send", "title": "Reply", "details": {"recipient": "Example support", "channel": "email", "body": "x" * 20001}}
    assert C.clean(raw) is None
    raw["details"]["body"] = "Line one\n\nLine two"
    assert C.clean(raw)["details"]["body"] == "Line one\n\nLine two"


@pytest.mark.parametrize("kind", sorted(C.KINDS - {"checkout"}))
def test_all_shared_types_validate(kind):
    details = {field: "Example" for field in C.REQUIRED[kind]}
    if kind == "pay":
        details.update(total="12.00", currency="USD", payment_last4="4242")
    if kind in {"subscription", "home", "delete_share"}:
        details["operation"] = {"subscription": "cancel", "home": "unlock", "delete_share": "share"}[kind]
    raw = {"type": kind, "title": "Example preview", "details": details}
    if kind == "form":
        raw["fields"] = [{"label": "Attendance", "value": "Two", "guessed": True}]
    v = C.clean(raw)
    assert v and v["state"] == "pending" and len(v["actions"]) == 2
    for field in C.REQUIRED[kind]:
        missing = copy.deepcopy(raw); del missing["details"][field]
        assert C.clean(missing) is None


def test_agent_cannot_supply_approved_state_or_buttons():
    raw = checkout(); raw.update(state="done", sha256="made-up", actions=[{"id": "buy"}], receipt={"order": "fake"})
    v = C.clean(raw)
    assert v["state"] == "pending" and "receipt" not in v
    assert v["sha256"] != "made-up" and v["actions"][0]["id"] == "approve"


@pytest.mark.parametrize("source,live", [("voice", False), ("agent", True), ("typed", True)])
def test_off_call_voice_and_agent_approval_refused(tmp_path, source, live):
    v = C.clean(checkout()); s = store(tmp_path, v)
    with pytest.raises(C.Conflict):
        C.claim(s, "run_card", 0, v["sha256"], "approve", source=source, live_call=live)
    assert json.loads(s._db.execute("SELECT result_json FROM runs").fetchone()[0])["views"][0]["state"] == "pending"


@pytest.mark.parametrize("source,live", [("tap", False), ("voice", True)])
def test_exact_preview_claims_once_and_survives_reopen(tmp_path, source, live):
    v = C.clean(checkout()); s = store(tmp_path, v)
    assert C.claim(s, "run_card", 0, v["sha256"], "approve", source=source, live_call=live)[2]
    s._db.close(); s._db = sqlite3.connect(tmp_path / "cards.sqlite", check_same_thread=False)
    assert not C.claim(s, "run_card", 0, v["sha256"], "approve", source=source, live_call=live)[2]


def test_price_change_requires_fresh_approval(tmp_path):
    before = C.clean(checkout()); raw = checkout()
    raw["details"].update(subtotal="90.00", total="96.40")
    after = C.clean(raw); s = store(tmp_path, after)
    with pytest.raises(C.Conflict, match="changed"):
        C.claim(s, "run_card", 0, before["sha256"], "approve", source="tap")
    assert C.claim(s, "run_card", 0, after["sha256"], "approve", source="tap")[2]


def test_mutation_without_rehash_is_rejected(tmp_path):
    v = C.clean(checkout()); v["details"]["address"] = "Elsewhere"
    s = store(tmp_path, v)
    with pytest.raises(C.Conflict):
        C.claim(s, "run_card", 0, v["sha256"], "approve", source="tap")


def test_cancel_never_dispatches_and_cannot_be_approved(tmp_path):
    v = C.clean(checkout()); s = store(tmp_path, v)
    assert not C.claim(s, "run_card", 0, v["sha256"], "cancel", source="tap")[2]
    assert not C.claim(s, "run_card", 0, v["sha256"], "approve", source="tap")[2]


def test_concurrent_double_tap_dispatches_once(tmp_path):
    v = C.clean(checkout()); s = store(tmp_path, v)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: C.claim(s, "run_card", 0, v["sha256"], "approve", source="tap")[2], range(12)))
    assert sum(results) == 1


def test_replayed_agent_output_cannot_restore_consumed_approval(tmp_path):
    v = C.clean(checkout()); s = store(tmp_path, v)
    assert C.claim(s, "run_card", 0, v["sha256"], "approve", source="tap")[2]
    with s._db:
        s._db.execute("UPDATE runs SET result_json=?", (json.dumps({"views": [v]}),))
    assert not C.claim(s, "run_card", 0, v["sha256"], "approve", source="tap")[2]


def test_execution_requests_fresh_total_check():
    msg = C.execution_message(C.clean(checkout()))
    assert "DO NOT ACT" in msg and "final total again" in msg
