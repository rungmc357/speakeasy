"""Short key points for answers laid out in sections (the lighter job page)."""
from pathlib import Path

from speakeasy.text import split_result, summary_points

REPORT = """Here's where the PRs stand.

## Open
- **#412** adds the new onboarding flow and still needs one more review from the design team
- #415 fixes the reload crash

## Merged this week
Three PRs landed: [#401](https://github.com/x/y/pull/401), #402 and #405.

## Blocked
- #399 waits on the Apple review
"""


def test_sections_give_one_short_point_each():
    points = summary_points(REPORT)
    assert points == ["#412 adds the new onboarding flow and still needs one",
                      "Three PRs landed", "#399 waits on the Apple review"]
    assert all(len(p.split()) <= 10 for p in points)


def test_a_list_of_three_or_more_gives_its_items():
    text = "Options:\n- Nopa at 7pm, outdoor seating\n- Zuni at 8pm\n- State Bird at 9pm. Loud.\n- Rich Table\n- Lazy Bear"
    assert summary_points(text) == ["Nopa at 7pm, outdoor seating", "Zuni at 8pm", "State Bird at 9pm", "Rich Table"]


def test_plain_answers_have_none():
    assert summary_points("It's 72 and sunny. Nice day for a walk.") is None
    assert summary_points("- one thing\n- another") is None
    assert summary_points(None) is None


def test_split_result_adds_them_and_keeps_full():
    result = split_result(REPORT + "\nSPOKEN: Two open, three merged, one blocked.", (Path("/nonexistent"),))
    assert result["summary_points"][0].startswith("#412")
    assert "## Open" in result["full"]
    assert "summary_points" not in split_result("Done. All good.", ())
