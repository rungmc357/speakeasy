"""A failed Hermes run tells the user why, in Speakeasy's own words: the task row, its detail, the
voice and the chat notice name the cause (out of credits, rejected key, rate limit, missing model)
and the fix. The provider's own error text is never stored, shown, or spoken."""
from __future__ import annotations

import json
import sqlite3
import time
import urllib.error

import pytest

from fakes import SDP, http, wait_for
from speakeasy import continuity, failures
from speakeasy.hermes_api import HermesError
from speakeasy.prompt import builder as P
from speakeasy.store import StateStore

# What Hermes put on the failed runs of 2026-10-05: Venice (custom:venice) out of balance.
VENICE_402 = "HTTP 402: Insufficient USD or Diem balance"


@pytest.mark.parametrize("error, kind", [
    (VENICE_402, "billing"),
    ("Error code: 402 - {'error': {'message': 'Insufficient USD or Diem balance'}}", "billing"),
    ("HTTP 402: This request requires more credits, or fewer max_tokens.", "billing"),
    # OpenAI's insufficient_quota arrives as a 429 and Anthropic's empty balance as a 400: still billing.
    ("HTTP 429: You exceeded your current quota, please check your plan and billing details.", "billing"),
    ("HTTP 400: Your credit balance is too low to access the Anthropic API.", "billing"),
    ("Billing or credits exhausted: HTTP 402: Insufficient USD or Diem balance", "billing"),
    ("HTTP 401: Incorrect API key provided: sk-proj-***", "auth"),
    ("HTTP 403: Forbidden", "auth"),
    ("⚠️ Provider authentication failed: No API key found for provider venice", "auth"),
    ("HTTP 429: Rate limit reached for gpt-4o in organization org-*** on requests per min.", "rate_limit"),
    ("⚠️ Provider rate-limited: quota resets at 14:00", "rate_limit"),
    ("HTTP 404: The model `gpt-9` does not exist or you do not have access to it.", "model_not_found"),
    ("HTTP 400: qwen-max-latest is not a valid model ID", "model_not_found"),
    ("Provider said: HTTP 404: model_not_found", "model_not_found"),
    # Hermes' own 402/403/429 verdicts (agent/error_classifier.py): a usage window to wait out is a
    # rate limit, a spending cap is billing.
    ("HTTP 402: Usage limit reached, try again in 5 minutes", "rate_limit"),
    ("HTTP 429: You exceeded your current quota, please check your plan and billing details. Please retry in 33.2s.",
     "rate_limit"),
    ("HTTP 429: Rate limit reached for gpt-4o. Please try again in 20s.", "rate_limit"),
    ("HTTP 403: Key limit exceeded (total limit). Manage it using https://openrouter.ai/settings/keys", "billing"),
    ("HTTP 403: Your team has reached its monthly spending limit", "billing"),
])
def test_known_error_shapes_map_to_a_kind(error, kind):
    assert failures.failure_kind(error) == kind


@pytest.mark.parametrize("error", [
    "HTTP 403 — Attention Required! | Cloudflare",  # a CDN page in front of the provider, not the key
    "HTTP 403: Your request was blocked.",           # a relay's block page: the key never got there
    "HTTP 500: Internal server error",
    "Hermes can't reach the model provider. You may be offline. Check your internet connection and try again.",
    "agent run failed", "", None, 402,
])
def test_anything_else_is_not_guessed(error):
    assert failures.failure_kind(error) is None


# Invented safety-filter refusals, shaped like what Hermes puts on a failed run (its own
# ``content_policy_blocked: …`` verdict, or a provider's refusal wording relayed in the error).
REFUSALS = [
    "content_policy_blocked: Your request was blocked by an automatic filter. Please rephrase.",
    "content_policy_blocked: model declined (content_filter)",
    "HTTP 400: Your request was blocked: it was flagged by our safety system as possibly unsafe. content_policy_block",
    "HTTP 400: Invalid prompt: your prompt was flagged by our safety system.",
    "HTTP 403: This request violates our usage policies. Your request was blocked.",
    "HTTP 400: finish_reason=content_filter",
    "provider refusal category: cyber",
]


