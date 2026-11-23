"""SPK-561: the call voice read plumbing aloud ("waiting for it to catch up", "it'll pick that up as
soon as this step finishes"), implied it would contact someone when the work could only draft a text,
and a "[cough]" tag reached the transcript as "coff] Thanks." Invented turns only."""
from __future__ import annotations

from speakeasy import transcript as T
from speakeasy.prompt import builder as P
from speakeasy.text import clean_transcript


def _rules() -> str:
    return (P.PROMPT_DIR / "rules.md").read_text()


def test_the_voice_is_told_to_say_outcomes_not_machinery():
    rules = _rules()
    assert "never the machinery" in rules
    for plumbing in ("steps finishing", "catching up", "picking something up", "a task's title used as a noun"):
        assert plumbing in rules
    # promises only what the work does: drafting a note is not "checking with him"
    assert "I'll check your calendar and draft him a note" in rules
    assert "never say you're checking with, asking, calling or messaging someone" in rules
    assert "never a sound or stage direction ([cough], (laughs), *sighs*)" in rules


def test_a_lined_up_addition_is_acknowledged_as_an_outcome():
    note = P.follow_up_queued_note("the dinner plan", "Reading the restaurant's menu", 70)
    assert "not to read out" in note and "Never describe the machinery" in note
    assert "catching up" in note and "never name the task's title" in note
    assert "I'll work that in, about a minute" in note


def test_a_status_answer_with_a_wait_says_the_gist_not_the_plumbing():
    text = P.status_answer("Dinner plan", "running", ["Reading the menu"], 120, None,
                           waiting="what they added hasn't been read yet. It's mid-step, likely in about a minute.")
    assert "never the machinery" in text and "one-word status" in text


def test_non_verbal_tags_never_reach_the_transcript():
    assert clean_transcript("coff] Thanks. Glad it lands.") == "Thanks. Glad it lands."
    assert clean_transcript("Sure. clears throat] The plan works.") == "Sure. The plan works."
    assert clean_transcript("[laughs] Fair. [cough] Next one?") == "Fair. Next one?"
    assert clean_transcript("(laughs) Fair point. *sighs* Okay.") == "Fair point. Okay."
    # real speech is kept
    assert clean_transcript("The demo (the second one) went well.") == "The demo (the second one) went well."
    assert clean_transcript("Use arr[0] and item] here") == "Use arr[0] and item] here"
    assert clean_transcript("Three * four is twelve") == "Three * four is twelve"
    long = "Brackets [are not closed because this text is far too long to be a tag]"
    assert clean_transcript(long) == long


def test_a_tag_split_across_pieces_or_missing_its_bracket_is_dropped_from_the_call_turns():
    frags = T.Fragments()
    for speaker, text in (("user", "That's the one."), ("assistant", "[co"), ("assistant", "ugh] Thanks, "),
                          ("assistant", "glad it helps."), ("user", "And the other?"),
                          ("assistant", "coff] Sure, "), ("assistant", "on it.")):
        frags.append({"speaker": speaker, "text": text, "at": 0.0})
    said = [t["text"] for t in T.spoken_turns(frags) if t["role"] == "assistant"]
    assert said == ["Thanks, glad it helps.", "Sure, on it."]
