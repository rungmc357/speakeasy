"""Files your agent made (docs/API.md "Files"): every file Hermes handed over in a reply, listed by
opaque id, downloadable only when Hermes itself would deliver it. Invented chats and files."""
import json
import sqlite3
import sys
import time

import pytest

from fakes import http
from speakeasy import files as F

NOW = time.time()


def make_db(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, display_name TEXT,
                  chat_type TEXT, parent_session_id TEXT, started_at REAL NOT NULL, hidden INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
                  content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT, timestamp REAL)""")
    db.commit()
    db.close()


def add_session(path, sid, source, title=None, parent=None, display=None):
    db = sqlite3.connect(path)
    db.execute("INSERT INTO sessions (id, source, title, display_name, parent_session_id, started_at) VALUES (?,?,?,?,?,?)",
               (sid, source, title, display, parent, NOW - 3600))
    db.commit()
    db.close()


def add_msg(path, sid, role, content, at):
    db = sqlite3.connect(path)
    db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)", (sid, role, content, at))
    db.commit()
    db.close()


@pytest.fixture(params=["hermes", "fallback"])
def mode(request, monkeypatch):
    """Both with Hermes' own extractor/delivery check and without it (an older Hermes)."""
    if request.param == "fallback":
        monkeypatch.setitem(sys.modules, "gateway.platforms.base", None)
    else:
        pytest.importorskip("gateway.platforms.base")
    return request.param


@pytest.fixture
def made(service, mode):
    home = service.home
    images, docs = home / "cache" / "images", home / "cache" / "documents"
    images.mkdir(parents=True, exist_ok=True)
    docs.mkdir(parents=True, exist_ok=True)
    chart = images / "porch-paint-chart.png"
    from PIL import Image
    Image.new("RGB", (900, 600), (40, 120, 200)).save(chart)
    plan = docs / "garden-plan.pdf"
    plan.write_bytes(b"%PDF-1.4\n" + b"x" * 5000)
    (home / ".env").write_text("SOME_KEY=" + "nope" * 4)
    db = service.rt.state_db
    make_db(db)
    add_session(db, "d1", "discord", title="Porch repaint", display="Workshop / #build")
    add_session(db, "t1", "telegram", display="Sam")
    add_session(db, "sub1", "subagent", title="helper", parent="t1")
    add_msg(db, "d1", "user", f"MEDIA:{chart}", NOW - 900)  # the person's own words don't count
    add_msg(db, "d1", "assistant", f"Here's the chart.\nMEDIA:{chart}", NOW - 800)
    add_msg(db, "sub1", "assistant", f"Plan done.\nMEDIA:{plan}", NOW - 700)
    add_msg(db, "t1", "assistant", f"Saved.\nMEDIA:{home / '.env'}\nMEDIA:{images / 'gone.png'}", NOW - 600)
    add_msg(db, "d1", "assistant", f"Updated chart.\nMEDIA:{chart}", NOW - 500)
    return {"chart": chart, "plan": plan}


def get(server, path, headers=None):
    return http(server.base_url, "GET", path, token=server.token, headers=headers)


def test_lists_each_file_once_newest_first_with_its_chat(server, made):
    status, body = get(server, "/voice/files")
    assert status == 200
    names = [f["name"] for f in body["files"]]
    assert names == ["porch-paint-chart.png", "garden-plan.pdf"]  # newest mention first, one row each
    chart, plan = body["files"]
    assert chart["kind"] == "image" and chart["mime"] == "image/png" and chart["session_id"] == "d1"
    assert chart["chat"] == "Porch repaint" and chart["source"].startswith("Discord")
    assert abs(chart["at"] - (NOW - 500)) < 1
    # a helper's file belongs to the chat that started the helper
    assert plan["session_id"] == "t1" and plan["kind"] == "document" and plan["bytes"] == 5009
    assert body["kinds"] == [{"id": "image", "label": "Images", "count": 1},
                             {"id": "document", "label": "Docs", "count": 1}]
    raw = json.dumps(body)
    assert str(made["chart"].parent) not in raw and "/cache/" not in raw  # never a path


def test_secrets_and_missing_files_never_list(server, made):
    _, body = get(server, "/voice/files")
    assert all(f["name"] not in (".env", "gone.png") for f in body["files"])


def test_each_file_names_the_reply_it_came_in(server, made):
    """Open chat (SPK-265): the file's message id is the session page's own entry id, so the app can
    scroll to that reply. A helper's file opens the parent chat with no message to point at."""
    _, body = get(server, "/voice/files")
    chart, plan = body["files"]
    assert chart["message_id"] == "m5"           # the newest reply that handed it over
    assert "message_id" not in plan
    status, page = get(server, "/voice/history/d1")
    assert status == 200
    entry = next(e for e in page["session"]["entries"] if e["id"] == chart["message_id"])
    assert [f["id"] for f in entry.get("files") or []] == [chart["id"]]


