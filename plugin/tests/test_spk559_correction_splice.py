"""SPK-559: a spoken correction replaces the misheard words cleanly, or (when the replacement can't
be pulled out as one clean phrase) keeps the request as asked and adds a clean note. Filler
("man", "bro", "like", "my, my") and half-sentences never reach the request or the title.
Invented data only."""
from __future__ import annotations

import re

import pytest

from fakes import wait_for
from speakeasy import calls, corrections as C, vocabulary as V
from test_routing import start_call

ASK = "Put all my stuff on Echo Hub back to normal"
SPLICED = "No, not Echo Hub, man. My house, like the heating stuff."
FILLER = re.compile(r"(?i)\b(?:man|bro|dude|like)\b|\bmy,\s*my\b")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)
    monkeypatch.setattr(calls, "FOLLOW_UP_POLL_S", 0.1)
    monkeypatch.setattr(calls, "STEER_RETRY_S", 0.05)
    V._cache.clear()


def _no_splice(request: str) -> None:
    assert "man." not in request and not FILLER.search(request.replace("Correction: they meant", "")), request
    assert "My house, like" not in request, request


def test_a_rambling_correction_is_never_spliced_into_the_request():
    fix = C.detect(SPLICED, ASK)
    assert fix is not None and fix.wrong == "Echo Hub"
    _no_splice(fix.request)
    # The request is either cleanly corrected or kept as asked with a clean note.
    assert fix.request.startswith(ASK) and "Correction: they meant" in fix.request
    assert fix.right and not FILLER.search(fix.right) and "heating" in fix.right


@pytest.mark.parametrize("words, right", [
    ("No, not Echo Hub, man, Eco Mode", "Eco Mode"),
    ("no not Echo Hub bro, eco mode", "eco mode"),
    ("No, not Echo Hub, like, my, my Eco Mode", "my Eco Mode"),
])
def test_filler_around_a_clean_replacement_is_stripped(words, right):
    fix = C.detect(words, ASK)
    assert fix is not None and (fix.wrong, fix.right) == ("Echo Hub", right)
    assert fix.request == ASK.replace("Echo Hub", right)


def test_corrected_title_never_carries_the_raw_correction():
    fix = C.detect(SPLICED, ASK)
    title = C.corrected_title("Restore Echo Hub devices", fix)
    assert title and "Echo Hub" not in title and not FILLER.search(title) and "." not in title, title
    clean = C.detect("No, not Echo Hub, man, Eco Mode", ASK)
    assert C.corrected_title("Restore Echo Hub devices", clean) == "Restore Eco Mode devices"


def test_on_a_call_the_request_and_title_have_no_filler_splice(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service)
    worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_restore", ASK)
    run = wait_for(lambda: next((r for r in worker.interaction.runs.values()
                                 if r.delegation_id == "call_restore" and r.run_id), None))
    service.store.set_title(run.idem_key, "Restore Echo Hub devices")
    steers = len(hermes.steers)
    worker.feed({"type": "session.output_transcript.delta", "delta": "On it.", "start_ms": 3, "end_ms": 4})
    worker.delegate("call_fix", SPLICED, task_id="call_restore")
    wait_for(lambda: service.store.corrections(run.idem_key))
    request = service.store.request_text(run.idem_key)
    _no_splice(request)
    title = service.store.title(run.idem_key) or ""
    assert not FILLER.search(title) and "My house" not in title and "." not in title, title
    assert "Echo Hub" not in title, title
    steer = wait_for(lambda: hermes.steers[steers:])[0][1]
    assert "heating" in steer
