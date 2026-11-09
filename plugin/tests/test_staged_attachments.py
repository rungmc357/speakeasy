"""Pictures and files from the iPhone/iPad composer: staged on the device's token
(POST /voice/attachments), then sent by id with a typed message to a job or a new job
(``attachments`` on /voice/work and /voice/work/{id}/message). They take the Mac's "look at this"
path from there: images as image parts of the run, files by saved path, all on the task's card.
Real HTTP against the server; Hermes is the fake API server. Invented data only."""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

import pytest

from fakes import http, wait_for
from test_routing import start_call, tasks
from speakeasy import attachments as A
from speakeasy.calls import ServiceError

JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
GPS_EXIF = b"Exif\x00\x00MM\x00*GPS-LAT-40.7128-LON-74.0060"
PNG_SIG = b"\x89PNG\r\n\x1a\n"


def jpeg(marker: bytes = b"pixels", size: int = 6000, exif: bool = False) -> bytes:
    """A JPEG-shaped body: SOI, APP0, optionally an Exif APP1 with a location, then a scan."""
    app1 = b""
    if exif:
        app1 = b"\xff\xe1" + (len(GPS_EXIF) + 2).to_bytes(2, "big") + GPS_EXIF
    scan = b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" + (marker * (size // len(marker) + 1))[:size] + b"\xff\xd9"
    return JPEG_HEAD + app1 + scan


def png_with_text() -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return len(body).to_bytes(4, "big") + kind + body + b"\x00\x00\x00\x00"
    return (PNG_SIG + chunk(b"IHDR", b"\x00" * 13) + chunk(b"tEXt", b"Location\x00Hoboken pier")
            + chunk(b"IDAT", b"pixels") + chunk(b"IEND", b""))


