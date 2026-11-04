"""Saying "on it" means a task exists; corrections replace, fragments and repeats start nothing."""
import pytest

from fakes import wait_for
from speakeasy import calls, router
from speakeasy.calls import whole_request
from test_routing import start_call, tasks


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "ACK_GRACE_S", 0.4)
    monkeypatch.setattr(calls, "SETTLE_S", 0.3)


def said(worker, text, at=10):
    worker.feed({"type": "session.output_transcript.delta", "delta": text, "start_ms": at, "end_ms": at + 1})


def _thinking(worker):
    return [c for k, _, c in worker.sent if k == "session.thinking.append"]


# -- the words ------------------------------------------------------------------------------------

def test_a_polite_ask_about_something_existing_is_work_not_an_idea():
    for q in ("Can we make the logo for the app on mobile match the desktop logo",
              "could you fix my build script", "I feel like— hey, where did our PRs leave off? Any work we have to keep doing"):
        assert not router.is_conversation(q), q
    for q in ("But I could make a sleep app called Nightcap", "what do you think about a weekly plan"):
        assert router.is_conversation(q), q


def test_work_acknowledgements_are_recognised():
    for a in ("On it, let's make those match up.", "Good question. Let me pull that together quickly.",
              "Switching those on.", "I'll take care of it."):
        assert router.acknowledges_work(a), a
    for a in ("Sure, let's just talk it through.", "Want me to look up what they charge?", "Hi there!"):
        assert not router.acknowledges_work(a), a


def test_corrections_and_fragments():
    assert router.correction("Our guest room, sorry") == ("phrase", "guest room")
    assert router.correction("living room... guest room, sorry") == ("phrase", "guest room")
    assert router.correction("no, turn them on") == ("replace", "turn them on")
    for text in ("No", "no thanks", "Uh, sure", "actually make the thermostat 21", "be- er, sorry,"):
        assert router.correction(text) is None, text
    assert router.apply_correction("Turn on the lights in the living room", "guest room") == \
        "Turn on the lights in the guest room"
    assert router.is_fragment("that") and router.is_fragment("there it is") and not router.is_fragment("lamps off")


def test_a_request_already_started_is_never_joined_onto_the_next():
    context = "User: Turn on my bedroom lamps, and turn off the bedroom ceiling light\nUser: Turn on the lamps"
    started = {calls._words("Turn on my bedroom lamps, and turn off the bedroom ceiling light")}
    assert whole_request(context, started) == "Turn on the lamps"
    assert whole_request("User: Bedroom lamps at forty percent\nUser: purple") == "Bedroom lamps at forty percent purple"


# -- on a call ------------------------------------------------------------------------------------

def test_on_it_after_a_request_taken_for_talk_starts_the_work(server, service, hermes, monkeypatch):
    # The gate's old blind spot: "can we make X match Y" read as a what-if. Force the same mistake.
    monkeypatch.setattr(router, "is_conversation", lambda request: True)
    _, worker = start_call(server, service)
    worker.delegate("call_logo", "Can we make the mobile logo match the desktop logo")
    wait_for(lambda: any("thinking out loud" in c for c in _thinking(worker)))
    assert not hermes.calls
    said(worker, "On it, let's make those match up.")
    wait_for(lambda: hermes.calls, timeout=10)
    assert "mobile logo" in hermes.calls[0]["input"]


def test_on_it_with_no_handoff_at_all_starts_the_request(server, service, hermes):
    _, worker = start_call(server, service)
    worker.feed({"type": "session.input_transcript.delta", "delta": "where did our pull requests leave off",
                 "start_ms": 1, "end_ms": 2})
    said(worker, "Good question. Let me pull that together quickly.")
    wait_for(lambda: hermes.calls, timeout=10)
    assert "pull requests leave off" in hermes.calls[0]["input"]


def test_on_it_after_a_real_handoff_starts_nothing_extra(server, service, hermes):
    _, worker = start_call(server, service)
    worker.delegate("call_r", "Research the best standing desks under five hundred dollars")
    said(worker, "On it.")
    wait_for(lambda: hermes.calls, timeout=10)
    import time
    time.sleep(1.0)
    assert len(hermes.calls) == 1


def test_on_it_to_a_fragment_says_plainly_nothing_started(server, service, hermes):
    _, worker = start_call(server, service)
    worker.feed({"type": "session.input_transcript.delta", "delta": "that", "start_ms": 1, "end_ms": 2})
    said(worker, "On it.")
    wait_for(lambda: any("nothing was started" in c for c in _thinking(worker)), timeout=10)
    assert not hermes.calls


def test_a_fragment_with_nothing_waiting_starts_nothing(server, service, hermes):
    _, worker = start_call(server, service)
    worker.delegate("call_f", "there it is")
    wait_for(lambda: any("fragment" in str(x) for x in worker.sent))
    assert not hermes.calls and not [t for t in tasks(server) if t["status"] != "rejected"]


def test_the_same_words_handed_off_twice_start_one_task(server, service, hermes):
    _, worker = start_call(server, service)
    worker.delegate("call_d1", "Fix it now")
    wait_for(lambda: hermes.calls, timeout=10)
    said(worker, "Fixing it.", at=3)
    worker.delegate("call_d2", "Fix it now")
    import time
    time.sleep(1.2)
    assert len(hermes.calls) == 1


def test_a_correction_after_the_request_went_replaces_it(server, service, hermes):
    _, worker = start_call(server, service)
    worker.delegate("call_c1", "Book a table for two at the noodle place in the living room district")
    wait_for(lambda: hermes.calls, timeout=10)
    said(worker, "On it.", at=3)
    worker.delegate("call_c2", "Our guest room district, sorry")
    wait_for(lambda: len(hermes.calls) >= 2, timeout=10)
    second = hermes.calls[1]["input"]
    assert second.rstrip().endswith("User: Book a table for two at the noodle place in the guest room district")


def test_a_correction_said_while_settling_replaces_the_words_it_fixes(monkeypatch):
    import asyncio
    import time
    from collections import deque
    from types import SimpleNamespace

    s = SimpleNamespace(fragments=deque(maxlen=64), absorbed=[])

    async def run(extra):
        task = asyncio.ensure_future(calls.SidebandWorker.settle(s, "Turn on the lights in the living room"))
        await asyncio.sleep(0.1)
        s.fragments.append({"speaker": "user", "text": extra, "at": time.monotonic()})
        return await task
    assert asyncio.run(run("Our guest room, sorry")) == "Turn on the lights in the guest room"
    s.fragments.clear()
    assert asyncio.run(run("no, turn them off")) == "turn them off"
    s.fragments.clear()
    # The same words again ("yeah, turn on the lights") add nothing.
    assert asyncio.run(run("yeah the lights")) == "Turn on the lights in the living room"