def test_a_file_from_a_home_job_points_at_home(service, made, monkeypatch):
    monkeypatch.setattr(service.home_chat, "started_runs", lambda: {"run_home"})
    monkeypatch.setattr(service.store, "keys_for_session",
                        lambda sid: [("k2", "run_later", "done"), ("k1", "run_home", "done")] if sid == "d1" else [])
    rows = service.files_list({})["files"]
    by_name = {r["name"]: r for r in rows}
    assert by_name["porch-paint-chart.png"]["home_run"] == "run_home"
    assert "home_run" not in by_name["garden-plan.pdf"]


def test_kind_filter_search_and_chat_filter(server, made):
    assert [f["name"] for f in get(server, "/voice/files?kind=document")[1]["files"]] == ["garden-plan.pdf"]
    assert [f["name"] for f in get(server, "/voice/files?q=porch")[1]["files"]] == ["porch-paint-chart.png"]
    assert [f["name"] for f in get(server, "/voice/files?session_id=t1")[1]["files"]] == ["garden-plan.pdf"]


def test_pages_with_a_cursor(server, made):
    _, first = get(server, "/voice/files?limit=1")
    assert len(first["files"]) == 1 and first["next"] == "1" and first["total"] == 2
    _, second = get(server, f"/voice/files?limit=1&cursor={first['next']}")
    assert [f["name"] for f in second["files"]] == ["garden-plan.pdf"] and second["next"] is None


def test_download_whole_file_and_a_range(server, made):
    _, body = get(server, "/voice/files?kind=document")
    fid = body["files"][0]["id"]
    status, data = get(server, f"/voice/files/{fid}")
    assert status == 200 and data == made["plan"].read_bytes()
    status, part = get(server, f"/voice/files/{fid}", headers={"Range": "bytes=0-7"})
    assert status == 206 and part == b"%PDF-1.4"


def test_thumbnail_is_a_small_jpeg(server, made):
    _, body = get(server, "/voice/files?kind=image")
    status, data = get(server, f"/voice/files/{body['files'][0]['id']}/thumb")
    assert status == 200 and data[:3] == b"\xff\xd8\xff"
    from io import BytesIO
    from PIL import Image
    assert max(Image.open(BytesIO(data)).size) <= F.THUMB_PX


def test_a_deleted_file_is_404_and_drops_off_the_list(server, made):
    _, body = get(server, "/voice/files?kind=document")
    fid = body["files"][0]["id"]
    made["plan"].unlink()
    assert get(server, f"/voice/files/{fid}")[0] == 404
    assert [f["name"] for f in get(server, "/voice/files")[1]["files"]] == ["porch-paint-chart.png"]


def test_unknown_or_malformed_ids_are_404(server, made):
    assert get(server, "/voice/files/" + "a" * 26)[0] == 404
    assert get(server, "/voice/files/..%2F.env")[0] == 404


def test_new_replies_show_up(server, service, made):
    get(server, "/voice/files")
    extra = service.home / "cache" / "documents" / "seed-list.csv"
    extra.write_text("tomato,basil\n")
    add_msg(service.rt.state_db, "d1", "assistant", f"List:\nMEDIA:{extra}", NOW - 10)
    service.files.scanned_at = 0
    assert get(server, "/voice/files")[1]["files"][0]["name"] == "seed-list.csv"


def test_files_need_a_device_token(server, made):
    status, _ = http(server.base_url, "GET", "/voice/files")
    assert status == 401


def test_ids_are_stable_across_restarts(service, made):
    one = F.FileIndex(service.rt.state_db, service.home, service.dir / "files.key")
    two = F.FileIndex(service.rt.state_db, service.home, service.dir / "files.key")
    assert one.file_id(made["chart"]) == two.file_id(made["chart"])
    assert F.ID_RE.fullmatch(one.file_id(made["chart"]))


def test_a_slow_first_scan_says_loading_instead_of_hanging(service, made, monkeypatch):
    import threading
    gate = threading.Event()
    real = F.FileIndex.refresh

    def slow(self, *, force=False):
        gate.wait(5)
        return real(self, force=force)
    monkeypatch.setattr(F.FileIndex, "refresh", slow)
    monkeypatch.setattr(F, "FIRST_WAIT_S", 0.05)
    first = service.files_list({})
    assert first["loading"] is True and first["files"] == []
    gate.set()
    service.files.ready.wait(5)
    later = service.files_list({})
    assert later["loading"] is False and len(later["files"]) == 2


