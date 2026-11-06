"""Voice messages sent as the recording itself, like a Telegram or Discord voice note: the clip is the
message, Hermes transcribes it on arrival, the agent gets the text the gateway gives it for a voice note,
and the reply carries the transcript for the bubble."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

import pytest

from fakes import http, wait_for
from speakeasy import transcribe as T
from test_history import NOW, add_msg, add_session, get, state  # noqa: F401 - the shared history fixture

CLIP = b"\x00\x00\x00\x18ftypM4A " + bytes(range(256)) * 8   # an invented clip: only its bytes matter


def send(server, path: str, data: bytes = CLIP, *, ctype: str = "audio/mp4", **headers: str):
    req = urllib.request.Request(f"{server.base_url}{path}", data=data, method="POST")
    req.add_header("Content-Type", ctype)
    req.add_header("Authorization", f"Bearer {server.token}")
    for name, value in headers.items():
        req.add_header("X-Speakeasy-" + name.replace("_", "-"), value)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def heard(service, tmp_path, monkeypatch):
    """Hermes' transcription, faked: records each clip it was given and says the scripted words."""
    seen: list[dict] = []
    words = {"text": "Order the cedar boards, then call the lumber yard about delivery"}
    monkeypatch.setattr(T, "keep_clip", lambda data, suffix, _d: _keep(tmp_path, data, suffix))

    def fake(path: str):
        with open(path, "rb") as f:
            seen.append({"path": path, "bytes": f.read()})
        if words["text"] is None:
            return {"success": False, "error": "provider down"}
        return {"success": True, "transcript": words["text"], "provider": "local"}
    service.transcriber = fake
    return seen, words


def _said(call) -> str:
    got = call["input"]
    return got if isinstance(got, str) else json.dumps(got, ensure_ascii=False)


def _keep(root, data, suffix):
    path = root / f"audio_{len(os.listdir(root))}{suffix}"
    path.write_bytes(data)
    return str(path)


def test_status_advertises_voice_notes(server):
    assert http(server.base_url, "GET", "/voice/status", token=server.token)[1]["capabilities"]["voice_notes"] is True


def test_the_recording_is_the_message_and_the_agent_sees_a_voice_note(server, service, hermes, state, heard):
    seen, words = heard
    add_session(state, "api_voice", "api_server", title="Shelf plan", at=NOW - 5)
    add_msg(state, "api_voice", "user", "Plan a shelf for the hallway", at=NOW - 9)
    status, body = send(server, "/voice/history/api_voice/voice-message", Client_Id="vm-one", Duration="31.4")
    assert status == 200, body
    assert body["voice"] == {"transcript": words["text"], "duration": 31.4}
    assert seen and seen[0]["bytes"] == CLIP                       # the whole clip went to Hermes
    call = wait_for(lambda: hermes.calls)[-1]
    assert call["session_id"] == "api_voice"
    # Exactly what the gateway gives the agent for a Telegram voice note: the transcript as one quoted line.
    assert f'"{words["text"]}"' in _said(call)
    # The page shows it as a voice message with the person's words (no quotes), its clip and length.
    add_msg(state, "api_voice", "user", f'"{words["text"]}"', at=NOW - 2)
    _, page = get(server, "/voice/history/api_voice")
    mine = [e for e in page["session"]["entries"] if e["kind"] == "user" and e.get("voice")]
    assert len(mine) == 1, page["session"]["entries"]
    assert mine[0]["text"] == words["text"]
    assert mine[0]["voice_clip"] == "vm-one" and mine[0]["voice_duration"] == 31.4
    # Only short hashes are kept, never the words.
    assert "cedar" not in (service.store.get_meta("voice_clips:api_voice") or "")


def test_a_pause_in_the_middle_keeps_both_halves(server, service, hermes, state, heard):
    """Speech, a two-second pause, more speech, stop: one clip, one message with both halves (nothing
    on the phone decides where the message ends)."""
    seen, words = heard
    words["text"] = "Book the table for four. Then text everyone the address."
    add_session(state, "api_pause", "api_server", at=NOW - 5)
    status, body = send(server, "/voice/history/api_pause/voice-message", Client_Id="vm-pause", Duration="9")
    assert status == 200, body
    assert len(seen) == 1
    sent = _said(wait_for(lambda: hermes.calls)[-1])
    assert "Book the table for four." in sent and "Then text everyone the address." in sent


def test_a_retry_is_never_transcribed_or_sent_twice(server, service, hermes, state, heard):
    seen, _ = heard
    add_session(state, "api_retry", "api_server", at=NOW - 5)
    first = send(server, "/voice/history/api_retry/voice-message", Client_Id="vm-retry", Duration="4")
    calls = len(wait_for(lambda: hermes.calls))
    again = send(server, "/voice/history/api_retry/voice-message", Client_Id="vm-retry", Duration="4")
    assert first[0] == again[0] == 200
    assert first[1]["run_id"] == again[1]["run_id"]
    assert len(seen) == 1 and len(hermes.calls) == calls


def test_a_clip_hermes_cant_read_still_goes_as_the_gateway_would_send_it(server, service, hermes, state, heard):
    seen, words = heard
    words["text"] = None
    add_session(state, "api_fail", "api_server", at=NOW - 5)
    status, body = send(server, "/voice/history/api_fail/voice-message", Client_Id="vm-fail", Duration="3")
    assert status == 200, body
    assert body["voice"]["transcript"] is None
    sent = _said(wait_for(lambda: hermes.calls)[-1])
    assert "voice message could not be transcribed automatically" in sent
    assert os.path.basename(seen[0]["path"]) in sent              # the agent can still listen to it


def test_empty_speech_gets_the_gateways_note(service, tmp_path):
    path = tmp_path / "a.m4a"
    path.write_bytes(CLIP)
    note, transcript = T.voice_note(str(path), lambda p: {"success": True, "transcript": "  "})
    assert transcript == "" and note == T.EMPTY_NOTE
    assert T.note_words(note) == "" and T.note_words('"Hello there"') == "Hello there"


def test_a_voice_message_to_a_task_goes_to_that_task(server, service, hermes, heard):
    seen, words = heard
    status, job = http(server.base_url, "POST", "/voice/work", {"text": "Find a quiet cafe near the river"},
                       server.token)
    assert status == 202, job
    wait_for(lambda: service.store.work(job["run_id"])["status"] == "completed")
    status, body = send(server, f"/voice/work/{job['run_id']}/voice-message", Client_Id="vm-task", Duration="5")
    assert status == 200, body
    assert body["voice"]["transcript"] == words["text"]
    assert f'"{words["text"]}"' in _said(hermes.calls[-1])


def test_not_audio_or_unknown_is_refused_before_reading(server, state, heard):
    seen, _ = heard
    assert send(server, "/voice/history/api_nope/voice-message", ctype="text/plain")[0] == 415
    assert send(server, "/voice/history/api_nope/voice-message", Client_Id="bad id!")[0] == 400
    assert send(server, "/voice/history/api_missing/voice-message", Client_Id="vm-x")[0] == 404
    assert not seen
