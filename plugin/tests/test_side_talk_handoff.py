"""SPK-556: what the voice says and what the server starts agree. "Didn't catch that, say it again?"
in the same turn as a hand-off starts nothing, and a short line meant for someone else in the room
("Grab me a coffee, Sam.") never starts a long job that guesses."""
import time

import pytest

from fakes import wait_for
from speakeasy import calls, router
from test_routing import start_call, tasks


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "ACK_GRACE_S", 0.4)
    monkeypatch.setattr(calls, "SETTLE_S", 0.3)


def said(worker, text, at=10):
    worker.feed({"type": "session.output_transcript.delta", "delta": text, "start_ms": at, "end_ms": at + 1})


def heard(worker, text, at=1):
    worker.feed({"type": "session.input_transcript.delta", "delta": text, "start_ms": at, "end_ms": at + 1})


def handoff(worker, delegation_id, offset_ms=50):
    worker.feed({"type": "session.delegation.created", "offset_ms": offset_ms,
                 "delegation": {"id": delegation_id, "target": "client"}})


def started(server):
    return [t for t in tasks(server) if t["status"] != "rejected"]


# -- the words --------------------------------------------------------------------------------------

def test_asks_to_repeat_is_recognised():
    for line in ("Didn't catch that one. Run it back for me?", "Didn't catch that, say it again?",
                 "Sorry, I didn't quite catch that.", "Could you repeat that?", "Say that again?",
                 "I missed that, come again?", "Sorry, what was that?"):
        assert router.asks_to_repeat(line), line
    for line in ("On it.", "Texting Sam now.", "Want me to look up what they charge?",
                 "I'll catch the next update for you.", "Turning off the kitchen lights."):
        assert not router.asks_to_repeat(line), line


def test_side_talk_is_recognised_and_clear_asks_are_not():
    for line in ("Grab me a coffee, Sam.", "Sam, grab me a coffee.", "Pass me the salt, Rowan.",
                 "Hand me that, Alex.", "Come here, Juniper."):
        assert router.side_talk(line), line
    for line in ("Text Sam that I'm running late", "Turn off the kitchen lights", "Grab me a coffee",
                 "Order me a coffee from the corner place, Sam", "Remind Sam to grab the coffee at five",
                 "Call Sam", "What's on my calendar tomorrow?", "Get me a ride to the airport at six, Sam"):
        assert not router.side_talk(line), line


# -- on a call --------------------------------------------------------------------------------------

def test_side_talk_replay_starts_no_task(server, service, hermes):
    # The reported turn: a line to someone in the room, the voice said it didn't catch it, and the
    # same turn still handed off.
    _, worker = start_call(server, service)
    heard(worker, "Grab me a coffee, Sam.")
    said(worker, "Didn't catch that one. Run it back for me?", at=10)
    handoff(worker, "call_side")
    time.sleep(1.5)
    assert not hermes.calls
    assert not started(server)


def test_side_talk_without_any_spoken_reply_starts_no_task(server, service, hermes):
    _, worker = start_call(server, service)
    worker.delegate("call_side2", "Grab me a coffee, Sam.")
    time.sleep(1.5)
    assert not hermes.calls
    assert not started(server)


def test_didnt_catch_said_after_the_handoff_starts_no_task(server, service, hermes):
    # The voice's words for the turn arrive just after the hand-off: the server still holds it.
    _, worker = start_call(server, service)
    worker.delegate("call_again", "Book a table for two at the noodle place downtown")
    said(worker, "Didn't catch that, say it again?", at=10)
    time.sleep(1.5)
    assert not hermes.calls
    assert not started(server)


def test_didnt_catch_said_before_the_handoff_starts_no_task(server, service, hermes):
    _, worker = start_call(server, service)
    heard(worker, "Book a table for two at the noodle place downtown")
    said(worker, "Didn't catch that, say it again?", at=10)
    handoff(worker, "call_again2")
    time.sleep(1.5)
    assert not hermes.calls
    assert not started(server)


@pytest.mark.parametrize("ask", ["Text Sam that I'm running late", "Turn off the kitchen lights"])
def test_clear_asks_still_hand_off(server, service, hermes, ask):
    _, worker = start_call(server, service)
    worker.delegate("call_clear", ask)
    said(worker, "On it.", at=10)
    wait_for(lambda: hermes.calls or started(server), timeout=10)


def test_an_earlier_didnt_catch_does_not_block_the_next_clear_ask(server, service, hermes):
    # "Didn't catch that" answered an earlier turn; the user then asked clearly: that one starts.
    _, worker = start_call(server, service)
    heard(worker, "mm", at=1)
    said(worker, "Didn't catch that, say it again?", at=3)
    heard(worker, "Text Sam that I'm running late", at=20)
    handoff(worker, "call_after", offset_ms=40)
    said(worker, "On it.", at=45)
    wait_for(lambda: hermes.calls, timeout=10)
