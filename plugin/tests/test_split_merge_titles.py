"""SPK-558: three quick asks on a call. The first one's routing split one subject into two parts
and merged them back; the next, unrelated ask must still be named for its own words, never for
the first ask. Invented data only."""
from __future__ import annotations

import json
import time

import pytest

from fakes import wait_for
from speakeasy import calls, session_titles
from test_routing import start_call, tasks

TRAIN = "When does my sister's train get in, and is it running on time?"
CALENDAR = "What's on my calendar tomorrow?"
WEATHER = "Will it rain on Saturday?"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)


def _own_titles_landed(before: set) -> bool:
    # PENDING is process-wide: in a full suite another test's push can sit there for good. Wait only
    # for the session names this test started.
    return not (session_titles.PENDING - before)


def _request_line(context: str) -> str:
    return next((l[len("Request: "):] for l in context.splitlines() if l.startswith("Request: ")), "")


def _route(messages):
    """The routing model: the train ask comes back as two parts of one subject (merged into one);
    everything else is one new task."""
    asked = messages[-1]["content"].rsplit("Request: ", 1)[-1]
    if "train" in asked:
        return json.dumps({"follow_up_task_id": None, "channel": None,
                           "parts": ["When does my sister's train get in", "When does my sister's train arrive"]})
    return json.dumps({"follow_up_task_id": None, "channel": None, "parts": [asked]})


def _titler(seen):
    """A titler that, like the real one, leans on the context it is given: the task it continues
    (when named) and the call's recent turns name a request that says little itself."""
    def title(context: str) -> str:
        seen.append(context)
        about = context
        if "train" in about.lower():
            # Any mention of the train in what the titler reads pulls the name toward it, as the
            # live titler did (that is the leak: it must only read this hand-off's own words).
            return "Check sister's train arrival"
        req = _request_line(context).lower()
        if "calendar" in req:
            return "Check tomorrow's calendar"
        if "saturday" in req:
            return "Check Saturday's rain forecast"
        return "Something else"
    return title


def _by_request(server):
    out = {}
    for t in tasks(server):
        if t["status"] == "rejected":
            continue
        out[t.get("request") or ""] = t.get("title") or ""
    return out


def _ask(worker, delegation_id, words, at):
    worker.feed({"type": "session.input_transcript.delta", "delta": words, "start_ms": at, "end_ms": at + 1})
    worker.feed({"type": "session.delegation.created", "offset_ms": at + 2,
                 "delegation": {"id": delegation_id, "target": "client"}})
    worker.feed({"type": "session.output_transcript.delta", "delta": "On it.", "start_ms": at + 3,
                 "end_ms": at + 4})


def test_merged_split_never_names_the_next_task(server, service, hermes, caplog):
    before = set(session_titles.PENDING)
    seen: list[str] = []
    service.rt.route_call = _route
    service.rt.title_call = _titler(seen)
    caplog.set_level("INFO")
    _, worker = start_call(server, service)
    _ask(worker, "call_t1", TRAIN, 100)
    wait_for(lambda: len(hermes.calls) >= 1, timeout=10)
    assert "routing split one subject into 2 parts; merged into 1" in caplog.text
    _ask(worker, "call_t2", CALENDAR, 200)
    wait_for(lambda: len(hermes.calls) >= 2, timeout=10)
    _ask(worker, "call_t3", WEATHER, 300)
    wait_for(lambda: len(hermes.calls) >= 3, timeout=10)
    wait_for(lambda: len(seen) >= 3, timeout=10)
    # The titler reads each hand-off's own words: never an earlier request this call started.
    calendar_ctx = next(c for c in seen if _request_line(c) == CALENDAR)
    assert "train" not in calendar_ctx.lower(), calendar_ctx
    time.sleep(0.5)
    got = _by_request(server)
    titles = {k: v for k, v in got.items()}
    train = next(v for k, v in titles.items() if "train" in k)
    calendar = next(v for k, v in titles.items() if "calendar" in k)
    weather = next(v for k, v in titles.items() if "Saturday" in k)
    assert "train" in train.lower(), titles
    assert "calendar" in calendar.lower() and "train" not in calendar.lower(), titles
    assert "train" not in weather.lower(), titles
    wait_for(lambda: _own_titles_landed(before), timeout=10)  # session names land before teardown


def test_told_elsewhere_matches_other_requests_only():
    others = ["When does my sister's train get in, and is it running on time?"]
    assert calls._told_elsewhere("When does my sister's train get in", others)
    assert not calls._told_elsewhere("What's on my calendar tomorrow?", others)
    assert not calls._told_elsewhere("yes", ["yes please book it"])  # a short reply is never dropped by overlap
    assert calls._told_elsewhere("lamps off", ["Lamps off"])


def test_a_short_ask_still_sees_what_the_voice_offered(server, service, hermes):
    before = set(session_titles.PENDING)
    seen: list[str] = []
    service.rt.title_call = lambda context: seen.append(context) or "Book the noodle place for two"
    _, worker = start_call(server, service)
    _ask(worker, "call_n1", "Find a noodle place near the office", 100)
    wait_for(lambda: len(hermes.calls) >= 1, timeout=10)
    worker.feed({"type": "session.output_transcript.delta", "delta": "The corner noodle bar has a table at seven.",
                 "start_ms": 150, "end_ms": 151})
    _ask(worker, "call_n2", "Book that one for two people", 200)
    wait_for(lambda: len(seen) >= 2, timeout=10)
    ctx = next(c for c in seen if "Book that one" in c.splitlines()[0])
    assert "corner noodle bar" in ctx  # the voice's own words stay, for what "that one" is
    wait_for(lambda: _own_titles_landed(before), timeout=10)
