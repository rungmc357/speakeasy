"""Corrections stick: a misheard name the user corrects ("no, not Man Pros, Mac Pro", "I meant
Mac Pro", a typed "mac pro") rewrites the task's request and title, reaches the running task at
once as a correction (never a queued follow-up), and nothing written around the misheard words is
said again. The user's own names (machines, models) reach the voice, the task brief and the STT
provider. Invented data only."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from fakes import wait_for
from speakeasy import calls, corrections as C, transcribe as T, vocabulary as V
from speakeasy.prompt import builder as P
from test_routing import spoken, start_call, tasks
from test_typing_together import typed_to_job

REQUEST = "The second Man Pros gets here Friday, plan what runs on it"
NAMES = ["Mac Pro", "Qwen", "DeepSeek", "cobalt"]


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)
    monkeypatch.setattr(calls, "FOLLOW_UP_POLL_S", 0.1)
    monkeypatch.setattr(calls, "STEER_RETRY_S", 0.05)
    V._cache.clear()


# -- detecting a correction -----------------------------------------------------------------------

@pytest.mark.parametrize("words, typed, wrong, right", [
    ("No, not Man Pros, Mac Pro", False, "Man Pros", "Mac Pro"),
    ("not Man Pros but Mac Pro", False, "Man Pros", "Mac Pro"),
    ("Mac Pro, not Man Pros, dude", False, "Man Pros", "Mac Pro"),
    ("I meant Mac Pro", False, "Man Pros", "Mac Pro"),
    ("no, Mac Pro", False, "Man Pros", "Mac Pro"),
    ("mac pro", True, "Man Pros", "Mac Pro"),
    ("The user corrected: it's Mac Pro, not Man Pros", False, "Man Pros", "Mac Pro"),
    # A half-fix is spelled the user's way when it is close to one of their names.
    ("No, not Man Pros, Max Pro", False, "Man Pros", "Mac Pro"),
    ("There is no Man Pros", False, "Man Pros", None),
])
def test_a_correction_names_the_misheard_words_and_the_fix(words, typed, wrong, right):
    fix = C.detect(words, REQUEST, typed=typed, vocabulary=NAMES)
    assert fix is not None and (fix.wrong, fix.right) == (wrong, right)
    if right:
        assert fix.request == REQUEST.replace("Man Pros", right)


@pytest.mark.parametrize("words, typed", [
    ("and also check the weather in Denver", False),
    ("no thanks", False),
    ("no, turn them on", False),
    ("not that one, the blue one", False),
    ("make sure it's not broken", False),
    ("yes", True),
    ("the logs too", True),
    ("blue one", True),
    ("Could the small one run the test jobs overnight", False),
])
def test_other_words_are_not_corrections(words, typed):
    assert C.detect(words, REQUEST, typed=typed, vocabulary=NAMES) is None


def test_a_correction_said_again_is_only_confirmed():
    fixed = REQUEST.replace("Man Pros", "Mac Pro")
    again = C.detect("mac pro", fixed, typed=True, vocabulary=NAMES, known_wrong=["Man Pros"])
    assert again is not None and again.wrong is None and again.right == "Mac Pro"
    assert C.detect("mac pro", fixed, typed=True, vocabulary=NAMES) is None  # never corrected: more words
    # Plurals and extra words around a name the task already has: still only a repeat.
    for words in ("like the Max Pros, dude", "mac pros"):
        again = C.detect(words, fixed, typed=True, vocabulary=NAMES, known_wrong=["Man Pros"])
        assert again is not None and again.wrong is None
    told = C.detect("There is no Man Pros", fixed, vocabulary=NAMES, known_wrong=["Man Pros"])
    assert told is not None and told.wrong is None
    assert C.detect("no, not Man Pros, Mac Pro", fixed, vocabulary=NAMES, known_wrong=["Man Pros"]).wrong is None


def test_rewrite_and_mentions():
    assert C.rewrite("Plan the second Man Pros", "Man Pros", "Mac Pro") == "Plan the second Mac Pro"
    assert C.rewrite("Keep cobalt for Quinn.", "Quinn", "Qwen") == "Keep cobalt for Qwen."
    assert C.mentions("Searching the code for Man Pro", ["Man Pros"])
    assert not C.mentions("Searching the code for Mac Pro", ["Man Pros"])


# -- the user's names ------------------------------------------------------------------------------

def test_names_come_from_the_users_own_hermes(tmp_path):
    (tmp_path / "memories").mkdir()
    (tmp_path / "memories" / "MEMORY.md").write_text(
        "Routing: helper lanes -> cobalt; big models on juniper. cobalt=Qwen, juniper=DeepSeek.\n")
    config = {"custom_providers": [{"name": "Mac Pro (local)", "base_url": "http://127.0.0.1:8000/v1"},
                                   {"name": "Garage box", "base_url": "http://juniper.tail0.ts.net:9000/v1"}],
              "providers": {"x": {"base_url": "http://10.0.0.5:1234/v1"}}}
    names = V.gather(tmp_path, config=config)
    for name in ("Mac Pro", "Garage box", "juniper", "cobalt", "Qwen", "DeepSeek"):
        assert name in names
    assert "lanes" not in names and "10" not in " ".join(names) and "Gemini" not in names
    assert len(names) <= V.MAX_TERMS


def test_the_voice_and_the_task_get_the_names(tmp_path):
    names = P.Names("Hermes", "Sam")
    voice = P.build_live_instructions(names, vocabulary=NAMES)
    assert "# Names Sam uses" in voice and "Mac Pro, Qwen, DeepSeek, cobalt" in voice
    assert "check it in one short line before starting" in voice
    task = P.build_task_prompt(names, 1, "User: hi", names_hint=P.task_vocabulary_line(names, NAMES))
    assert "can mishear names" in task and "cobalt" in task


def test_the_stt_provider_gets_the_names_only_for_speakeasy_clips():
    seen = {}

    def fake_transcribe(path, model, source):
        seen["prompt"] = T.pre_transcription(prompt="Base words.", source=source)
        return {"success": True, "transcript": "hello"}

    import sys
    import types
    module = types.ModuleType("tools.transcription_tools")
    module.transcribe_audio = fake_transcribe  # type: ignore[attr-defined]
    old = sys.modules.get("tools.transcription_tools")
    sys.modules["tools.transcription_tools"] = module
    try:
        run = T.hermes_transcriber(lambda: ["Mac Pro", "Qwen"])
        assert run is not None and run("/tmp/none.m4a")["success"]
    finally:
        if old is None:
            sys.modules.pop("tools.transcription_tools", None)
        else:
            sys.modules["tools.transcription_tools"] = old
    assert seen["prompt"] == {"prompt": "Base words. Names that may come up: Mac Pro, Qwen."}
    assert T.pre_transcription(prompt=None, source="gateway") is None  # anyone else's voice note: untouched


# -- on a call ------------------------------------------------------------------------------------

def _say(worker, delegation_id, words, **extra):
    worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 1, "end_ms": 2})
    worker.delegate(delegation_id, words, **extra)


def _running(server, service, hermes):
    hermes.hold = True
    _, worker = start_call(server, service)
    _say(worker, "call_plan", REQUEST)
    run = wait_for(lambda: next((r for r in worker.interaction.runs.values()
                                 if r.delegation_id == "call_plan" and r.run_id), None))
    service.store.set_title(run.idem_key, "Plan the second Man Pros")
    # The task got going on the misheard words: its own status says so.
    asyncio.run_coroutine_threadsafe(worker._handle_hermes_event(run, {
        "event": "message.interim",
        "text": "STATUS: Searching the code for Man Pros\nDETAIL: Looking for where Man Pros is set up"}),
        worker.loop).result(5)
    return worker, run


def _notes(worker):
    return [c for k, _, c in worker.sent if k == "session.thinking.append"]


def _card(service, run):
    return service.store.work(idem_key=run.idem_key) or {}


@pytest.mark.parametrize("words, marked", [
    ("No, not Man Pros, Mac Pro", True),
    ("No, not Man Pros, Mac Pro", False),   # the voice didn't say which task: still that one
    ("I meant Mac Pro", True),
])
def test_a_spoken_correction_rewrites_the_task_and_steers_it_now(server, service, hermes, words, marked):
    worker, run = _running(server, service, hermes)
    steers_before = len(hermes.steers)
    _say(worker, "call_fix", words, **({"task_id": "call_plan"} if marked else {}))
    steer = wait_for(lambda: hermes.steers[steers_before:])[0][1]
    assert steer.startswith("Correction from the user:") and '"Man Pros" was a mishearing of "Mac Pro"' in steer
    assert "Redo anything you based on" in steer and "The second Mac Pro gets here Friday" in steer
    # The task's request and title now say what was meant.
    assert service.store.request_text(run.idem_key).startswith("The second Mac Pro gets here Friday")
    assert service.store.title(run.idem_key) == "Plan the second Mac Pro"
    card = _card(service, run)
    assert "Man Pro" not in " ".join(str(card.get(k) or "") for k in ("title", "short_status", "detail", "now_line"))
    # Never a queued follow-up, and only one new run (the same task).
    assert not any(n.startswith("Queued:") for n in _notes(worker))
    assert not card.get("follow_ups")
    note = wait_for(lambda: [n for n in _notes(worker) if n.startswith("Corrected:")])[0]
    assert '"Mac Pro"' in note and "Never say the misheard words again" in note
    assert len([r for r in worker.interaction.runs.values() if r.run_id]) == 1
    # Nothing written before the correction is spoken afterwards.
    line = worker.card_update(run, ("Searching the code for Man Pros", "Looking for where Man Pros is set up",
                                    False))
    assert line is None
    assert not any("Man Pro" in s for s in spoken(worker))


def test_a_typed_fix_on_the_tasks_page_is_a_correction(server, service, hermes):
    worker, run = _running(server, service, hermes)
    steers_before = len(hermes.steers)
    status, body = typed_to_job(server, run.run_id, "mac pro", iid=worker.interaction.interaction_id)
    assert status == 200, body
    steer = wait_for(lambda: hermes.steers[steers_before:])[0][1]
    assert "was a mishearing of \"Mac Pro\" (the user typed so)" in steer
    assert service.store.title(run.idem_key) == "Plan the second Mac Pro"
    # Typed again: taken as already done (typed words are never spoken over), never sent twice or queued.
    status, _ = typed_to_job(server, run.run_id, "Mac pro", iid=worker.interaction.interaction_id)
    assert status == 200
    assert len(hermes.steers) == steers_before + 1
    assert not _card(service, run).get("follow_ups")


def test_the_live_line_written_before_the_correction_is_dropped(server, service, hermes):
    worker, run = _running(server, service, hermes)
    _say(worker, "call_fix", "No, not Man Pros, Mac Pro", task_id="call_plan")
    wait_for(lambda: service.store.corrections(run.idem_key))
    # The task hasn't read the steer yet and writes another stale status: still never shown or said.
    asyncio.run_coroutine_threadsafe(worker._handle_hermes_event(run, {
        "event": "message.interim", "text": "STATUS: Reading the Man Pros setup notes\nDETAIL: Nothing yet"}),
        worker.loop).result(5)
    card = _card(service, run)
    assert card["short_status"] == C.CORRECTING and "Man Pro" not in str(card.get("detail") or "")
    assert worker.card_line(run.idem_key) is None
    # Its next real step is shown and said as usual.
    asyncio.run_coroutine_threadsafe(worker._handle_hermes_event(run, {
        "event": "message.interim", "text": "STATUS: Checking the Mac Pro setup notes\nDETAIL: Found the plan"}),
        worker.loop).result(5)
    assert _card(service, run)["short_status"] == "Checking the Mac Pro setup notes"
    assert any(t.get("title") == "Plan the second Mac Pro" for t in tasks(server))


def test_a_correction_to_a_finished_task_runs_it_again_corrected(server, service, hermes):
    _, worker = start_call(server, service)
    _say(worker, "call_plan", REQUEST)
    run = wait_for(lambda: next((r for r in worker.interaction.runs.values()
                                 if r.delegation_id == "call_plan" and r.status == "completed"), None))
    _say(worker, "call_fix", "No, not Man Pros, Mac Pro", task_id="call_plan")
    again = wait_for(lambda: [r for r in worker.interaction.runs.values() if r.delegation_id == "call_fix"
                              and r.run_id])[0]
    assert service.store.request_text(run.idem_key).startswith("The second Mac Pro")
    assert again.idem_key != run.idem_key
    prompt = hermes.calls[-1]["input"]
    assert '"Man Pros" was a mishearing of "Mac Pro"' in prompt


def test_saying_it_again_is_confirmed_once_and_never_sent_again(server, service, hermes):
    worker, run = _running(server, service, hermes)
    _say(worker, "call_fix", "No, not Man Pros, Mac Pro", task_id="call_plan")
    wait_for(lambda: [n for n in _notes(worker) if n.startswith("Corrected:")])
    steers = len(hermes.steers)
    _say(worker, "call_fix2", "There is no Man Pros, dude", task_id="call_plan")
    note = wait_for(lambda: [n for n in _notes(worker) if n.startswith("Already corrected:")])[0]
    assert '"Mac Pro"' in note and "Man Pro" not in note
    assert len(hermes.steers) == steers
    assert not any(n.startswith("Queued:") for n in _notes(worker))
