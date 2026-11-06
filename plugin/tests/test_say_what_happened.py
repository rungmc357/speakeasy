"""The voice says only what really happened: a task's progress stays in progress ("submitting" is never
"submitted" until the result says so), no "picking up where we left off" on a task's first update, no
task title read aloud, and an ask the user took back never has its result read out. Invented calls only."""
from __future__ import annotations

import asyncio
import time

from fakes import wait_for
from speakeasy import calls
from speakeasy import router
from speakeasy.prompt import builder as P
from test_call_fixes import notes, say, spoken, start_call, tasks, voice
from test_call_pulse import clock, setup, tick  # noqa: F401  (clock is a fixture)


def status(worker, backend, line, detail):
    event = {"event": "message.interim", "text": f"STATUS: {line}\nDETAIL: {detail}"}
    asyncio.run(worker._handle_hermes_event(backend, event))


def voice_says(worker, words):
    async def go():
        await worker.handle_event({"type": "session.output_transcript.delta", "delta": words,
                                   "start_ms": 1, "end_ms": 2})
        for _ in range(20):
            await asyncio.sleep(0)
        if worker.dispatch_tasks:
            await asyncio.gather(*list(worker.dispatch_tasks))
    asyncio.run(go())


# -- 1. progress is relayed as in progress -----------------------------------------------------------

def test_a_submitting_note_reaches_the_voice_as_still_in_progress(service, clock):
    worker, backend = setup(service, clock, request="Submit build 50 for outside testing")
    status(worker, backend, "Submitting build 50", "Filling in the test information and submitting build 50")
    note = next(n for n in worker.sent if n[0] == "session.thinking.append" and "submitting build 50" in n[2])[2]
    assert "still in progress" in note and "\"submitting\" is not \"submitted\"" in note
    assert backend.last_progress.startswith("Submitting build 50")


def test_the_voice_calling_a_running_step_done_is_corrected_once(service, clock):
    worker, backend = setup(service, clock, request="Submit build 50 for outside testing")
    status(worker, backend, "Submitting build 50", "Filling in the test information and submitting build 50")
    voice_says(worker, "Good news, the build is submitted with the test info.")
    said = spoken(worker)
    assert said == ["Correction: it isn't submitted yet. I'm still submitting build 50."]
    voice_says(worker, "Like I said, it's submitted.")
    assert len(spoken(worker)) == 1  # once per task, never a running argument


def test_saying_it_will_be_submitted_is_not_an_overclaim():
    progress = "Submitting build 50. Filling in the test information"
    assert P.overclaimed("I'll tell you once it's submitted.", progress) is None
    assert P.overclaimed("It isn't submitted yet, still filling it in.", progress) is None
    assert P.overclaimed("The build is submitted.", progress) == "submitted"
    assert P.overclaimed("The build is submitted.", "Reading the release notes") is None  # nothing to contradict


def test_a_spoken_update_on_a_running_task_never_says_done():
    assert P.claims_done("The form is submitted") and P.claims_done("It's live now") and P.claims_done("All done.")
    assert not P.claims_done("Submitting build 50") and not P.claims_done("Pushed the branch, running the tests")
    # The recap claimed done: only the card's own progress line is said.
    assert P.in_progress_line("Checking the confirmation email. The form is submitted.",
                              "Checking the confirmation email") == "Checking the confirmation email."
    assert P.in_progress_line("The build is live.", "The build is live") is None
    assert P.in_progress_line("Running the tests.", "Running the tests") == "Running the tests."


def test_the_pulse_drops_a_done_claim_while_the_task_runs(service, clock):
    worker, backend = setup(service, clock, request="Submit the expense form")
    status(worker, backend, "Checking the confirmation email", "The expense form is submitted")
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker)
    said = spoken(worker)[-1]
    assert said == "Checking the confirmation email." and "submitted" not in said


def test_status_answers_for_a_running_task_keep_the_tense():
    text = P.status_answer("the build", "running", ["Submitting build 50"], 90, None)
    assert "still in progress" in text and "never say it is done, submitted" in text
    done = P.status_answer("the build", "completed", [], 90, "It went in.")
    assert "still in progress" not in done


def test_the_voice_prompt_says_progress_is_not_a_result():
    rules = P.rules_text(P.Names.from_settings({}))
    assert "keep their tense" in rules
    assert "only a final result may say done" in rules


