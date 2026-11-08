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


def test_a_long_winner_name_cuts_the_go_button_at_a_word():
    long = {**DESKS, "options": [{**o, "name": "Lift Pro Carbon"} if i == 1 else o for i, o in enumerate(DESKS["options"])]}
    card = _result(DESK_ANSWER.replace("Lift Pro at", "Lift Pro Carbon at"), [long])["views"][0]
    assert card["actions"][0]["label"] == "Go with Lift Pro Carbon"
    longer = {**DESKS, "options": [{**o, "name": "Lift Pro Carbonfiber"} if i == 1 else o for i, o in enumerate(DESKS["options"])]}
    card = _result(DESK_ANSWER.replace("Lift Pro at", "Lift Pro Carbonfiber at"), [longer])["views"][0]
    assert card["actions"][0]["label"] == "Go with Lift Pro"          # never "Go with Lift Pro Carbonf"


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


SERVERS = {"kind": "status", "source": "Servers", "scope": "fieldnote-prod", "items": [
    {"name": "api-2", "state": "bad", "note": "Not answering since 3:12 PM"},
    {"name": "worker", "state": "warn", "value": "1,240", "unit": "jobs waiting"}],
    "actions": [{"label": "Restart api-2"}, {"label": "Drain the queue"}, {"label": "Third one"}],
    "links": [{"label": "api-2 logs", "url": "https://logs.example.com/api-2"},
              {"label": "Bad", "url": "javascript:alert(1)"}, {"label": "Dup", "url": "https://logs.example.com/api-2"}]}
SERVERS_SAID = "api-2 is down, not answering since 3:12 PM, and the worker has 1,240 jobs waiting."


def test_a_work_card_keeps_two_offered_next_steps_and_harmful_ones_confirm():
    [view] = views.shape([views.clean(SERVERS)], SERVERS_SAID, "is everything up?")
    assert view["actions"] == [
        {"id": "do_0", "label": "Restart api-2", "style": "destructive", "confirm": True},
        {"id": "do_1", "label": "Drain the queue", "style": "primary", "confirm": True}]
    assert view["links"] == [{"label": "api-2 logs", "url": "https://logs.example.com/api-2"}]
    assert views.follow_up(view, "do_0") == "Restart api-2 (Servers · fieldnote-prod)."
    assert views.follow_up(view, "do_9") is None


def test_an_offered_step_replaces_the_generic_fix_and_sends_its_own_words():
    from types import SimpleNamespace

    from speakeasy.service import VoiceService

    pr = {"kind": "pr", "repo": "fieldnote/app", "number": 418, "title": "Share sheet fix", "state": "failing",
          "reason": "UI tests failed on the share sheet.",
          "checks": [{"name": "UI tests", "state": "failed"}], "url": "https://github.com/fieldnote/app/pull/418",
          "actions": [{"label": "Rerun failed", "primary": True}, {"id": "fix", "label": "Smuggled"}]}
    [view] = views.shape([views.clean(pr)], "PR #418 Share sheet fix is failing UI tests.", "")
    assert [a["label"] for a in view["actions"]] == ["Rerun failed"]
    assert view["actions"][0]["style"] == "primary" and not view["actions"][0]["confirm"]
    sent = []
    fake = SimpleNamespace(store=SimpleNamespace(run_result=lambda run: ("key_1", {"views": [view]})),
                           task_message=lambda run, body: sent.append((run, body)))
    assert VoiceService.card_action(fake, "run_abc123", {"view": 0, "action": "do_0"})["ok"] is True
    assert sent == [("run_abc123", {"text": "Rerun failed (PR #418).", "source": "typed"})]
    with pytest.raises(Exception):
        VoiceService.card_action(fake, "run_abc123", {"view": 0, "action": "fix"})
    # Without an offered step the generic Fix stays.
    [plain] = views.shape([views.clean({**pr, "actions": []})], "PR #418 Share sheet fix is failing UI tests.", "")
    assert [a["label"] for a in plain["actions"]] == ["Fix the checks"]


