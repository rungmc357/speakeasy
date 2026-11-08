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