def stage(server, data: bytes, kind: str = "picture", ctype: str = "image/jpeg", name: str | None = None,
          token: str | None | bool = None, headers: dict[str, str] | None = None):
    req = urllib.request.Request(f"{server.base_url}/voice/attachments", data=data, method="POST")
    req.add_header("Content-Type", ctype)
    if kind:
        req.add_header("X-Speakeasy-Kind", kind)
    if name:
        req.add_header("X-Speakeasy-Filename", name)
    if token is not False:
        req.add_header("Authorization", f"Bearer {token or server.token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def second_device(server, service) -> str:
    code = service.devices.new_pairing_code()
    status, paired = http(server.base_url, "POST", "/voice/pair", {"code": code, "device_name": "Test iPad"})
    assert status == 201
    return paired["token"]


def images_in(run_input) -> list[bytes]:
    if isinstance(run_input, str):
        return []
    return [base64.b64decode(p["image_url"]["url"].split(",", 1)[1])
            for p in run_input[0]["content"] if p["type"] == "image_url"]


def text_of(run_input) -> str:
    return run_input if isinstance(run_input, str) else run_input[0]["content"][0]["text"]


def shared_copies(home) -> list:
    return sorted(p for p in (home / "cache" / "speakeasy" / "shared").glob("*/*"))


@pytest.fixture
def reads_images(service):
    service.hermes.image_support = {"images": True, "vision": "native"}


# -- staging: auth, types, sizes ------------------------------------------------------------------

def test_status_advertises_attachments_and_the_file_types(server):
    status = http(server.base_url, "GET", "/voice/status", token=server.token)[1]
    assert status["capabilities"]["attachments"] is True
    assert status["attachments"]["max_attachments"] == 3
    assert "application/pdf" in status["attachments"]["file_types"]


def test_staging_needs_the_device_token(server):
    assert stage(server, jpeg(), token=False)[0] == 401
    assert stage(server, jpeg(), token="not-a-real-token")[0] == 401
    assert http(server.base_url, "DELETE", "/voice/attachments/att_00000000", token="nope")[0] == 401


def test_a_picture_is_staged_and_a_file_is_saved_under_shared(server, service, home):
    status, picture = stage(server, jpeg())
    assert status == 200 and picture["kind"] == "picture" and picture["id"].startswith("att_")
    assert picture["expires_in"] == A.STAGED_TTL_S
    assert not shared_copies(home)                                   # a picture waits in memory
    status, doc = stage(server, b"%PDF-1.7 invented lease terms", "file", "application/pdf", "lease.pdf")
    assert status == 200 and doc["name"] == "lease.pdf"
    [saved] = shared_copies(home)
    assert saved.name == "lease.pdf" and saved.parent.parent == home / "cache" / "speakeasy" / "shared"
    assert oct(saved.stat().st_mode & 0o777) == "0o600"


@pytest.mark.parametrize("kind,ctype,data,name,code", [
    ("picture", "image/heic", b"\x00\x00\x00\x18ftypheic", None, 415),     # the phone converts HEIC first
    ("picture", "image/jpeg", PNG_SIG + b"rest", None, 415),               # bytes must match the type
    ("file", "application/zip", b"PK\x03\x04", "a.zip", 415),
    ("file", "application/pdf", b"not a pdf", "a.pdf", 415),
    ("file", "text/plain", b"bin\x00ary", "a.txt", 415),
    ("file", "text/plain", b"\xff\xfe\xfa", "a.txt", 415),
    ("file", "text/plain", b"notes", None, 400),                            # files need a name
    ("screen", "image/jpeg", jpeg(), None, 400),                            # only calls take captures
    ("", "image/jpeg", jpeg(), None, 400),
])
def test_unsupported_or_wrong_uploads_are_refused(server, kind, ctype, data, name, code):
    assert stage(server, data, kind, ctype, name)[0] == code


@pytest.mark.parametrize("ctype,name", [("video/mp4", "porch-4s.mp4"), ("video/quicktime", "porch-4s.mov")])
def test_video_attachments_is_advertised_with_staging_that_takes_videos(server, service, ctype, name):
    """SPK-230: the composers offer videos only with ``video_attachments``, so it's on exactly when
    staging takes the video types, whichever way either changes. Bad video bytes are still refused
    (test_video_attachments.py)."""
    status = http(server.base_url, "GET", "/voice/status", token=server.token)[1]
    takes = ctype in status["attachments"]["file_types"]
    assert status["capabilities"]["video_attachments"] is takes
    try:
        service.check_stage("device", {"X-Speakeasy-Kind": "file", "Content-Type": ctype,
                                       "X-Speakeasy-Filename": name}, 4096)
        staged = True
    except ServiceError as exc:
        assert exc.status == 415
        staged = False
    assert staged is takes


def test_oversized_uploads_are_refused_before_the_body_is_read(server):
    status, body = stage(server, b"\xff\xd8\xff" + b"\x00" * (A.MAX_IMAGE_BYTES + 1))
    assert status == 413 and body["reason"] == "too_large"
    status, body = stage(server, b"%PDF-" + b"0" * A.MAX_FILE_BYTES, "file", "application/pdf", "big.pdf")
    assert status == 413 and body["reason"] == "too_large"


def test_a_device_can_stage_only_so_many_at_once(server):
    for n in range(A.MAX_STAGED):
        assert stage(server, jpeg(str(n).encode()))[0] == 200
    status, body = stage(server, jpeg(b"one-more"))
    assert status == 409 and body["reason"] == "too_many"


# -- redaction ---------------------------------------------------------------------------------

def test_a_staged_pictures_location_is_stripped_before_it_is_kept(server, service):
    status, body = stage(server, jpeg(b"harbor", exif=True))
    assert status == 200
    [kept] = [a for mine in service.staged._items.values() for a in mine.values()]
    assert b"GPS-LAT" not in kept.data and b"Exif" not in kept.data
    assert kept.data.startswith(b"\xff\xd8") and b"harbor" in kept.data and kept.data.endswith(b"\xff\xd9")


def test_png_text_chunks_are_stripped():
    stripped = A.strip_metadata(png_with_text(), "image/png")
    assert b"Hoboken" not in stripped and b"IDAT" in stripped and stripped.endswith(b"IEND\x00\x00\x00\x00")
    broken = b"\xff\xd8\xff\xe1\x00"
    assert A.strip_metadata(broken, "image/jpeg") == broken     # never mangles what it can't parse


def test_logs_never_carry_bytes_or_names(server, service, hermes, caplog, reads_images):
    caplog.set_level(logging.DEBUG)
    picture = jpeg(b"SECRET-PIXELS-5512")
    a = stage(server, picture)[1]["id"]
    b = stage(server, b"%PDF-1.4 SECRET-DEED-0042", "file", "application/pdf", "custody%20plan.pdf")[1]["id"]
    assert http(server.base_url, "POST", "/voice/work", {"text": "Read these", "attachments": [a, b]},
                server.token)[0] == 202
    wait_for(lambda: hermes.calls)
    logged = caplog.text + "".join(repr(r.args) for r in caplog.records)
    assert "staged from a device" in logged and "carried 2 attachment" in logged
    for secret in ("SECRET-PIXELS-5512", "SECRET-DEED-0042", "custody", base64.b64encode(picture)[:40].decode()):
        assert secret not in logged


# -- removing, devices, expiry -----------------------------------------------------------------

def test_removing_a_staged_file_deletes_its_copy(server, home):
    doc = stage(server, b"invented grocery list", "file", "text/plain", "list.txt")[1]["id"]
    assert shared_copies(home)
    status, body = http(server.base_url, "DELETE", f"/voice/attachments/{doc}", token=server.token)
    assert status == 200 and body["removed"] is True
    assert not shared_copies(home)
    assert http(server.base_url, "DELETE", f"/voice/attachments/{doc}", token=server.token)[0] == 404


def test_one_device_never_sends_or_removes_anothers(server, service, hermes):
    other = second_device(server, service)
    mine = stage(server, jpeg())[1]["id"]
    assert http(server.base_url, "DELETE", f"/voice/attachments/{mine}", token=other)[0] == 404
    status, body = http(server.base_url, "POST", "/voice/work", {"text": "Look", "attachments": [mine]}, other)
    assert status == 404 and body["reason"] == "unknown"
    assert not hermes.calls


def test_what_nobody_sent_expires_with_its_copy(server, service, home):
    stage(server, b"invented notes", "file", "text/markdown", "notes.md")
    stage(server, jpeg())
    assert service.expire_staged(now=10**12) == 2
    assert not shared_copies(home) and not service.staged.file_paths()


# -- sending: a new job, a message to a job ------------------------------------------------------

def test_a_new_job_carries_a_picture_as_an_image_part_and_a_file_by_path(server, service, hermes, home, reads_images):
    picture = jpeg(b"invented-receipt ")
    a = stage(server, picture, name="receipt.jpg")[1]["id"]
    b = stage(server, b"%PDF-1.7 invented invoice", "file", "application/pdf", "invoice.pdf")[1]["id"]
    status, body = http(server.base_url, "POST", "/voice/work",
                        {"text": "Do these totals match?", "attachments": [a, b], "client_id": "c_att_1"},
                        server.token)
    assert status == 202, body
    assert [x["kind"] for x in body["attachments"]] == ["picture", "file"]
    call = wait_for(lambda: hermes.calls)[0]
    assert images_in(call["input"]) == [picture]
    prompt = text_of(call["input"])
    assert "Do these totals match?" in prompt and "invoice.pdf" in prompt
    assert str(home / "cache" / "speakeasy" / "shared") in prompt          # the file is named by its copy
    row = wait_for(lambda: [t for t in tasks(server) if t.get("run_id") == body["run_id"] and t.get("shared")])[0]
    assert row["shared"] == [{"kind": "picture", "name": "receipt.jpg"}, {"kind": "file", "name": "invoice.pdf"}]
    assert "path" not in json.dumps(row["shared"])
    status, raw = http_bytes(server, f"/voice/shared-image/{body['run_id']}/1")
    assert status == 200 and raw == picture                              # the job page shows it
    # Sent: gone from the device's pool; the same ids again are refused.
    again = http(server.base_url, "POST", "/voice/work", {"text": "Again", "attachments": [a]}, server.token)
    assert again[0] == 404


def test_a_message_that_is_only_attachments_gets_words(server, hermes, reads_images):
    a = stage(server, jpeg())[1]["id"]
    assert http(server.base_url, "POST", "/voice/work", {"text": "", "attachments": [a]}, server.token)[0] == 202
    call = wait_for(lambda: hermes.calls)[0]
    assert "Take a look at what I attached." in text_of(call["input"])
    assert http(server.base_url, "POST", "/voice/work", {"text": ""}, server.token)[0] == 400


def test_a_typed_follow_up_to_a_finished_job_carries_the_picture(server, service, hermes, reads_images):
    _, worker = start_call(server, service)
    worker.feed({"type": "session.output_transcript.delta", "delta": "Okay.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_1", "Plan the garden beds")
    task = wait_for(lambda: [t for t in tasks(server) if t["task_id"] == "call_1" and t["status"] == "completed"])[0]
    picture = jpeg(b"invented-garden-sketch ")
    a = stage(server, picture)[1]["id"]
    status, body = http(server.base_url, "POST", f"/voice/work/{task['run_id']}/message",
                        {"text": "Use this layout", "attachments": [a]}, server.token)
    assert status == 200 and body["delivered_as"] in {"follow_up", "answer"}, body
    follow = wait_for(lambda: [c for c in hermes.calls if images_in(c["input"])])[0]
    assert images_in(follow["input"]) == [picture] and "Use this layout" in text_of(follow["input"])
    assert not worker.interaction.attachments.capture_for(body["task"]["task_id"])  # never a capture


def test_a_typed_steer_names_the_saved_copy(server, service, hermes, home, reads_images):
    hermes.hold = True
    _, worker = start_call(server, service)
    worker.feed({"type": "session.output_transcript.delta", "delta": "Okay.", "start_ms": 1, "end_ms": 2})
    worker.delegate("call_1", "Draft the garden newsletter")
    task = wait_for(lambda: [t for t in tasks(server) if t["task_id"] == "call_1" and t.get("run_id")])[0]
    a = stage(server, jpeg(b"invented-flyer "), name="flyer.jpg")[1]["id"]
    status, body = http(server.base_url, "POST", f"/voice/work/{task['run_id']}/message",
                        {"text": "Match this flyer", "attachments": [a]}, server.token)
    assert status == 200 and body["delivered_as"] == "steer", body
    steer = wait_for(lambda: hermes.steers)[-1][1]
    assert "Match this flyer" in steer and str(home / "cache" / "speakeasy" / "shared") in steer


def test_too_many_or_unknown_ids_are_refused_and_nothing_is_lost(server, service, hermes):
    ids = [stage(server, jpeg(str(n).encode()))[1]["id"] for n in range(4)]
    status, body = http(server.base_url, "POST", "/voice/work", {"text": "x", "attachments": ids}, server.token)
    assert status == 400
    status, body = http(server.base_url, "POST", "/voice/work", {"text": "x", "attachments": ids[:2] + ["att_gone"]},
                        server.token)
    assert status == 404 and body["reason"] == "unknown"
    assert len(service.staged._items[next(iter(service.staged._items))]) == 4  # all still waiting
    for bad in ([1], "att_x", [ids[0], ids[0]]):
        assert http(server.base_url, "POST", "/voice/work", {"text": "x", "attachments": bad}, server.token)[0] == 400
    assert not hermes.calls


def test_a_failed_send_gives_the_attachments_back(server, service, monkeypatch):
    a = stage(server, jpeg())[1]["id"]
    def boom(*_a, **_k):
        raise RuntimeError("hermes down")
    monkeypatch.setattr(service, "_start_job", boom)
    with pytest.raises(Exception):
        service.start_job({"text": "x", "attachments": [a]}, next(iter(service.staged._items)))
    assert a in service.staged._items[next(iter(service.staged._items))]


def http_bytes(server, path: str) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{server.base_url}{path}")
    req.add_header("Authorization", f"Bearer {server.token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_a_picture_shared_during_a_call_loses_its_location_too(server, service):
    """The iPhone call screen sends pictures with the Mac's call upload; that strips them as well."""
    session, _ = start_call(server, service)
    iid = session["interaction_id"]
    req = urllib.request.Request(f"{server.base_url}/voice/interactions/{iid}/attachments",
                                 data=jpeg(b"dock", exif=True), method="POST")
    for key, value in {"Content-Type": "image/jpeg", "X-Speakeasy-Kind": "picture",
                       "Authorization": f"Bearer {server.token}"}.items():
        req.add_header(key, value)
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200
    [pending] = service.interaction(iid).attachments.pending()
    assert b"GPS-LAT" not in pending.data and b"dock" in pending.data