def test_a_draft_ignores_offered_buttons():
    draft = {"kind": "draft", "channel": "email", "to": "Sam Rivera", "body": "Thursday works.",
             "actions": [{"label": "Send now", "primary": True}]}
    [view] = views.shape([views.clean(draft)], "A reply to Sam Rivera.", "")
    assert [a["id"] for a in view["actions"]] == ["send"]


# -- timeline (P2), rows (P4), metric (P6), preview (P7) ---------------------------------------------------

TRIP_DAYS = {"kind": "timeline", "title": "Porto trip", "days": [
    {"label": "Fri 12", "sleep": "Casa do Rio", "items": [
        {"when": "9:40 AM", "what": "Flight TP 1932", "where": "Porto airport", "state": "DONE"},
        {"when": "8:00 PM", "what": "Dinner", "where": "Taberna Azul", "lat": 41.1, "state": "later"}]},
    {"label": "Sat 13", "items": [{"when": "11:00 AM", "what": "Port tasting", "lat": 41.13, "lon": -8.61},
                                  {"what": "no time"}]},
    {"label": "Sun 14", "items": []}]}
TRIP_DAYS_ANSWER = ("Friday 12: you land at Porto airport at 9:40 AM and stay at Casa do Rio; dinner is at "
                    "Taberna Azul at 8:00 PM. Saturday 13: port tasting at 11:00 AM.")


def test_planning_timeline_keeps_dated_rows_and_checks_states_and_coordinates():
    card = _result(TRIP_DAYS_ANSWER, [TRIP_DAYS])["views"][0]
    assert [d["label"] for d in card["days"]] == ["Fri 12", "Sat 13"]
    fri, sat = card["days"]
    assert fri["items"][0]["state"] == "done" and "state" not in fri["items"][1]
    assert "lat" not in fri["items"][1]                          # half a coordinate pair is no pair
    assert sat["items"] == [{"when": "11:00 AM", "what": "Port tasting", "lat": 41.13, "lon": -8.61}]


def test_release_stages_timeline_drops_when_a_time_is_not_in_the_answer():
    stages = {"kind": "timeline", "title": "Release 4.2", "days": [{"label": "Today", "items": [
        {"when": "10:00", "what": "Tests passed", "state": "done"}, {"when": "14:30", "what": "Store review", "state": "now"}]}]}
    assert _result("Tests passed at 10:00; store review started at 14:30.", [stages])["views"][0]["days"][0]["items"][1]["state"] == "now"
    assert _result("Tests passed at 10:00; store review is under way.", [stages])["views"] == []
    # "Nov 8, 2:50 AM" said as "2:50 AM today": the clock time pins the step.
    dated = {"kind": "timeline", "title": "Release 4.2", "days": [{"label": "Done", "items": [
        {"when": "Nov 8, 2:50 AM", "what": "Store review", "state": "done"}]}]}
    assert _result("Store review finished at 2:50 AM today.", [dated])["views"]


ISSUES = {"kind": "rows", "title": "Needs review", "entity": "PR", "items": [
    {"ref": "#41", "name": "Fix the login timeout", "state": "CI passing", "tone": "good",
     "url": "https://github.com/acme/app/pull/41"},
    {"ref": "#42", "name": "Add dark mode", "state": "CI failing", "tone": "loud"},
    {"note": "no name"}]}
ISSUES_ANSWER = "Two pull requests wait on you: #41 Fix the login timeout is green, #42 Add dark mode fails CI."


def test_coding_rows_keep_named_items_and_allowed_tones():
    card = _result(ISSUES_ANSWER, [ISSUES])["views"][0]
    # D1: attention first: the failing one (no allowed tone, so neutral) leads the green one.
    assert card["entity"] == "pr" and [i["name"] for i in card["items"]] == ["Add dark mode", "Fix the login timeout"]
    assert card["items"][1]["tone"] == "good" and "tone" not in card["items"][0]
    assert "actions" not in card                                 # rows open their links; nothing sends from here


