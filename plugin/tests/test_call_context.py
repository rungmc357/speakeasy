"""A task started from a call gets the whole call, in whole turns.

The voice's replies stream in a word or two per piece, and the call used to keep only the newest
512 pieces: a task asked for ten minutes in saw the last three or four exchanges, starting mid-reply
("Assistant: Sure, I'll"). Now the pieces are joined into turns as they arrive, the task gets the
whole call up to a character budget with the oldest turns dropped whole, the router reads the same
turns, and the brief lists the call's other tasks. Every call here is invented.
"""
from __future__ import annotations

from collections import deque

from fakes import wait_for
from speakeasy import router, transcript
from speakeasy.prompt import builder as P
from test_routing import start_call

# Ten invented exchanges. The second offers two dinner ideas; the ninth points back at "the first one".
EXCHANGES = [
    ("Hey, can you check if the bakery on the corner is open on Sunday", "It's open Sunday from eight to two, "
     "and they usually sell out of the sourdough by noon, so go early if that's what you want."),
    ("What could we cook tonight with what's in the fridge", "Two ideas from what you have: first, a lemon "
     "pasta with peas and parmesan, about twenty minutes; second, a mushroom risotto, closer to forty."),
    ("How cold is it going to get tonight", "Down to about forty-one degrees after midnight, with a light "
     "breeze, so a jacket if you head out later."),
    ("Remind me what time the hardware store closes", "It closes at seven tonight and opens at nine "
     "tomorrow; they have the paint you asked about last week in stock."),
    ("Is the train running on time this evening", "Yes, the evening trains are on schedule, with the next "
     "one in twelve minutes and then every fifteen after that."),
    ("Did the package from the camera shop ship yet", "It shipped this morning and is due Thursday by the "
     "end of the day; you'll get a text when it's close."),
    ("What's a good movie for a quiet night in", "A calm pick would be a slow detective story with a "
     "great soundtrack, around two hours, nothing too heavy."),
    ("How long would it take to walk to the park from here", "About eighteen minutes at an easy pace, "
     "mostly flat, past the farmers market on the way."),
    ("Find me a full recipe for the first one you mentioned for dinner", None),
]


def _feed_call(worker, exchanges=EXCHANGES, piece=2):
    """User turns in one piece, the voice's replies in tiny pieces (as the providers stream them)."""
    ms = 0
    for asked, reply in exchanges:
        ms += 10
        worker.feed({"type": "session.input_transcript.delta", "delta": asked, "start_ms": ms, "end_ms": ms + 1})
        reply = " " + reply if reply else ""
        for i in range(0, len(reply), piece):
            ms += 1
            worker.feed({"type": "session.output_transcript.delta", "delta": reply[i:i + piece],
                         "start_ms": ms, "end_ms": ms})
    return ms + 5


def test_pieces_join_into_whole_turns_and_the_oldest_turn_goes_whole():
    log = transcript.Fragments(maxlen=4, log_chars=60)
    for speaker, text in [("user", "Turn on the porch light"), ("assistant", " Sure,"), ("assistant", " it's on."),
                          ("user", "And the hallway one too"), ("assistant", " Done.")]:
        log.append({"speaker": speaker, "text": text, "at": 0.0, "start_ms": 1, "end_ms": 1})
    assert len(log) == 4  # the recent pieces stay bounded for the live checks
    turns = transcript.spoken_turns(log)
    # Past the budget the first turn went whole; no turn starts mid-reply.
    assert [t["text"] for t in turns] == ["Sure, it's on.", "And the hallway one too", "Done."]


def test_a_plain_deque_is_joined_the_same_way():
    pieces = deque([{"speaker": "user", "text": "Water the plants", "at": 0.0, "start_ms": 1, "end_ms": 1},
                    {"speaker": "assistant", "text": " On", "at": 0.0, "start_ms": 2, "end_ms": 2},
                    {"speaker": "assistant", "text": " it.", "at": 0.0, "start_ms": 3, "end_ms": 3}])
    assert [t["text"] for t in transcript.spoken_turns(pieces)] == ["Water the plants", "On it."]
    assert [t["text"] for t in transcript.spoken_turns(pieces, before_ms=2)] == ["Water the plants", "On"]


def test_whole_turns_drop_the_oldest_and_say_so():
    lines = [f"User: question number {i}" for i in range(10)]
    kept, dropped = transcript.whole_turns(lines, 80)
    assert kept[-1] == "User: question number 9" and dropped == 10 - len(kept)
    assert all(line in lines for line in kept)
    assert "3 older turns" in transcript.dropped_note(3)


def test_tail_text_cuts_at_a_turn_boundary():
    call = "User: first ask about the garden\nAssistant: Here is a long answer about the garden beds\nUser: ok"
    tail = transcript.tail_text(call, 70)
    assert tail.startswith(("User: ", "Assistant: ")) and tail.endswith("User: ok")
    assert "Assistant: Here is a long answer" in tail or tail == "User: ok"


