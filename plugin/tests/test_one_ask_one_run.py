"""One ask, one run: a slow admission (pictures) and a just-started run refusing guidance never make
duplicate runs or a wrong "restart Hermes" (hermes_api.start_run, calls.steer_follow_up)."""
import asyncio
import io
import json
import types

from speakeasy import calls, failures
from speakeasy.hermes_api import HermesAPI, HermesError


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_slow_start_with_pictures_is_asked_again_under_the_same_key_not_failed():
    seen = []

    def opener(req, timeout):
        seen.append((req.get_header("Idempotency-key"), timeout))
        if len(seen) == 1:
            raise TimeoutError("read timed out")
        return _Resp(json.dumps({"run_id": "run_abc123"}).encode())

    api = HermesAPI("http://127.0.0.1:8642", lambda: "", opener=opener)
    run = api.start_run("Look at these", "idem-1", images=["data:image/png;base64,AAAA"])
    assert run == "run_abc123"
    assert [k for k, _ in seen] == ["idem-1", "idem-1"]           # the same run, never a second one
    assert all(t >= 120 for _, t in seen)                        # pictures get room to be read in


def test_a_timeout_is_not_called_unreachable():
    exc = HermesError(504, "Hermes timed out answering")
    assert failures.start_failure_kind(exc) != "hermes_unreachable"
    assert failures.start_failure_kind(HermesError(502, "Hermes unavailable: URLError")) == "hermes_unreachable"


def test_a_just_started_run_refusing_guidance_is_asked_again(monkeypatch):
    monkeypatch.setattr(calls, "STEER_RETRY_S", 0.01)
    answers = iter([False, False, True])
    hermes = types.SimpleNamespace(steer=lambda run_id, text: next(answers))
    target = types.SimpleNamespace(run_id="run_new1", status="running")
    worker = types.SimpleNamespace(hermes=hermes, interaction=types.SimpleNamespace(lock=__import__("threading").Lock()),
                                   rt=types.SimpleNamespace(state_db=None), store=types.SimpleNamespace(
                                       progress=lambda *a: None, add_follow_up=lambda *a: None),
                                   dispatch_tasks=set())

    async def no_watch(_target):
        return None
    worker.watch_follow_ups = no_watch
    monkeypatch.setattr(calls.threads, "last_message_id", lambda db: 0)
    ok = asyncio.run(calls.SidebandWorker.steer_follow_up(worker, target, "k1", "You didn't get them", "added text"))
    assert ok                                                     # folded into the run, no new one


def test_a_finished_run_is_not_waited_on(monkeypatch):
    monkeypatch.setattr(calls, "STEER_RETRY_S", 5.0)
    tries = []
    hermes = types.SimpleNamespace(steer=lambda run_id, text: tries.append(1) or False)
    target = types.SimpleNamespace(run_id="run_old1", status="completed")
    worker = types.SimpleNamespace(hermes=hermes, interaction=types.SimpleNamespace(lock=__import__("threading").Lock()),
                                   rt=types.SimpleNamespace(state_db=None))
    monkeypatch.setattr(calls.threads, "last_message_id", lambda db: 0)
    assert asyncio.run(calls.SidebandWorker.steer_follow_up(worker, target, "k1", "w", "t")) is False
    assert len(tries) == 1


def test_a_closing_offer_after_a_report_is_not_a_choice_card():
    report = ("Here's the quick picture: the next build isn't cut yet. The last one went out earlier tonight and "
              "we're still polishing the page fix and the lost-job fix; two smaller fixes wait on your go-ahead. ")
    offer = "Want me to hold until tests finish, or walk through what's in the page fix?"
    assert calls.closing_offer(report + offer, offer)
    # A pick asked on its own is still a card.
    assert not calls.closing_offer("Want me to book the 6 pm or the 8 pm table?", "Want me to book the 6 pm or the 8 pm table?")
    assert not calls.closing_offer(report + "The quick summary or the full report?", "The quick summary or the full report?")


def test_titles_name_the_subject_not_a_generic_verb_or_a_reference():
    from speakeasy.router import clean_task_title
    for bad in ("Check work status", "Decide on holding", "Confirm the earlier task", "Review the update now"):
        title, why = clean_task_title('{"title": "%s"}' % bad, "Request: is it done?")
        assert title is None and why, bad
    for good in ("Board: make stopped tasks dismissible", "Speakeasy build 66: why task cards freeze",
                 "Check tomorrow's New York weather"):
        title, why = clean_task_title('{"title": "%s"}' % good, "Request: something")
        assert title == good, (good, why)
