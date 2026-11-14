"""SPK-172 call replay, invented wording: one call, four different asks (is release 41 going public
for outside testers, what's happening with release 42, what's the error message waiting on me, go
ahead and push 41 to public). Each ask is its own task unless it is the same job, a request naming 41
never lands on the release-42 task, "those are two different jobs" really separates them, a progress
line never talks over a new acknowledgement, and the error-message question is never lost inside
another task. The voice writes numbers the way it hears them, often as words ("forty-one")."""
from __future__ import annotations

import contextlib
import json
import re
import time

import pytest

from fakes import SDP, FakeLiveWorker, FakeTransport, wait_for
from speakeasy import calls, recap, router
from test_routing import _progress_worker, _run_with_clock, _step, _task, spoken

ASK_41_PUBLIC = "Is release forty-one going public for outside testers?"
ASK_42 = "What's happening with release forty-two?"
ASK_ERROR = "What's the error message that's waiting on me?"
GO_41 = "I'm fine with us pushing forty-one to public now"
TWO_JOBS = "Those are two different jobs"


def model_says(follow_up=None, parts=None, status=None):
    return json.dumps({"status_task_id": status, "follow_up_task_id": follow_up, "conversation": None,
                       "conversation_part": None, "parts": parts or [], "channel": None, "show": False})


def open_tasks_in(messages):
    """(task id, its words) for each open task the routing model was shown."""
    text = messages[-1]["content"]
    return re.findall(r"(?m)^- ([A-Za-z0-9_-]+): (.*?) \(", text.split("\n\nChannels:")[0])


def asked(messages):
    return messages[-1]["content"].rsplit("Request: ", 1)[-1]


def start(home, route):
    from speakeasy.service import VoiceService
    workers: list[FakeLiveWorker] = []
    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport, route_call=route,
                       openai_negotiate=lambda key, payload: {"session": {"id": "s"}, "transport": {"sdp": "v=0\r\n"}},
                       openai_worker=lambda rt, i: workers.append(FakeLiveWorker(rt, i)) or workers[-1])
    svc.settings.patch({"voice": {"provider": "openai"}})
    svc.create_session({"sdp": SDP}, "req_spk172")
    return svc, workers[-1]


def say(worker, delegation_id, words, reply="On it."):
    worker.delegate(delegation_id, words)
    worker.feed({"type": "session.output_transcript.delta", "delta": reply, "start_ms": 3, "end_ms": 4})


def finished(worker, delegation_id):
    run = worker.interaction.runs.get(delegation_id)
    return run is not None and run.status == "completed"


def started(worker, delegation_id):
    """Its own task is under way: sent to Hermes, or held behind a running task (calls.wait_for_fence)."""
    run = worker.interaction.runs.get(delegation_id)
    return run is not None and bool(run.run_id or run.status == "waiting_on_task")


def asks(svc, worker):
    """Each task the call started, by handoff id: the request it runs on (what its card says)."""
    with worker.interaction.lock:
        runs = [(d, r.idem_key) for d, r in worker.interaction.runs.items() if r.status != "rejected"]
    return {d: svc.store.request_text(key) for d, key in runs}


def dropped(worker, delegation_id):
    run = worker.interaction.runs.get(delegation_id)
    return run.error if run is not None and run.status == "rejected" else None


# -- 1. merged asks ----------------------------------------------------------------------------

@pytest.mark.parametrize("parts", [
    [ASK_41_PUBLIC, ASK_42],
    ["What's the status of build sixty-eight?", "What's the status of build sixty-nine?"],
    ["Is build twelve green?", "Is build thirteen green?"],
    ["What's happening with release 41?", "What's happening with release forty-two?"],
])
def test_two_releases_said_as_words_in_one_breath_stay_two_tasks(parts):
    """The routing model split them right; the same-subject merge saw "release forty" in both (the
    digits-only number check never fires on spoken numbers) and glued them into one task."""
    d = router.parse_decision(model_says(parts=parts), " And ".join(parts), [], [])
    assert d is not None
    assert d.parts == [router.Part(router.NEW, p) for p in parts]


@pytest.mark.parametrize("parts", [
    ["status of release forty-one?", "what's still left on release 41?"],
    ["status of release 41?", "what's still left on release 41?"],
])
def test_one_release_said_two_ways_in_one_breath_is_one_task(parts):
    said = " And ".join(parts)
    d = router.parse_decision(model_says(parts=parts), said, [], [])
    assert d is not None
    assert d.parts == [router.Part(router.NEW, said)]


