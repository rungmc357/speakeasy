"""Visual cards ("views"): catalog validation, quick-answer cards, task blocks. Made-up data."""
from __future__ import annotations

from speakeasy import instant, markets, quick, views
from speakeasy.text import split_result


def test_unknown_kind_and_bad_fields_are_dropped():
    assert views.clean({"kind": "hologram", "x": 1}) is None
    v = views.clean({"kind": "quote", "symbol": "ACME", "price": "12.5", "logo_url": "http://x.example/a.png",
                     "points": [1, "two", 3, float("nan")], "secret": "nope"})
    assert v == {"kind": "quote", "symbol": "ACME", "price": 12.5, "points": [1.0, 3.0]}


def test_weather_rows_with_only_a_label_are_left_out():
    v = views.clean({"kind": "weather", "place": "Springfield", "temp": 51, "high": 57, "low": 36,
                     "days": [{"label": "6am"}, {"label": "9am", "icon": "cloud"}]})
    assert "days" not in v and v["high"] == 57
    v = views.clean({"kind": "weather", "place": "Springfield", "temp": 51,
                     "hours": [{"label": "6am", "temp": 40}, {"label": "9am"}]})
    assert v["hours"] == [{"label": "6am", "temp": 40.0}]


def test_lists_are_capped_and_rows_validated():
    v = views.clean({"kind": "list", "title": "Errands", "items": [{"text": f"item {i}", "done": "yes"} for i in range(40)]})
    assert len(v["items"]) == 20 and "done" not in v["items"][0]


def test_task_block_is_stripped_and_becomes_views():
    out = ("Here's the spot.\n```speakeasy-views\n[{\"kind\": \"place\", \"name\": \"Corner Cafe\", \"rating\": 4.6,"
           " \"lat\": 1.5, \"lon\": 2.5}]\n```\nSPOKEN: Corner Cafe is open.")
    result = split_result(out, ())
    assert result["views"] == [{"kind": "place", "name": "Corner Cafe", "rating": 4.6, "lat": 1.5, "lon": 2.5}]
    assert "speakeasy-views" not in result["full"]


def test_clock_and_math_views():
    clock = instant.view("what time is it in London")
    assert clock["kind"] == "clock" and clock["zones"][-1]["here"] is True and len(clock["zones"]) == 2
    assert instant.view("what's 15 percent of 80") == {"kind": "math", "expression": "15% of 80", "result": "12"}


def test_market_subjects():
    assert markets.subject("where is acme at") is None
    assert markets.subject("how is eth doing today") == ("coin", "ETH")
    assert markets.subject("what did nvidia close at") == ("stock", "NVDA")
    assert markets.subject("how tall is the tower") is None


def test_stock_card_from_feed():
    def fake(url):
        assert "assetclass=stocks" in url
        return {"data": {"symbol": "ACME", "company": "Acme Rockets, Inc. Common Stock", "lastSalePrice": "$10.00",
                         "netChange": "-0.50", "percentageChange": "-4.76%", "previousClose": "$10.50",
                         "chart": [{"y": 10.5}, {"y": 10.2}, {"y": 10.0}]}}
    c = markets.stock_card("ACME", fake)
    assert c["name"] == "Acme Rockets" and c["price"] == 10.0 and c["change_pct"] == -4.76 and c["points"] == [10.5, 10.2, 10.0]


def test_views_in_results():
    rs = [{"title": "a", "text": "b", "view": {"kind": "fact", "title": "Height", "value": "828 m"}}, {"title": "c", "text": "d"}]
    assert quick.views_in(rs) == [{"kind": "fact", "title": "Height", "value": "828 m"}]


def test_task_prompt_asks_informational_answers_for_a_card_and_headline():
    from speakeasy.prompt import builder as P
    rule = P.VIEWS_RULE
    assert "Answers count too" in rule and "at least one fitting card" in rule
    assert "never invent" in rule and "skip the block when nothing is worth showing" in rule
    assert "DONE label the answer itself" in rule
    assert len(rule) < 4200, "keep the views rule short"
    prompt = P.build_task_prompt(P.Names("Hermes", "Sam"), 1, "User: what's the weather this weekend")
    assert "Answers count too" in prompt
    # The headline it asks for still passes the DONE label check.
    from speakeasy.text import valid_done_label
    assert valid_done_label("Saturday is the drier day") == "Saturday is the drier day"


