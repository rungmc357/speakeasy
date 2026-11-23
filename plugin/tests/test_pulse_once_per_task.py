"""SPK-562: during one 3-minute job the voice spoke after every card change, ~50 s apart ("Pulling up
the schedule now." / "Digging for that schedule now." / "Still on it, digging through the thread.")
while the user said nothing. One task gets at most one unprompted line per TASK_PULSE_EVERY_S, card
changes in between only update the screen, and a line that restates earlier news is never said.
Invented data only."""
from __future__ import annotations

import asyncio

from speakeasy import calls
from test_call_pulse import card, clock, run_quiet, setup, tick, tool  # noqa: F401  (fixture)
from test_routing import spoken


def test_three_card_changes_in_two_minutes_speak_once(service, clock):
    worker, backend = setup(service, clock, request="Find the soccer schedule in the team thread")
    said = []
    for n, (name, preview) in enumerate((("web_search", "team soccer schedule"),
                                         ("read_file", "schedule_notes.txt"),
                                         ("search_files", "fixtures spring"))):
        tool(service, backend, clock, name, preview, f"c{n}")   # the card changes...
        before = card(service, backend)["now_line"]
        said += run_quiet(worker, clock, 50)                       # ...about every 50 s, nobody talks
        assert card(service, backend)["now_line"] == before        # the screen shows each change
    assert len(said) == 1, f"one job, quiet user: {len(said)} unprompted lines {said}"


def test_the_tasks_own_status_lines_share_the_spacing(service, clock):
    """Narration the task writes itself counts too: three STATUS lines 50 s apart, one spoken."""
    worker, backend = setup(service, clock)
    clock.t += calls.PROGRESS_AFTER_S + 1
    for status, detail in (("Pulling up the schedule", "Opening the team thread"),
                           ("Reading the fixtures", "Found the spring list"),
                           ("Checking the times", "Matching kick-off times")):
        asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim",
                                                          "text": f"STATUS: {status}\nDETAIL: {detail}"}))
        clock.t += 50
    assert len(spoken(worker)) == 1
    # A heads-up is never held back by the spacing.
    asyncio.run(worker._handle_hermes_event(backend, {"event": "message.interim",
                                                      "text": "STATUS: Fixing the broken sign-in\nDETAIL: The team site rejected the login"}))
    assert spoken(worker)[-1] == "Fixing the broken sign-in. The team site rejected the login."


def test_a_line_that_only_restates_earlier_news_is_not_said():
    earlier = ["Pulling up the schedule now."]
    assert calls.restates("I'm digging for that schedule now.", earlier)
    assert calls.restates("Still on it, digging through it.", earlier)
    assert not calls.restates("Found it: games are Saturdays at nine.", earlier)
    assert not calls.restates("Pulling up the schedule now.", [])
