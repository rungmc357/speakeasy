"""SPK-231 final card decisions (D1-D5) and rough spots (R1) on the card validator. Invented fixtures."""
from __future__ import annotations

import json

from speakeasy import views
from speakeasy.text import split_result


def _result(answer: str, cards: list[dict], spoken: str = "Here it is.") -> dict:
    out = answer + "\n```speakeasy-views\n" + json.dumps(cards) + f"\n```\nSPOKEN: {spoken}"
    result = split_result(out, ()) or {}
    result.setdefault("views", [])
    return result


# -- D1: rows ----------------------------------------------------------------------------------------

def test_rows_sort_attention_first_and_keep_detail_on_the_top_five_only():
    items = [{"ref": f"#{10 + n}", "name": f"Tidy module {n}", "state": "Open", "who": "Sam K.", "when": f"{n}d",
              "note": "Waiting on review"} for n in range(1, 8)]
    items[6] = {**items[6], "state": "CI failing", "tone": "bad"}
    items[3] = {**items[3], "state": "Stale", "tone": "warn", "who": ""}
    card = views.clean({"kind": "rows", "entity": "pr", "title": "Open PRs", "items": items})
    names = [i["name"] for i in card["items"]]
    assert names[:2] == ["Tidy module 7", "Tidy module 4"]           # bad, then warn, then the agent's order
    assert names[2:] == ["Tidy module 1", "Tidy module 2", "Tidy module 3", "Tidy module 5", "Tidy module 6"]
    for row in card["items"][:5]:
        assert "when" in row and "note" in row
    assert "who" not in card["items"][1]                             # an empty field is left out
    for row in card["items"][5:]:
        assert not {"who", "when", "note"} & set(row) and row["state"]


# -- D2: metric baselines ----------------------------------------------------------------------------

def test_a_non_server_metric_keeps_its_number_and_drops_its_comparison():
    card = views.clean({"kind": "metric", "label": "Newsletter signups today", "value": "48", "delta": "+12%",
                        "good": True, "threshold": 40, "threshold_label": "Normal", "note": "3x a normal day",
                        "points": [{"label": "Mon", "value": 30}, {"label": "Tue", "value": 48}]})
    assert card == {"kind": "metric", "label": "Newsletter signups today", "value": "48"}


def test_a_server_metric_keeps_a_said_baseline_and_loses_an_invented_one():
    raw = {"kind": "metric", "label": "Errors per hour", "source": "Sentry", "value": "270", "delta": "9x",
           "threshold": 30, "threshold_label": "Normal"}
    said = "Errors are at 270 an hour, 9x the normal 30, since 3:12 PM."
    card = _result(said, [raw])["views"][0]
    assert card["delta"] == "9x" and card["threshold"] == 30
    card = _result("Errors are at 270 an hour.", [raw])["views"][0]
    assert card["value"] == "270" and not {"delta", "threshold", "threshold_label"} & set(card)


# -- D3: document previews ---------------------------------------------------------------------------

def test_a_document_preview_has_no_thumbnail_and_keeps_its_editor_and_app():
    raw = {"kind": "preview", "entity": "document", "title": "Offline drafts spec", "summary": "Drafts save offline.",
           "photo_url": "https://example.com/thumb.png", "editor": "Dana", "edited": "2h ago", "app": "Notion"}
    card = _result("The Offline drafts spec was edited by Dana 2h ago; drafts now save offline.", [raw])["views"][0]
    assert "photo_url" not in card and card["editor"] == "Dana" and card["edited"] == "2h ago" and card["app"] == "Notion"
    card = _result("The Offline drafts spec says drafts save offline.", [raw])["views"][0]
    assert "editor" not in card and "edited" not in card                # not said: left off, the card stays


def test_an_album_preview_keeps_its_cover_and_has_no_editor():
    raw = {"kind": "preview", "entity": "media", "title": "Night Garden", "photo_url": "https://example.com/c.jpg",
           "editor": "Dana"}
    card = views.clean(raw)
    assert card["photo_url"] == "https://example.com/c.jpg" and "editor" not in card


# -- D5: pending checks ------------------------------------------------------------------------------