def test_a_finished_task_never_keeps_a_progress_bar():
    """A count put up as a progress bar ("4 builds of 9 builds · 44%") on a done task is a fact card."""
    out = ("Four of the nine nightly builds kept their logs.\n```speakeasy-views\n"
           "[{\"kind\": \"progress\", \"label\": \"Nightly builds with logs\", \"value\": 4, \"total\": 9, \"unit\": \"builds\"}]"
           "\n```\nSPOKEN: Four of nine builds kept logs.")
    result = split_result(out, ())
    assert result["views"] == [{"kind": "fact", "title": "Nightly builds with logs", "value": "4 of 9", "unit": "builds"}]
    assert "%" not in str(result["views"])


def test_progress_is_only_kept_while_running():
    raw = {"kind": "progress", "label": "Pages checked", "value": 3, "total": 12, "unit": "pages", "detail": "Still going"}
    assert views.clean(raw, running=True)["kind"] == "progress"
    assert views.clean(raw) == {"kind": "fact", "title": "Pages checked", "value": "3 of 12", "unit": "pages",
                                "subtitle": "Still going"}
    assert views.clean({"kind": "progress", "value": 2.5})["value"] == "2.5"
    assert views.clean({"kind": "progress", "label": "Pages", "value": 7, "total": 0})["value"] == "7"
    assert views.clean({"kind": "progress", "unit": "pages"}) is None   # nothing to say


def test_stored_results_lose_old_progress_bars_and_keep_card_positions():
    from speakeasy.text import public_result
    stored = {"spoken": "Done.", "views": [
        {"kind": "progress", "label": "Photos tagged", "value": 30, "total": 30},
        {"kind": "progress", "label": "Without numbers"},
        {"kind": "home", "title": "Office", "devices": [], "actions": [{"id": "toggle", "label": "Turn off"}]}]}
    shown = public_result(stored)["views"]
    assert [v["kind"] for v in shown] == ["fact", "fact", "home"]   # buttons are addressed by position
    assert shown[0]["value"] == "30 of 30" and shown[1] == {"kind": "fact", "title": "Without numbers"}
    assert stored["views"][0]["kind"] == "progress", "the stored row itself is untouched"


def test_task_prompt_never_offers_a_progress_card_for_counts():
    from speakeasy.prompt import builder as P
    assert "progress {" not in P.VIEWS_RULE
    assert "never draw a progress bar" in P.VIEWS_RULE and "unit once" in P.VIEWS_RULE

def test_chart_keeps_metric_order_and_freshness_fields():
    raw = {"kind": "chart", "title": "Five large stocks", "style": "bar", "unit": "$",
           "labels": ["AAA", "BBB"], "series": [{"name": "Share price", "values": [120.5, 98]}],
           "metric": "Share price", "sort": "High-First", "order_by": "index weight",
           "source": "Example Exchange", "as_of": "2026-11-06T15:42:00-05:00", "delayed": True}
    view = views.clean(raw)
    assert view["metric"] == "Share price" and view["sort"] == "high_first" and view["order_by"] == "index weight"
    assert view["source"] == "Example Exchange" and view["as_of"].startswith("2026-11-06") and view["delayed"] is True


def test_chart_drops_an_unknown_sort_and_never_adds_freshness():
    view = views.clean({"kind": "chart", "title": "Runs", "labels": ["Mon"], "series": [{"values": [3]}], "sort": "by vibes"})
    assert "sort" not in view
    assert not {"source", "as_of", "delayed"} & set(view)


def test_views_rule_asks_charts_for_metric_order_and_real_freshness_only():
    from speakeasy.prompt import builder as P
    rule = P.VIEWS_RULE
    assert "metric" in rule and "high_first|low_first|as_listed" in rule and "order_by" in rule
    assert "source/as_of only from the data, never guessed" in rule


def test_rows_keep_the_lists_real_total_only_when_more_than_sent():
    rows = [{"ref": f"SPK-{n}", "name": f"Invented issue {n}", "state": "Backlog"} for n in range(12)]
    v = views.clean({"kind": "rows", "source": "Linear", "entity": "issue", "total": 13, "items": rows})
    assert v["total"] == 13   # "1 more not shown" rather than a count that disagrees with the reply
    for bad in (12, 3, True, 10 ** 6):
        assert "total" not in views.clean({"kind": "rows", "entity": "issue", "total": bad, "items": rows})


def test_an_agent_rows_corner_is_its_time_and_the_check_in_is_said_once():
    v = views.clean({"kind": "jobs", "items": [{"name": "Home integration", "elapsed": "about 19 min at last check-in",
                                                 "status": "Merging the composer; last check-in about 3:00 AM"}]})
    assert v["items"][0]["elapsed"] == "19 min" and v["items"][0]["status"] == "Merging the composer"