def test_a_long_call_reaches_the_task_whole(server, service, hermes):
    session, worker = start_call(server, service)
    end = _feed_call(worker, piece=1)
    # More pieces than the old 512-piece cap ever held: it lost the start of this call.
    assert len(list(worker.fragments)) == 512 and worker.fragments[0]["text"] != EXCHANGES[0][0]
    context = worker.context(end)
    lines = context.splitlines()
    # The whole call, starting at the very first turn (not mid-reply), the second exchange included.
    assert lines[0] == "User: " + EXCHANGES[0][0]
    assert any("first, a lemon pasta with peas" in line for line in lines)
    assert lines[-1] == "User: " + EXCHANGES[-1][0]
    assert all(line.startswith(("User: ", "Assistant: ")) for line in lines)


def test_the_ninth_ask_reaches_the_task_and_the_router_with_the_second(server, service, hermes):
    session, worker = start_call(server, service)
    seen: list[str] = []

    def route(request, tasks, marked, chats=None, call_so_far="", replied_task_id=None, focus=None):
        seen.append(call_so_far)
        return router.fallback_decision(request)

    worker.rt.route = route
    end = _feed_call(worker)
    worker.feed({"type": "session.delegation.created", "offset_ms": end,
                 "delegation": {"id": "call_recipe", "target": "client"}})
    wait_for(lambda: hermes.calls)
    prompt = hermes.calls[-1]["input"]
    said = prompt.split("Recent timestamped voice transcript", 1)[1].split("\n", 1)[1]
    assert said.startswith("User: " + EXCHANGES[0][0])
    assert "first, a lemon pasta with peas" in said and EXCHANGES[-1][0] in said
    # The router sees the turn the request points back at, cut at a turn boundary.
    assert seen and "first, a lemon pasta with peas" in seen[-1]
    assert seen[-1].startswith(("User: ", "Assistant: "))


def test_a_very_long_call_drops_its_oldest_turns_whole(server, service, hermes):
    session, worker = start_call(server, service)
    end = _feed_call(worker, EXCHANGES[:-1] * 12 + EXCHANGES[-1:])
    context = worker.context(end)
    assert len(context) <= transcript.TASK_CONTEXT_CHARS + 100
    first, second = context.splitlines()[:2]
    assert first.startswith("(Earlier in the call:") and "older turns not shown" in first
    assert second.startswith(("User: ", "Assistant: "))
    replies = {"Assistant: " + r for _, r in EXCHANGES if r}
    asks = {"User: " + a for a, _ in EXCHANGES}
    assert all(line in replies | asks for line in context.splitlines()[1:])  # every turn whole


def test_the_brief_lists_the_calls_other_tasks(server, service, hermes):
    hermes.responder = lambda prompt, sid: ("It opens at eight on Sunday.\nDONE: Checked the hours\n"
                                            "SPOKEN: It opens at eight on Sunday.")
    session, worker = start_call(server, service)
    worker.delegate("call_bakery", EXCHANGES[0][0])
    wait_for(lambda: hermes.calls)
    wait_for(lambda: any(r.status == "completed" for r in worker.interaction.runs.values()))
    worker.feed({"type": "session.output_transcript.delta", "delta": "It opens at eight.", "start_ms": 3, "end_ms": 3})
    worker.feed({"type": "session.input_transcript.delta", "delta": "Now check the weather for the park walk later",
                 "start_ms": 6, "end_ms": 7})
    worker.feed({"type": "session.delegation.created", "offset_ms": 9,
                 "delegation": {"id": "call_weather", "target": "client"}})
    wait_for(lambda: len(hermes.calls) >= 2)
    first, second = hermes.calls[0]["input"], hermes.calls[1]["input"]
    assert P.CALL_SO_FAR not in first  # nothing else on the call yet
    block = second.split(P.CALL_SO_FAR, 1)[1].split("\n\n", 1)[0]
    assert ": done. It opens at eight on Sunday." in block
    assert "weather" not in block.lower()  # the task itself is not listed


def test_task_lines_are_plain_words():
    assert P.call_task_line("Bakery hours", "completed", answer="Open at eight.") == "- Bakery hours: done. Open at eight."
    assert P.call_task_line("Train times", "running", now="Checking the evening schedule") == \
        "- Train times: still running (Checking the evening schedule)"
    assert P.call_task_line("Dinner pick", "completed", question="Pasta or risotto?") == \
        "- Dinner pick: asked the user: Pasta or risotto?"
    assert P.call_task_line("Old ask", "failed") == "- Old ask: didn't finish"
    assert P.call_so_far_block([]) == ""
