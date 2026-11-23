"""SPK-563: Home's instant reply stated a guessed fact ("it's only available through a hosted API")
while starting a job about exactly that, and the job said the opposite 2.5 minutes later. A job's
reply now only acknowledges what the job will find out; whatever else Home said goes to the job,
which says so plainly when its answer differs. Invented model and data only."""
from __future__ import annotations

import json

from fakes import wait_for
from speakeasy import home_chat as HC
from speakeasy.prompt import builder as P
from test_home_chat import coordinator, post


def _start(request: str, reply: str) -> dict:
    return {"tool": "mcp__start_job", "text": "", "args": {"request": request, "more_jobs": [], "reply": reply}}


def test_the_reply_to_a_lookup_never_states_its_answer(server, service, hermes):
    coordinator(service, _start("Can the user run the Foobar-3 model locally, and is it already on one of their machines?",
                                "Foobar-3 is only available through a hosted API, so there's no local option."))
    body = post(server, {"text": "Can I run Foobar-3 locally?", "client_id": "c_foo"})[1]
    assert body["action"] == "start" and body["run_id"]
    said = body["reply"]["text"]
    assert "API" not in said and "no local" not in said, said
    assert said in HC.ACKS                                         # it acknowledges and defers
    call = wait_for(lambda: hermes.calls)[0]
    assert "Before this job started" not in json.dumps(call["input"])   # nothing was claimed to check


def test_what_else_home_said_goes_to_the_job_to_check(server, service, hermes):
    coordinator(service, _start("Find a birthday gift idea for the user's sister under $50",
                                "Let me find you a few. And a soft-boiled egg takes about 6 minutes."))
    body = post(server, {"text": "find a gift for my sister under $50, and how long do I boil an egg",
                         "client_id": "c_gift"})[1]
    assert "6 minutes" in body["reply"]["text"]                   # another part's answer stays
    prompt = json.dumps(wait_for(lambda: hermes.calls)[0]["input"])
    assert "Before this job started, Home already replied" in prompt and "soft-boiled egg" in prompt
    assert "Correction:" in prompt and "If it matches, don't mention it" in prompt


def test_guess_detection():
    req = "Can I run Foobar-3 locally?"
    assert HC.defer_to_job("Unfortunately Foobar-3 is API-only.", req)[0] in HC.ACKS
    assert HC.defer_to_job("Foobar-3 is hosted only. Let me double-check.", req) == ("Let me double-check.", True)
    kept = "Let me check what Foobar-3 needs and what's on your machines."
    assert HC.defer_to_job(kept, req) == (kept, False)              # promising the work is fine
    assert not HC.guesses_about("Want me to check the big machine too?", req)   # asking back is fine
    # the first line written while the reply is still coming obeys the same rule
    assert HC.clean_ack("JOB: Foobar-3 only runs in the cloud.", req) is None
    assert HC.clean_ack("JOB: Checking whether Foobar-3 runs at home.", req) == "Checking whether Foobar-3 runs at home."


def test_the_job_brief_says_how_to_correct_an_earlier_line():
    block = P.earlier_reply_block("It's only available through a hosted API.")
    assert block.startswith("Before this job started, Home already replied to the user:")
    assert "without looking anything up" in block and "Correction:" in block


def test_the_coordinator_is_told_not_to_answer_what_a_job_checks():
    assert "never answers or guesses at what that job will find out" in HC._PROMPT
    assert "Never an answer or a guess about what this job will look up" in HC._START_REPLY["description"]
