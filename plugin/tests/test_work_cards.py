"""Work cards (Home v7): issue, pr, jobs, site, mockups, and a calendar day's overlaps. The agent sends
facts only; the checker drops a card rather than show a wrong one, the server sets every button, and a
picture path becomes a card number the app fetches (never a server path). Every fixture is invented."""
from __future__ import annotations

import json
from pathlib import Path

from speakeasy import views
from speakeasy.prompt import builder
from speakeasy.text import split_result


def _result(answer: str, cards: list[dict], roots: tuple = ()) -> dict:
    out = answer + "\n```speakeasy-views\n" + json.dumps(cards) + "\n```\nSPOKEN: Here it is."
    result = split_result(out, roots) or {}
    result.setdefault("views", [])
    return result


# -- issue ------------------------------------------------------------------------------------------

ISSUE = {"kind": "issue", "source": "Linear", "ref": "FN-142", "title": "Redesign notification settings",
         "state": "In review", "tone": "warn", "assignee": "Mara K.", "priority": "High",
         "labels": ["Design", "iOS"], "note": "2 comments, both on spacing", "updated": "2h ago",
         "url": "https://linear.app/fieldnote/issue/FN-142", "layout": "banner", "color": "#ff0000"}
ISSUE_ANSWER = "FN-142, Redesign notification settings, is in review with two spacing comments."


def test_issue_keeps_its_facts_and_never_layout():
    card = _result(ISSUE_ANSWER, [ISSUE])["views"][0]
    assert card["kind"] == "issue" and card["source"] == "linear" and card["ref"] == "FN-142"
    assert card["labels"] == ["Design", "iOS"] and card["tone"] == "warn"
    assert "layout" not in card and "color" not in card and "actions" not in card


def test_issue_needs_a_title_and_its_ref_said():
    assert views.clean({"kind": "issue", "ref": "FN-1"}) is None
    assert _result("Redesign notification settings is in review.", [ISSUE])["views"] == []


def test_issue_title_may_come_from_the_users_own_ask():
    # The agent wrote the ticket from the user's words and its answer only names the ref: the title
    # is grounded by the ask (as a label), the ref still has to be in the answer.
    card = {"kind": "issue", "source": "linear", "ref": "FN-77", "title": "Share sheet hides behind keyboard on small phones",
            "state": "In Progress"}
    answer = "Here's the card for FN-77, made from what you gave me.\n```speakeasy-views\n" + json.dumps([card]) + "\n```\nSPOKEN: FN-77 is ready."
    ask = "Make an example issue FN-77: the Share sheet hides behind the keyboard on small phones."
    assert (split_result(answer, ()) or {}).get("views", []) == []
    kept = split_result(answer, (), asked=ask)["views"]
    assert [v["ref"] for v in kept] == ["FN-77"]
    # A title neither the answer nor the ask says still goes, and so does an unsaid ref.
    assert (split_result(answer, (), asked="Make an example issue FN-77 about the login page.") or {}).get("views", []) == []
    unsaid = answer.replace("FN-77", "it").replace('"ref": "it"', '"ref": "FN-77"')
    assert (split_result(unsaid, (), asked=ask) or {}).get("views", []) == []


def test_issue_unknown_source_is_other_and_odd_tone_dropped():
    card = views.clean({"kind": "issue", "title": "X", "source": "trello", "tone": "purple"})
    assert card["source"] == "other" and "tone" not in card


def test_issue_mockup_path_becomes_a_card_number(tmp_path: Path):
    shot = tmp_path / "mock.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    result = _result(ISSUE_ANSWER, [{**ISSUE, "image": str(shot)}], roots=(tmp_path,))
    card = result["views"][0]
    assert card["image"] == 1 and str(tmp_path) not in json.dumps(result["views"])


def test_issue_picture_outside_the_allowed_folders_is_dropped(tmp_path: Path):
    card = _result(ISSUE_ANSWER, [{**ISSUE, "image": "/etc/hosts"}], roots=(tmp_path,))["views"][0]
    assert "image" not in card


# -- pr ---------------------------------------------------------------------------------------------

PR = {"kind": "pr", "repo": "fieldnote/app", "number": 418, "title": "Offline drafts keep their attachments",
      "state": "Conflicts", "reason": "DraftStore.swift changed on main after you branched.", "files": 4,
      "checks": [{"name": "Build", "state": "passed"}, {"name": "Unit tests", "state": "passed", "note": "412 passed"},
                 {"name": "UI tests", "state": "skipped", "note": "conflicts first"}, {"name": "Lint", "state": "odd"}],
      "url": "https://github.com/fieldnote/app/pull/418", "actions": [{"id": "merge", "label": "Merge"}]}
PR_ANSWER = "PR #418, Offline drafts keep their attachments, has conflicts in the draft store."


def test_pr_conflicts_offers_fix_and_never_the_agents_buttons():
    card = _result(PR_ANSWER, [PR])["views"][0]
    assert card["state"] == "conflicts" and [c["name"] for c in card["checks"]] == ["Build", "Unit tests", "UI tests"]
    assert card["actions"] == [{"id": "fix", "label": "Fix conflicts", "style": "primary", "confirm": False}]


def test_pr_failing_offers_fix_the_checks_and_passing_offers_nothing():
    failing = _result(PR_ANSWER, [{**PR, "state": "failing"}])["views"][0]
    assert failing["actions"][0]["label"] == "Fix the checks"
    assert "actions" not in _result(PR_ANSWER, [{**PR, "state": "passing"}])["views"][0]


def test_pr_needs_a_reason_and_a_known_state():
    assert views.clean({**PR, "reason": ""}) is None
    assert views.clean({**PR, "state": "vibing"}) is None


