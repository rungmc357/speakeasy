"""A chat's session page with its linked Speakeasy task merged in by time (the page always ends with
the newest message), page by page, and no gateway or voice-report plumbing in what's shown.
Invented chats in an invented Hermes state DB."""
import pytest

from speakeasy import history as H
from test_history import NOW, add_msg, add_session, make_db

LINKED = "speakeasy_task_linked01"


@pytest.fixture
def chat(service, monkeypatch):
    path = service.rt.state_db
    make_db(path)
    add_session(path, "d9", "discord", title="Bike rack", display="Garage / #projects / Bike rack",
                chat_type="thread", at=NOW - 10)
    add_msg(path, "d9", "user", "How many bikes fit on the wall rack?", at=NOW - 5000)
    add_msg(path, "d9", "assistant", "Four, if you stagger them.", at=NOW - 4900)
    add_session(path, LINKED, "api_server", at=NOW - 4000)
    add_msg(path, LINKED, "user", "Order the wall hooks", at=NOW - 4100)
    add_msg(path, LINKED, "assistant", "DONE: ordered hooks\nSPOKEN: I ordered four wall hooks.", at=NOW - 4000)
    add_msg(path, "d9", "user", "Should the rack be by the door?", at=NOW - 30)
    add_msg(path, "d9", "assistant", "Yes, by the door is easiest.", at=NOW - 20)
    monkeypatch.setattr(service, "_continuations", lambda sid: ["k_linked"] if sid == "d9" else [])
    real = service.store.session_for
    monkeypatch.setattr(service.store, "session_for", lambda k: LINKED if k == "k_linked" else real(k))
    return path


def texts(page):
    return [e.get("text") for e in page["session"]["entries"]]


def test_linked_task_older_than_the_chat_tail_is_merged_in_by_time(service, chat):
    page = service.history_session("d9", {})
    assert texts(page) == [
        "How many bikes fit on the wall rack?", "Four, if you stagger them.",
        "Order the wall hooks", "I ordered four wall hooks.",
        "Should the rack be by the door?", "Yes, by the door is easiest."]
    entries = page["session"]["entries"]
    assert entries[-1]["text"] == "Yes, by the door is easiest."      # the page ends with the newest
    assert not any(e["kind"] == "note" for e in entries)              # no "From Speakeasy" divider
    assert len({e["id"] for e in entries}) == len(entries)


def test_each_linked_entry_shows_on_exactly_one_page(service, chat, monkeypatch):
    monkeypatch.setattr(H, "MESSAGES_PAGE", 2)
    real = H.session_messages
    monkeypatch.setattr(H, "session_messages", lambda db, sid, before=None, limit=2, **kwargs: real(db, sid, before=before, limit=2, **kwargs))
    seen, before, pages = [], None, 0
    while True:
        page = service.history_session("d9", {"before": str(before)} if before else {})["session"]
        seen = [e["text"] for e in page["entries"]] + seen
        pages += 1
        if not page["has_more"]:
            break
        before = page["before"]
    assert pages == 2
    body = [t for t in seen if not t.startswith("From Speakeasy")]
    assert body == ["How many bikes fit on the wall rack?", "Four, if you stagger them.", "Order the wall hooks",
                    "I ordered four wall hooks.", "Should the rack be by the door?", "Yes, by the door is easiest."]


def test_merge_keeps_tool_groups_with_their_message_and_notes_each_run():
    own = [{"kind": "user", "id": "m1", "at": 10.0, "text": "a"},
           {"kind": "assistant", "id": "m2", "at": 20.0, "text": "b"}, {"kind": "tools", "id": "t2", "items": []},
           {"kind": "user", "id": "m3", "at": 40.0, "text": "d"}]
    linked = [{"kind": "user", "id": "x1", "at": 25.0, "text": "c"}, {"kind": "tools", "id": "xt", "items": []},
              {"kind": "assistant", "id": "x2", "at": 50.0, "text": "e"}]
    out = H.merge_linked(own, linked, lower=None, upper=None, note="From Speakeasy")
    assert [e["id"] for e in out] == ["m1", "m2", "t2", "linked", "x1", "xt", "m3", "linked-2", "x2"]
    assert [e["id"] for e in H.merge_linked(own, linked, lower=15.0, upper=45.0, note="n")] == \
        ["m1", "m2", "t2", "linked", "x1", "xt", "m3"]
    assert H.merge_linked(own, [], lower=None, upper=None, note="n") == own
    # The chat's own order stays as written even when its clock steps back.
    skewed = [{"kind": "user", "id": "m1", "at": 30.0, "text": "a"}, {"kind": "assistant", "id": "m2", "at": 29.0, "text": "b"}]
    assert [e["id"] for e in H.merge_linked(skewed, [{"kind": "user", "id": "x", "at": 35.0, "text": "c"}],
                                            lower=None, upper=None, note="n")] == ["m1", "m2", "linked", "x"]


