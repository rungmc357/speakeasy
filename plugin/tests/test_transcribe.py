"""Voice messages transcribed by Hermes (POST /voice/transcribe): the app sends the recorded audio
and gets back the text Hermes' own transcription made, as for a Telegram voice note."""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

from fakes import http
from speakeasy import transcribe as T


def send(server, data: bytes, ctype: str = "audio/mp4", token: str | None | bool = None):
    req = urllib.request.Request(f"{server.base_url}/voice/transcribe", data=data, method="POST")
    req.add_header("Content-Type", ctype)
    if token is not False:
        req.add_header("Authorization", f"Bearer {token or server.token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_status_advertises_transcription(server):
    assert http(server.base_url, "GET", "/voice/status", token=server.token)[1]["capabilities"]["transcribe"] is True


def test_the_whole_clip_goes_to_hermes_and_its_text_comes_back(server, service, caplog):
    seen = {}

    def fake(path: str):
        seen["suffix"] = os.path.splitext(path)[1]
        with open(path, "rb") as f:
            seen["bytes"] = f.read()
        seen["path"] = path
        return {"success": True, "transcript": "  rename the files in the drafts folder ", "provider": "command"}

    service.transcriber = fake
    with caplog.at_level(logging.DEBUG):
        status, body = send(server, b"m4a-audio-bytes")
    assert status == 200
    assert body == {"text": "rename the files in the drafts folder", "provider": "command"}
    assert seen["suffix"] == ".m4a" and seen["bytes"] == b"m4a-audio-bytes"
    assert not os.path.exists(seen["path"]), "the audio is deleted once transcribed"
    assert "rename the files" not in caplog.text and "m4a-audio-bytes" not in caplog.text


def test_refusals(server, service):
    service.transcriber = lambda path: {"success": True, "transcript": "x"}
    assert send(server, b"abc", token=False)[0] == 401
    assert send(server, b"abc", ctype="image/jpeg")[0] == 415
    import pytest
    from speakeasy.calls import ServiceError
    with pytest.raises(ServiceError) as too_big:  # checked from Content-Length, before any body is read
        T.check("audio/mp4", T.MAX_AUDIO_BYTES + 1)
    assert too_big.value.status == 413 and too_big.value.reason == "too_large"
    assert T.check("audio/x-m4a; codecs=aac", 10) == ".m4a"
    service.transcriber = lambda path: {"success": False, "error": "provider down"}
    status, body = send(server, b"abc")
    assert status == 502 and body["reason"] == "failed"


def test_a_hermes_without_transcription_says_so():
    import pytest
    from speakeasy.calls import ServiceError
    with pytest.raises(ServiceError) as exc:
        T.transcribe(b"abc", ".m4a", None)
    assert exc.value.status == 503 and exc.value.reason == "unavailable"