# -- 2. no fake "picking up where we left off" -------------------------------------------------------

def test_no_resume_phrase_on_a_tasks_first_update(service, clock):
    worker, backend = setup(service, clock, request="Find why playback stutters on the TV")
    status(worker, backend, "Picking up where I left off", "Checking the play queue on the TV app")
    clock.t += calls.PULSE_EVERY_S + 1
    assert tick(worker)
    said = spoken(worker)[-1]
    assert said == "Checking the play queue on the TV app.", said


def test_resume_openers_are_stripped_from_spoken_lines():
    assert P.without_resume_lead("Picking up where we left off: checking the queue.") == "Checking the queue."
    assert P.without_resume_lead("Back on the trip planning, the hotels look full.") == "The hotels look full."
    assert P.without_resume_lead("Picking up where I left off on playback") is None
    assert P.without_resume_lead("Background checks are running") == "Background checks are running"
    assert "left off" not in P.status_note("Picking up where I left off: reading the logs")


def test_the_prompt_allows_a_resume_phrase_only_when_really_returning():
    rules = P.rules_text(P.Names.from_settings({}))
    assert "once per call at most, never on a first update" in rules
    assert "picking up where you left off" not in P.continuing_in_note("your earlier chat")
    assert "left off" not in P.resume_block([], P.Names.from_settings({}))


# -- 3. no task title read aloud -------------------------------------------------------------------

def test_task_titles_become_a_few_words_about_the_subject():
    assert P.spoken_subject("Plan remaining work for build sixty-seven") == "build sixty-seven"
    assert P.spoken_subject("Update the pricing page copy") == "the pricing page copy"
    assert P.spoken_subject("Speakeasy build 66: why task cards freeze") == "Speakeasy build 66"
    assert P.spoken_subject("Check why the nightly backup job keeps failing on the second drive") == \
        "the nightly backup job"
    assert P.spoken_subject("Look into playback stutter on the TV") == "playback stutter"
    for subject in ("build sixty-seven", "the pricing page copy"):
        assert P.spoken_subject(subject) == subject  # passing a subject in again leaves it alone


def test_adding_to_a_task_names_its_subject_not_its_title():
    note = P.adding_to_note("Plan remaining work for build sixty-seven", say=True)
    assert "Adding that to build sixty-seven." in note and "Plan remaining work" not in note
    assert "never the task's title" in note
    moved = P.moved_note("Fix the dashboard chart colors")
    assert "Moved it to the dashboard chart colors." in moved and "Fix the dashboard" not in moved


def test_the_prompt_never_reads_a_title():
    rules = P.rules_text(P.Names.from_settings({}))
    assert "never read a task's or conversation's title aloud" in rules


# -- 4. a taken-back ask's result stays quiet --------------------------------------------------------

def test_not_talking_to_you_takes_the_ask_back():
    for said in ("Sorry, I wasn't talking to you", "that wasn't for you", "I was talking to my friend"):
        assert router.meta_kind(said) == router.META_UNDO, said
    assert router.meta_kind("I wasn't talking to you, check the weather in Denver") is None


def test_a_retracted_asks_result_is_never_read_aloud(server, service, hermes):
    hermes.hold = True
    hermes.responder = lambda prompt, sid: ("The sudo prompt is now a masked pop-up.\n"
                                            "DONE: Masked pop-up\nSPOKEN: The sudo prompt is now a masked pop-up.")
    _, worker = start_call(server, service, "req_swh_1")
    say(worker, "call_1", "Make the sudo prompt a masked pop-up", 10)
    task = wait_for(lambda: [t for t in tasks(server) if t.get("run_id")])[0]
    voice(worker, "On it.", 20)
    say(worker, "call_2", "Sorry, I wasn't talking to you", 30)
    wait_for(lambda: any("taken back" in n.lower() or "taken back" in n for n in notes(worker)))
    for run in hermes.runs.values():
        run["done"].set()
    time.sleep(1.5)
    said = " ".join(spoken(worker))
    assert "masked" not in said and "Stopped" not in said, spoken(worker)
    run = next(r for r in worker.interaction.runs.values() if r.run_id == task["run_id"])
    assert run.retracted


def test_retracted_result_note_is_background_only():
    note = P.retracted_result_note("Make the sudo prompt a masked pop-up", "The sudo prompt is a masked pop-up.")
    assert note.startswith("Background, do not say this") and "say any work was done" in note
