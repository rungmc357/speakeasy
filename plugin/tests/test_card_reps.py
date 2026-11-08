"""Real tool output → cards, shaped to the design (SPK-231 card reps). Invented fixtures with the shapes
real Linear, GitHub, Gmail, calendar and checkout answers produced: commit-style PR titles, Linear
priority numbers, workflow labels that repeat the state, raw CI words, run ids and senders in the ref
slot, spelled-out counts, a recipient with an aside, and a cart nested inside the confirm details."""
from speakeasy import views
from speakeasy.confirms import clean as clean_confirm


def test_pr_title_loses_its_commit_prefix_and_raw_ci_words():
    v = views.clean({"kind": "pr", "repo": "acme/widgets", "number": 418, "title": "fix(sync): keep draft photos",
                     "state": "conflicts", "reason": "Main moved since you branched.",
                     "checks": [{"name": "CI", "state": "pending", "note": "action_required on 05396a4"},
                                {"name": "Lint", "state": "passed", "note": "No new warnings"}]})
    assert v["title"] == "Keep draft photos"
    assert "note" not in v["checks"][0] and v["checks"][1]["note"] == "No new warnings"


def test_plain_titles_are_untouched():
    for title in ("Redesign notification settings", "Fixes: the sync bug", "fixup the button"):
        assert views._work_title(title) == title


def test_issue_priority_in_words_and_labels_that_repeat_the_state_go():
    v = views.clean({"kind": "issue", "source": "linear", "ref": "FN-12", "title": "Offline drafts",
                     "state": "ready to ship", "priority": "3", "labels": ["Bug", "ready-to-ship", "iOS", "Medium"]})
    assert v["priority"] == "Medium" and v["state"] == "Ready to ship"
    assert v["labels"] == ["Bug", "iOS"]
    assert "priority" not in views.clean({"kind": "issue", "title": "x", "priority": 0})
    assert views.clean({"kind": "issue", "title": "x", "priority": "High priority"})["priority"] == "High"


def test_rows_drop_machine_refs_and_move_senders_to_the_note():
    v = views.clean({"kind": "rows", "entity": "email", "items": [
        {"ref": "Dana Whitfield", "name": "Pro-rata number", "note": "Reply by Monday"},
        {"ref": "t_98ab845a", "name": "Widgets pass"}, {"ref": "41394219659", "name": "Main CI"},
        {"ref": "#418", "name": "Offline drafts"}, {"ref": "Orders", "name": "Shipped"}]})
    rows = v["items"]
    assert "ref" not in rows[0] and rows[0]["note"] == "Dana Whitfield · Reply by Monday"
    assert "ref" not in rows[1] and "ref" not in rows[2]
    assert rows[3]["ref"] == "#418"
    assert rows[4]["note"] == "Orders"


def test_pr_and_issue_rows_read_as_titles():
    v = views.clean({"kind": "rows", "entity": "pr", "items": [
        {"ref": "#7", "name": "feat(api): add search", "state": "open"}]})
    assert v["items"][0]["name"] == "Add search" and v["items"][0]["state"] == "Open"


def test_ticket_cards_are_pinned_by_their_ref():
    issue = views.clean({"kind": "issue", "ref": "SPK-233", "title": "Jump to work shows while the work is on screen"})
    assert views.grounded(issue, "SPK-233 is ready to ship: the jump chip no longer shows while the work is on screen.")
    # Without the ref said, the whole title must be.
    assert not views.grounded(issue, "It's ready to ship: the jump chip no longer shows while the work is on screen.")
    # A wrong ref never passes.
    assert not views.grounded(issue, "SPK-234 is ready to ship; the work is on screen.")


def test_rows_with_a_said_ref_need_half_their_words():
    rows = views.clean({"kind": "rows", "entity": "issue", "items": [
        {"ref": "SPK-196", "name": "Home cleanup, swipe to dismiss, Clear", "state": "Shipped"}]})
    assert views.grounded(rows, "SPK-196 shipped: swipe a card away, or Clear Home.")
    assert not views.grounded(rows, "Something shipped: swipe a card away.")


