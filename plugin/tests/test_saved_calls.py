"""Saved-call access after the phone has lost its local transcript. Invented data only."""
import json
import time

from fakes import http
from speakeasy.saved_calls import conversation
from speakeasy.tune import CallLog, KEEP_DAYS
from test_history import get, state  # noqa: F401
from test_now import task


def test_saved_call_reopens_without_a_live_interaction(server, service, state):
    turns = [{"role": "user", "text": "Find a quiet hotel"},
             {"role": "assistant", "text": "Checking the options."},
             {"role": "assistant", "text": "The Harbor Inn is quiet."}]
    service.call_log.record("vi_saved", turns, [], device="private-device", client={"build": "123"})
    task(service.store, "k_saved", "completed", interaction="vi_saved", session="s_hotel",
         result={"spoken": "The Harbor Inn is quiet.", "full": "**Harbor Inn**\n\n[Hotel](https://example.com/hotel)\n\n```text\nQuiet rooms\n```"})
    task(service.store, "k_other", "completed", interaction="vi_other", session="s_other",
         result={"full": "Not part of this call."})
    service.interactions.clear()
    # Recreate the reader, as after plugin reload. No provider session or Hermes history required.
    service.call_log = CallLog(service.dir)
    status, body = get(server, "/voice/calls/vi_saved")
    assert status == 200 and body["interaction_id"] == "vi_saved"
    assert [t["kind"] for t in body["turns"]] == ["speech", "speech", "text"]
    assert body["turns"][1]["text"] == "Checking the options."
    assert body["turns"][2]["text"].startswith("**Harbor Inn**")
    assert "```text\nQuiet rooms\n```" in body["turns"][2]["text"]
    assert body["turns"][2]["task_id"] == "d_k_saved"
    assert "private-device" not in json.dumps(body) and "Not part" not in json.dumps(body)
    assert "client" not in body


def test_saved_calls_require_auth_and_work_on_another_paired_device(server, service):
    service.call_log.record("vi_saved", [{"role": "user", "text": "Check the weather"}], [])
    assert http(server.base_url, "GET", "/voice/calls/vi_saved")[0] == 401
    assert http(server.base_url, "GET", "/voice/calls/vi_saved", token="wrong")[0] == 401
    other, token = service.devices.redeem(service.devices.new_pairing_code(), "Tablet")
    assert http(server.base_url, "GET", "/voice/calls/vi_saved", token=token)[0] == 200
    service.devices.revoke(other)
    assert http(server.base_url, "GET", "/voice/calls/vi_saved", token=token)[0] == 401


def test_missing_or_expired_call_has_a_clear_error(server, service):
    assert get(server, "/voice/calls/vi_missing")[0] == 404
    service.call_log.clock = lambda: time.time() - (KEEP_DAYS + 1) * 86400
    service.call_log.record("vi_old", [{"role": "user", "text": "Old call"}], [])
    service.call_log = CallLog(service.dir)
    status, body = get(server, "/voice/calls/vi_old")
    assert status == 404 and "no longer saved" in body["error"]


def test_typed_turns_and_voice_only_calls_survive(server, service):
    service.call_log.record("vi_typed", [{"role": "user", "kind": "text", "text": "Compare both"},
                                        {"role": "assistant", "text": "The smaller one fits."}], [])
    turns = get(server, "/voice/calls/vi_typed")[1]["turns"]
    assert turns[0]["kind"] == "typed" and turns[1]["kind"] == "speech"


def test_written_answer_appears_once_and_an_acknowledgement_is_not_replaced():
    log = {"id": "vi_test", "turns": [{"role": "assistant", "text": "All set."},
                                       {"role": "assistant", "text": "The hotel is near the station."},
                                       {"role": "assistant", "text": "The hotel is near the station."}]}
    answer = {"task_id": "d_hotel", "spoken": "The hotel is near the station.", "full": "## Hotel\nFull details."}
    turns = conversation(log, [answer, answer])["turns"]
    assert [t["text"] for t in turns] == ["All set.", "## Hotel\nFull details."]


def test_repeated_words_after_a_new_ask_are_not_deleted():
    log = {"id": "vi_test", "turns": [{"role": "user", "text": "Which hotel?"},
                                       {"role": "assistant", "text": "Harbor Inn."},
                                       {"role": "user", "text": "Say its name again"},
                                       {"role": "assistant", "text": "Harbor Inn."}]}
    answer = {"task_id": "d_hotel", "spoken": "Harbor Inn.", "full": "**Harbor Inn** with a courtyard room."}
    turns = conversation(log, [answer])["turns"]
    assert len(turns) == 4 and turns[-1]["text"] == "Harbor Inn."
    assert turns[1]["kind"] == "text"


def test_unspoken_written_results_are_kept_and_bad_turns_are_dropped():
    log = {"id": "vi_test", "turns": [{"role": "system", "text": "internal"}, None,
                                       {"role": "user", "text": "Find a hotel"}]}
    turns = conversation(log, [{"task_id": "d_hotel", "full": "The result arrived after the call."}])["turns"]
    assert len(turns) == 2 and turns[-1]["kind"] == "text"
