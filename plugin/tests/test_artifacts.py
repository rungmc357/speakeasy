"""Artifacts v1 (explore, compare, status, draft): typed fields, the checker that drops a card rather
than show a wrong one, and the buttons the server sets. Every fixture is invented and together they
cover the public categories: coding, research, scheduled monitoring, messaging, machines, decisions,
releases, planning, files, numbers and home."""
from __future__ import annotations

import json

import pytest

from speakeasy import views
from speakeasy.prompt import builder
from speakeasy.text import split_result


def _result(answer: str, cards: list[dict]) -> dict:
    out = answer + "\n```speakeasy-views\n" + json.dumps(cards) + "\n```\nSPOKEN: Here it is."
    result = split_result(out, ()) or {}
    result.setdefault("views", [])
    return result


# -- explore (P1 carousel) ---------------------------------------------------------------------------

STAYS = {"kind": "explore", "title": "Lisbon weekend", "sections": [
    {"label": "Hotels", "entity": "stay", "items": [
        {"name": "Casa do Rio", "when": "Fri 12 → Sun 14", "city": "Lisbon", "price": "$210",
         "photo_url": "https://hotel.example/rio.jpg", "url": "https://hotel.example", "lat": 38.71, "lon": -9.14}]},
    {"label": "Dinners", "entity": "reservation", "items": [
        {"name": "Taberna Azul", "when": "Sat 13 · 8:00 PM", "address": "12 Rua Nova, Lisbon"}]},
]}
TRIP_ANSWER = "Booked Casa do Rio for $210 a night, Fri 12 to Sun 14, and dinner at Taberna Azul Saturday at 8."


def test_planning_explore_card_is_kept_when_grounded():
    result = _result(TRIP_ANSWER, [STAYS])
    card = result["views"][0]
    assert card["kind"] == "explore" and [s["label"] for s in card["sections"]] == ["Hotels", "Dinners"]
    assert card["sections"][0]["items"][0]["lat"] == 38.71
    assert "actions" not in card


def test_a_name_missing_from_the_answer_drops_the_whole_card():
    result = _result("Booked Casa do Rio for $210 a night.", [STAYS])
    assert result["views"] == []


def test_a_price_missing_from_the_answer_drops_the_card():
    result = _result(TRIP_ANSWER.replace("$210", "$190"), [STAYS])
    assert result["views"] == []


def test_research_sources_as_articles_and_unknown_entities_are_dropped():
    card = views.clean({"kind": "explore", "title": "Sources", "sections": [
        {"label": "Read", "entity": "article", "items": [{"name": "Battery study", "url": "https://journal.example/a1"},
                                                          {"sub": "no name"}]},
        {"label": "Odd", "entity": "spaceship", "items": [{"name": "X"}]}]})
    assert [s["label"] for s in card["sections"]] == ["Read"]
    assert card["sections"][0]["items"] == [{"name": "Battery study", "url": "https://journal.example/a1"}]


def test_media_ids_are_checked_and_never_a_scheme():
    card = views.clean({"kind": "explore", "title": "Listen", "sections": [{"label": "Album", "entity": "media", "items": [
        {"name": "Night Garden", "creator": "The Paper Kites", "media_kind": "Album",
         "ids": {"apple_music": "1440857781", "spotify": "spotify:album:bad"}, "url": "music://x"}]}]})
    item = card["sections"][0]["items"][0]
    assert item["ids"] == {"apple_music": "1440857781"} and item["media_kind"] == "album" and "url" not in item


def test_ids_only_stay_on_media_and_bad_coordinates_go():
    card = views.clean({"kind": "explore", "sections": [{"label": "Cafes", "entity": "place", "items": [
        {"name": "Bean Bar", "ids": {"apple_music": "12345"}, "lat": 120, "lon": 4, "media_kind": "album"}]}]})
    assert card["sections"][0]["items"][0] == {"name": "Bean Bar"}


