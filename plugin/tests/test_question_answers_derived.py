"""SPK-345: a question that arrives without its answers still gets one-tap answers, taken from its
own words (the choices it names, else Yes / No), with no model call; nothing sensible, nothing."""
import pytest

from speakeasy import text, views
from speakeasy.now import public_question


@pytest.mark.parametrize("question, answers", [
    ("Three desks fit your 62 in. wall. Which one should I order, Oakline or Birch?", ["Oakline", "Birch"]),
    ("Should I book Nopa or Zuni?", ["Nopa", "Zuni"]),
    ("Den, bedroom, or both?", ["Den", "Bedroom", "Both"]),
    ("Want the quick summary or the full report?", ["The quick summary", "The full report"]),
    ("Do you want me to email Dana or text her?", ["Email Dana", "Text her"]),
    ("Pick one: Kayak, Expedia or Google Flights?", ["Kayak", "Expedia", "Google Flights"]),
    ("Which is better, Oakline or Birch?", ["Oakline", "Birch"]),
])
def test_the_choices_a_question_names_become_its_answers(question, answers):
    assert views.derive_answers(question) == answers


@pytest.mark.parametrize("question", [
    "Should I order it?", "Do you want me to send it now?", "Okay to merge the PR?",
    "So, should I go ahead?", "Is this the right desk or not?", "Can I book the 7 PM table?",
])
def test_a_yes_no_question_gets_yes_and_no(question):
    assert views.derive_answers(question) == ["Yes", "No"]


@pytest.mark.parametrize("question", [
    "Which one should I order?", "What time works for you?", "Where should the photos go?", "",
    "Want me to keep going, or something else?", "Which of the 12 vendors in the sheet should I call first?",
    "Should I use the long version with every note, the short one or a summary for Dana's whole team?",
])
def test_nothing_sensible_derives_nothing(question):
    assert views.derive_answers(question) == []


def test_derivation_is_the_same_every_time():
    q = "Which one should I order, Oakline, Birch Lift or Ridge Pro?"
    assert {tuple(views.derive_answers(q)) for _ in range(20)} == {("Oakline", "Birch Lift", "Ridge Pro")}


def test_a_question_block_without_options_still_becomes_a_tappable_card():
    out = ("I found three desks under $400 that fit.\n\n```speakeasy-question\n"
           '{"question": "Which one should I order, Oakline or Birch?"}\n```\nDONE: desk')
    r = text.split_result(out, ())
    q = r["question"]
    assert q["options"] == ["Oakline", "Birch"] and q.get("derived") is True and "recommended" not in q
    assert "1. Oakline" in r["full"] and "2. Birch" in r["full"]
    home = public_question(q, "run_desk", 1_800_000_000)
    assert [o["id"] for o in home["options"]] == ["o1", "o2"]
    assert [o["text"] for o in home["options"]] == ["Oakline", "Birch"]
    assert home["shape"] == "words"


def test_a_yes_no_block_with_one_option_gets_yes_and_no():
    _, q = views.extract_question('Done.\n```speakeasy-question\n{"question": "Should I send it?", "options": ["Yes"]}\n```')
    assert q["options"] == ["Yes", "No"] and q["derived"] is True


def test_server_options_always_win_over_derivation():
    _, q = views.extract_question('Done.\n```speakeasy-question\n'
                                  '{"question": "Oakline or Birch?", "options": ["Oakline 60 in.", "Birch Lift 55 in."], '
                                  '"recommended": 0}\n```')
    assert q["options"] == ["Oakline 60 in.", "Birch Lift 55 in."] and q["recommended"] == 0 and "derived" not in q


def test_a_block_with_nothing_derivable_is_still_dropped():
    out, q = views.extract_question('Done.\n```speakeasy-question\n{"question": "Which one should I order?"}\n```')
    assert q is None and out == "Done."