def test_spelled_out_counts_ground_card_numbers():
    status = views.clean({"kind": "status", "scope": "Build 70", "items": [
        {"name": "Open work", "state": "warn", "value": "6", "unit": "open"}]})
    assert views.grounded(status, "Six pieces of open work are left.")
    assert not views.grounded(status, "Seven pieces of open work are left.")


def test_draft_recipient_loses_the_aside_and_counts_by_first_name():
    d = views.clean({"kind": "draft", "channel": "slack", "to": "Priya Raman (#ios, thread reply)", "body": "Yep, by 3."})
    assert d["to"] == "Priya Raman"
    assert views.grounded(d, "I drafted your reply to Priya.")
    assert not views.grounded(d, "I drafted your reply to Theo.")


def test_checkout_cart_nested_in_details_is_read_with_the_same_checks():
    raw = {"kind": "confirm", "type": "checkout", "title": "Buy the desk for $423.53", "details": {
        "merchant": "Desks Co", "address": "Brooklyn, NY", "delivery": "Tue", "payment_last4": "4242",
        "subtotal": "389.00", "tax": "34.53", "shipping": "0.00", "total": "423.53", "currency": "USD",
        "checkout_url": "https://desks.example/checkout", "total_source": "merchant_final",
        "items": [{"name": "Standing desk", "quantity": 1}]}}
    v = clean_confirm(raw)
    assert v and v["items"] == [{"name": "Standing desk", "quantity": 1}]
    raw["details"]["items"] = [{"name": "Standing desk", "quantity": 0}]
    assert clean_confirm(raw) is None
    raw["details"]["items"] = [{"name": "Standing desk", "quantity": 1}]
    raw["details"]["total"] = "400.00"   # totals still have to add up
    assert clean_confirm(raw) is None


# -- round 5 (real-shaped answers: CI-only asks, Sentry, Vercel, Linear boards, checkout titles) -----

def test_ci_only_pr_card_is_named_by_its_number_and_counts_the_rest():
    checks = [{"name": f"Job {n}", "state": "skipped"} for n in range(14)] + [{"name": "Lint", "state": "failed"}]
    v = views.clean({"kind": "pr", "repo": "acme/widgets", "number": 5120, "checks": checks,
                     "reason": "Fourteen checks skipped and lint failed."})
    assert v["title"] == "Checks on #5120" and v["state"] == "failing"
    assert v["checks"][0]["name"] == "Lint" and len(v["checks"]) == 6
    assert v["checks_total"] == 15 and v["checks_rest"] == "skipped"
    quiet = views.clean({"kind": "pr", "number": 7, "reason": "All skipped.",
                         "checks": [{"name": "A", "state": "skipped"}, {"name": "B", "state": "skipped"}]})
    assert "state" not in quiet
    assert views.shape([quiet], "PR 7: checks A and B were all skipped.")   # no state, no crash
    assert views.clean({"kind": "pr", "number": 7, "reason": "x"}) is None   # no title, no checks: no card


def test_one_reason_on_every_check_is_the_cards_reason():
    v = views.clean({"kind": "pr", "title": "Offline drafts", "state": "open", "reason": "Waiting on approval.",
                     "checks": [{"name": n, "state": "pending", "note": "Needs a maintainer to approve"} for n in "AB"]})
    assert all("note" not in c for c in v["checks"])


def test_stats_drop_tracker_keys_and_capitalize_word_values():
    v = views.clean({"kind": "stats", "title": "Shared notes crash (FIELDNOTE-IOS-3KD)",
                     "items": [{"label": "Level", "value": "fatal"}, {"label": "Crashes", "value": "312"}]})
    assert v["title"] == "Shared notes crash"
    assert [i["value"] for i in v["items"]] == ["Fatal", "312"]


