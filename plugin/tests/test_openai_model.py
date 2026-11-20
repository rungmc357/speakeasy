"""The API-key voice uses gpt-live-1.1, and falls back to gpt-live-1 when the account can't use it yet.

Live check 2026-11-20: `gpt-live-1.1` creates sessions with the same price ($0.05/min), events, client
delegation and voices as `gpt-live-1`. An account or region where 1.1 isn't rolled out must still get a call.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from speakeasy import openai_live
from speakeasy.calls import ServiceError


@pytest.fixture(autouse=True)
def fresh_model_memory():
    openai_live.reset_model_memory()
    yield
    openai_live.reset_model_memory()


def _http_error(code: int, body: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.openai.com/v1/live/sessions", code, "err", {},
                                  io.BytesIO(json.dumps(body).encode()))


def test_new_calls_ask_for_gpt_live_1_1():
    seen = []

    def negotiate(key, payload):
        seen.append(payload["session"]["model"])
        return {"session": {"id": "sess_1"}, "transport": {"sdp": "v=0"}}

    result = openai_live.start_session(negotiate, "k", "be brief", "marin", "v=0", [])
    assert seen == ["gpt-live-1.1"]
    assert result["session"]["id"] == "sess_1"


def test_a_model_refusal_falls_back_to_gpt_live_1_and_is_remembered():
    seen = []

    def negotiate(key, payload):
        model = payload["session"]["model"]
        seen.append(model)
        if model == "gpt-live-1.1":
            raise ServiceError(400, "GPT-Live session creation failed (400)", reason=openai_live.MODEL_REFUSED)
        return {"session": {"id": "sess_2"}, "transport": {"sdp": "v=0"}}

    assert openai_live.start_session(negotiate, "k", "i", "marin", "v=0", [])["session"]["id"] == "sess_2"
    assert seen == ["gpt-live-1.1", "gpt-live-1"]
    # The next call doesn't pay for the refused try again.
    openai_live.start_session(negotiate, "k", "i", "marin", "v=0", [])
    assert seen == ["gpt-live-1.1", "gpt-live-1", "gpt-live-1"]


def test_other_failures_are_not_retried_on_the_older_model():
    seen = []

    def negotiate(key, payload):
        seen.append(payload["session"]["model"])
        raise ServiceError(401, "GPT-Live session creation failed (401)")

    with pytest.raises(ServiceError) as raised:
        openai_live.start_session(negotiate, "k", "i", "marin", "v=0", [])
    assert raised.value.status == 401
    assert seen == ["gpt-live-1.1"]


def test_negotiate_marks_a_model_refusal_from_the_error_body():
    def opener(req, timeout=0):
        raise _http_error(400, {"error": {"type": "invalid_request_error", "code": "model_not_found",
                                          "param": "session.model",
                                          "message": "The model `gpt-live-1.1` does not exist or you do not have access to it."}})
    with pytest.raises(ServiceError) as raised:
        openai_live.negotiate("k", {"session": {"model": "gpt-live-1.1"}}, opener=opener)
    assert raised.value.reason == openai_live.MODEL_REFUSED
    assert raised.value.status == 400


def test_negotiate_leaves_other_client_errors_unmarked():
    def opener(req, timeout=0):
        raise _http_error(400, {"error": {"type": "invalid_request_error", "param": "transport.sdp",
                                          "message": "Invalid SDP offer."}})
    with pytest.raises(ServiceError) as raised:
        openai_live.negotiate("k", {"session": {"model": "gpt-live-1.1"}}, opener=opener)
    assert raised.value.reason is None
