"""Images on task start and continued chats, whether Hermes can read them, and keeping image data out
of conversation snippets ("look at this", plan unit U1)."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import sqlite3
import sys
import threading
import time
import types

import pytest

from fakes import FAKE_API_KEY, wait_for
from speakeasy import attachments as A
from speakeasy import continuity, hermes_api
from speakeasy.hermes_api import HermesAPI, HermesError, user_content

JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x01" * 4000 + b"\xff\xd9"
OTHER_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x02" * 4000 + b"\xff\xd9"


def data_url(raw: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}"


def big_jpeg(size: int, fill: int) -> bytes:
    return b"\xff\xd8\xff\xe0" + bytes([fill]) * (size - 6) + b"\xff\xd9"


@pytest.fixture
def api(hermes):
    return HermesAPI(hermes.base, lambda: FAKE_API_KEY)


# -- /v1/runs input ----------------------------------------------------------------------------

def test_a_run_with_two_images_sends_a_text_part_and_two_image_url_parts(api, hermes):
    urls = [data_url(JPEG), data_url(OTHER_JPEG)]
    run_id = api.start_run("What's this error?", "se_img_two", "speakeasy_task_1", images=urls)
    call = hermes.calls[-1]
    assert call["run_id"] == run_id and call["session_id"] == "speakeasy_task_1"
    assert call["input"] == [{"role": "user", "content": [
        {"type": "text", "text": "What's this error?"},
        {"type": "image_url", "image_url": {"url": urls[0]}},
        {"type": "image_url", "image_url": {"url": urls[1]}}]}]
    parts = call["input"][0]["content"][1:]
    assert all(p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in parts)


@pytest.mark.parametrize("images", [None, []])
def test_a_run_without_images_sends_a_plain_string(api, hermes, images):
    api.start_run("Book a table for two at eight", f"se_txt_{images is None}", images=images)
    assert hermes.calls[-1]["input"] == "Book a table for two at eight"


def test_a_retried_image_run_reuses_its_key_and_gets_the_same_run(api, hermes):
    urls = [data_url(JPEG)]
    first = api.start_run("Make this the header image", "se_img_retry", images=urls)
    again = api.start_run("Make this the header image", "se_img_retry", images=urls)
    assert again == first and len(hermes.calls) == 1
    # Hermes replays only an identical body: a retry must carry the same images, not new ones.
    with pytest.raises(HermesError) as err:
        api.start_run("Make this the header image", "se_img_retry", images=[data_url(OTHER_JPEG)])
    assert err.value.status == 409 and len(hermes.calls) == 1


@pytest.mark.parametrize("bad", [
    "https://example.com/screen.jpg",                         # a link, not image data
    "data:text/plain;base64," + base64.b64encode(b"hi").decode(),
    "data:image/svg+xml;base64," + base64.b64encode(b"<svg/>").decode(),
    "data:image/jpeg;base64,not base64!",
    b"\xff\xd8\xff",                                          # raw bytes
])
def test_anything_but_an_image_data_url_is_refused_before_hermes(api, hermes, bad):
    with pytest.raises(HermesError) as err:
        api.start_run("What's this?", "se_img_bad", images=[bad])
    assert err.value.status == 400 and hermes.calls == []
    assert "base64," not in err.value.message  # the message never repeats image data


def test_images_over_the_request_limits_are_refused(api, hermes):
    too_many = [data_url(JPEG)] * (A.MAX_REQUEST_IMAGES + 1)
    too_big = [data_url(big_jpeg(A.MAX_IMAGE_BYTES + 1000, 3))]
    too_much = [data_url(big_jpeg(A.MAX_IMAGE_BYTES - 1000, n)) for n in range(3)]  # 7.5 MB together
    for images in (too_many, too_big, too_much):
        with pytest.raises(HermesError) as err:
            api.start_run("What's this?", "se_img_limit", images=images)
        assert err.value.status == 413
    assert hermes.calls == []


# -- continued chats (/api/sessions/{id}/chat/stream) ----------------------------------------------

CONV = continuity.Conversation("s_dm", "telegram", "555", "dm", "", "42", "", "Sam", "Kitchen redesign", time.time())


def sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def chat_stream(sent: list, reply: bytes = b""):
    """An opener that records the posted body and streams Hermes' answer: ``run.started`` echoing
    the message exactly as posted, then ``reply`` (default: an answer and ``run.completed``)."""
    def opener(req, timeout):
        body = json.loads(req.data)
        sent.append(body)
        echo = sse("run.started", {"session_id": CONV.session_id, "run_id": "run_chat1", "seq": 1, "ts": 1.0,
                                   "user_message": {"role": "user", "content": body["message"]}, "runtime": {}})
        rest = reply or (sse("assistant.completed", {"content": "The island is too close to the range."})
                         + sse("run.completed", {"usage": {}}))
        return io.BytesIO(echo + rest)
    return opener


def test_a_continued_chat_with_an_image_posts_the_message_as_parts():
    sent, events = [], []
    url = data_url(JPEG)
    message = user_content("Is this layout right?", [url])
    assert continuity.stream_session_chat("http://127.0.0.1:9", "k", CONV, message,
                                          lambda name, payload: events.append(name), opener=chat_stream(sent))
    assert sent[0]["message"] == [{"type": "text", "text": "Is this layout right?"},
                                  {"type": "image_url", "image_url": {"url": url}}]
    assert events == ["run.started", "assistant.completed", "run.completed"]


def test_the_echo_of_a_two_image_message_does_not_count_against_the_stream_budget():
    sent, events = [], []
    urls = [data_url(big_jpeg(2_400_000, 4)), data_url(big_jpeg(2_400_000, 5))]
    message = user_content("Which of these two kitchens fits the room?", urls)
    assert len(json.dumps(message)) > continuity.STREAM_BUDGET  # the echo alone used to trip the budget
    done = continuity.stream_session_chat("http://127.0.0.1:9", "k", CONV, message,
                                          lambda name, payload: events.append((name, payload)),
                                          opener=chat_stream(sent))
    assert done is True
    assert [name for name, _ in events] == ["run.started", "assistant.completed", "run.completed"]
    started = events[0][1]
    assert started["run_id"] == "run_chat1" and "user_message" not in started  # the echo isn't passed on
    assert all("base64" not in json.dumps(payload) for _, payload in events)


def test_the_stream_budget_still_holds_after_run_started():
    flood = b"".join(sse("assistant.delta", {"delta": "x" * 60_000}) for _ in range(80))  # about 4.8 MB
    message = user_content("Is this right?", [data_url(JPEG)])
    with pytest.raises(ValueError):
        continuity.stream_session_chat("http://127.0.0.1:9", "k", CONV, message, lambda name, payload: None,
                                       opener=chat_stream([], flood))


def test_a_stream_that_never_starts_is_still_bounded():
    flood = b"".join(sse("assistant.delta", {"delta": "x" * 60_000}) for _ in range(80))

    def opener(req, timeout):
        return io.BytesIO(flood)
    with pytest.raises(ValueError):
        continuity.stream_session_chat("http://127.0.0.1:9", "k", CONV, "Plain words", lambda n, p: None,
                                       opener=opener)


# -- can Hermes read images? -------------------------------------------------------------------------

def hermes_modules(monkeypatch, *, supports=None, aux_backend=None, fails=None):
    """Stand-ins for the Hermes modules detection asks (the plugin runs inside Hermes; tests don't)."""
    asked: dict = {}
    agent = types.ModuleType("agent")
    agent.__path__ = []
    routing = types.ModuleType("agent.image_routing")

    def lookup(provider, model, cfg):
        if fails:
            raise fails
        asked["main"] = (provider, model, cfg)
        return supports
    routing._lookup_supports_vision = lookup
    prep = types.ModuleType("agent.vision_message_prep")
    prep.VisionMessagePrepMixin = type("VisionMessagePrepMixin", (), {})
    aux = types.ModuleType("agent.auxiliary_client")
    aux._read_main_provider = lambda: "openrouter"
    aux._read_main_model = lambda: "text-only/model"

    @contextlib.contextmanager
    def probe_mode():
        asked["probe_mode"] = True
        yield
    aux.aux_probe_mode = probe_mode

    def resolve(provider=None, model=None, **kw):
        asked.setdefault("aux", []).append(provider)
        return (aux_backend, object(), "vision-model") if aux_backend else (None, None, None)
    aux.resolve_vision_provider_client = resolve
    cli = types.ModuleType("hermes_cli")
    cli.__path__ = []
    config = types.ModuleType("hermes_cli.config")
    config.load_config = lambda: {"model": {"provider": "openrouter", "default": "text-only/model"}}
    for mod in (agent, routing, prep, aux, cli, config):
        monkeypatch.setitem(sys.modules, mod.__name__, mod)
    return asked


LIMITS = {"max_attachments": 3, "max_image_bytes": 2_500_000, "max_request_image_bytes": 6_500_000,
          "max_file_bytes": 10_000_000,
          "file_types": ["application/json", "application/pdf", "text/csv", "text/markdown", "text/plain", "video/mp4", "video/quicktime"]}


def test_without_hermes_image_routing_status_reports_no_images(service, monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.image_routing", None)  # not importable
    service.refresh_image_support()
    assert service.status()["attachments"] == {"images": False, "vision": "none", **LIMITS}
    assert service.hermes.capabilities().images is False


def test_a_main_model_with_vision_reads_images_natively(service, monkeypatch):
    asked = hermes_modules(monkeypatch, supports=True)
    service.refresh_image_support()
    assert service.status()["attachments"] == {"images": True, "vision": "native", **LIMITS}
    assert asked["main"][:2] == ("openrouter", "text-only/model")  # Hermes' own main model lookup
    assert service.hermes.capabilities().images is True


def test_a_main_model_without_vision_gets_descriptions_from_an_auxiliary_vision_model(service, monkeypatch):
    asked = hermes_modules(monkeypatch, supports=None, aux_backend="openrouter")
    service.refresh_image_support()
    assert service.status()["attachments"]["vision"] == "described"
    assert asked["probe_mode"] is True  # providers resolved without building real clients
    assert service.hermes.capabilities().images is True


def test_no_vision_anywhere_reads_as_none(service, monkeypatch):
    hermes_modules(monkeypatch, supports=False, aux_backend=None)
    service.refresh_image_support()
    assert service.status()["attachments"] == {"images": True, "vision": "none", **LIMITS}
    assert service.hermes.capabilities().images is False


def test_a_detection_failure_reads_as_unknown(service, monkeypatch):
    hermes_modules(monkeypatch, fails=RuntimeError("models.dev unreachable"))
    service.refresh_image_support()
    assert service.status()["attachments"] == {"images": True, "vision": "unknown", **LIMITS}
    assert service.hermes.capabilities().images is True  # unknown keeps the feature; Hermes' answer will say


def test_status_never_waits_for_detection(service, monkeypatch):
    release, entered = threading.Event(), threading.Event()

    def slow_detection():
        entered.set()
        release.wait(10)
        return {"images": True, "vision": "native"}
    monkeypatch.setattr(hermes_api, "detect_image_support", slow_detection)
    before = service.status()["attachments"]["vision"]
    checker = threading.Thread(target=service.refresh_image_support, daemon=True)
    checker.start()
    assert entered.wait(5)
    seen: list = []
    reader = threading.Thread(target=lambda: seen.append(service.status()["attachments"]), daemon=True)
    reader.start()
    reader.join(5)
    assert seen and seen[0]["vision"] == before  # answered from the cache while the check is still running
    release.set()
    checker.join(5)
    assert service.status()["attachments"]["vision"] == "native"


def test_the_idle_loop_checks_at_startup_and_then_periodically(service, monkeypatch):
    from speakeasy import service as service_mod
    checks: list[float] = []
    monkeypatch.setattr(hermes_api, "detect_image_support",
                        lambda: checks.append(time.monotonic()) or {"images": True, "vision": "described"})
    monkeypatch.setattr(service_mod, "IDLE_CHECK_S", 0.01)
    monkeypatch.setattr(service_mod, "IMAGE_SUPPORT_EVERY_S", 0.1)
    loop = threading.Thread(target=service._idle_loop, daemon=True)
    loop.start()
    try:
        wait_for(lambda: len(checks) >= 1, timeout=5)
        assert service.status()["attachments"]["vision"] == "described"
        wait_for(lambda: len(checks) >= 3, timeout=5)
    finally:
        service._stop.set()
        loop.join(5)
    assert checks[2] - checks[1] >= 0.09  # spaced by the interval, not every idle tick


def test_a_slow_image_check_never_holds_up_the_idle_loop(service, monkeypatch):
    from speakeasy import service as service_mod
    release = threading.Event()
    monkeypatch.setattr(hermes_api, "detect_image_support",
                        lambda: release.wait(10) and {"images": True, "vision": "native"})
    idle_checks: list[float] = []
    monkeypatch.setattr(service, "idle_check", lambda: idle_checks.append(time.monotonic()))
    monkeypatch.setattr(service_mod, "IDLE_CHECK_S", 0.01)
    loop = threading.Thread(target=service._idle_loop, daemon=True)
    loop.start()
    try:
        wait_for(lambda: len(idle_checks) >= 5, timeout=5)  # auto-pause keeps running mid-check
    finally:
        release.set()
        service._stop.set()
        loop.join(5)


# -- continuity snippets ------------------------------------------------------------------------------

def stored(content) -> str:
    """How Hermes stores a message with parts in state.db (hermes_state ``_encode_content``)."""
    return "\x00json:" + json.dumps(content)


def test_stored_parts_keep_only_their_text():
    url = data_url(JPEG)
    assert continuity.message_text(stored([{"type": "image_url", "image_url": {"url": url}},
                                           {"type": "text", "text": "what is wrong with this screen"}])) \
        == "what is wrong with this screen"
    assert continuity.message_text(stored({"type": "text", "text": "just words"})) == "just words"
    assert continuity.message_text(stored([{"type": "input_image", "image_url": url}])) == ""
    assert continuity.message_text("\x00json:[{\"type\": \"text\", \"text\": \"cut off" + url) == ""  # fails closed
    assert continuity.message_text("plain words") == "plain words"
    assert continuity.message_text(None) == ""


def test_a_stored_image_message_gives_a_snippet_with_only_its_text(tmp_path):
    db_path = tmp_path / "state.db"
    db = sqlite3.connect(db_path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_type TEXT, thread_id TEXT, "
               "origin_json TEXT, title TEXT, started_at REAL, ended_at REAL)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
               "content TEXT, tool_calls TEXT, timestamp REAL)")
    now = time.time()
    origin = {"platform": "telegram", "chat_id": "555", "chat_type": "dm", "chat_name": "Sam", "user_id": "42"}
    db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
               ("s_dm", "telegram", "dm", None, json.dumps(origin), "Kitchen redesign", now - 3600, None))
    # The image part first: a cut-off raw row would show the start of its data URL.
    picture = stored([{"type": "image_url", "image_url": {"url": data_url(JPEG * 8)}},
                      {"type": "text", "text": "[sam] does the island fit between the counters"}])
    for role, content, age in [("user", "[sam] plan the kitchen redesign budget", 900),
                               ("assistant", "Sure, about 30k.", 890), ("user", picture, 60)]:
        db.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)",
                   ("s_dm", role, content, None, now - age))
    db.commit()
    db.close()
    [candidate] = continuity.conversations_with_context(db_path, "does the kitchen island fit")
    assert candidate.snippets[0] == "does the island fit between the counters"
    assert "plan the kitchen redesign budget" in candidate.snippets
    assert not any("base64" in s or "data:" in s or "\x00" in s for s in candidate.snippets)
