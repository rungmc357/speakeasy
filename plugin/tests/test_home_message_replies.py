"""Home quotes are resolved by the server, persisted, and provided as task context."""
import pytest
from test_home_chat import coordinator, post, stream
from speakeasy import home_chat as HC
from fakes import wait_for
from test_voice_notes import heard  # noqa: F401 - transcription fixture


def test_home_quote_persists_in_response_stream_and_coordinator_history(server, service):
    coordinator(service, "Try the blue one.")
    first = post(server, {"text": "Which colour?", "client_id": "quote-first"})[1]
    original = first["reply"]
    seen = coordinator(service, "The navy shade.")
    status, result = post(server, {"text": "Which blue?", "client_id": "quote-second", "reply_to": original["id"]})
    assert status == 200
    quote = {"id": original["id"], "role": "assistant", "text": "Try the blue one."}
    assert result["message"]["text"] == "Which blue?"
    assert result["message"]["quote"] == quote
    assert "Try the blue one." in seen[0][-1]["content"]
    assert next(e for e in stream(server) if e["id"] == result["message"]["id"])["quote"] == quote
    restarted = HC.HomeChat(service)
    assert next(e for e in [restarted.entry(row) for row in restarted.shown()] if e["id"] == result["message"]["id"])["quote"] == quote
    again = post(server, {"text": "Which blue?", "client_id": "quote-second", "reply_to": original["id"]})[1]
    assert again["replayed"] and again["message"]["quote"] == quote
    later = coordinator(service, "Yes.")
    assert post(server, {"text": "Got it"})[0] == 200
    assert any("Replying to this Home message" in m["content"] for m in later[0][1:-1])


@pytest.mark.parametrize("reply_to", ["unknown", "m:ha_0000000000000000", 42, {"text": "forged"}])
def test_invalid_or_missing_quote_is_rejected_without_saving_a_message(server, service, reply_to):
    coordinator(service, "unused")
    assert post(server, {"text": "reply", "reply_to": reply_to})[0] == 400
    assert not stream(server)


def test_quote_user_message_reaches_started_job(server, service, hermes):
    coordinator(service, "Sure.")
    first = post(server, {"text": "The shed is green"})[1]
    coordinator(service, {"reply": "Checking.", "action": "start", "request": "Find matching paint"})
    status, result = post(server, {"text": "Match this", "reply_to": first["message"]["id"]})
    assert status == 200 and result["message"]["quote"]["role"] == "user"
    calls = wait_for(lambda: hermes.calls)
    assert "The shed is green" in str(calls[0]["input"])


@pytest.mark.parametrize("transcribed", [True, False])
def test_home_voice_reply_carries_quote_to_the_job(server, service, hermes, heard, transcribed):
    from test_voice_notes import send
    coordinator(service, "The shed is green.")
    first = post(server, {"text": "What colour is the shed?"})[1]
    heard[1]["text"] = "Find matching paint" if transcribed else None
    coordinator(service, {"reply": "Checking.", "action": "start", "request": "Find matching paint"})
    status, result = send(server, "/voice/home/voice-message", Client_Id="quoted-voice", Duration="2",
                          Reply_To=first["reply"]["id"])
    assert status == 200, result
    assert "The shed is green." in str(wait_for(lambda: hermes.calls)[0]["input"])
    if transcribed:
        assert next(e for e in stream(server) if e.get("client_id") == "quoted-voice")["quote"]["id"] == first["reply"]["id"]


def test_reply_to_a_job_card_quotes_it_and_focuses_that_job(server, service, hermes):
    coordinator(service, {"reply": "Let me look for a table.", "action": "start", "request": "Find a dinner table for four"})
    first = post(server, {"text": "Find a dinner table for four"})[1]
    run_id = first["run_id"]
    assert run_id
    seen = coordinator(service, "Sure, I'll move it to 8.")
    status, result = post(server, {"text": "change it to 8pm", "reply_to": "r:" + run_id})
    assert status == 200, result
    quote = result["message"]["quote"]
    assert quote["role"] == "card" and quote["run_id"] == run_id and quote["id"] == "r:" + run_id
    assert quote["text"]
    system = seen[0][0]["content"]
    assert "selected right now" in system            # "it" means the quoted job
    assert run_id not in seen[0][-1]["content"] and "Replying to this job card" in seen[0][-1]["content"]
    assert next(e for e in stream(server) if e["id"] == result["message"]["id"])["quote"]["run_id"] == run_id


def test_reply_to_an_unknown_job_card_is_rejected(server, service):
    coordinator(service, "unused")
    assert post(server, {"text": "reply", "reply_to": "r:lr_doesnotexist"})[0] == 400
    assert not stream(server)