@pytest.mark.parametrize("error", REFUSALS)
def test_a_safety_refusal_is_never_a_firewall(error):
    """A refusal that says "your request was blocked" is the model's filter, never a firewall,
    the key, or credits: the card says so and offers a retry or another model."""
    assert failures.failure_kind(error) == "content_refused"
    assert "firewall" not in failures.failure_reason("failed", "content_refused")
    assert failures.failure_reason("failed", "content_refused") == "The model's safety filter refused this request."
    assert failures.reason_text("content_refused") == ("The model's safety filter refused this request. "
                                                       "Try again, or switch model for this chat.")


@pytest.mark.parametrize("error", [
    "HTTP 403 — Attention Required! | Cloudflare",
    "HTTP 403 — Just a moment...",
    "HTTP 403: <!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head>"
    "<body><div class=\"cf-error-details\">Sorry, you have been blocked</div></body></html>",
    "HTTP 503: <html><body><form id=\"challenge-form\" action=\"/cdn-cgi/challenge-platform/h/b\"></form></body></html>",
])
def test_only_a_real_challenge_page_is_called_a_firewall(error):
    assert failures.failure_kind(error) is None
    assert failures.failure_hint(error) == "blocked"
    assert "firewall" in failures.failure_reason("failed", None, failures.failure_hint(error))


def test_plain_blocked_words_are_not_a_firewall():
    """No HTML page, no refusal marker: "request was blocked" alone isn't proof of a firewall."""
    for error in ("HTTP 403: Your request was blocked.", "HTTP 400: request blocked"):
        assert failures.failure_hint(error) != "blocked"
        assert "firewall" not in failures.failure_reason("failed", failures.failure_kind(error),
                                                         failures.failure_hint(error))


def test_a_refusal_reply_is_read_as_a_refusal():
    """The session chat stream has no error field: Hermes' refusal reply opens with its own fixed
    sentence, then quotes the provider, whose line alone would read like a block page."""
    reply = ("⚠️ Anthropic's safety filter refused this request, so the model didn't answer. Try rewording "
             "your message or removing sensitive attachments, or switch to another model with /model.\n\n"
             "Provider said: Your request was blocked.")
    line = failures.provider_line(reply)
    assert line.startswith("content_policy_blocked: ") and failures.failure_kind(line) == "content_refused"
    # Any other failure reply keeps its provider line as it was.
    assert failures.provider_line("Something broke.\n\nProvider said: HTTP 404: model not found") == \
        "Provider said: HTTP 404: model not found"


def test_the_sentences_are_speakeasys_own():
    for kind, (label, text) in failures.REASONS.items():
        assert label and text and failures.reason_text(kind) == text
        assert failures.public_failure(kind) == {"kind": kind, "label": label, "text": text}
    assert "hermes model" in failures.reason_text("billing")
    unknown = failures.public_failure(failures.UNKNOWN)
    assert "label" not in unknown and "error log" in unknown["text"]
    assert failures.public_failure(None) is None and failures.public_failure("from-a-newer-version") is None


def test_unknown_names_the_hermes_error_log(tmp_path, monkeypatch):
    assert str(tmp_path / "logs" / "errors.log") in failures.unknown_text(tmp_path)
    monkeypatch.setattr(failures.Path, "home", staticmethod(lambda: tmp_path))  # a real install: under ~
    assert failures.unknown_text(tmp_path / ".hermes").endswith(": ~/.hermes/logs/errors.log")
    # The voice gets the pointer without a path to read aloud.
    assert "/" not in failures.voice_text(failures.UNKNOWN)


def test_only_the_provider_line_of_a_failed_reply_is_read():
    billing = ("Billing or credits exhausted: HTTP 402: Insufficient USD or Diem balance\n\n"
               "Add credits with Venice, or run `hermes model` to switch.")
    assert failures.provider_line(billing) == "Billing or credits exhausted: HTTP 402: Insufficient USD or Diem balance"
    model = ("Model 'qwen-9' isn't available on Venice. Pick a different model with /model.\n\n"
             "Provider said: HTTP 404: model not found")
    assert failures.provider_line(model) == "Provider said: HTTP 404: model not found"
    # A partial answer that talks about billing or rate limits is not an error line.
    assert failures.provider_line("I compared rate limits and billing plans for three providers.") is None
    assert failures.provider_line(None) is None


