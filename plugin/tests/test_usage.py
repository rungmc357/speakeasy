"""A chat's usage (tokens + estimated cost) on its session page: compression segments, helpers and a
continued chat's Speakeasy follow-ups are added up, each billing kind is worded honestly, and every
figure is labelled an estimate. Invented chats in an invented Hermes state DB."""
import sqlite3

import pytest

from speakeasy import usage as U
from test_history import NOW, add_msg, add_session, make_db

COLS = ("input_tokens INTEGER DEFAULT 0", "output_tokens INTEGER DEFAULT 0", "cache_read_tokens INTEGER DEFAULT 0",
        "cache_write_tokens INTEGER DEFAULT 0", "reasoning_tokens INTEGER DEFAULT 0", "api_call_count INTEGER DEFAULT 0",
        "estimated_cost_usd REAL", "actual_cost_usd REAL", "cost_status TEXT", "billing_provider TEXT", "model TEXT")


def with_usage(path):
    make_db(path)
    db = sqlite3.connect(path)
    for c in COLS:
        db.execute(f"ALTER TABLE sessions ADD COLUMN {c}")
    db.commit()
    db.close()


def spend(path, sid, **vals):
    db = sqlite3.connect(path)
    db.execute(f"UPDATE sessions SET {', '.join(k + '=?' for k in vals)} WHERE id=?", (*vals.values(), sid))
    db.commit()
    db.close()


@pytest.fixture
def db(service):
    path = service.rt.state_db
    with_usage(path)
    return path


def test_priced_chat_adds_its_segments_and_helpers(service, db):
    add_session(db, "c1", "discord", title="Garden plan", at=NOW - 900)
    add_msg(db, "c1", "user", "Plan the tomato beds", at=NOW - 890)
    add_session(db, "c2", "discord", parent="c1", at=NOW - 100)        # compression segment
    add_session(db, "h1", "subagent", parent="c2", at=NOW - 80)        # a helper
    add_session(db, "h2", "subagent", parent="h1", at=NOW - 70)        # its helper
    add_session(db, "x1", "discord", at=NOW - 60)                      # someone else's chat
    common = dict(cost_status="estimated", billing_provider="anthropic", model="claude-opus-5-5")
    spend(db, "c1", input_tokens=1000, cache_read_tokens=9000, output_tokens=500, api_call_count=3,
          estimated_cost_usd=0.25, **common)
    spend(db, "c2", input_tokens=2000, output_tokens=500, reasoning_tokens=100, api_call_count=2,
          estimated_cost_usd=0.5, **common)
    spend(db, "h1", input_tokens=500, output_tokens=500, api_call_count=1, estimated_cost_usd=0.75, **common)
    spend(db, "h2", input_tokens=500, output_tokens=0, api_call_count=1, estimated_cost_usd=1.0, **common)
    spend(db, "x1", input_tokens=99999, output_tokens=99999, estimated_cost_usd=50, **common)

    usage = service.history_session("c2", {})["session"]["usage"]
    assert usage["estimate"] is True and usage["basis"] == "estimated"
    assert usage["cost_usd"] == pytest.approx(2.5)
    assert usage["tokens"] == 1000 + 9000 + 500 + 2000 + 500 + 1000 + 500
    assert usage["output_tokens"] == 1500 and usage["cache_read_tokens"] == 9000 and usage["model_calls"] == 7
    assert usage["helpers"] == 2
    assert usage["summary"] == "About $2.50 · estimate"
    lines = {l["label"]: l["value"] for l in usage["lines"]}
    assert lines["Cost"] == "About $2.50 (estimate)"
    assert lines["Sent to the model"] == "13K (9K reused from cache)"
    assert lines["Written by the model"] == "1.5K (incl. 100 thinking)"
    assert lines["Includes"] == "2 helpers"
    assert "estimate" in usage["note"].lower()
    assert [m["name"] for m in usage["models"]] == ["Claude Opus 5.5"]


def test_plan_sign_in_says_included_not_free_or_zero_dollars(service, db):
    add_session(db, "p1", "telegram", title="Packing list", at=NOW - 50)
    spend(db, "p1", input_tokens=40000, output_tokens=2000, cost_status="included", estimated_cost_usd=0.0,
          billing_provider="openai-codex", model="gpt-5.6")
    usage = service.history_session("p1", {})["session"]["usage"]
    assert usage["basis"] == "included" and usage["cost_usd"] is None
    assert usage["summary"] == "Included in your ChatGPT plan · 42K tokens"
    assert {l["label"]: l["value"] for l in usage["lines"]}["Cost"] == "Nothing billed per use"
    assert "$" not in usage["summary"]