def test_messaging_inbox_rows_and_bills_need_their_amounts_said():
    inbox = {"kind": "rows", "entity": "email", "items": [{"name": "Lease renewal", "note": "From the landlord"}]}
    assert _result("One email needs you: Lease renewal.", [inbox])["views"][0]["entity"] == "email"
    bills = {"kind": "rows", "entity": "bill", "items": [{"name": "Water", "amount": "$48.20", "when": "Due Nov 20"}]}
    assert _result("Water is $48.20, due Nov 20.", [bills])["views"]
    assert _result("Water is due soon.", [bills])["views"] == []


def test_unknown_row_entity_is_dropped_but_the_rows_stay():
    card = views.clean({"kind": "rows", "entity": "spaceship", "items": [{"name": "Water the plants"}]})
    assert card == {"kind": "rows", "items": [{"name": "Water the plants"}]}


SPEND = {"kind": "metric", "label": "Cloud spend this month", "value": "$1,260", "delta": "+18%", "good": False,
         "threshold": 1500, "threshold_label": "Budget", "points": [
             {"label": "Nov 1", "value": 40}, {"label": "Nov 2", "value": 95.5},
             {"label": "Nov 3", "value": 310, "mark": "Load test"}]}


def test_numbers_metric_grounds_the_hero_and_marked_points_only():
    # D2: cloud spend isn't a server or error tracker: its number and plain state, no comparison.
    answer = "Cloud spend is $1,260 this month, up 18%, under the $1,500 budget. The spike was a 310 load test."
    card = _result(answer, [SPEND])["views"][0]
    assert card["value"] == "$1,260" and not {"points", "delta", "good", "threshold", "threshold_label"} & set(card)
    assert _result(answer.replace("$1,260", "$1,200"), [SPEND])["views"] == []   # the number itself must be said
    errors = {**SPEND, "label": "Errors per hour", "source": "Sentry", "value": "1,260"}
    card = _result(answer, [errors])["views"][0]
    assert len(card["points"]) == 3 and card["delta"] == "+18%" and card["good"] is False
    assert _result(answer.replace("310", "300"), [errors])["views"] == []       # a marked point not said
    unsaid = _result(answer.replace("18%", "a lot"), [errors])["views"][0]      # the change not said: left off
    assert "delta" not in unsaid and "good" not in unsaid and unsaid["value"] == "1,260"


def test_monitoring_metric_with_a_broken_series_keeps_the_number_only():
    card = views.clean({"kind": "metric", "label": "Uptime", "value": "99.95%",
                        "points": [{"label": "Mon", "value": 99.9}, {"value": 100}]})
    assert "points" not in card and card["value"] == "99.95%"
    assert views.clean({"kind": "metric", "label": "Uptime"}) is None


def test_media_preview_with_ids_and_a_track_list():
    album = {"kind": "preview", "entity": "Media", "media_kind": "album", "title": "Night Garden",
             "creator": "The Paper Kites", "year": "2021", "ids": {"spotify": "4aawyAB9vmqN3uQ7FjRGTy"},
             "list_label": "Tracks", "list": ["Opening", "Midnight Walk"], "url": "spotify:album:x"}
    card = _result("Today's album is Night Garden by The Paper Kites.", [album])["views"][0]
    assert card["entity"] == "media" and card["ids"] == {"spotify": "4aawyAB9vmqN3uQ7FjRGTy"} and "url" not in card
    assert _result("Today's album is Night Garden.", [album])["views"] == []    # the artist must be said too


def test_files_contract_preview_keeps_flags_and_drops_ids():
    contract = {"kind": "preview", "entity": "contract", "title": "Office lease", "summary": "Three years, renews itself.",
                "flags": ["Auto-renews unless cancelled 90 days ahead"], "ids": {"apple_music": "12345"},
                "media_kind": "album", "url": "https://files.example/lease.pdf"}
    card = _result("The Office lease runs three years and auto-renews.", [contract])["views"][0]
    assert card["flags"] and "ids" not in card and "media_kind" not in card
    assert views.clean({"kind": "preview", "entity": "spaceship", "title": "X"}) is None