def test_start_failures_name_speakeasys_link_to_hermes():
    assert failures.start_failure_kind(HermesError(401, "Hermes HTTP 401")) == "hermes_key"
    assert failures.start_failure_kind(HermesError(502, "Hermes unavailable right now. Try again in a moment.")) == "hermes_unreachable"
    assert failures.start_failure_kind(HermesError(502, "Hermes HTTP 500")) is None
    assert failures.start_failure_kind(urllib.error.HTTPError("u", 401, "no", {}, None)) == "hermes_key"
    assert failures.start_failure_kind(urllib.error.URLError("refused")) == "hermes_unreachable"
    assert failures.start_failure_kind(RuntimeError("bug")) is None


def test_chat_notice_carries_the_reason():
    names, why = P.Names(), failures.reason_text("billing")
    notice = P.stopped_notice(names, "failed", "Weather in Lisbon", why)
    assert notice.startswith("Stopped: the work failed — Weather in Lisbon") and why in notice
    assert P.stopped_notice(names, "failed", "x") == "Stopped: the work failed — x"


def test_next_call_and_resumed_call_hear_why(tmp_path):
    """Failures that settled with no one listening (between calls, or while paused) reach the voice's
    background notes with Speakeasy's sentence, in the same ``failure`` object the task carries."""
    store = StateStore(tmp_path / "state.sqlite3", hermes_home=tmp_path)
    store.set_meta("last_call_end", str(time.time() - 60))
    for key, run_id, kind, asked in (("k1", "run_1", "billing", "Weather in Lisbon"),
                                     ("k2", "run_2", failures.UNKNOWN, "Book a table")):
        store.reserve_run(key, "int_1", "call_" + key, 1)
        store.progress(key, "request", asked)
        store.update_run(key, run_id, "failed")
        store.set_failure(key, kind)
    away = {item["run_id"]: item for item in store.away()}
    assert away["run_1"]["failure"] == store.work(idem_key="k1")["failure"]
    block = P.away_block(list(away.values()), P.Names())
    assert f"Weather in Lisbon: failed: {failures.voice_text('billing')}" in block
    assert f"Book a table: failed: {failures.voice_text(failures.UNKNOWN)}" in block
    assert str(tmp_path) not in block  # the log path is for the app, not something to read aloud
    resumed = P.resume_block([store.work(idem_key="k1")], P.Names())
    assert f"Weather in Lisbon: failed: {failures.voice_text('billing')}" in resumed


def test_failure_kind_survives_a_restart_and_old_databases_upgrade(tmp_path):
    path = tmp_path / "state.sqlite3"
    old = sqlite3.connect(path)  # the runs table as the first release created it (no failure column)
    old.execute("""CREATE TABLE runs (idem_key TEXT PRIMARY KEY, interaction_id TEXT NOT NULL,
        delegation_id TEXT NOT NULL, revision INTEGER NOT NULL, run_id TEXT, status TEXT NOT NULL,
        updated REAL NOT NULL, short_status TEXT, detail TEXT, progress_updated REAL, status_source TEXT,
        authored_at REAL, result_json TEXT, settled_at REAL, title TEXT, dismissed INTEGER,
        session_id TEXT, summary TEXT, continued TEXT)""")
    old.commit()
    old.close()
    store = StateStore(path, hermes_home=tmp_path)
    store.reserve_run("k1", "int_1", "call_1", 1)
    store.update_run("k1", "run_1", "failed")
    store.set_failure("k1", "billing")
    store.close()
    again = StateStore(path, hermes_home=tmp_path)
    assert again.work(idem_key="k1")["failure"]["label"] == "Out of credits"
    again.update_run("k1", None, "completed")  # only a failed run shows a failure
    assert "failure" not in again.work(idem_key="k1")


# -- end to end: fake Hermes API server, real Speakeasy server, fake live call ---------------------

def start_call(server, service, key="req_fail_1"):
    status, session = http(server.base_url, "POST", "/voice/sessions", {"sdp": SDP}, server.token,
                           {"Idempotency-Key": key})
    assert status == 201, session
    return session, service.workers[-1]


