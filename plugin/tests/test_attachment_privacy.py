"""Look at this, privacy: a shared screen, picture or file goes from the Mac to the plugin to the
user's own Hermes, and nowhere else. Everything else a request touches (the routing model, task
naming, quick answers, Jev, home control, chat notices, the call log, the voice, continuity snippets,
the logs and the app's event feed) never sees image data or a file's contents."""
from __future__ import annotations

import base64
import json
import logging
import sqlite3
import time

import pytest

from fakes import SDP, FakeLiveWorker, FakeMac, FakeTransport, http, upload_attachment, wait_for
from speakeasy import calls, continuity, jev
from speakeasy.home_control import Reply

JPEG_HEAD = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01"


def jpeg(marker: bytes, size: int = 20_000) -> bytes:
    return JPEG_HEAD + (marker * (size // len(marker) + 1))[:size - len(JPEG_HEAD)]


PICTURE = jpeg(b"SECRET-PIXELS-5521 ")
WINDOW = jpeg(b"SECRET-WINDOW-0915 ")
CHAT_IMAGE = jpeg(b"SECRET-CHAT-IMAGE-3307 ")   # an image Hermes stored in a continued chat earlier
CONTRACT = b"%PDF-1.7\n" + b"SECRET-CONTRACT-4410 terms and signatures " * 400
SECRETS = (b"SECRET-PIXELS-5521", b"SECRET-WINDOW-0915", b"SECRET-CHAT-IMAGE-3307", b"SECRET-CONTRACT-4410")


def leaks(text: str) -> list[str]:
    """What in this string would be shared data: base64, a data URL, raw bytes or a file's contents."""
    found = [word for word in ("base64", "data:image") if word in text]
    found += [s.decode() for s in SECRETS if s.decode() in text]
    for data in (PICTURE, WINDOW, CHAT_IMAGE, CONTRACT):
        encoded = base64.b64encode(data).decode()
        found += [encoded[i:i + 40] for i in (0, 400, 4000) if encoded[i:i + 40] in text]
    return found


def chat_db(path) -> None:
    """A DM the user continued from voice before, whose stored message carries an image (Hermes keeps
    a message with parts as "\\x00json:" + JSON)."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_type TEXT, thread_id TEXT, "
               "origin_json TEXT, title TEXT, started_at REAL, ended_at REAL)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
               "content TEXT, tool_calls TEXT, timestamp REAL)")
    now = time.time()
    origin = {"platform": "telegram", "chat_id": "555", "chat_type": "dm", "user_id": "42", "chat_name": "Sam"}
    db.execute("INSERT INTO sessions VALUES ('s_invoice','telegram','dm','',?,?,?,NULL)",
               (json.dumps(origin), "Invoice draft", now - 7200))
    parts = [{"type": "text", "text": "[sam] the invoice draft has a wrong total"},
             {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,"
                                                 + base64.b64encode(CHAT_IMAGE).decode()}}]
    for role, content, age in (("user", "\x00json:" + json.dumps(parts), 600),
                               ("assistant", "The total double-counts shipping; fixed.", 590)):
        db.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)",
                   ("s_invoice", role, content, None, now - age))
    db.commit()
    db.close()


def say(worker, delegation_id: str, words: str, **extra) -> None:
    """One spoken request after the voice's last reply, so it reads as a request of its own."""
    worker.feed({"type": "session.output_transcript.delta", "delta": "Sure.", "start_ms": 1, "end_ms": 2})
    worker.delegate(delegation_id, words, **extra)


@pytest.fixture(autouse=True)
def quick_settle(monkeypatch):
    monkeypatch.setattr(calls, "SETTLE_S", 0.2)


def test_shared_things_reach_only_hermes(home, hermes, monkeypatch, caplog):
    from speakeasy.server import SpeakeasyServer
    from speakeasy.service import VoiceService
    caplog.set_level(logging.DEBUG)
    sinks: list[tuple[str, str]] = []

    def record(name: str, *values) -> None:
        sinks.append((name, json.dumps(values, default=repr)))

    def route_call(messages):
        record("routing", messages)
        if "in that chat" in messages[-1]["content"].rsplit("Request: ", 1)[-1]:
            return '{"follow_up_task_id": null, "conversation": "c1", "parts": ["x"], "channel": null}'
        return None

    def jev_route(hermes_home, provider, request, open_tasks=None, **kw):
        record("jev", request, open_tasks)
        return ("quick", 0.99, 3) if "hockey" in request else (None, 0.0, 3)

    def home_respond(request, notes, answering=None, following=False):
        record("home", request, notes, answering)
        return Reply(True, "Kitchen lights at 30%.") if "kitchen lights" in request else None

    workers: list[FakeLiveWorker] = []
    svc = VoiceService(home, notifier=None, start_threads=False, codex_factory=FakeTransport,
                       openai_negotiate=lambda key, payload: {"session": {"id": "sess_fake"}, "transport": {"sdp": "v=0\r\n"}},
                       openai_worker=lambda rt, i: workers.append(FakeLiveWorker(rt, i)) or workers[-1],
                       route_call=route_call,
                       title_call=lambda request: record("title", request) or "A task",
                       polish_call=lambda request: record("polish", request) or None,
                       status_call=lambda request: record("status", request) or None)
    svc.settings.patch({"voice": {"provider": "openai"}, "fast_routing": {"jev": "venice"},
                        "delivery": {"target": "telegram:555", "channels": []}})
    svc.hermes.image_support = {"images": True, "vision": "native"}
    svc.rt.quick_call = lambda request, today: record("quick", request, today) or "The Otters won 5 to 2."
    svc.rt.home = type("Home", (), {"enabled": True, "respond": staticmethod(home_respond)})()
    monkeypatch.setattr(jev, "route", jev_route)
    monkeypatch.setattr(jev, "placement", lambda *a, **kw: record("jev_place", a, kw) or None)
    monkeypatch.setattr(svc.notices, "post", lambda key, text, limit=600, target=None:
                        record("notice", key, text, target) or True)
    real_log = svc.call_log.record
    monkeypatch.setattr(svc.call_log, "record", lambda *a, **kw: record("call_log", a, kw) or real_log(*a, **kw))
    streamed: list[tuple[str, object]] = []

    def stream(base, key, conv, message, callback, **kw):
        streamed.append((conv.session_id, message))
        callback("run.started", {"run_id": "run_chat1"})
        callback("assistant.completed", {"content": "Fixed the total.\nSPOKEN: Fixed the total."})
        callback("run.completed", {})
        return True
    monkeypatch.setattr(continuity, "stream_session_chat", stream)
    monkeypatch.setattr(continuity, "session_busy", lambda db, sid, **kw: False)
    chat_db(home / "state.db")
    hermes.responder = lambda prompt, sid: "Looked at it.\nDONE: Checked\nSPOKEN: The error is a missing semicolon."

    srv = SpeakeasyServer(svc, "127.0.0.1", 0)
    srv.start()
    mac = None
    try:
        status, paired = http(srv.base_url, "POST", "/voice/pair", {"code": svc.devices.new_pairing_code(),
                                                                    "device_name": "Mac"})
        token = paired["token"]
        status, session = http(srv.base_url, "POST", "/voice/sessions", {"sdp": SDP, "screen": "ready"}, token,
                               {"Idempotency-Key": "req_privacy"})
        assert status == 201
        iid = session["interaction_id"]
        worker = workers[-1]
        interaction = svc.interaction(iid)
        mac = FakeMac(srv.base_url, token, interaction, lambda capture_id: {"data": WINDOW, "app": "Xcode"})
        assert http(srv.base_url, "POST", f"/voice/interactions/{iid}/screen", {"on": True, "seq": 1}, token)[0] == 200
        assert upload_attachment(srv.base_url, token, iid, PICTURE)[0] == 200
        assert upload_attachment(srv.base_url, token, iid, CONTRACT, "file", "application/pdf",
                                 {"X-Speakeasy-Filename": "contract.pdf"})[0] == 200

        # A new task carrying all three; then home control, the instant and quick lanes (captures dropped).
        say(worker, "call_err", "What's this error in the invoice draft?")
        wait_for(lambda: hermes.calls)
        say(worker, "call_home", "kitchen lights to 30%")
        wait_for(lambda: any("Kitchen lights" in c for _, _, c in worker.sent))
        say(worker, "call_nairobi", "what time is it in Nairobi")
        wait_for(lambda: any("Nairobi" in c for _, _, c in worker.sent))
        say(worker, "call_hockey", "who won the hockey game last night")
        wait_for(lambda: any("Otters" in c for _, _, c in worker.sent))
        # Continuing the user's own DM: the capture goes along as an image part of that message.
        say(worker, "call_chat", "keep going on the invoice draft in that chat")
        wait_for(lambda: streamed)
        # A running task asked about the screen: the steer names saved paths, never bytes.
        hermes.hold = True
        say(worker, "call_vendor", "Draft a reply to the vendor about the invoice")
        running = wait_for(lambda: next((r for r in worker.interaction.runs.values()
                                         if r.delegation_id == "call_vendor" and r.run_id), None))
        say(worker, "call_look", "and look at this", task_id="call_vendor")
        wait_for(lambda: hermes.steers)
        hermes.runs[running.run_id]["done"].set()
        wait_for(lambda: all(r.status not in calls.ACTIVE_RUN_STATES for r in worker.interaction.runs.values()))
        worker.call_closed()
        wait_for(lambda: any(name == "call_log" for name, _ in sinks))
    finally:
        if mac is not None:
            mac.stop()
        srv.httpd.shutdown()
        srv.httpd.server_close()
        svc.close()

    # It reached Hermes: the screen and the picture as image parts, the file by its saved path only.
    first = hermes.calls[0]["input"]
    assert isinstance(first, list), "the first run carries image parts"
    parts = first[0]["content"]
    urls = [p["image_url"]["url"] for p in parts if p["type"] == "image_url"]
    assert [base64.b64decode(u.split(",", 1)[1]) for u in urls] == [WINDOW, PICTURE]
    prompt = parts[0]["text"]
    assert "contract.pdf" in prompt and "/cache/speakeasy/shared/" in prompt
    assert leaks(prompt) == [], "the prompt text names files and paths, never bytes or contents"
    chat_message = streamed[0][1]
    assert isinstance(chat_message, list) and chat_message[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert leaks(chat_message[0]["text"]) == []
    steer = hermes.steers[-1][1]
    assert "/cache/speakeasy/shared/" in steer and "vision_analyze" in steer and leaks(steer) == []

    # ... and nowhere else.
    sinks += [("voice", c) for _, _, c in worker.sent]
    sinks += [("feed", json.dumps(p, default=repr)) for _, _, p in list(interaction.feed.ring)]
    sinks += [("log", caplog.text + "".join(repr(r.args) for r in caplog.records))]
    for name in ("routing", "title", "polish", "status", "quick", "jev", "home", "notice", "call_log"):
        assert any(n == name for n, _ in sinks), f"the {name} sink was never reached: the test lost its reach"
    bad = [(name, leaks(text)) for name, text in sinks if leaks(text)]
    assert bad == []
    snippets = [text for name, text in sinks if name == "routing" and "invoice draft has a wrong total" in text]
    assert snippets, "the DM's snippet reached the routing model, with its stored image left out"