def test_status_notes_are_one_short_clause_and_command_lines_go():
    v = views.clean({"kind": "status", "scope": "Deploys", "items": [
        {"name": "api", "state": "bad", "value": "Build failed", "note": "npm run build exited 1"},
        {"name": "web", "state": "ok", "note": "Very busy; 18.0 GB free, 206 GB disk free, load 12"}]})
    assert "note" not in v["items"][0]
    assert v["items"][1]["note"] == "Very busy"


def test_rows_that_all_share_one_state_drop_it():
    v = views.clean({"kind": "rows", "entity": "pr", "items": [
        {"ref": f"#{n}", "name": f"Change {n}", "state": "Open"} for n in (1, 2, 3)]})
    assert all("state" not in i for i in v["items"])
    mixed = views.clean({"kind": "rows", "entity": "pr", "items": [
        {"ref": "#1", "name": "A", "state": "Open"}, {"ref": "#2", "name": "B", "state": "Draft"}, {"ref": "#3", "name": "C", "state": "Open"}]})
    assert all("state" in i for i in mixed["items"])


def test_names_keep_their_own_casing():
    assert views._cap_first("iPhone Duo side panel") == "iPhone Duo side panel"
    assert views._cap_first("video attachments") == "Video attachments"


def test_preview_key_points_that_restate_the_summary_go():
    v = views.clean({"kind": "preview", "entity": "document", "title": "Offline drafts spec",
                     "summary": "Drafts keep their photos when the phone is offline.",
                     "list": ["Goal: drafts keep their photos offline", "Sync resumes on reconnect"]})
    assert "list" not in v and v["summary"].endswith("Sync resumes on reconnect.")   # a lone point joins the summary


def test_confirm_title_drops_the_total_it_repeats():
    v = clean_confirm({"type": "checkout", "title": "Buy Oakline desk \u2014 $423.53",
                       "details": [{"label": "Total", "value": "$423.53"}]})
    assert v is None or v["title"] == "Buy Oakline desk"


def test_a_pr_reason_that_repeats_the_reply_goes_and_the_card_can_open_the_pr():
    pr = views.clean({"kind": "pr", "repo": "acme/widgets", "number": 77, "title": "Offline drafts", "state": "open",
                      "reason": "All 15 checks were skipped, so nothing ran."})
    assert pr["url"] == "https://github.com/acme/widgets/pull/77"
    said = views.shape([pr], "CI didn't test PR 77 Offline drafts. All 15 checks were skipped, so nothing passed.")
    assert said and "reason" not in said[0]
    kept = views.shape([pr], "PR 77 Offline drafts is waiting on a review from the design team.")
    assert kept[0]["reason"] == "All 15 checks were skipped, so nothing ran."


def test_long_tracker_titles_lose_scope_prefix_and_second_clause_and_release_buckets_go():
    v = views.clean({"kind": "issue", "source": "linear", "ref": "FN-9", "state": "In review",
                     "title": "Build 12 iPad: Jump chip shows while the card is on screen; new replies stop auto-scrolling",
                     "labels": ["Bug", "build-12", "Release 3", "iOS"]})
    assert v["title"] == "Jump chip shows while the card is on screen"
    assert v["labels"] == ["Bug", "iOS"]
    assert views._work_title("Home mockups: three design directions") == "Home mockups: three design directions"


def test_long_refs_and_phrase_times_and_long_roles_are_shaped():
    v = views.clean({"kind": "status", "scope": "Deploys", "source": "Vercel", "items": [
        {"name": "api", "state": "bad", "role": "Production \u00b7 pg 8.13 bump from Renovate"}]})
    assert v["source"] == "Vercel" and v["items"][0]["role"] == "Production"