def test_a_lone_one_is_a_word_not_a_number():
    assert recap.subject("Fix the one you mentioned") == {"fix", "mentioned"}


def test_one_inside_a_spoken_number_is_part_of_it():
    assert recap.subject("status of build twenty-one") == recap.subject("status of build twenty one") == {"build", "21"}


@pytest.mark.parametrize("said, written", [
    ("release forty-one", "release 41"),
    ("build twelve", "build 12"),
    ("Is the build done by the twenty-first?", "Is the build done by the twenty-first?"),
    ("Push version two point three to public", "Push version two point three to public"),
    ("push one twenty-three now", "push one twenty-three now"),
    ("release ten forty-one", "release ten forty-one"),
    ("check again at four thirty", "check again at four thirty"),
])
def test_only_a_number_said_on_its_own_becomes_digits(said, written):
    """An ordinal, a decimal, an hour-and-minutes or a hundreds number is never turned into a different
    number ("20-first", "2 point 3", "one 23"): it stays as the words that were said."""
    assert recap.digits(said) == written


def test_a_date_said_as_an_ordinal_is_not_merged_with_a_build_of_that_number():
    parts = ["What's the status of build 20?", "Is the build done by the twenty-first?"]
    d = router.parse_decision(model_says(parts=parts), " And ".join(parts), [], [])
    assert d is not None
    assert d.parts == [router.Part(router.NEW, p) for p in parts]


@pytest.mark.parametrize("earlier, again", [
    ("what are the top three blockers on the checkout flow", "status of the top five blockers on the checkout flow"),
    ("fix the two flaky tests", "where are we on the flaky tests for build twelve"),
])
def test_a_repeat_status_ask_with_a_different_count_reuses_the_task_that_knows(earlier, again):
    """A count said as a word ("three blockers") is not a build number: the repeat ask goes to the
    earlier task, as it did before spoken numbers were read."""
    task = recap.Earlier("k", "t_earlier", earlier, "completed", created=9_940.0, settled_at=9_970.0)
    assert recap.pick(again, [task], 10_000.0) == task


PUSH_IT = "I'm fine with us pushing that to public now"


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="SPK-172: 'two different jobs' starts no task yet")
def test_saying_they_are_two_different_jobs_gives_the_publish_its_own_task(home, hermes):
    """After the publish was glued onto the release-42 task, "those are two different jobs" must
    start the publish as a task of its own (the voice said "separating them now"). The publish
    names no release here, so routing may glue it: the separation is what is tested."""
    def route(messages):
        tasks = open_tasks_in(messages)
        if asked(messages).startswith(PUSH_IT):
            return model_says(follow_up=tasks[-1][0])
        if asked(messages).startswith(TWO_JOBS):
            # The routing prompt: pushing back on what the assistant just said is a follow-up to that task.
            return model_says(follow_up=tasks[-1][0])
        return model_says(parts=[asked(messages)])

    svc, worker = start(home, route)
    try:
        say(worker, "call_r42", ASK_42)
        wait_for(lambda: finished(worker, "call_r42"), timeout=10)
        say(worker, "call_go", PUSH_IT, reply="Adding that to release forty-two.")
        wait_for(lambda: finished(worker, "call_go"), timeout=10)
        assert asks(svc, worker)["call_go"] == f"{PUSH_IT} (following up: {ASK_42})"  # glued: the starting point
        before = len(hermes.calls)
        say(worker, "call_two", TWO_JOBS, reply="Separating them now.")
        wait_for(lambda: len(hermes.calls) > before or hermes.steers or dropped(worker, "call_two"), timeout=10)
        with contextlib.suppress(AssertionError):
            wait_for(lambda: PUSH_IT in asks(svc, worker).values(), timeout=5)
        assert PUSH_IT in asks(svc, worker).values(), asks(svc, worker)
    finally:
        svc.close()


# -- 2. wrong build ----------------------------------------------------------------------------
# Every way an existing task gets picked: the routing model's follow_up_task_id, the task id the
# voice marked, and the focused task when the routing model gives no answer.

def _via_model(words, tasks):
    return router.decide(words, tasks, call=lambda m: model_says(follow_up=tasks[-1].task_id))


def _via_mark(words, tasks):
    return router.decide(words, tasks, marked_task_id=tasks[-1].task_id, call=lambda m: None)


def _via_focus(words, tasks):
    return router.decide(words, tasks, call=lambda m: None, focus=router.Focus(tasks[-1].task_id))


PICKS = {"model": _via_model, "marked": _via_mark, "focus": _via_focus}
FOLLOWED_UP_42 = f"push it to public now (following up: {ASK_42})"  # a follow-up run's request (calls.follow_up)