def test_identical_waiting_checks_collapse_and_every_failure_is_listed():
    checks = [{"name": f"Lint {n}", "state": "failed"} for n in range(1, 8)]
    checks += [{"name": f"Build {n}", "state": "pending"} for n in range(1, 7)]
    checks += [{"name": "Docs", "state": "passed"}]
    card = views.clean({"kind": "pr", "number": 42, "title": "Tidy the share sheet", "state": "failing",
                        "reason": "Seven lint checks fail.", "checks": checks})
    names = [c["name"] for c in card["checks"]]
    assert names[:7] == [f"Lint {n}" for n in range(1, 8)]              # all seven failures, past the six cap
    assert names[7] == "6 checks waiting" and card["checks"][7] == {"name": "6 checks waiting", "state": "pending", "count": 6}
    assert card["checks_total"] == 14 and card["checks_rest"] == "passed"


def test_one_waiting_check_stays_by_name_and_noted_ones_stay_apart():
    card = views.clean({"kind": "pr", "number": 7, "title": "Bump the parser", "state": "open", "reason": "Waiting on CI.",
                        "checks": [{"name": "Build", "state": "pending"}, {"name": "Deploy", "state": "pending",
                                                                            "note": "Waiting for approval to run"}]})
    assert [c["name"] for c in card["checks"]] == ["Build", "Deploy"] and "checks_total" not in card


# -- R1: cards don't repeat the reply ----------------------------------------------------------------

def test_a_check_note_that_restates_the_cause_goes():
    raw = {"kind": "pr", "number": 88, "title": "Share sheet polish", "state": "failing",
           "reason": "A snapshot test fails on the share sheet layout.",
           "checks": [{"name": "Tests", "state": "failed", "note": "Snapshot mismatch on share sheet layout"},
                      {"name": "Lint", "state": "passed"}]}
    card = _result("PR #88 Share sheet polish is failing: a snapshot test fails on the share sheet layout. "
                   "It's flaky on main too, so rerun it.", [raw])["views"][0]
    assert "reason" not in card and all("note" not in c for c in card["checks"])
    labels = [a["label"] for a in card.get("actions") or []]
    assert labels[0] == "Rerun checks" and "Fix the checks" not in labels   # the button agrees with the reply
    assert card["actions"][0]["style"] == "primary"
    assert views.follow_up(card, card["actions"][0]["id"]) == "Rerun checks (PR #88)."


def test_an_offered_rerun_leads_over_an_offered_fix_when_the_reply_says_rerun():
    raw = {"kind": "pr", "number": 91, "title": "Cache warmup", "state": "failing", "reason": "One test timed out.",
           "checks": [{"name": "Tests", "state": "failed"}],
           "actions": [{"id": "do_0", "label": "Fix the test", "style": "primary"}, {"id": "do_1", "label": "Rerun failed"}]}
    card = _result("PR #91 Cache warmup failed on one timeout; that test is flaky, so rerun it.", [raw])["views"][0]
    assert [a["label"] for a in card["actions"]] == ["Rerun failed"] and card["actions"][0]["style"] == "primary"


def test_a_fix_stays_when_the_reply_says_fix():
    raw = {"kind": "pr", "number": 92, "title": "Cache warmup", "state": "failing", "reason": "A real bug breaks the cache test.",
           "checks": [{"name": "Tests", "state": "failed"}]}
    card = _result("PR #92 Cache warmup has a real bug in the cache test; it needs a code fix.", [raw])["views"][0]
    assert card["actions"][0]["label"] == "Fix the checks"


def test_todays_weather_row_doesnt_repeat_the_hero_high():
    card = views.clean({"kind": "weather", "place": "Springfield", "temp": 61, "high": 68, "low": 50,
                        "days": [{"label": "Today", "high": 68, "low": 50}, {"label": "Sat", "high": 64, "low": 48}]})
    assert [d["label"] for d in card["days"]] == ["Sat"]
    card = views.clean({"kind": "weather", "place": "Springfield", "temp": 61, "high": 68, "low": 50,
                        "days": [{"label": "Today", "high": 68, "low": 50}]})
    assert "days" not in card
