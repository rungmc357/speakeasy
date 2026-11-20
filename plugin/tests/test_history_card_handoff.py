"""SPK-541: the hand-off Speakeasy sends after a card is answered is never drawn as the person's words.

A tap on a confirm or email card starts a run whose input carries the approved preview's JSON for the
model. Hermes stores that input as a user turn, so the thread page (Mac and iPhone read the same
history) used to show it as a message the person typed, raw JSON and all.
"""
import json

import pytest

from speakeasy import confirms as C
from speakeasy import history as H
from speakeasy.prompt import builder as P
from test_history import NOW, add_msg, add_session, make_db

# An invented approval: a short note to an invented address.
VIEW = {"kind": "confirm", "type": "send_message", "title": "Send the note to Rowan",
        "details": {"to": "rowan@example.com", "body": "The blue paint arrives Tuesday."},
        "sha256": "3f" * 32, "state": "approved"}
DRAFT = json.dumps({"to": ["rowan@example.com"], "subject": "Paint delivery", "body": "Arrives Tuesday."})
NAMES = P.Names("Hermes", "Sam")


def _confirm_handoff() -> str:
    # Exactly what confirm_actions.act sends: the execution message plus the receipt instructions.
    return C.execution_message(VIEW) + (
        '\nOn confirmed success, include a ```speakeasy-receipt block with a JSON object '
        '{"sha256":"' + VIEW["sha256"] + '","reference":"service confirmation id"}. '
        'Do not invent a confirmation. If terms changed, return a fresh speakeasy-views confirm card instead.')


HANDOFFS = [
    (_confirm_handoff(), "Approved on the card"),
    (P.draft_approved_message(NAMES, DRAFT), "Approved on the card"),
    (P.draft_approved_message(NAMES, DRAFT, edited=True), "Approved on the card"),
    (P.draft_approved_message(P.Names(), DRAFT), "Approved on the card"),   # no user name: "The user"
    (P.draft_save_message(NAMES, DRAFT), "Saved as a draft from the card"),
    (P.draft_denied_message(NAMES), "Declined on the card"),
]


@pytest.mark.parametrize("raw,step", HANDOFFS)
def test_card_handoff_is_a_quiet_automatic_note(raw, step):
    assert H.user_words(raw) == ("note", step)


def test_revise_shows_only_the_persons_own_words():
    raw = P.draft_revise_message(NAMES, "Make it shorter and say thanks.")
    assert H.user_words(raw) == ("user", "Make it shorter and say thanks.")


def test_a_person_quoting_the_words_still_shows():
    said = "Why did it say the user approved exactly this preview?"
    assert H.user_words(said) == ("user", said)


def test_approval_round_page_never_shows_the_handoff_as_a_user_message(service):
    path = service.rt.state_db
    make_db(path)
    sid = "api7"
    add_session(path, sid, "api_server", title="Send the note to Rowan", at=NOW - 200)
    add_msg(path, sid, "user", "Tell Rowan the blue paint arrives Tuesday", at=NOW - 100)
    add_msg(path, sid, "assistant", "Here's the note, ready to send.", at=NOW - 90)
    add_msg(path, sid, "user", _confirm_handoff(), at=NOW - 60)
    add_msg(path, sid, "assistant", "Sent the note to Rowan.", at=NOW - 50)
    add_msg(path, sid, "user", P.draft_approved_message(NAMES, DRAFT), at=NOW - 40)
    add_msg(path, sid, "assistant", "Sent the email.", at=NOW - 30)
    page = H.session_messages(path, sid)
    shown = [(e["kind"], e.get("text")) for e in page["entries"]]
    # Every reply stays a reply (no work step after it to fold it away), split by a quiet note.
    assert shown == [("user", "Tell Rowan the blue paint arrives Tuesday"),
                     ("assistant", "Here's the note, ready to send."), ("note", "Approved on the card"),
                     ("assistant", "Sent the note to Rowan."), ("note", "Approved on the card"),
                     ("assistant", "Sent the email.")]
    for e in page["entries"]:
        blob = json.dumps(e)
        for bad in ("approved exactly", VIEW["sha256"], "rowan@example.com", "speakeasy-receipt", "Do only this action"):
            assert bad not in blob, (bad, e)
    row = next(s for s in H.list_sessions(path)["sessions"] if s["id"] == sid)
    assert row["preview"] == "Sent the email." and "approved" not in (row["title"] or "").lower()