# Must get its own task: the request names another build than the one the task is about.
OTHER_NUMBER = [
    (ASK_42, GO_41),
    (ASK_42, "I'm fine with us pushing 41 to public now"),
    ("What's happening with release 42?", GO_41),
    ("build sixty-eight progress", "push sixty-seven to public now"),
    ("What's happening with release forty two?", "I'm fine with us pushing forty one to public now"),
    (ASK_42, "go ahead with forty-one"),
    (ASK_42, "Is forty-one going public for outside testers?"),
    (ASK_42, "Ship forty-one for the testers"),
    (ASK_42, "push forty-one tomorrow"),
    (ASK_42, "what about forty-one"),
    (ASK_42, "do the same for forty-one"),
    (ASK_42, "roll back to forty-one"),
    (ASK_42, "roll back to forty-one instead"),
    (ASK_42, "forty-one's ready to go"),
    (ASK_42, "push forty-one for the outside testers"),
    (ASK_42, "show me forty-one"),
    (ASK_42, "I'm fine with forty-one going public"),
    (ASK_42, "Forty-one's good to go"),
    (ASK_42, "and what about forty-one?"),
    (ASK_42, "same for forty-one"),
    (ASK_42, "check forty-one's logs"),
    (ASK_42, "ship forty-one tomorrow"),
    (ASK_42, "push 41 for the outside testers"),
    (ASK_42, "what about 41?"),
    (ASK_42, "forty-one looks good, push it"),
    (ASK_42, "push forty-one for outside testers"),
    (ASK_42, "what about release forty-one"),
    # The new task does not carry release 42's context ("that fix"): out of this round (see the plan).
    (ASK_42, "backport that fix to release forty-one"),
    (FOLLOWED_UP_42, "what about forty-one"),
]

# Must stay on the task: the request names the task's own build, no build, or a number that counts,
# dates or times something.
SAME_TASK = [
    (ASK_42, "push forty-two to public now"),
    (ASK_42, "42 is fine to push"),
    (ASK_42, "push it to public now"),
    ("book a table for 4", "make it six"),
    ("set the thermostat to 72", "set it to 70"),
    (ASK_42, "give me the top three blockers"),
    (ASK_42, "check again in ten minutes"),
    (ASK_42, "the 4:15 then"),
    (ASK_42, "push the one you mentioned to public"),
    (ASK_42, "push forty-two at nine thirty"),
    (ASK_42, "the four fifteen then"),
    (ASK_42, "ok, the four thirty then"),
    (ASK_42, "let's do four thirty"),
    (ASK_42, "four fifteen then"),
    (ASK_42, "check again at four thirty"),
    (ASK_42, "push it to public at four thirty"),
    (ASK_42, "check again in 40 minutes"),
    (ASK_42, "check again in forty minutes"),
    (ASK_41_PUBLIC, "go with option two"),
    (ASK_41_PUBLIC, "merge all three"),
    (ASK_41_PUBLIC, "do the other two"),
    (ASK_41_PUBLIC, "let's do two"),
    (ASK_41_PUBLIC, "fifty is fine"),
    (ASK_42, "go with number two"),
    (ASK_42, "push it live on November 12"),
    (ASK_42, "push it public on May 12"),
    (ASK_42, "push it public on June 3"),
    (ASK_42, "make sure it works on iOS 18"),
    (ASK_42, "check it on iOS 18 too"),
    (ASK_42, "test it on Python 3.12 as well"),
    (ASK_42, "ship it in Q3"),
    (ASK_42, "send it to all 30"),
    (ASK_42, "we have 3"),
    (ASK_42, "5 is enough"),
    (ASK_42, "we need three"),
    (ASK_42, "add twenty to TestFlight"),
    (ASK_42, "wait ten then push"),
    (ASK_42, "wait five and then push it"),
    (ASK_42, "step 2 is failing now"),
    (ASK_42, "compare it with the last three builds"),
    ("How many outside testers for release forty-two?", "Three is enough"),
    ("Find flights to Paris on June 3", "make it June 5"),
    ("Book a flight to Denver on May 12", "Actually change it to May 14"),
    ("Book a flight to Rome on March 20", "Actually, move it to March 22"),
    ("Book room 204", "actually room 210"),
    ("find a hotel, budget 300", "budget 400 then"),
    ("Reserve Nopa 7 tonight", "let's do 8 instead"),
    ("upgrade the repo to Python 3.11", "use Python 3.12 instead"),
    ("Check if PR 12 and 13 pass", "and thirteen?"),
    ("What's happening with build 123?", "build one twenty-three"),
    ("release 123", "push one twenty-three now"),
    ("release 1041", "release ten forty-one"),
    ("release version 2.3", "Push version two point three to public"),
    ("What's happening with release 2.4?", "push two point four to public now"),
    ("no, I meant build forty-three (following up: rerun build forty-two)", "push forty-three now"),
    # Known wrong attaches, kept: saying the task's own number wins ("not forty-two" is not read), and a
    # task that never names its build with a word like "release" is about no build.
    (ASK_42, "Push forty-one, not forty-two"),
    ("Is forty-two going out today?", "go ahead with forty-one"),
]