def failed_task(server):
    def find():
        tasks = http(server.base_url, "GET", "/voice/work/latest", token=server.token)[1]["tasks"]
        return next((t for t in tasks if t["status"] == "failed" and t.get("failure")), None)
    return wait_for(find)


def spoken(worker):
    return [c for kind, _, c in worker.sent if kind == "session.commentary.append"]


def test_out_of_credits_reaches_the_task_and_the_voice(server, service, hermes):
    hermes.fail_with = VENICE_402
    stopped = []
    service.notices.stopped = lambda run_id, status, request, why=None: stopped.append((status, why))
    _, worker = start_call(server, service)
    worker.delegate("call_weather", "What's the weather in Lisbon?")
    task = failed_task(server)
    reason, said = failures.reason_text("billing"), failures.voice_text("billing")
    assert task["failure"] == {"kind": "billing", "label": "Out of credits", "text": reason}
    assert task["events"][-1] == {"kind": "result", "text": f"Work failed: {reason}", "at": task["events"][-1]["at"]}
    wait_for(lambda: P.failed_because(said) in spoken(worker))
    assert not any("hermes model" in c for c in spoken(worker))  # the command is on screen, not spoken
    assert stopped == [("failed", reason)]
    # The provider's words stay in Hermes' log: not in the task, not in anything said on the call.
    assert "Diem" not in json.dumps(task) and not any("Diem" in c for _, _, c in worker.sent)


def test_reason_is_read_from_the_run_status_when_the_stream_drops(server, service, hermes):
    hermes.fail_with, hermes.drop_terminal = "HTTP 401: Incorrect API key provided: sk-proj-***", True
    _, worker = start_call(server, service)
    worker.delegate("call_key", "Summarize my inbox")
    task = failed_task(server)
    assert task["failure"]["kind"] == "auth"
    wait_for(lambda: P.failed_because(failures.voice_text("auth")) in spoken(worker))
    assert "sk-proj" not in json.dumps(task)


def test_unrecognized_failure_points_at_the_error_log(server, service, hermes, home):
    hermes.fail_with = "HTTP 500: upstream exploded"
    _, worker = start_call(server, service)
    worker.delegate("call_odd", "Book a table for two")
    task = failed_task(server)
    assert task["failure"]["kind"] == "unknown" and "label" not in task["failure"]
    assert str(home / "logs" / "errors.log") in task["failure"]["text"]
    assert "exploded" not in json.dumps(task)
    wait_for(lambda: P.FAILED_SPOKEN in spoken(worker))


def test_rejected_api_server_key_is_named(server, service, hermes, home):
    _, worker = start_call(server, service)
    # The key in .env no longer matches the one the API server checks (read per request).
    env = home / ".env"
    env.write_text(env.read_text().replace("test-api-server-key-not-real", "rotated-key-not-real"))
    worker.delegate("call_rotated", "Check my calendar tomorrow")
    task = failed_task(server)
    assert task["failure"]["kind"] == "hermes_key"
    wait_for(lambda: P.failed_because(failures.voice_text("hermes_key")) in spoken(worker))


def test_continued_conversation_failure_is_explained(server, service, hermes, monkeypatch):
    """The session chat stream's run.failed has no error field; the reason comes from the provider
    line Hermes quotes in its failure reply."""
    conv = continuity.Conversation("s_league", "discord", "777", "thread", "777", "42", "111",
                                   "Server / #work / League", "League team", time.time())
    monkeypatch.setattr(continuity, "conversations_with_context", lambda db, request, **kw: [continuity.Candidate(conv, ())])
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **k: False)
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [  # the thread's channel is approved
        {"target": "discord:111", "label": "#work", "topic": "work"}]}})
    from speakeasy import router
    monkeypatch.setattr(router, "aux_call", lambda messages, timeout=router.ROUTE_TIMEOUT_S:
                        '{"follow_up_task_id": null, "conversation": "c1", "parts": ["x"], "channel": null}')

    def fake_stream(base, key, c, message, callback, **k):
        callback("run.started", {"run_id": "run_cont_fail"})
        callback("assistant.completed", {"content": "Billing or credits exhausted: " + VENICE_402
                                         + "\n\nAdd credits with Venice, or run `hermes model` to switch.",
                                         "completed": False, "partial": False, "interrupted": False})
        callback("run.failed", {"completed": False, "partial": False, "interrupted": False})
        return True
    monkeypatch.setattr(continuity, "stream_session_chat", fake_stream)
    _, worker = start_call(server, service)
    worker.delegate("call_cont", "In the league team thread, am I going to win this week?")
    task = failed_task(server)
    assert task["failure"]["kind"] == "billing"
    # The reply was Hermes' failure message quoting the provider: none of it is kept as the answer.
    assert task["result"] is None and "Diem" not in json.dumps(task)
    wait_for(lambda: P.failed_because(failures.voice_text("billing")) in spoken(worker))
    assert not any("Diem" in c for _, _, c in worker.sent)


