"""SPK-231 D4 (a reply leads with its verdict: one cheap rewrite) and R2 (an "On it" never reads the ask
back). Invented fixtures."""
from __future__ import annotations

from fakes import SDP, http, wait_for
from speakeasy import home_chat as HC
from speakeasy import text as T


# -- D4: the no-model detector -----------------------------------------------------------------------

def test_a_bare_count_is_weak_and_a_verdict_is_kept():
    assert T.weak_reply("3 PRs need review.")
    assert T.weak_reply("There are 5 open issues.")
    assert T.weak_reply("Four emails need replies.")
    assert not T.weak_reply("Two PRs are blocked on CI; the fix is a rebase.")
    assert not T.weak_reply("Two PRs are blocked on CI.")
    assert not T.weak_reply("Only one PR needs you, and it's ready to merge.")
    assert not T.weak_reply("The build is green and live.")


def test_a_line_that_only_says_the_card_again_is_weak():
    card = [{"kind": "metric", "label": "Errors per hour", "value": "270", "source": "Sentry"}]
    assert T.weak_reply("Errors per hour: 270.", card)
    assert not T.weak_reply("Errors spiked to 270 an hour after the 3:12 deploy; roll it back.", card)
    assert not T.restates_card("Errors per hour: 270.", None)                   # no card: only the count rule applies


def test_a_rewrite_is_vetted_and_never_weak_itself():
    assert T.verdict_line("Two PRs are blocked on CI; a rebase fixes both.") == "Two PRs are blocked on CI; a rebase fixes both."
    assert T.verdict_line("3 PRs need review.") is None                         # still a bare count
    assert T.verdict_line("Should I rebase them?") is None                      # a question
    assert T.verdict_line(" ".join(["word"] * 26) + ".") is None                # over 25 words
    assert T.verdict_line(None) is None and T.verdict_line("") is None


def _start_call(server, service, key):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return service.workers[-1]


def _tasks(server):
    return http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]


def test_a_bare_count_headline_gets_exactly_one_rewrite(server, service, hermes):
    asked: list[str] = []
    service.rt.headline_call = lambda request, report: "3 PRs need review."
    service.rt.verdict_call = lambda request, report, line: asked.append(line) or \
        "Two PRs are blocked on CI; the third is ready to merge."
    hermes.responder = lambda prompt, sid: ("3 PRs need review. Two are blocked on CI and the third is ready to merge."
                                            "\nSPOKEN: Three PRs need review.")
    worker = _start_call(server, service, "req_d4_1")
    worker.delegate("call_d4", "which PRs need my review")
    task = wait_for(lambda: [t for t in _tasks(server)
                             if (t.get("result") or {}).get("headline", "").startswith("Two PRs")])[0]
    assert task["result"]["headline"] == "Two PRs are blocked on CI; the third is ready to merge."
    assert asked == ["3 PRs need review."]


def test_an_unusable_rewrite_keeps_the_original_line(server, service, hermes):
    asked: list[str] = []
    service.rt.headline_call = lambda request, report: "3 PRs need review."
    service.rt.verdict_call = lambda request, report, line: asked.append(line) or "Four PRs need review."
    hermes.responder = lambda prompt, sid: "3 PRs need review.\nSPOKEN: Three PRs need review."
    worker = _start_call(server, service, "req_d4_2")
    worker.delegate("call_d4b", "which PRs need my review")
    task = wait_for(lambda: asked and [t for t in _tasks(server) if (t.get("result") or {}).get("headline")
                                       == "3 PRs need review."])[0]
    assert task["result"]["headline"] == "3 PRs need review." and len(asked) == 1


def test_a_verdict_headline_asks_for_no_rewrite(server, service, hermes):
    asked: list[str] = []
    service.rt.headline_call = lambda request, report: "Two PRs are blocked on CI; the fix is a rebase"
    service.rt.verdict_call = lambda request, report, line: asked.append(line) or "x"
    hermes.responder = lambda prompt, sid: "Two PRs are blocked on CI; the fix is a rebase.\nSPOKEN: Two are blocked."
    worker = _start_call(server, service, "req_d4_3")
    worker.delegate("call_d4c", "which PRs need my review")
    wait_for(lambda: [t for t in _tasks(server) if (t.get("result") or {}).get("headline", "").startswith("Two PRs")])
    assert asked == []


# -- R2: "On it" ------------------------------------------------------------------------------------

def test_an_ack_that_reads_the_ask_back_becomes_on_it():
    ask = "Find a waterproof rain jacket under $100"
    assert HC.acknowledgement("On it — finding a rain jacket under $100 in waterproof fabric.", ask) == "On it."
    assert HC.acknowledgement("Looking for a waterproof rain jacket under $100.", ask) == "On it."
    assert HC.acknowledgement("On it.", ask) == "On it."
    assert HC.acknowledgement("Looking now.", ask) == "Looking now."
    assert HC.acknowledgement("Checking tomorrow's forecast.", "Find tomorrow's forecast for the user's town") \
        == "Checking tomorrow's forecast."                                          # short: a few words of its own
    egg = HC.acknowledgement("Looking for a hardware store open Sunday near downtown. A soft-boiled egg takes about "
                             "6 minutes.", "Find a hardware store open Sunday near downtown")
    assert egg == "On it. A soft-boiled egg takes about 6 minutes."                 # the other answer stays


def test_parse_trims_a_parroting_start_reply():
    got = HC.parse({"tool": "start_job", "text": "", "args": {
        "request": "Book a table for four at an Italian place downtown Friday at 7",
        "reply": "Booking a table for four at an Italian place downtown Friday at 7."}}, {})
    assert got["action"] == "start" and got["reply"] == "On it."
    chat = HC.parse({"tool": "start_job", "text": "", "args": {"request": "Find a dentist open Saturday",
                                                               "reply": "Looking now."}}, {})
    assert chat["reply"] == "Looking now."


def test_the_start_line_never_explains_the_apps_mechanics():
    # Seen live on a video sent from Home: the coordinator read its own instruction back.
    said = "I can't see the picture or file you attached. Start a job so one can look at it"
    assert HC.acknowledgement(said, "") == HC.ACK
    assert HC.acknowledgement("Looking now.", "") == "Looking now."
    assert HC.acknowledgement("On it. I can't open the clip myself, so a job will look at it.", "") == "On it."
    # An answer that happens to mention files is kept.
    assert HC.acknowledgement("On it. The sharing link is in the files tab.", "") == "On it. The sharing link is in the files tab."


def test_a_start_with_no_request_still_gets_a_clean_line():
    parsed = HC.parse({"tool": "start_job", "args": {"reply": "I can't see the video you attached. Starting a job to look."},
                       "text": ""}, {})
    assert parsed and parsed["action"] == "start" and parsed["reply"] == HC.ACK


def test_the_suggested_acknowledgements_count_as_promising_work():
    # "Looking now." with no tool call must trigger the start-the-job retry, like "On it."
    for said in ("Looking now.", "Checking now.", "On it.", "Looking at it now.", "Checking."):
        assert HC.claims_work(said), said
    for said in ("Looking at the forecast, you won't need one.", "Nope, only a 2% chance of rain."):
        assert not HC.claims_work(said), said