# Corrections: the running job should switch, so the words stay on it.
CORRECTIONS = [
    ("rerun build forty-two", "no, I meant forty-three"),
    ("rerun build forty-two", "sorry, forty-three"),
    (ASK_42, "no, switch it to forty-one"),
    ("deliver to apartment 5", "sorry, apartment 6"),
]

# A task the routing model is never asked about: the fragment rule picks the newest task first.
PICKED_BEFORE_THE_MODEL = {"and thirteen?": "fragment"}


def _source(how, words):
    return PICKED_BEFORE_THE_MODEL.get(words, how) if how == "model" else how


@pytest.mark.parametrize("how", PICKS)
@pytest.mark.parametrize("task, words", OTHER_NUMBER)
def test_a_request_naming_another_release_never_joins_the_task(how, task, words):
    d = PICKS[how](words, [router.OpenTask("call_r42", task, "running", "", 40.0)])
    assert d.parts == [router.Part(router.NEW, words)], d


@pytest.mark.parametrize("how", PICKS)
@pytest.mark.parametrize("task, words", SAME_TASK)
def test_a_request_naming_the_same_release_or_none_still_joins_the_task(how, task, words):
    d = PICKS[how](words, [router.OpenTask("call_r42", task, "running", "", 40.0)])
    assert (d.parts, d.source) == ([router.Part("follow_up", words, "call_r42")], _source(how, words)), d


@pytest.mark.parametrize("how", PICKS)
@pytest.mark.parametrize("task, words", CORRECTIONS)
def test_a_correction_stays_on_the_task_it_corrects(how, task, words):
    d = PICKS[how](words, [router.OpenTask("call_r42", task, "running", "", 40.0)])
    assert (d.parts, d.source) == ([router.Part("follow_up", words, "call_r42")], how), d


@pytest.mark.parametrize("how", PICKS)
def test_a_request_kept_off_another_builds_task_says_so(how):
    """The decision says the request was kept off the task that was picked, not that the voice's mark or
    the focus decided where it went (calls.py logs the source)."""
    d = PICKS[how](GO_41, [router.OpenTask("call_r42", ASK_42, "running", "", 40.0)])
    assert (d.parts, d.source) == ([router.Part(router.NEW, GO_41)], "kept_apart"), d


def test_the_call_replay_publish_never_queues_with_the_release_42_work(home, hermes):
    """Release 42's status ran and finished; then "go ahead and push forty-one" came back from the
    routing model as a follow-up to it. What the person saw: the publish ran as "<words> (following
    up: what's happening with release forty-two)" and the voice was told it was added to that task."""
    def route(messages):
        tasks = open_tasks_in(messages)
        if asked(messages).startswith(GO_41) and tasks:
            return model_says(follow_up=tasks[-1][0])
        return model_says(parts=[asked(messages)])

    svc, worker = start(home, route)
    try:
        say(worker, "call_r42", ASK_42)
        wait_for(lambda: finished(worker, "call_r42"), timeout=10)
        say(worker, "call_go41", GO_41)
        wait_for(lambda: started(worker, "call_go41"), timeout=10)
        assert (asks(svc, worker), worker.last_routed) == ({"call_r42": ASK_42, "call_go41": GO_41}, None)
    finally:
        svc.close()


def test_a_publish_naming_no_release_still_joins_the_finished_release_42_task(home, hermes):
    def route(messages):
        tasks = open_tasks_in(messages)
        if asked(messages).startswith(PUSH_IT) and tasks:
            return model_says(follow_up=tasks[-1][0])
        return model_says(parts=[asked(messages)])

    svc, worker = start(home, route)
    try:
        say(worker, "call_r42", ASK_42)
        wait_for(lambda: finished(worker, "call_r42"), timeout=10)
        say(worker, "call_go", PUSH_IT)
        wait_for(lambda: started(worker, "call_go"), timeout=10)
        assert asks(svc, worker)["call_go"] == f"{PUSH_IT} (following up: {ASK_42})"
        assert (worker.last_routed or {}).get("task_id") == "call_r42"
    finally:
        svc.close()