def test_safety_refusal_reaches_the_card_and_the_voice(server, service, hermes):
    hermes.fail_with = REFUSALS[0]
    _, worker = start_call(server, service)
    worker.delegate("call_refused", "Tidy up the checkout tests")
    task = failed_task(server)
    assert task["failure"]["kind"] == "content_refused"
    assert task["failure_reason"] == "The model's safety filter refused this request."
    assert "firewall" not in json.dumps(task) and "automatic filter" not in json.dumps(task)
    wait_for(lambda: P.failed_because(failures.voice_text("content_refused")) in spoken(worker))


def test_failure_reply_read_back_from_the_run_status(server, service, hermes):
    """The stream drops and GET /v1/runs returns a session-chat-shaped failed run: the reply is
    Hermes' failure message and there is no error field. Only its kind is kept."""
    hermes.fail_with, hermes.drop_terminal = "", True
    hermes.fail_reply = ("Model 'qwen-9' isn't available on Venice. Pick a different model with /model.\n\n"
                         "Provider said: HTTP 404: The model `qwen-9` does not exist")
    _, worker = start_call(server, service)
    worker.delegate("call_model", "Summarize the news")
    task = failed_task(server)
    assert task["failure"]["kind"] == "model_not_found"
    assert task["result"] is None and "qwen-9" not in json.dumps(task)


def test_continued_conversation_that_cannot_reach_hermes(server, service, hermes, monkeypatch):
    conv = continuity.Conversation("s_league", "discord", "777", "thread", "777", "42", "111",
                                   "Server / #work / League", "League team", time.time())
    monkeypatch.setattr(continuity, "conversations_with_context", lambda db, request, **kw: [continuity.Candidate(conv, ())])
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **k: False)
    service.settings.patch({"delivery": {"target": "telegram:555", "channels": [  # the thread's channel is approved
        {"target": "discord:111", "label": "#work", "topic": "work"}]}})
    from speakeasy import router
    monkeypatch.setattr(router, "aux_call", lambda messages, timeout=router.ROUTE_TIMEOUT_S:
                        '{"follow_up_task_id": null, "conversation": "c1", "parts": ["x"], "channel": null}')

    def refused(*a, **k):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(continuity, "stream_session_chat", refused)
    _, worker = start_call(server, service)
    worker.delegate("call_cont_down", "In the league team thread, am I going to win this week?")
    task = failed_task(server)
    assert task["failure"]["kind"] == "hermes_unreachable"
    wait_for(lambda: P.failed_because(failures.voice_text("hermes_unreachable")) in spoken(worker))


def test_voice_never_reads_a_command_aloud():
    """The voice names the fix in words; the command (hermes model, hermes voice setup) is only on
    screen and in the chat notice, where it can be copied."""
    from speakeasy.text import looks_like_command
    for kind in failures.REASONS:
        said = failures.voice_text(kind)
        assert said and "hermes model" not in said and "voice setup" not in said and "`" not in said
        assert not looks_like_command(said)
        assert "in the app" in said
        if kind != "content_refused":  # its fix is in the app (Try again, Switch model), not a command
            assert "hermes " in failures.reason_text(kind)  # the on-screen text keeps the command
    assert "in the app" not in failures.voice_text(failures.UNKNOWN)