def test_status_advertises_files():
    from speakeasy.service import CAPABILITIES
    assert CAPABILITIES["files"] is True


def test_a_missing_or_unlisted_file_skips_hermes_slow_check(service, made, monkeypatch):
    """Hermes' delivery check resolves every denied prefix per call (~6 ms each): a scan of a big
    history must not pay that for files that are gone or of a type Files never lists (SPK-264)."""
    base = pytest.importorskip("gateway.platforms.base")
    if sys.modules.get("gateway.platforms.base") is None:
        pytest.skip("older Hermes mode")
    seen = []
    real = base.validate_media_delivery_path
    monkeypatch.setattr(base, "validate_media_delivery_path", lambda p, *a, **k: seen.append(p) or real(p, *a, **k))
    gone = service.home / "cache" / "images" / "gone.png"
    assert F.deliverable(str(gone), service.home) is None
    assert F.deliverable(str(service.home / "cache" / "images"), service.home) is None   # a folder
    assert seen == []
    assert F.deliverable(str(made["chart"]), service.home) is not None
    assert len(seen) == 1


def test_one_file_named_in_many_replies_is_checked_once(service, made, monkeypatch):
    calls = []
    real = F.deliverable
    monkeypatch.setattr(F, "deliverable", lambda raw, home: calls.append(raw) or real(raw, home))
    for i in range(5):
        add_msg(service.rt.state_db, "d1", "assistant", f"Again.\nMEDIA:{made['chart']}", NOW - 400 + i)
    service.files.refresh(force=True)
    assert calls.count(str(made["chart"])) == 1


def test_the_index_warms_up_after_start_so_the_first_open_is_instant(service, made, monkeypatch):
    import speakeasy.service as S
    monkeypatch.setattr(S, "FILES_WARM_AFTER_S", 0.01)
    assert not service.files.ready.is_set()
    service._warm_files()
    assert service.files.ready.wait(5)
    first = service.files_list({})
    assert first["loading"] is False and len(first["files"]) == 2


def test_a_cold_first_list_answers_well_before_the_app_gives_up():
    assert F.FIRST_WAIT_S <= 3   # the app waits 30 s; the reply says loading and the app asks again


def test_outside_the_media_caches_only_with_hermes_rules(service, monkeypatch, tmp_path):
    """Without Hermes' delivery check only Hermes' own media caches count."""
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", None)
    stray = tmp_path / "notes.txt"
    stray.write_text("hi")
    assert F.deliverable(str(stray), service.home) is None
    inside = service.home / "cache" / "documents"
    inside.mkdir(parents=True, exist_ok=True)
    (inside / "notes.txt").write_text("hi")
    assert F.deliverable(str(inside / "notes.txt"), service.home) is not None
    assert F.deliverable(str(inside / "script.py"), service.home) is None


# -- pictures in a chat's thread (SPK-214) ---------------------------------------------------------

def test_a_thread_reply_carries_its_pictures_not_the_tag_text(server, made):
    status, body = get(server, "/voice/history/d1")
    assert status == 200
    replies = [e for e in body["session"]["entries"] if e["kind"] == "assistant"]
    assert [e["text"] for e in replies] == ["Here's the chart.", "Updated chart."]
    assert all("MEDIA:" not in json.dumps(e) for e in replies)
    shot = replies[0]["files"][0]
    assert shot["name"] == "porch-paint-chart.png" and shot["kind"] == "image" and shot["mime"] == "image/png"
    assert str(made["chart"].parent) not in json.dumps(replies)   # an id, never a path
    status, _ = get(server, f"/voice/files/{shot['id']}/thumb")
    assert status == 200   # the same id the Files page uses


def test_thread_tags_keep_hermes_rules(service, made):
    home = service.home
    text = (f"Saved.\nMEDIA:{home / '.env'}\nMEDIA:{home / 'cache' / 'images' / 'gone.png'}\n"
            f"```\nMEDIA:{made['chart']}\n```")
    clean, files = service.files.message_files(text, session_id="t1")
    assert files == []
    assert f"MEDIA:{home / '.env'}" in clean          # refused: left as it was, never served
    assert "gone.png" not in clean                 # missing pictures vanish; no path or dead tile
    assert f"MEDIA:{made['chart']}" in clean          # inside a code block: words, not a picture


def test_one_picture_named_twice_shows_once(service, made):
    clean, files = service.files.message_files(f"Two views.\nMEDIA:{made['chart']}\n  MEDIA:`{made['chart']}`",
                                               session_id="d1")
    assert clean == "Two views." and len(files) == 1