def test_unpriced_local_model_says_no_price_known(service, db):
    add_session(db, "u1", "cli", title="Rename photos", at=NOW - 50)
    spend(db, "u1", input_tokens=1200, output_tokens=300, cost_status="unknown", estimated_cost_usd=0.0,
          billing_provider="custom", model="some-local-model")
    usage = service.history_session("u1", {})["session"]["usage"]
    assert usage["basis"] == "unknown" and usage["cost_usd"] is None
    assert usage["summary"] == "1.5K tokens · no price known"


def test_mixed_billing_names_both_parts(service, db):
    add_session(db, "m1", "discord", title="Trip budget", at=NOW - 50)
    add_session(db, "m2", "subagent", parent="m1", at=NOW - 40)
    spend(db, "m1", input_tokens=1000, output_tokens=0, cost_status="estimated", estimated_cost_usd=0.004,
          billing_provider="anthropic", model="claude-opus-5-5")
    spend(db, "m2", input_tokens=3000, output_tokens=0, cost_status="included", billing_provider="openai-codex",
          model="gpt-5.6")
    usage = service.history_session("m1", {})["session"]["usage"]
    assert usage["basis"] == "mixed"
    assert usage["summary"] == "Under 1¢ · estimate, plus plan use"
    lines = {l["label"]: l["value"] for l in usage["lines"]}
    assert lines["Covered by ChatGPT plan"] == "3K tokens"
    assert len(usage["models"]) == 2


def test_continued_chat_counts_its_speakeasy_follow_ups(service, db, monkeypatch):
    add_session(db, "d9", "discord", title="Bike rack", at=NOW - 500)
    add_msg(db, "d9", "user", "How many bikes fit on the wall rack?", at=NOW - 490)
    add_session(db, "speakeasy_task_l1", "api_server", at=NOW - 100)
    spend(db, "d9", input_tokens=1000, output_tokens=100, cost_status="estimated", estimated_cost_usd=0.1,
          model="claude-opus-5-5")
    spend(db, "speakeasy_task_l1", input_tokens=500, output_tokens=50, cost_status="estimated",
          estimated_cost_usd=0.05, model="claude-opus-5-5")
    monkeypatch.setattr(service, "_continuations", lambda sid: ["k1"] if sid == "d9" else [])
    real = service.store.session_for
    monkeypatch.setattr(service.store, "session_for", lambda k: "speakeasy_task_l1" if k == "k1" else real(k))
    usage = service.history_session("d9", {})["session"]["usage"]
    assert usage["cost_usd"] == pytest.approx(0.15) and usage["linked_tasks"] == 1
    assert usage["summary"] == "About 15¢ · estimate"
    assert {l["label"]: l["value"] for l in usage["lines"]}["Includes"] == "1 Speakeasy follow-up"


def test_no_tokens_or_old_hermes_means_no_usage_block(service):
    path = service.rt.state_db
    make_db(path)                       # an older Hermes: no usage columns at all
    add_session(path, "o1", "discord", title="Old chat", at=NOW - 50)
    assert "usage" not in service.history_session("o1", {})["session"]
    assert U.session_usage(path, "o1") is None


def test_empty_usage_columns_mean_no_block(service, db):
    add_session(db, "e1", "discord", title="Quiet chat", at=NOW - 50)
    assert "usage" not in service.history_session("e1", {})["session"]


@pytest.mark.parametrize("n,text", [(0, "0"), (999, "999"), (1000, "1K"), (1234, "1.2K"), (99_500, "99.5K"),
                                    (250_000, "250K"), (2_500_000, "2.5M"), (375_000_000, "375M")])
def test_tokens_text(n, text):
    assert U.tokens_text(n) == text


@pytest.mark.parametrize("usd,text", [(0.004, "under 1¢"), (0.4, "40¢"), (3.761, "$3.76"), (1234.5, "$1,234")])
def test_money_text(usd, text):
    assert U.money_text(usd) == text