def test_calendar_move_label_cuts_at_a_word_and_free_time_names_real_gaps():
    day = {"kind": "calendar_day", "date": "2026-11-09", "events": [
        {"start": "2026-11-09T12:30:00-05:00", "end": "2026-11-09T13:30:00-05:00", "title": "Lunch with Sam"},
        {"start": "2026-11-09T13:00:00-05:00", "end": "2026-11-09T13:30:00-05:00", "title": "Investor update call"}],
        "free": ["Before 9:30 AM", "9:45\u201311:00 AM", "1:30\u20136:00 PM", "After 7:00 PM"]}
    out = views.shape([day], "")[0]
    assert out["actions"][0]["label"] == "Move Investor update"
    assert out["free"] == ["9:45\u201311 AM", "1:30\u20136 PM"]


def test_a_row_label_with_a_qualifier_counts_as_said_by_its_head():
    said, nums = views._norm("The wallboard updater and the backup job missed their runs."), set()
    assert views._grounded_label("Wallboard updater \u2014 daytime", said, nums)
    assert not views._grounded_label("Open lane", said, nums)


def test_metric_label_drops_the_tracker_key():
    v = views.clean({"kind": "metric", "label": "Sync failures (ACME-WEB-9QZ)", "value": "12", "unit": "per hour"})
    assert v["label"] == "Sync failures"


def test_weather_rows_get_an_icon_from_their_words():
    v = views.clean({"kind": "weather", "place": "Testville", "temp": 50, "condition": "Clear", "days": [
        {"label": "Sun", "high": 59, "low": 46, "condition": "Light drizzle"},
        {"label": "Mon", "high": 56, "low": 44, "condition": "Mostly cloudy"},
        {"label": "Tue", "high": 61, "low": 47, "condition": "Partly cloudy"},
        {"label": "Thu", "high": 62, "low": 55, "condition": "Rain showers", "icon": "storm"}]})
    assert v["icon"] == "sun"
    assert [d["icon"] for d in v["days"]] == ["drizzle", "cloud", "cloud_sun", "storm"]


def test_pr_checks_take_the_count_the_reason_says():
    checks = [{"name": f"Job {n}", "state": "skipped"} for n in range(6)]
    v = views.clean({"kind": "pr", "repo": "acme/widgets", "number": 4120, "title": "Checks on #4120",
                     "reason": "All 15 checks were skipped, so CI never tested it.", "checks": checks})
    assert v["checks_total"] == 15 and v["checks_rest"] == "skipped"
    v = views.clean({"kind": "pr", "repo": "acme/widgets", "number": 4121, "title": "Fix login", "state": "failing",
                     "reason": "2 of 9 checks failed on the login test.", "checks": [
                         {"name": "Unit", "state": "failed"}, {"name": "Lint", "state": "passed"}]})
    assert v["checks_total"] == 9 and "checks_rest" not in v


def test_jobs_elapsed_loses_about_and_a_lone_key_point_joins_the_summary():
    v = views.clean({"kind": "jobs", "items": [{"name": "Index rebuild", "status": "Copying shards", "elapsed": "about 19 min"}]})
    assert v["items"][0]["elapsed"] == "19 min"
    v = views.clean({"kind": "preview", "entity": "document", "title": "Sync spec", "summary": "Goal: edits made offline merge cleanly.",
                     "list_label": "Key points", "list": ["Out of scope: shared folders"]})
    assert "list" not in v and "list_label" not in v
    assert v["summary"] == "Goal: edits made offline merge cleanly. Out of scope: shared folders."


def test_rows_fill_tracker_links_from_one_linked_row():
    v = views.clean({"kind": "rows", "entity": "issue", "items": [
        {"ref": "ACME-12", "name": "Fix sync", "state": "Done", "url": "https://tracker.example/acme/issue/ACME-12/fix-sync"},
        {"ref": "ACME-14", "name": "Offline drafts", "state": "Open"},
        {"ref": "#99", "name": "Not a tracker key", "state": "Open"}]})
    assert v["items"][1]["url"] == "https://tracker.example/acme/issue/ACME-14"
    assert "url" not in v["items"][2]