def test_coding_pull_requests_explore():
    answer = "Two pull requests are open: #41 Fix the login timeout passes CI, #42 Add dark mode is failing."
    result = _result(answer, [{"kind": "explore", "title": "Open PRs", "sections": [
        {"label": "Pull requests", "entity": "pr", "items": [
            {"name": "Fix the login timeout", "sub": "#41", "state": "CI passing", "url": "https://github.com/acme/app/pull/41"},
            {"name": "Add dark mode", "sub": "#42", "state": "CI failing", "url": "https://github.com/acme/app/pull/42"}]}]}])
    assert len(result["views"][0]["sections"][0]["items"]) == 2


# -- compare (P3) --------------------------------------------------------------------------------------

DESKS = {"kind": "compare", "title": "Standing desks", "winner": 1, "why": "Steadiest at full height",
         "reasons": ["Longest warranty"], "options": [
             {"name": "Riser One", "price": "$549", "why": "Cheapest, more sway"},
             {"name": "Lift Pro", "price": "$699"},
             {"name": "Stand Max", "price": "$649"}]}
DESK_ANSWER = "Lift Pro at $699 is my pick; Stand Max is $649 and Riser One is $549 but sways."


def test_decisions_compare_keeps_winner_and_gets_a_confirmed_go_button():
    card = _result(DESK_ANSWER, [DESKS])["views"][0]
    assert card["winner"] == 1
    assert card["actions"] == [{"id": "go", "label": "Go with Lift Pro", "style": "primary", "confirm": True}]


@pytest.mark.parametrize("change", [{"winner": None}, {"winner": 7}, {"winner": 0.5}, {"why": ""},
                                    {"why": "x" * 81}, {"options": [{"name": "Only one"}]}])
def test_compare_without_a_real_winner_or_two_options_is_dropped(change):
    raw = {**DESKS, **change}
    if raw.get("winner") is None:
        raw.pop("winner")
    assert views.clean(raw) is None


def test_agent_actions_on_artifacts_are_replaced():
    raw = {**DESKS, "actions": [{"id": "buy_now", "label": "Buy now", "style": "primary"}]}
    card = _result(DESK_ANSWER, [raw])["views"][0]
    assert [a["id"] for a in card["actions"]] == ["go"]


# -- status (P5) ---------------------------------------------------------------------------------------

MACHINES = {"kind": "status", "scope": "Build servers", "items": [
    {"name": "runner-a", "role": "CI", "state": "OK", "value": "41", "unit": "jobs/hr", "history": [30, 35, 41]},
    {"name": "runner-b", "role": "Cache", "state": "warn", "value": "92%", "note": "disk nearly full"},
    {"name": "runner-c", "state": "exploded"}]}
MACHINE_ANSWER = "runner-a is fine at 41 jobs an hour; runner-b's disk is at 92%."


def test_machines_status_tiles_drop_unknown_states():
    card = _result(MACHINE_ANSWER, [MACHINES])["views"][0]
    assert [(i["name"], i["state"]) for i in card["items"]] == [("runner-a", "ok"), ("runner-b", "warn")]


def test_status_value_not_in_answer_drops_card():
    assert _result(MACHINE_ANSWER.replace("92%", "90%"), [MACHINES])["views"] == []


def test_home_devices_and_monitoring_as_status():
    answer = "The porch light is on and the garage door is closed. The price watch is quiet at 412."
    result = _result(answer, [
        {"kind": "status", "scope": "Home", "items": [{"name": "Porch light", "state": "on"},
                                                       {"name": "Garage door", "state": "off", "note": "closed"}]},
        {"kind": "status", "scope": "Watches", "items": [{"name": "price watch", "state": "ok", "value": "412"}]}])
    assert [v["scope"] for v in result["views"]] == ["Home", "Watches"]


def test_release_stages_as_status():
    answer = "Release 2.4: tests passed, the upload is waiting on review."
    card = _result(answer, [{"kind": "status", "scope": "Release 2.4", "items": [
        {"name": "tests", "state": "ok"}, {"name": "upload", "state": "needs_you", "note": "waiting on review"}]}])["views"][0]
    assert card["items"][1]["state"] == "needs_you"


# -- draft (P8) ----------------------------------------------------------------------------------------