def test_research_explore_by_day_needs_the_day_said():
    card = {"kind": "explore", "sections": [{"label": "Things to do", "entity": "event", "items": [
        {"name": "Harbor Lights", "day": "Sat 13"}]}]}
    assert _result("On Saturday 13, Harbor Lights.", [card])["views"]
    assert _result("Harbor Lights is worth a look.", [card])["views"] == []


def test_every_artifact_kind_is_in_the_brief():
    for kind in views.ARTIFACTS:
        assert kind + " (" in builder.ARTIFACTS_RULE


# -- status and rows names are labels (SPK-236) ------------------------------------------------------

def test_status_labels_written_in_other_words_keep_the_card():
    """A tile's name is the agent's short label for a line of its answer, not a quote of it."""
    answer = ("The release is ready: the Mac tests and the phone tests passed, and the privacy checks are clean. "
              "The upload is waiting on store review.")
    card = _result(answer, [{"kind": "status", "scope": "Release 2.4", "items": [
        {"name": "Mac tests", "state": "ok"}, {"name": "Phone tests passed", "state": "ok"},
        {"name": "Privacy checks clean", "state": "ok"}, {"name": "Store review", "state": "needs_you"}]}])["views"]
    assert [i["name"] for i in card[0]["items"]] == ["Mac tests", "Phone tests passed", "Privacy checks clean",
                                                    "Store review"]


def test_rows_labels_in_other_words_keep_the_card_but_refs_stay_exact():
    answer = "Two pull requests wait on you: #41 fixes the login timeout and #42 adds dark mode."
    rows = {"kind": "rows", "entity": "pr", "items": [{"ref": "#41", "name": "Login timeout fix"},
                                                      {"ref": "#42", "name": "Dark mode"}]}
    assert [i["name"] for i in _result(answer, [rows])["views"][0]["items"]] == ["Login timeout fix", "Dark mode"]
    assert _result(answer.replace("#42", "#43"), [rows])["views"] == []


def test_invented_labels_and_unsaid_numbers_still_drop_the_card():
    answer = "runner-a is fine at 41 jobs an hour."
    for name in ("runner-z", "Disk cleanup", "runner-a build 7"):
        card = {"kind": "status", "scope": "Runners", "items": [{"name": "runner-a", "state": "ok", "value": "41"},
                                            {"name": name, "state": "warn"}]}
        assert _result(answer, [card])["views"] == [], name
    ok = {"kind": "status", "scope": "Runners", "items": [{"name": "runner-a", "state": "ok", "value": "41"},
                                                         {"name": "runner-a jobs", "state": "ok"}]}
    assert _result(answer, [ok])["views"]


def test_a_number_said_only_in_the_spoken_line_grounds_the_card():
    out = ("Plugin rollout is done and pushed to main.\n```speakeasy-views\n" + json.dumps([{"kind": "status", "scope": "Rollout", "items": [
        {"name": "Pushed to main", "state": "ok"},
        {"name": "Installed on this Mac", "state": "ok", "value": "0.3.35"}]}]) + "\n```\n"
        "SPOKEN: Version 0.3.35 is installed on this Mac.")
    assert [v["kind"] for v in (split_result(out, ()) or {}).get("views", [])] == ["status"]


def test_pr_checks_count_from_the_answer_and_a_question_owns_its_choice():
    pr = {"kind": "pr", "repo": "fieldnote/app", "number": 418, "title": "Checks on #418", "reason": "Every job was skipped.",
          "checks": [{"name": f"Job {n}", "state": "skipped"} for n in range(6)]}
    [card] = views.shape([views.clean(pr)], "CI didn't test #418: all fifteen checks were skipped.", "")
    assert card["checks_total"] == 15 and card["checks_rest"] == "skipped"
    places = views.clean({"kind": "places", "title": "Dinner", "items": [{"name": "Rosa"}, {"name": "Luce"}],
                          "actions": [{"label": "Check Rosa tables"}, {"label": "Check Luce tables"}]})
    question = {"kind": "question", "question": "Which one?", "options": ["Rosa", "Luce"]}
    [_, kept] = views.drop_steps_the_question_asks([question, places], question)
    assert "actions" not in kept and [i["name"] for i in kept["items"]] == ["Rosa", "Luce"]
