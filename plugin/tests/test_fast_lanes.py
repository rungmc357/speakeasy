"""Quick answers and optional Jev routing (made-up wording throughout)."""
from __future__ import annotations

import io
import json
from pathlib import Path

from speakeasy import jev, quick


def test_quick_eligible_only_for_public_questions():
    assert quick.eligible("How tall is the Space Needle")
    assert quick.eligible("Who won the hockey game last night")
    assert not quick.eligible("What's on my calendar Friday")
    assert not quick.eligible("Book a table for two at seven")
    assert not quick.eligible("Turn the porch light off")
    assert not quick.eligible("ok")


def test_quick_answer_or_none():
    results = [{"title": "Space Needle", "text": "605 feet tall"}, {"title": "Seattle", "text": "built 1962"}]
    assert quick.answer("How tall is the Space Needle", "today", lambda q: results,
                        lambda m: "The Space Needle is 605 feet tall.") == "The Space Needle is 605 feet tall."
    assert quick.answer("How tall is the Space Needle", "today", lambda q: results, lambda m: "UNSURE") is None
    assert quick.answer("How tall is the Space Needle", "today", lambda q: results[:1], lambda m: "x") is None
    assert quick.answer("How tall is the Space Needle", "today", lambda q: results,
                        lambda m: "**605 ft** see https://example.com") is None


def test_quick_question_regex():
    assert quick.QUESTION.search("so who owns the yankees")
    assert not quick.QUESTION.search("tell me a joke about otters")


def _fake(answers):
    return lambda *a, **k: answers


def test_jev_route_confident_and_unsure(tmp_path: Path):
    ok = {"route": {"choice": "quick", "confidence": 0.97}}
    assert jev.route(tmp_path, "venice", "How far is the moon", evaluator=_fake(ok))[0] == "quick"
    low = {"route": {"choice": "quick", "confidence": 0.5}}
    assert jev.route(tmp_path, "venice", "How far is the moon", evaluator=_fake(low))[0] is None
    assert jev.route(tmp_path, "venice", "x", evaluator=_fake(None))[0] is None


def test_jev_never_quick_when_about_a_running_task(tmp_path: Path):
    answers = {"route": {"choice": "quick", "confidence": 0.95}, "about_task": {"noul": 0.8}}
    lane, _, _ = jev.route(tmp_path, "venice", "is it open on sunday", ["find a brunch spot"], evaluator=_fake(answers))
    assert lane == "agent"


def test_jev_placement(tmp_path: Path):
    answers = {"several": {"noul": 0.02}, "show": {"noul": 0.1},
               "channel": {"choice": "Research", "confidence": 0.95},
               "conversation": {"choice": "none", "confidence": 0.99}}
    out = jev.placement(tmp_path, "venice", "look into heat pumps", [("Research", "deep dives")],
                        [("c1", "a chat about a bike")], evaluator=_fake(answers))
    assert out == {"show": False, "channel": "Research", "conversation": None}
    several = dict(answers, several={"noul": 0.9})
    assert jev.placement(tmp_path, "venice", "x", [], [], evaluator=_fake(several)) is None
    unsure = dict(answers, channel={"choice": "Research", "confidence": 0.4})
    assert jev.placement(tmp_path, "venice", "x", [("Research", "")], [], evaluator=_fake(unsure)) is None


def test_jev_evaluate_reads_key_and_parses(tmp_path: Path):
    (tmp_path / ".env").write_text("VENICE_API_KEY=test-not-real\n")
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def opener(req, timeout):
        seen["url"], seen["auth"] = req.full_url, req.headers.get("Authorization")
        return Resp(json.dumps({"answers": {"route": {"choice": "home", "confidence": 1}}}).encode())

    out = jev.evaluate(tmp_path, "venice", "s", jev.ROUTE_QUESTION, opener=opener)
    assert out["route"]["choice"] == "home"
    assert seen["url"].endswith("/decisions") and seen["auth"] == "Bearer test-not-real"
    assert jev.evaluate(tmp_path, "openrouter", "s", {}, opener=opener) is None  # no key: off
    assert [p["id"] for p in jev.providers_status(tmp_path)] == ["venice", "openrouter", "typesafe"]


