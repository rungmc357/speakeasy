"""SPK-220: a picture or clip sent from Home's composer shows in the thread as itself (the message
carries ``files``), never as placeholder words or a path. Invented clips and pictures only."""
from __future__ import annotations

import json

from fakes import http, wait_for
from speakeasy import home_chat as HC
from test_home_chat import coordinator, post, stream
from test_staged_attachments import jpeg, reads_images, stage  # noqa: F401  (reads_images is a fixture)
from test_video_attachments import clips  # noqa: F401  (a fixture)


def _user(entries):
    return [e for e in entries if e["kind"] == "message" and e["role"] == "user"]


def test_a_caption_less_video_is_stored_and_shown_as_the_clip(server, service, hermes, clips):
    staged = stage(server, clips["mp4"], "file", "video/mp4", "porch-light.mp4")[1]
    seen = coordinator(service, {"reply": "Watching it now.", "action": "start"})
    status, body = post(server, {"text": "", "attachments": [staged["id"]], "client_id": "c_clip"})
    assert status == 200, body
    message = body["message"]
    assert message["text"] == ""
    [f] = message["files"]
    assert f["id"] == staged["id"] and f["kind"] == "video" and f["mime"] == "video/mp4"
    assert f["name"] == "porch-light.mp4" and 1.5 <= f["duration"] <= 2.5
    assert set(f) <= {"id", "kind", "mime", "name", "duration"}
    raw = json.dumps(message)
    assert "/" not in raw.replace("video/mp4", "") and "path" not in raw   # never a path
    # The thread (every device) shows the same, with no placeholder words.
    [shown] = _user(stream(server))
    assert shown["text"] == "" and shown["files"] == message["files"]
    assert "(a picture)" not in json.dumps(stream(server))
    # The coordinator's current turn never gets empty content; the job gets a video ask.
    assert seen[0][-1]["content"].strip()
    call = wait_for(lambda: hermes.calls)[0]
    assert "video I sent" in json.dumps(call["input"])


def test_empty_words_get_a_placeholder_only_in_the_model_history(server, service, hermes, reads_images):
    a = stage(server, jpeg())[1]["id"]
    coordinator(service, {"reply": "Got it.", "action": "start"})
    body = post(server, {"text": "", "attachments": [a], "client_id": "c_pic1"})[1]
    assert body["message"]["text"] == "" and body["message"]["files"][0]["kind"] == "picture"
    wait_for(lambda: hermes.calls)
    seen = coordinator(service, {"reply": "Hi again.", "action": "none"})
    post(server, {"text": "what did you think", "client_id": "c_pic2"})
    users = [m["content"] for m in seen[0] if m["role"] == "user"]
    assert "(sent a picture)" in users and "" not in users
    # ...and never in the thread.
    assert [e["text"] for e in _user(stream(server))] == ["", "what did you think"]


def test_words_and_files_together_keep_the_words(server, service, hermes, reads_images):
    a = stage(server, jpeg())[1]["id"]
    coordinator(service, {"reply": "Looking.", "action": "start"})
    body = post(server, {"text": "is this mold?", "attachments": [a], "client_id": "c_pic3"})[1]
    assert body["message"]["text"] == "is this mold?" and len(body["message"]["files"]) == 1


def test_plain_messages_carry_no_files(server, service):
    coordinator(service, {"reply": "Hey.", "action": "none"})
    body = post(server, {"text": "hi", "client_id": "c_plain"})[1]
    assert "files" not in body["message"] and "files" not in body["reply"]


def test_describe_is_read_only_device_scoped_and_pathless(service, clips, tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(clips["mp4"])
    att = service.staged.add("dev_a", "file", mime="video/mp4", size=len(clips["mp4"]), path=str(path),
                             name="clip.mp4")
    att.media = {"duration": 12.04, "frames": [{"path": str(tmp_path / "f0.jpg")}]}
    pic = service.staged.add("dev_a", "picture", mime="image/jpeg", size=10, data=b"x" * 10)
    doc = service.staged.add("dev_a", "file", mime="application/pdf", size=10, path=str(tmp_path / "a.pdf"),
                             name="lease.pdf")
    got = service.staged.describe("dev_a", [att.id, pic.id, doc.id, "att_missing"])
    assert got == [{"id": att.id, "mime": "video/mp4", "kind": "video", "name": "clip.mp4", "duration": 12.0},
                   {"id": pic.id, "mime": "image/jpeg", "kind": "picture"},
                   {"id": doc.id, "mime": "application/pdf", "kind": "file", "name": "lease.pdf"}]
    assert str(tmp_path) not in json.dumps(got)
    assert service.staged.describe("dev_b", [att.id]) == []
    assert len(service.staged.claim("dev_a", [att.id, pic.id, doc.id])) == 3   # still all there


def test_an_old_thread_without_the_column_migrates(service):
    db = service.store._db
    with service.store._lock, db:
        db.execute("DROP TABLE home_messages")
        db.execute("""CREATE TABLE home_messages (
            id TEXT PRIMARY KEY, client_id TEXT, role TEXT NOT NULL, text TEXT NOT NULL, at REAL NOT NULL,
            model TEXT, run_id TEXT, action TEXT, voice TEXT, reply_to TEXT)""")
        db.execute("INSERT INTO home_messages (id, role, text, at) VALUES ('hu_old', 'user', '(a picture)', ?)",
                   (service.home_chat.clock(),))
    chat = HC.HomeChat(service)
    assert "files" in {r[1] for r in db.execute("PRAGMA table_info(home_messages)")}
    [row] = chat.shown()
    assert row["files"] is None and "files" not in chat.entry(row)
    HC.HomeChat(service)   # idempotent


def test_sent_words():
    assert HC.sent_words([{"kind": "video"}]) == "(sent a video)"
    assert HC.sent_words([{"kind": "picture"}, {"kind": "picture"}]) == "(sent 2 pictures)"
    assert HC.sent_words([{"kind": "video"}, {"kind": "file"}]) == "(sent 2 files)"
    assert HC.sent_words(None) == "(sent something)"
    assert HC.look_words([{"kind": "video"}]) == "Take a look at the video I sent."
    assert HC.look_words([]) == "Take a look at what I sent."