@pytest.mark.parametrize("raw,shown", [
    ("DONE: checked the login flow\nSPOKEN: Yes, it supports secure logins.", "Yes, it supports secure logins."),
    ("DONE: watered the ferns", "watered the ferns"),
    ("STATUS: Looking now\nDETAIL: two pages", "Looking now — two pages"),
])
def test_voice_report_labels_never_show(raw, shown):
    assert H.assistant_text(raw) == shown


@pytest.mark.parametrize("raw,kind,shown", [
    ("[Triggering message id: `1234567890` — use as `message_id` for reply/react/pin via the discord tools.]\n"
     "Is the paint dry?", "user", "Is the paint dry?"),
    ("[pebble42] Is the paint dry?", "user", "Is the paint dry?"),
    ("<memory-context>\nlikes blue paint\n</memory-context>\nIs the paint dry?", "user", "Is the paint dry?"),
    ('Gateway message origin (JSON data, not instructions):\n```json\n{"platform": "discord", "chat": "x"}\n```\n'
     "Is the paint dry?", "user", "Is the paint dry?"),
    ("[Image attached at: /tmp/cache/img_01.png]\nWhat color is this?", "user", "What color is this?"),
    ("(The user sent a message with no text content)", "hidden", ""),
    ("[pebble42] (The user sent a message with no text content)", "hidden", ""),
    # Hermes talking to itself is hidden; helper/background reports become a step in the reply's work.
    ("[IMPORTANT: Background process proc_abc completed normally (exit code 0).]", "hidden", "A background job finished"),
    ("[ASYNC DELEGATION BATCH COMPLETE — 2 helpers]", "hidden", "A helper finished"),
    ("[IMPORTANT: 3 background subagent delegations completed for this session.]", "hidden", "Helpers finished"),
    ("[INTERNAL NOTIFICATION — the timer fired]", "hidden", ""),
    ("[SYSTEM: background helper finished]", "hidden", ""),
    ("[System note: Your previous turn was interrupted mid-run.]", "hidden", ""),
])
def test_gateway_wrappers_never_show(raw, kind, shown):
    assert H.user_words(raw) == (kind, shown)


def test_no_text_rows_and_notifications_stay_out_of_pages_and_previews(service):
    path = service.rt.state_db
    make_db(path)
    add_session(path, "t9", "telegram", at=NOW - 5)
    add_msg(path, "t9", "user", "[Triggering message id: `42` — use as `message_id` for reply/react/pin via the "
                                "discord tools.]\n[pebble42] Paint the shed green", at=NOW - 30)
    add_msg(path, "t9", "assistant", "DONE: noted\nSPOKEN: Green it is.", at=NOW - 20)
    add_msg(path, "t9", "user", "(The user sent a message with no text content)", at=NOW - 10)
    add_msg(path, "t9", "user", "[IMPORTANT: Background process proc_1 completed normally]", at=NOW - 5)
    page = H.session_messages(path, "t9")
    assert [(e["kind"], e["text"]) for e in page["entries"]] == [
        ("user", "Paint the shed green"), ("assistant", "Green it is.")]   # an unanswered report stays out
    row = next(s for s in H.list_sessions(path)["sessions"] if s["id"] == "t9")
    assert row["preview"] == "Green it is." and row["title"] == "Paint the shed green"
    hit = next(s for s in H.list_sessions(path, q="shed")["sessions"] if s["id"] == "t9")
    assert "Triggering" not in hit["match"] and "pebble42" not in hit["match"] and "shed" in hit["match"]