def _espn(url):
    if url.endswith("/teams?limit=500"):
        return {"sports": [{"leagues": [{"teams": [
            {"team": {"id": "7", "displayName": "Rivertown Otters", "shortDisplayName": "Otters", "name": "Otters",
                      "location": "Rivertown", "abbreviation": "RIV"}},
            {"team": {"id": "8", "displayName": "Hill City Owls", "shortDisplayName": "Owls", "name": "Owls",
                      "location": "Hill City", "abbreviation": "HCO"}}]}]}]}
    if "/schedule" in url:
        if "seasontype=3" in url:
            return {"events": []}
        return {"events": [{"id": "1", "date": "2020-05-01T23:00Z", "competitions": [{
            "status": {"type": {"completed": True, "state": "post"}},
            "competitors": [
                {"homeAway": "home", "winner": False, "score": {"displayValue": "2"}, "team": {"id": "8", "displayName": "Hill City Owls"}},
                {"homeAway": "away", "winner": True, "score": {"displayValue": "5"}, "team": {"id": "7", "displayName": "Rivertown Otters"}}]}]}]}
    if url.endswith("/teams/7"):
        return {"team": {"record": {"items": [{"summary": "10-4"}]}}}
    return {}


def test_scores_facts_from_feed():
    from speakeasy import scores
    scores._cache.clear()
    facts = scores.facts("did the otters win last night", fetch=_espn)
    text = " ".join(f["text"] for f in facts)
    assert "Rivertown Otters (away) 5" in text and "Hill City Owls (home) 2" in text
    assert "Rivertown Otters won" in text and "10-4" in text
    assert scores.facts("how tall is the tallest tower", fetch=_espn) == []
    assert scores.facts("tell me about otters", fetch=_espn) == []   # no game words: not a sports question


def test_scores_feed_down_is_no_facts():
    from speakeasy import scores
    scores._cache.clear()
    def broken(url):
        raise OSError("down")
    assert scores.facts("did the otters win", fetch=broken) == []



def test_instant_clock_date_math():
    from datetime import datetime, timezone
    from speakeasy import instant
    now = datetime(2026, 3, 2, 15, 5, tzinfo=timezone.utc)
    nairobi = instant.answer("What time is it in Nairobi", now)
    assert nairobi.startswith("It's 6:05 PM, Monday, in Nairobi")
    assert instant.answer("what's 18 percent of 240") == "18 percent of 240 is 43.2."
    assert instant.answer("whats 12 times 7") == "That's 84."
    assert instant.answer("what is 1,250 divided by 4") == "That's 312.5."
    assert instant.answer("what is the capital of France") is None
    assert instant.answer("what time does the bakery close") is None
    assert instant.answer("what day is it", now).startswith("It's ")
    assert instant.answer("what time is it in Nowhereville", now, fetch=lambda u: {"results": []}) is None


def test_weather_facts_and_place():
    from speakeasy import weather
    assert weather.place_in("will it rain in Oslo tomorrow") == "Oslo"
    assert not weather.is_weather("turn the heat up in the bedroom")
    assert weather.facts("what's the weather tomorrow") == []   # no place, no home place: web search instead
    def fake(url):
        if "geocoding" in url:
            return {"results": [{"name": "Oslo", "latitude": 59.9, "longitude": 10.7, "country_code": "NO"}]}
        return {"current": {"time": "2026-03-02T15:00", "temperature_2m": 3.2, "apparent_temperature": 0.1,
                            "weather_code": 71, "wind_speed_10m": 12},
                "daily": {"time": ["2026-03-02", "2026-03-03"], "weather_code": [71, 3],
                          "temperature_2m_max": [4, 6], "temperature_2m_min": [-2, 0],
                          "precipitation_probability_max": [80, 10], "sunrise": ["2026-03-02T07:10"] * 2,
                          "sunset": ["2026-03-02T17:40"] * 2},
                "hourly": {"time": ["2026-03-02T15:00"], "temperature_2m": [3], "precipitation_probability": [70],
                           "weather_code": [71]}}
    facts = weather.facts("will it snow in Oslo tomorrow", fetch=fake)
    text = " ".join(f["title"] + " " + f["text"] for f in facts)
    assert "3°C" in text and "light snow" in text and "Tomorrow" in text and "80%" in text


def test_league_slate():
    from speakeasy import scores
    def fake(url):
        if "/scoreboard?dates=" in url:
            return {"events": [{"id": "9", "date": "2099-01-05T01:15Z", "competitions": [{
                "status": {"type": {"completed": False, "state": "pre"}}, "venue": {"fullName": "River Dome"},
                "competitors": [{"homeAway": "home", "team": {"id": "1", "displayName": "Rivertown Otters"}},
                                {"homeAway": "away", "team": {"id": "2", "displayName": "Hill City Owls"}}]}]}]}
        return {}
    facts = scores.league_facts("any nfl games this week", "football/nfl", fetch=fake)
    assert len(facts) == 2 and "River Dome" in facts[1]["text"]



def test_home_place_with_coordinates_skips_lookup():
    from speakeasy import weather
    def no_lookup(url):
        assert "geocoding" not in url
        return {}
    where = weather.locate("Riverside, Sometown (40.7, -73.9)", fetch=no_lookup)
    assert where == {"name": "Riverside, Sometown", "lat": 40.7, "lon": -73.9, "country": "US"}
