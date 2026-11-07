import pytest
from speakeasy.prompt import builder as P

N = P.Names()


def test_task_brief_checks_calendar_before_notes():
    text = P.build_task_prompt(N, 1, "user: what dinners do I have in Lisbon and Porto?")
    low = text.lower()
    assert "calendar and any confirmation" in low and "notes and shortlists only to fill gaps" in low
    assert "never say there is" in low and "dates and the city" in low


def test_voice_says_checking_until_task_finishes():
    rules = P.rules_text(N)
    assert "Only a finished task's result is an answer" in rules
    assert "never state a count" in rules and "get handed off" in rules
    assert "good catch" in rules


@pytest.mark.parametrize("raw,want", [
    (", I'll add the hotels with dates.", "I'll add the hotels with dates."),
    ("Good catch. Thanks. You're right, there are four dinners.", "There are four dinners."),
    ("Good catch... Thanks. I'm checking your calendar now.", "I'm checking your calendar now."),
    ("Four dinners are booked.", "Four dinners are booked."),
    ("Okay", "Okay"),
    ("", ""),
])
def test_tidy_spoken(raw, want):
    assert P.tidy_spoken(raw) == want


def test_late_and_part_results_are_tidied():
    assert ", Two stays booked." not in P.late_result("hotels", ", Two stays booked.")
    assert "Two stays booked." in P.result_for_part("hotels", "Good catch. Two stays booked.", False)
    assert "Good catch" not in P.result_for_part("hotels", "Good catch. Two stays booked.", False)
