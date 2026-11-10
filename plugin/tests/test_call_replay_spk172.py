"""SPK-172 call replay, invented wording: one call, four different asks (is release 41 going public
for outside testers, what's happening with release 42, what's the error message waiting on me, go
ahead and push 41 to public). Each ask is its own task unless it is the same job, a request naming 41
never lands on the release-42 task, "those are two different jobs" really separates them, a progress
line never talks over a new acknowledgement, and the error-message question is never lost inside
another task. The voice writes numbers the way it hears them, often as words ("forty-one")."""
from __future__ import annotations

import json
import re
import time

from fakes import SDP, FakeLiveWorker, FakeTransport, wait_for
from speakeasy import calls, router
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


def asks(svc, worker):
    """Each task the call started, by handoff id: the request it runs on (what its card says)."""
    with worker.interaction.lock:
        runs = [(d, r.idem_key) for d, r in worker.interaction.runs.items() if r.status != "rejected"]
    return {d: svc.store.request_text(key) for d, key in runs}


def dropped(worker, delegation_id):
    run = worker.interaction.runs.get(delegation_id)
    return run.error if run is not None and run.status == "rejected" else None


def notes(worker):
    return [c for _, _, c in worker.sent]


# -- 1. merged asks ----------------------------------------------------------------------------

def test_two_releases_said_as_words_in_one_breath_stay_two_tasks():
    """The routing model split them right; the same-subject merge saw "release forty" in both (the
    digits-only number check never fires on spoken numbers) and glued them into one task."""
    said = f"{ASK_41_PUBLIC} And {ASK_42}"
    d = router.parse_decision(model_says(parts=[ASK_41_PUBLIC, ASK_42]), said, [], [])
    assert d is not None
    assert [p.request for p in d.parts] == [ASK_41_PUBLIC, ASK_42]


def test_saying_they_are_two_different_jobs_gives_the_publish_its_own_task(home, hermes):
    """After the publish was glued onto the release-42 task, "those are two different jobs" must
    start the publish as a task of its own (the voice said "separating them now")."""
    def route(messages):
        tasks = open_tasks_in(messages)
        if asked(messages).startswith(GO_41):
            return model_says(follow_up=tasks[-1][0])  # the call's bad decision, as the model's JSON
        if asked(messages).startswith(TWO_JOBS):
            # The routing prompt: pushing back on what the assistant just said is a follow-up to that task.
            return model_says(follow_up=tasks[-1][0])
        return model_says(parts=[asked(messages)])

    svc, worker = start(home, route)
    try:
        say(worker, "call_r42", ASK_42)
        wait_for(lambda: finished(worker, "call_r42"), timeout=10)
        say(worker, "call_go41", GO_41, reply="Adding that to release forty-two.")
        wait_for(lambda: len(hermes.calls) == 2, timeout=10)
        wait_for(lambda: finished(worker, "call_go41"), timeout=10)
        assert asks(svc, worker)["call_go41"] != GO_41  # glued: the setup this test starts from
        before = len(hermes.calls)
        say(worker, "call_two", TWO_JOBS, reply="Separating them now.")
        wait_for(lambda: len(hermes.calls) > before or hermes.steers or dropped(worker, "call_two"), timeout=10)
        time.sleep(1.0)
        assert GO_41 in asks(svc, worker).values(), asks(svc, worker)
    finally:
        svc.close()


# -- 2. wrong build ----------------------------------------------------------------------------

def test_a_router_follow_up_from_release_41_to_the_release_42_task_is_refused():
    open_42 = [router.OpenTask("call_r42", ASK_42, "running", "", 40.0)]
    for words in (GO_41, "I'm fine with us pushing 41 to public now"):
        d = router.parse_decision(model_says(follow_up="call_r42"), words, open_42, [])
        assert d is None or d.parts[0].task_id != "call_r42", d


def test_a_voice_marked_release_42_task_never_takes_a_release_41_request():
    open_42 = [router.OpenTask("call_r42", ASK_42, "running", "", 40.0)]
    d = router.decide(GO_41, open_42, marked_task_id="call_r42", call=lambda m: None)
    assert d.parts[0].task_id != "call_r42", d


def test_the_call_replay_publish_never_queues_with_the_release_42_work(home, hermes):
    """Release 42's status ran and finished; then "go ahead and push forty-one" came back from the
    routing model as a follow-up to it. What the person sees: the publish ran as "<words> (following
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
        wait_for(lambda: len(hermes.calls) == 2, timeout=10)
        assert asks(svc, worker) == {"call_r42": ASK_42, "call_go41": GO_41}
        assert not [n for n in notes(worker) if "added to the task already working on" in n], notes(worker)
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
        wait_for(lambda: (worker.interaction.runs.get("call_r42") or calls.BackendRun("x", 0, "x")).run_id,
                 timeout=10)
        say(worker, "call_go41", GO_41)
        wait_for(lambda: len(hermes.calls) == 2 or hermes.steers, timeout=10)
        time.sleep(0.5)
        lined_up = [n for n in notes(worker) if "lined up for the task already working on" in n]
        got = (asks(svc, worker), [words for _, words in hermes.steers], lined_up)
        assert got == ({"call_r42": ASK_42, "call_go41": GO_41}, [], []), got
    finally:
        svc.close()


# -- 4. spliced speech -------------------------------------------------------------------------

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
