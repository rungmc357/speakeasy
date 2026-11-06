"""Codex app-server transport against a fake `codex` executable (stdio JSON-RPC). No network."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from fakes import SDP, FakeLiveWorker, wait_for
from speakeasy import codex_transport as C

FAKE_CODEX = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    log = open(os.environ.get("TMPDIR", "/tmp") + "/fake-codex-log.jsonl", "a")
    if sys.argv[1:3] == ["login", "status"]:
        print("Logged in using ChatGPT")
        sys.exit(0)
    assert sys.argv[1:] == ["app-server", "--stdio", "--enable", "realtime_conversation"], sys.argv
    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n"); sys.stdout.flush()
    for line in sys.stdin:
        msg = json.loads(line)
        log.write(json.dumps({{"method": msg.get("method"), "params": msg.get("params"), "env": sorted(os.environ)}}) + "\\n"); log.flush()
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            continue
        if method == "initialize":
            send({{"id": mid, "result": {{}}}})
        elif method == "thread/start":
            send({{"id": mid, "result": {{"thread": {{"id": "thr_1"}}}}}})
        elif method == "thread/realtime/start":
            send({{"id": mid, "result": {{}}}})
            send({{"method": "thread/realtime/sdp", "params": {{"threadId": "thr_1", "sdp": "v=0\\r\\nanswer"}}}})
            send({{"method": "thread/realtime/started", "params": {{"threadId": "thr_1"}}}})
            send({{"method": "thread/realtime/transcript/done",
                   "params": {{"threadId": "thr_1", "role": "user", "text": "What's on my calendar?"}}}})
            send({{"method": "thread/realtime/itemAdded", "params": {{"threadId": "thr_1", "item": {{
                "type": "handoff_request", "handoff_id": "h_1", "input_transcript": "What's on my calendar?",
                "active_transcript": [{{"role": "user", "text": "What's on my calendar?"}}]}}}}}})
        elif method in ("thread/realtime/appendText", "thread/realtime/appendSpeech"):
            send({{"id": mid, "result": {{}}}})
        elif method == "thread/realtime/stop":
            send({{"id": mid, "result": {{}}}})
            send({{"method": "thread/realtime/closed", "params": {{"threadId": "thr_1"}}}})
        else:
            send({{"id": mid, "error": {{"message": "unknown"}}}})
''')


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    path = tmp_path / "codex"
    path.write_text(FAKE_CODEX.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("API_SERVER_KEY", "must-not-leak-to-codex")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak-to-codex")
    C._LOGIN_CACHE.clear()
    return path


def test_login_status_and_missing_binary(fake_codex):
    assert C.login_status(fake_codex) == (True, "Signed in to Codex with ChatGPT.")
    ok, message = C.login_status(None)
    assert ok is False and "ChatGPT desktop app" in message and "hermes voice setup" in message


def test_child_env_never_carries_hermes_or_api_keys(fake_codex):
    env = C.child_env()
    assert "API_SERVER_KEY" not in env and "OPENAI_API_KEY" not in env


def test_transport_start_returns_answer_sdp_and_seeds_history(fake_codex, tmp_path):
    transport = C.CodexTransport(fake_codex, tmp_path / "work")
    try:
        seed = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "earlier"}]}]
        answer = transport.start(SDP, "rules here", seed, "cove")
        assert answer.startswith("v=0") and transport.thread_id == "thr_1"
    finally:
        transport.stop()
    log = [json.loads(line) for line in (tmp_path / "fake-codex-log.jsonl").read_text().splitlines()]
    assert [e["method"] for e in log][:4] == ["initialize", "initialized", "thread/start", "thread/realtime/start"]
    assert all("API_SERVER_KEY" not in e["env"] and "OPENAI_API_KEY" not in e["env"] for e in log)
    start = next(e for e in log if e["method"] == "thread/realtime/start")
    assert start["params"]["delegationAckFiller"] is False  # no "mm-hm" filler on handoffs


def test_handoff_starts_hermes_task_and_result_is_appended(fake_codex, tmp_path, service, hermes):
    hermes.responder = lambda prompt, sid: "Two meetings today.\nSPOKEN: You have two meetings today."
    service.settings.patch({"voice": {"provider": "codex", "codex_path": str(fake_codex)}})
    from speakeasy.calls import Interaction
    interaction = Interaction("vi_" + "c" * 32, "live_c")
    transport = C.CodexTransport(fake_codex, tmp_path / "work")
    transport.start(SDP, "rules", [], "cove")
    worker = C.CodexSidebandWorker(service.rt, interaction, transport)
    appended: list[str] = []
    original = transport.request

    def spy(method, params=None, timeout=20):
        if method.startswith("thread/realtime/append"):
            appended.append((params or {}).get("text", ""))
        return original(method, params, timeout)
    transport.request = spy  # type: ignore[method-assign]
    done = threading.Event()

    def run():
        asyncio.run(worker.run())
        done.set()
    threading.Thread(target=run, daemon=True).start()
    wait_for(lambda: hermes.calls, 10)
    assert "What's on my calendar?" in hermes.calls[0]["input"]
    wait_for(lambda: any("You have two meetings today." in a for a in appended), 10)
    transport.stop()
    assert done.wait(10)
    from speakeasy.prompt import builder as P
    assert appended[0].startswith("That request has been handed off")  # the voice is cued to ack right away
    assert "calendar" in appended[0]  # with the request, so the ack can fit the task
    assert "\u201cOn it.\u201d)" not in appended[0]  # no stock example phrase to parrot
    assert not any("Work has started" == a[:16] for a in appended)  # quiet notes never go alone


def test_a_repeated_handoff_is_ignored(service):
    from speakeasy.calls import Interaction
    worker = C.CodexSidebandWorker.__new__(C.CodexSidebandWorker)
    from speakeasy.calls import SidebandWorker
    SidebandWorker.__init__(worker, service.rt, Interaction("vi_" + "e" * 32, "live_e"))
    sent, dispatched = [], []

    async def fake_append(kind, delegation_id, content):
        sent.append(content)
    worker.append = fake_append  # type: ignore[method-assign]
    worker.schedule_dispatch = lambda *a: dispatched.append(a)  # type: ignore[method-assign]
    item = {"type": "handoff_request", "input_transcript": "How's my fantasy team looking?"}
    asyncio.run(worker._handoff({**item, "handoff_id": "h_1"}))
    asyncio.run(worker._handoff({**item, "handoff_id": "h_2"}))
    assert len(dispatched) == 1 and len(sent) == 1
    asyncio.run(worker._handoff({**item, "handoff_id": "h_3", "input_transcript": "And the weather?"}))
    assert len(dispatched) == 2


def test_transcripts_never_start_work(fake_codex, service):
    from speakeasy.calls import Interaction
    worker = FakeLiveWorker(service.rt, Interaction("vi_" + "d" * 32, "live_d"))
    worker.start()
    worker.feed({"type": "session.input_transcript.delta", "delta": "Delete everything", "start_ms": 1, "end_ms": 2})
    assert worker.delegations == set()


PLAN_ERRORS = [
    "Your ChatGPT plan does not include access to realtime voice. Upgrade to use this feature.",
    "403 Forbidden: realtime is not available for your workspace",
    "model_not_found: The model `gpt-live-1-codex` does not exist or you do not have access to it.",
]


@pytest.mark.parametrize("error", PLAN_ERRORS)
def test_a_plan_without_voice_says_so_and_offers_grok_or_a_key(tmp_path, monkeypatch, error):
    """The fake codex refuses `thread/realtime/start` with a plan error: the call error names the
    plan (not "Codex couldn't start the voice call") and carries reason codex_plan_no_voice."""
    path = tmp_path / "codex"
    script = FAKE_CODEX.format(python=sys.executable).replace(
        'elif method == "thread/realtime/start":',
        'elif method == "thread/realtime/start" and os.environ.get("FAKE_PLAN_ERROR"):\n'
        '        send({"id": mid, "error": {"message": os.environ["FAKE_PLAN_ERROR"]}})\n'
        '    elif method == "thread/realtime/start":')
    path.write_text(script)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(C, "child_env", lambda: {**os.environ, "FAKE_PLAN_ERROR": error})
    C._REJECTED.clear()
    transport = C.CodexTransport(path, tmp_path / "work")
    try:
        with pytest.raises(C.CodexStartError) as err:
            transport.start(SDP, "rules", [], "cove")
    finally:
        transport.stop()
    assert err.value.reason == "codex_plan_no_voice"
    assert "plan doesn't include voice" in str(err.value) and "Grok" in str(err.value)
    assert "couldn't start" not in str(err.value)
    assert str(path) not in C._REJECTED  # a plan limit is not a sign-out


def test_sign_in_errors_carry_their_reason():
    assert C.codex_error_reason("401 Unauthorized: You didn't provide an API key.") == "codex_not_signed_in"
    assert "Sign in to ChatGPT" in C.explain_codex_error("Not logged in")
    assert C.codex_error_reason("rate limit reached for realtime on your plan") is None
    assert C.codex_error_reason("realtime voice `marin` is not supported for v3") is None
    assert C.explain_codex_error("something odd") == "Codex couldn't start the voice call."