def test_messaging_draft_gets_a_confirmed_send_and_never_sends_itself():
    answer = "Here's a reply to Sam Rivera about moving the review to Thursday."
    card = _result(answer, [{"kind": "draft", "channel": "Email", "to": "Sam Rivera", "subject": "Thursday",
                             "body": "Hi Sam, could we move the review to Thursday?", "actions": [
                                 {"id": "send_now", "label": "Send now"}]}])["views"][0]
    assert card["channel"] == "email"
    assert card["actions"] == [{"id": "send", "label": "Send", "style": "primary", "confirm": True}]
    assert views.follow_up(card, "send") == "Send the email to Sam Rivera exactly as drafted on the card."


@pytest.mark.parametrize("missing", ["channel", "to", "body"])
def test_draft_without_required_fields_is_dropped(missing):
    raw = {"kind": "draft", "channel": "slack", "to": "#team", "body": "Deploy is done."}
    raw.pop(missing)
    assert views.clean(raw) is None


def test_draft_to_someone_not_in_the_answer_is_dropped():
    answer = "Drafted a note to the landlord."
    assert _result(answer, [{"kind": "draft", "channel": "sms", "to": "Alex", "body": "Rent is sent."}])["views"] == []


# -- files and numbers keep the older kinds; caps --------------------------------------------------------

def test_older_kinds_pass_through_untouched_and_artifacts_cap_at_two():
    answer = "Disk use is 61 GB. runner-a is fine at 41 jobs an hour; runner-b's disk is at 92%."
    stats = {"kind": "stats", "title": "Disk", "items": [{"label": "Used", "value": "61 GB"}]}
    result = _result(answer, [stats, MACHINES, MACHINES, MACHINES])
    assert [v["kind"] for v in result["views"]] == ["stats", "status", "status"]


def test_files_artifact_kind_is_not_drawn_yet_so_unknown_is_skipped():
    assert views.clean({"kind": "artifact", "items": [{"name": "report.pdf"}]}) is None


# -- server buttons ---------------------------------------------------------------------------------

def test_follow_up_words_for_each_button():
    card = _result(DESK_ANSWER, [DESKS])["views"][0]
    assert views.follow_up(card, "go") == "Go with Lift Pro."
    assert views.follow_up(card, "send") is None
    assert views.follow_up({"kind": "status"}, "go") is None


def test_numbers_match_with_commas_and_trailing_zeros():
    assert views._grounded_value("$1,260", views._numbers("about 1260 passed"))
    assert views._grounded_value("1.9 TB", views._numbers("1.90 TB free"))
    assert not views._grounded_value("12", views._numbers("121 left"))


# -- the brief -----------------------------------------------------------------------------------------

def test_brief_lists_the_artifact_kinds_and_grounding_rule():
    rule = builder.ARTIFACTS_RULE
    for word in ("explore", "compare", "status", "draft", "winner", "never send a draft yourself",
                 "must also be written in your answer text"):
        assert word in rule
    assert "spotify:" not in rule and "music://" not in rule   # ids and https only, never schemes


def test_send_on_a_draft_reaches_the_task_as_a_follow_up_and_nothing_else():
    from types import SimpleNamespace

    from speakeasy.service import VoiceService

    sent = []
    draft = _result("A reply to Sam Rivera.", [{"kind": "draft", "channel": "email", "to": "Sam Rivera",
                                                "body": "Thursday works."}])["views"][0]
    fake = SimpleNamespace(store=SimpleNamespace(run_result=lambda run: ("key_1", {"views": [draft]})),
                           task_message=lambda run, body: sent.append((run, body)))
    out = VoiceService.card_action(fake, "run_abc123", {"view": 0, "action": "send"})
    assert out == {"ok": True, "message": "Sent to the task."}
    assert sent == [("run_abc123", {"text": "Send the email to Sam Rivera exactly as drafted on the card.",
                                    "source": "typed"})]
    with pytest.raises(Exception):
        VoiceService.card_action(fake, "run_abc123", {"view": 0, "action": "delete_all"})
