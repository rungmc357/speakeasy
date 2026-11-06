"""A task that carried pictures or files and failed: its retry (the apps' Retry, a typed "try
again", a follow-up said on a call) takes the same pictures and files again, from Speakeasy's saved
copies. The failed run may never have reached Hermes with them, so a retry with words only would
answer without ever seeing them. Real HTTP against the server; Hermes is the fake API server.
Invented data only."""
from __future__ import annotations

import json

from fakes import http, upload_attachment, wait_for
from test_routing import start_call, tasks
from test_staged_attachments import images_in, jpeg, reads_images, stage, text_of  # noqa: F401


def _failed_job_with(server, hermes, picture: bytes, *, with_file: bool = False) -> dict:
    """A typed job carrying ``picture`` (and an invented PDF) whose run fails on Hermes."""
    hermes.fail_with = "HTTP 500: upstream exploded"
    ids = [stage(server, picture, name="shelf.jpg")[1]["id"]]
    if with_file:
        ids.append(stage(server, b"%PDF-1.7 invented parts list", "file", "application/pdf", "parts.pdf")[1]["id"])
    status, body = http(server.base_url, "POST", "/voice/work",
                        {"text": "Which bracket fits this shelf?", "attachments": ids}, server.token)
    assert status == 202, body
    run_id = body["run_id"]
    return wait_for(lambda: next((t for t in tasks(server) if t.get("run_id") == run_id and t["status"] == "failed"),
                                 None))


def test_retrying_a_failed_job_sends_its_picture_and_file_again(server, service, hermes, home, reads_images):
    picture = jpeg(b"invented-shelf ")
    failed = _failed_job_with(server, hermes, picture, with_file=True)
    first = hermes.calls[0]
    assert images_in(first["input"]) == [picture]
    hermes.fail_with = None
    # The apps' Retry: a message to the failed task, words only.
    status, body = http(server.base_url, "POST", f"/voice/work/{failed['run_id']}/message",
                        {"text": "That failed. Please try again: Which bracket fits this shelf?"}, server.token)
    assert status == 200 and body["delivered_as"] == "follow_up", body
    retry = wait_for(lambda: hermes.calls[1:])[0]
    assert images_in(retry["input"]) == [picture]                       # the same picture, as an image part
    prompt = text_of(retry["input"])
    assert "parts.pdf" in prompt and str(home / "cache" / "speakeasy" / "shared") in prompt  # the file by its copy
    row = wait_for(lambda: next((t for t in tasks(server) if t.get("run_id") == body["run_id"] and t.get("shared")),
                                None))
    assert row["shared"] == [{"kind": "picture", "name": "shelf.jpg"}, {"kind": "file", "name": "parts.pdf"}]
    assert "path" not in json.dumps(row["shared"])


def test_a_retry_that_brings_the_same_picture_again_sends_it_once(server, service, hermes, reads_images):
    picture = jpeg(b"invented-shelf ")
    failed = _failed_job_with(server, hermes, picture)
    hermes.fail_with = None
    again = stage(server, picture, name="shelf.jpg")[1]["id"]
    other = jpeg(b"invented-wall ")
    extra = stage(server, other, name="wall.jpg")[1]["id"]
    status, body = http(server.base_url, "POST", f"/voice/work/{failed['run_id']}/message",
                        {"text": "Try again, here is the wall too", "attachments": [again, extra]}, server.token)
    assert status == 200, body
    retry = wait_for(lambda: hermes.calls[1:])[0]
    assert sorted(images_in(retry["input"])) == sorted([picture, other])  # no duplicate of the shelf


def test_a_retry_after_a_run_that_never_started_still_carries_the_picture(server, service, hermes, monkeypatch,
                                                                          reads_images):
    picture = jpeg(b"invented-receipt ")
    worker = service.text_worker()
    real = worker.hermes.start_run
    calls = {"n": 0}

    def refuse_once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("hermes is restarting")
        return real(*args, **kwargs)

    monkeypatch.setattr(worker.hermes, "start_run", refuse_once)
    a = stage(server, picture, name="receipt.jpg")[1]["id"]
    status, body = http(server.base_url, "POST", "/voice/work", {"text": "Add this receipt up", "attachments": [a]},
                        server.token)
    assert status == 202, body
    failed = wait_for(lambda: next((t for t in tasks(server) if t["status"] == "failed"), None))
    assert not hermes.calls                                               # it never reached Hermes
    status, body = http(server.base_url, "POST", f"/voice/work/{failed['run_id'] or failed['task_id']}/message",
                        {"text": "Try again"}, server.token)
    assert status == 200, body
    retry = wait_for(lambda: hermes.calls)[0]
    assert images_in(retry["input"]) == [picture]


def test_a_follow_up_to_a_finished_job_does_not_resend_its_picture(server, service, hermes, reads_images):
    picture = jpeg(b"invented-plant ")
    a = stage(server, picture, name="plant.jpg")[1]["id"]
    status, body = http(server.base_url, "POST", "/voice/work", {"text": "What plant is this?", "attachments": [a]},
                        server.token)
    assert status == 202
    done = wait_for(lambda: next((t for t in tasks(server) if t.get("run_id") == body["run_id"]
                                  and t["status"] == "completed"), None))
    status, reply = http(server.base_url, "POST", f"/voice/work/{done['run_id']}/message",
                         {"text": "How often should I water it?"}, server.token)
    assert status == 200, reply
    follow = wait_for(lambda: hermes.calls[1:])[0]
    assert images_in(follow["input"]) == []          # it continues the session, which already has the picture


def test_retry_on_a_call_carries_the_picture_dropped_on_the_panel(server, service, hermes, reads_images):
    picture = jpeg(b"invented-map ")
    session, worker = start_call(server, service)
    hermes.fail_with = "HTTP 500: upstream exploded"
    status, _ = upload_attachment(server.base_url, server.token, session["interaction_id"], picture)
    assert status == 200
    worker.feed({"type": "session.output_transcript.delta", "delta": "Okay.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_1", "Plan a route on this map")
    failed = wait_for(lambda: next((t for t in tasks(server) if t["task_id"] == "call_1" and t["status"] == "failed"),
                                   None))
    assert images_in(hermes.calls[0]["input"]) == [picture]
    hermes.fail_with = None
    # The panel's Retry during the call: a message to the failed task naming the call.
    status, body = http(server.base_url, "POST", f"/voice/work/{failed['run_id']}/message",
                        {"text": "That failed. Please try again: Plan a route on this map",
                         "interaction_id": session["interaction_id"]}, server.token)
    assert status == 200, body
    retry = wait_for(lambda: hermes.calls[1:])[0]
    assert images_in(retry["input"]) == [picture]
    assert not worker.interaction.attachments.capture_for(body["task"]["task_id"])  # never a capture


def test_a_retry_whose_saved_copy_is_gone_goes_with_words(server, service, hermes, home, reads_images):
    picture = jpeg(b"invented-shelf ")
    failed = _failed_job_with(server, hermes, picture)
    for copy in (home / "cache" / "speakeasy" / "shared").glob("*/*"):
        copy.unlink()
    hermes.fail_with = None
    status, body = http(server.base_url, "POST", f"/voice/work/{failed['run_id']}/message", {"text": "Try again"},
                        server.token)
    assert status == 200, body
    retry = wait_for(lambda: hermes.calls[1:])[0]
    assert images_in(retry["input"]) == [] and "Try again" in text_of(retry["input"])