def test_the_publish_is_never_lined_up_with_a_running_release_42_task(home, hermes):
    """The same decision while release 42's task is still running: the publish is steered into it
    and the voice is told it is "lined up for the task already working on" release 42 (on the call:
    "queued that with the sixty-eight work")."""
    hermes.hold = True

    def route(messages):
        tasks = open_tasks_in(messages)
        if asked(messages).startswith(GO_41) and tasks:
            return model_says(follow_up=tasks[-1][0])
        return model_says(parts=[asked(messages)])

    svc, worker = start(home, route)
    try:
        say(worker, "call_r42", ASK_42)
        wait_for(lambda: started(worker, "call_r42"), timeout=10)
        say(worker, "call_go41", GO_41)
        # Its own task either starts or is held behind release 42 (calls.wait_for_fence); a steer or an
        # "added to" announcement is the wrong-build outcome.
        wait_for(lambda: started(worker, "call_go41") or hermes.steers or worker.last_routed, timeout=10)
        got = (asks(svc, worker), hermes.steers, worker.last_routed)
        assert got == ({"call_r42": ASK_42, "call_go41": GO_41}, [], None), got
    finally:
        svc.close()


# -- 4. spliced speech -------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, raises=AssertionError, reason="SPK-172: a task's progress line is spoken over the voice's acknowledgement")
def test_a_progress_line_waits_while_the_voice_acknowledges_a_new_request(service):
    """The person just asked for the publish and the voice is saying "Got it, checking that publish
    now" when the release-42 task reports a step. The call pulse holds back in that moment
    (someone_speaking / user_mid_thought); the progress line from a task's own status does not."""
    worker = _progress_worker(service)
    backend = _task(service, "idem_r42", "call_r42")

    def go(clock):
        clock[0] = backend.started + 50
        worker.fragments.append({"speaker": "user", "text": GO_41, "at": clock[0] - 1.0, "start_ms": 0, "end_ms": 0})
        worker.last_handoff_at = clock[0] - 0.5
        worker.fragments.append({"speaker": "assistant", "text": "Got it, checking that publish",
                                 "at": clock[0], "start_ms": 0, "end_ms": 0})
        _step(worker, backend, clock, 50, "Checking the error-message request", "One error-message request open")
    _run_with_clock(go)
    assert spoken(worker) == []


# -- 5. unanswered question --------------------------------------------------------------------

@pytest.mark.xfail(strict=True, raises=AssertionError, reason="SPK-172: a question said during the settle wait is absorbed into the "
                                       "publish and never answered")
def test_a_question_said_right_after_the_publish_is_not_lost_inside_it(home, hermes, monkeypatch):
    """"Go ahead and push forty-one" (rightly a follow-up to the release-41 task), then straight away
    "what's the error message that's waiting on me?". The settle wait joins the question onto the
    publish, the whole thing goes to the release-41 task, and the question's own handoff is dropped as
    already absorbed: nothing answers it on its own."""
    real_settle = calls.SidebandWorker.settle

    async def person_keeps_talking(self, request):
        if request.startswith(GO_41):
            self.loop.call_later(0.3, lambda: self.fragments.append(
                {"speaker": "user", "text": ASK_ERROR, "at": time.monotonic(), "start_ms": 0, "end_ms": 0}))
        return await real_settle(self, request)

    monkeypatch.setattr(calls.SidebandWorker, "settle", person_keeps_talking)

    def route(messages):
        tasks = dict(open_tasks_in(messages))
        if asked(messages).startswith(GO_41):
            return model_says(follow_up=next(t for t, words in tasks.items() if "forty-one" in words))
        return model_says(parts=[asked(messages)])

    svc, worker = start(home, route)
    try:
        say(worker, "call_r41", ASK_41_PUBLIC)
        wait_for(lambda: finished(worker, "call_r41"), timeout=10)
        say(worker, "call_go41", GO_41)
        wait_for(lambda: len(hermes.calls) == 2, timeout=10)
        say(worker, "call_err", ASK_ERROR)
        wait_for(lambda: "call_err" in worker.interaction.runs, timeout=10)
        time.sleep(2.0)
        got = (dropped(worker, "call_err"), asks(svc, worker)["call_go41"])
        assert got == (None, f"{GO_41} (following up: {ASK_41_PUBLIC})"), got
    finally:
        svc.close()
