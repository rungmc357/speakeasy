"""Task names: specific, never cut mid-phrase, follow-ups named with the task they continue."""
from __future__ import annotations

import pytest

from fakes import wait_for
from speakeasy import router
from speakeasy.store import _is_stand_in
from speakeasy.text import (fix_product_names, is_fragment, short_title, stand_in_titles, title_input,
                            v1_short_title)

UNFINISHED = {"to", "the", "a", "of", "for", "in", "at", "and", "is", "going", "look", "my", "like", "be"}


@pytest.mark.parametrize("request_, expected", [
    ("Can you check the forecast for Saturday in Lisbon? Is it still going to rain", "Check the forecast for Saturday in Lisbon"),
    ("They switched on and then off again. Can you turn the porch lights back on", "Turn the porch lights back on"),
    ("Hey, for Speak Easy, during onboarding, where are we pulling the login from", "Where are we pulling the login"),
    ("Please look into finding a decent standing desk for the studio, like, um, something sturdy",
     "Look into finding a decent standing desk for the studio"),
])
def test_the_stand_in_names_the_ask_and_never_stops_mid_phrase(request_, expected):
    assert short_title(request_) == expected


def test_a_long_request_is_cut_on_a_finished_word():
    title = short_title("What's the weather going to be like in Lisbon on Saturday when we land there")
    assert title and title.split()[-1].lower() not in UNFINISHED, title


def test_call_notes_and_fragments_never_become_the_stand_in():
    assert short_title("Yeah, do both (Geo is looking at: Add previews for the voices)") is None
    assert "looking at" not in (short_title("Draft the Lisbon packing list (Geo is looking at: Notes)") or "")


def test_product_names_are_spelled_right():
    assert fix_product_names("Speak Easy test flight build for Hermès") == "Speakeasy TestFlight build for Hermes"
    assert "Speak Easy" not in short_title("Ship the Speak Easy onboarding screen today")


def test_fragments_are_recognised():
    for said in ("Fix and ship", "Do both", "gonna win", "Don't worry about that", "Yeah, try again", "For what",
                 "anything we need to do on the others?"):
        assert is_fragment(said), said
    for said in ("Check tomorrow's weather in Lisbon", "Draft an email to the landlord about the boiler"):
        assert not is_fragment(said), said


def test_titler_input_carries_the_parent_task_and_recent_turns():
    text = title_input("Fix and ship", parent="ChatGPT login during onboarding",
                       parent_request="where does onboarding pull the ChatGPT login from",
                       recent=[{"role": "assistant", "text": "It comes from the Codex app."}])
    assert "Request: Fix and ship" in text
    assert "ChatGPT login during onboarding" in text and "Codex app" in text


@pytest.mark.parametrize("raw", [
    '{"title": "Continue previous discussion"}', '{"title": "Follow up on remaining items"}',
    '{"title": "Check the weather for tomorrow in"}', '{"title": "Where do you think this mismatch…"}',
    '{"title": "Fix and ship"}', '{"title": "Ship"}', "",
])
def test_unusable_titles_are_rejected(raw):
    title, why = router.clean_task_title(raw, "Request: Fix and ship")
    assert title is None and why


def test_a_good_title_is_kept_and_spelled_right():
    assert router.clean_task_title('{"title": "Ship the Speak Easy login onboarding fix"}', "Request: Fix and ship") == (
        "Ship the Speakeasy login onboarding fix", "")


def test_the_titler_retries_once_with_the_reason(monkeypatch):
    replies = iter(['{"title": "Fix and ship"}', '{"title": "Ship the onboarding login fix"}'])
    seen = []

    def fake(messages, timeout):
        seen.append(messages)
        return next(replies)

    monkeypatch.setattr(router, "_title_llm", fake)
    assert router.smart_title("Request: Fix and ship\nIt continues or answers this earlier task: login fix") == \
        "Ship the onboarding login fix"
    assert len(seen) == 2 and "Not usable" in seen[1][-1]["content"]


def test_the_titler_gives_up_after_one_retry(monkeypatch):
    monkeypatch.setattr(router, "_title_llm", lambda messages, timeout: '{"title": "Continue previous discussion"}')
    assert router.smart_title("Request: keep going") is None


def test_older_stand_ins_and_cut_questions_still_count_as_stand_ins():
    request = "Can you check the weather for tomorrow in New York? Is it still going to be raining"
    assert v1_short_title(request) == "Check the weather for tomorrow in New York?"
    assert v1_short_title(request) in stand_in_titles(request)
    assert _is_stand_in(v1_short_title(request), request)
    assert _is_stand_in("Where do you think this mismatch shows up right now, in the…", None)
    assert not _is_stand_in("Check tomorrow's New York rain forecast", request)


def test_a_follow_up_is_named_with_the_task_it_continues(server, service, hermes):
    from test_routing import start_call, tasks
    seen = []

    def titler(text):
        seen.append(text)
        return "Ship the standing desk order" if "Fix and ship" in text else "Research standing desks under 500"

    service.rt.title_call = titler
    _, worker = start_call(server, service)
    worker.delegate("call_a", "Research the best standing desks under five hundred dollars")
    wait_for(lambda: any(t.get("title") == "Research standing desks under 500" for t in tasks(server)), timeout=10)
    worker.delegate("call_b", "Fix and ship")
    wait_for(lambda: any(t.get("title") == "Ship the standing desk order" for t in tasks(server)), timeout=10)
    follow = next(s for s in seen if "Fix and ship" in s)
    assert "Research standing desks under 500" in follow and "five hundred dollars" in follow
    assert not any(t.get("title") == "Fix and ship" for t in tasks(server))


def test_the_sweep_renames_surviving_stand_ins_with_context(service):
    store = service.store
    for key, rowid_req in (("k_1", "Research the best standing desks under five hundred dollars"), ("k_2", "Fix and ship")):
        store._db.execute("INSERT INTO runs (idem_key, interaction_id, delegation_id, revision, status, title, updated) "
                          "VALUES (?, 'vi_s', ?, 1, 'completed', ?, 2)", (key, "call_" + key, short_title(rowid_req)))
        store.progress(key, "request", rowid_req)
    store._db.execute("UPDATE runs SET title='Research standing desks under 500' WHERE idem_key='k_1'")
    seen = []
    service.rt.title_call = lambda text: seen.append(text) or "Order the chosen standing desk"
    assert service.sweep_titles() == 1
    assert store.title("k_2") == "Order the chosen standing desk"
    assert "Research standing desks under 500" in seen[0]
    assert service.sweep_titles() == 0  # nothing left, and a named task is never renamed