def test_pr_number_must_be_said():
    assert _result("Offline drafts keep their attachments has conflicts.", [PR])["views"] == []


def test_pr_fix_reaches_the_task_in_plain_words():
    card = _result(PR_ANSWER, [PR])["views"][0]
    words = views.follow_up(card, "fix")
    assert words == "Fix the merge conflicts on the pull request fieldnote/app #418 (Offline drafts keep their attachments)."


# -- jobs -------------------------------------------------------------------------------------------

JOBS = {"kind": "jobs", "title": "Running now", "items": [
    {"name": "Fix offline attachments", "status": "Running the UI tests on a second iPhone", "elapsed": "22 min"},
    {"name": "Release notes for 2.9", "status": "Two headlines ready. Pick one and it finishes.", "elapsed": "8 min",
     "needs_you": True, "ask": "Pick a headline", "task_id": "t_abc"},
    {"name": "Pricing page copy", "status": "Reading the three competitor pages", "ask": "Should be dropped"},
    {"name": "No status"}, {"progress": 40}]}
JOBS_ANSWER = "Three running: Fix offline attachments, Release notes for 2.9 and Pricing page copy."


def test_jobs_keeps_status_lines_only_and_ask_only_when_it_needs_you():
    card = _result(JOBS_ANSWER, [JOBS])["views"][0]
    assert [i["name"] for i in card["items"]] == ["Fix offline attachments", "Release notes for 2.9", "Pricing page copy"]
    assert card["items"][1]["ask"] == "Pick a headline" and "ask" not in card["items"][2]
    assert "progress" not in json.dumps(card)


def test_jobs_every_name_said():
    assert _result("Two running.", [JOBS])["views"] == []


# -- site -------------------------------------------------------------------------------------------

def test_site_needs_its_url_and_keeps_title_and_sub():
    assert views.clean({"kind": "site", "title": "Pricing"}) is None
    assert views.clean({"kind": "site", "url": "http://fieldnote.app"}) is None
    card = _result("Here's the pricing preview.", [{"kind": "site", "url": "https://fieldnote.app/pricing",
                                                    "title": "Pricing", "sub": "Preview from PR #433"}])["views"][0]
    assert card == {"kind": "site", "url": "https://fieldnote.app/pricing", "title": "Pricing", "sub": "Preview from PR #433"}


# -- mockups ----------------------------------------------------------------------------------------

def test_mockups_keep_rendered_screens_and_next_steps_become_buttons(tmp_path: Path):
    shots = []
    for n in range(3):
        p = tmp_path / f"s{n}.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([n]) * 64)
        shots.append(str(p))
    raw = {"kind": "mockups", "title": "Onboarding", "items": [{"image": s, "caption": c} for s, c in
                                                               zip(shots, ["Welcome", "Bring your notes", "You're set"])]
           + [{"caption": "no picture"}], "next": ["Make it darker", "Send to Linear as a new issue please"]}
    card = _result("Three onboarding screens.", [raw], roots=(tmp_path,))["views"][0]
    assert [i["image"] for i in card["items"]] == [1, 2, 3] and len(card["items"]) == 3
    assert [a["id"] for a in card["actions"]] == ["next_0", "next_1"]
    assert card["actions"][1]["label"] == "Send to Linear as a new"
    assert views.follow_up(card, "next_0") == "Make it darker."
    assert views.follow_up(card, "next_9") is None


def test_mockups_with_no_picture_are_dropped():
    assert views.clean({"kind": "mockups", "title": "X", "items": [{"caption": "a"}]}) is None


# -- calendar day overlaps --------------------------------------------------------------------------

DAY = {"kind": "calendar_day", "title": "Monday, November 9", "events": [
    {"start": "2026-11-09T08:45", "end": "2026-11-09T09:00", "title": "Standup", "join_url": "https://meet.example/abc"},
    {"start": "2026-11-09T13:00", "end": "2026-11-09T14:00", "title": "Design review"},
    {"start": "2026-11-09T13:30", "end": "2026-11-09T14:00", "title": "1:1 with Theo"},
    {"start": "2026-11-09T16:00", "end": "2026-11-09T16:30", "title": "Release check-in"}]}


def test_day_overlap_is_found_and_offers_to_move_the_later_event():
    assert views.day_overlaps(DAY) == [(1, 2)]
    card = _result("Busy until 2.", [DAY])["views"][0]
    assert card["events"][0]["join_url"] == "https://meet.example/abc"
    assert card["actions"] == [{"id": "move", "label": "Move 1:1 with Theo", "style": "secondary", "confirm": False}]
    assert views.follow_up(card, "move") == ("Move 1:1 with Theo so it no longer overlaps Design review. "
                                             "Show me the new time before changing it.")


def test_day_without_overlap_or_with_word_times_offers_nothing():
    calm = {**DAY, "events": [DAY["events"][0], DAY["events"][3]]}
    assert "actions" not in _result("Two things.", [calm])["views"][0]
    words = {"kind": "calendar_day", "events": [{"start": "1 PM", "end": "2 PM", "title": "A"},
                                                {"start": "1:30 PM", "end": "2 PM", "title": "B"}]}
    assert views.day_overlaps(views.clean(words)) == []
    assert views.clean({**DAY, "events": [{**DAY["events"][0], "join_url": "zoommtg://x"}]})["events"][0].get("join_url") is None


# -- the brief --------------------------------------------------------------------------------------

def test_brief_names_the_work_cards_and_their_facts():
    rule = builder.ARTIFACTS_RULE
    for words in ("issue (", "pr (", "jobs (", "site (", "mockups (", "reason (one plain line", "needs_you",
                  "join_url"):
        assert words in rule or words in builder.VIEWS_RULE, words
