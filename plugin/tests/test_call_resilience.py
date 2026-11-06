"""Calls with long and many tasks: an answer already opened in the app is never read out, a call
waiting on a running task isn't paused or cut at its cap, and many long tasks never starve the
short calls (Stop, follow-ups, spoken lines). Invented requests throughout."""
from __future__ import annotations

import asyncio
import threading
import time

from fakes import SDP, http, wait_for
from speakeasy import calls
from speakeasy.prompt import builder as P


def start_call(server, service, key="req_res_1"):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def say(worker, delegation_id, words, at):
    worker.feed({"type": "session.input_transcript.delta", "delta": words, "start_ms": at, "end_ms": at + 1})
    worker.feed({"type": "session.delegation.created", "offset_ms": at + 5,
                 "delegation": {"id": delegation_id, "target": "client"}})


def spoken(worker):
    return [c for k, _, c in worker.sent if k == "session.commentary.append"]


def notes(worker):
    return [c for k, _, c in worker.sent if k == "session.thinking.append"]


# -- an answer opened in the app is not read out ---------------------------------------------------

def test_answer_opened_in_the_app_is_not_read_out_when_it_lands(server, service, hermes):
    hermes.hold = True
    hermes.responder = lambda prompt, sid: "Three headlines: the harbour bridge reopens, rain all week, and the tram strike ends."
    _, worker = start_call(server, service)
    say(worker, "call_news", "Fetch me the morning news", 10)
    run = wait_for(lambda: next((r for r in worker.interaction.runs.values() if r.run_id), None))
    status, body = http(server.base_url, "POST", f"/voice/tasks/{run.run_id}/seen", {}, server.token)
    assert status == 200 and body["seen"] is True
    assert service.store.is_seen(run.idem_key)
    hermes.runs[run.run_id]["done"].set()
    wait_for(lambda: run.status == "completed")
    time.sleep(0.5)
    assert not any("harbour bridge" in line for line in spoken(worker))
    assert any("already read" in n and "harbour bridge" in n for n in notes(worker))


def test_seen_endpoint_validates(server, service):
    status, _ = http(server.base_url, "POST", "/voice/tasks/run_nope0000/seen", {}, server.token)
    assert status == 404
    status, _ = http(server.base_url, "POST", "/voice/tasks/run_nope0000/seen", {"x": 1}, server.token)
    assert status == 400


def test_answer_not_opened_is_still_spoken(server, service, hermes):
    hermes.responder = lambda prompt, sid: "The pharmacy on Elm closes at nine tonight."
    _, worker = start_call(server, service, "req_res_2")
    say(worker, "call_pharm", "When does the pharmacy close", 10)
    wait_for(lambda: any("Elm" in line for line in spoken(worker)))


def test_resumed_call_doesnt_reread_an_opened_answer():
    names = P.Names("Hermes", "Sam")
    tasks = [{"events": [{"kind": "request", "text": "Fetch the news"}], "status": "completed", "seen": True,
              "result": {"spoken": "Rain all week."}},
             {"events": [{"kind": "request", "text": "Check the parcel"}], "status": "completed",
              "result": {"spoken": "It arrives Friday."}}]
    block = P.resume_block(tasks, names)
    assert "Fetch the news: finished, and Sam already read it in the app (don't read it out" in block
    assert "- Check the parcel: finished: It arrives Friday." in block


# -- a call waiting on a running task stays up -----------------------------------------------------

def test_idle_pause_waits_while_a_task_runs(server, service, hermes):
    hermes.hold = True
    session, worker = start_call(server, service, "req_res_3")
    say(worker, "call_long", "Compare the three moving quotes in detail", 10)
    wait_for(lambda: any(r.run_id for r in worker.interaction.runs.values()))
    interaction = service.interaction(session["interaction_id"])
    minutes = service.settings.get()["idle_pause_minutes"]
    interaction.last_activity = time.monotonic() - minutes * 60 - 5
    assert service.idle_check(now=time.monotonic()) == []  # an answer is on its way
    interaction.last_activity = time.monotonic() - minutes * 60 * 5  # far past the stretch: paused
    assert session["interaction_id"] in service.idle_check(now=time.monotonic())


def test_cap_is_stretched_while_work_runs_then_pauses_resumably(server, service, hermes):
    hermes.hold = True
    session, worker = start_call(server, service, "req_res_4")
    assert worker.cap_reached(10, 1800) is False
    assert worker.cap_reached(1800, 1800) is True  # nothing running: the cap holds
    say(worker, "call_cap", "Draft the long grant report", 10)
    wait_for(lambda: any(r.run_id for r in worker.interaction.runs.values()))
    assert worker.cap_reached(1900, 1800) is False
    assert worker.cap_reached(1800 * calls.CAP_STRETCH, 1800) is True
    assert asyncio.run_coroutine_threadsafe(worker.pause_at_cap(), worker.loop).result(10) is True
    interaction = service.interaction(session["interaction_id"])
    assert interaction.paused and interaction.snapshot()["limit_paused"] is True
    service.resumable(session["interaction_id"])  # Resume works like after Pause


# -- many long tasks never starve the short calls ---------------------------------------------------

def test_long_waits_never_starve_short_calls():
    release = threading.Event()

    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        loop.set_default_executor(__import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=2))
        waits = [asyncio.ensure_future(calls.in_own_thread(release.wait, 10)) for _ in range(8)]
        await asyncio.sleep(0.05)
        start = time.monotonic()
        await asyncio.wait_for(asyncio.to_thread(lambda: "stop sent"), 2)  # e.g. Stop on another task
        took = time.monotonic() - start
        release.set()
        assert all(await asyncio.gather(*waits))
        return took

    assert asyncio.run(scenario()) < 1.0


def test_in_own_thread_passes_errors_back():
    def boom():
        raise ValueError("invented")

    async def scenario():
        try:
            await calls.in_own_thread(boom)
        except ValueError as exc:
            return str(exc)

    assert asyncio.run(scenario()) == "invented"
