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


def test_a_fragment_the_router_did_not_join_is_named_from_its_words_and_the_call(server, service, hermes):
    """A "Fix and ship" routed as a new task answers nothing the router knows of: the titler sees the
    request and the call's last turns, never "the task before it" as the task it continues."""
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
    assert "It continues or answers" not in follow
    assert "five hundred dollars" in follow  # the call's own recent turns still say what "that" was


ASKS = """Voice: plan the garden beds
I sketched two bed layouts.

```speakeasy-question
{"question": "Fix the drainage before ordering soil?", "options": ["Fix and ship", "Leave it"]}
```
DONE: garden
SPOKEN: I sketched two layouts."""


def test_an_answer_is_named_with_the_task_it_answers_not_the_newest_one(server, service, hermes):
    """The wrong-parent bug: an answer tapped on task A's question, after an unrelated task B ran on
    the same call, was named after B (the newest task) instead of A, the task it answers."""
    from test_routing import start_call, tasks
    from fakes import http
    hermes.responder = lambda prompt, sid: (
        ASKS if "garden" in prompt.lower() and "Fix and ship" not in prompt
        else "Drafted.\nDONE: email\nSPOKEN: Drafted." if "landlord" in prompt.lower()
        else "Done.\nDONE: drainage\nSPOKEN: Drainage is fixed.")
    seen = []

    def titler(text):
        seen.append(text)
        first = text.split("\n", 1)[0]
        return ("Fix garden bed drainage first" if "Fix and ship" in first else
                "Draft the landlord boiler email" if "landlord" in first else "Plan the spring garden beds")

    service.rt.title_call = titler
    session, worker = start_call(server, service)
    worker.delegate("call_a", "Plan the garden beds for spring")
    asked = wait_for(lambda: [t for t in tasks(server) if t["status"] == "completed" and t.get("result", {}).get("question")],
                     timeout=10)[0]
    wait_for(lambda: any(t.get("title") == "Plan the spring garden beds" for t in tasks(server)), timeout=10)
    worker.delegate("call_b", "Draft an email to the landlord about the broken boiler")
    wait_for(lambda: any(t.get("title") == "Draft the landlord boiler email" and t["status"] == "completed"
                         for t in tasks(server)), timeout=10)
    status, body = http(server.base_url, "POST", f"/voice/interactions/{session['interaction_id']}/answer",
                        {"task_id": asked["task_id"], "text": "Fix and ship"}, server.token)
    assert status == 200 and body["sent"], body
    wait_for(lambda: any(t.get("title") == "Fix garden bed drainage first" for t in tasks(server)), timeout=10)
    answer = next(s for s in seen if s.startswith("Request: Fix and ship"))
    parent_line = next(line for line in answer.splitlines() if line.startswith("It continues or answers"))
    assert "Plan the spring garden beds" in parent_line and "landlord" not in parent_line


def _row(store, key, request, *, title=None, delegation="item_x", interaction="vi_s", continued=None, result=None):
    store._db.execute("INSERT INTO runs (idem_key, interaction_id, delegation_id, revision, status, title, updated, "
                      "continued, result_json) VALUES (?, ?, ?, 1, 'completed', ?, 2, ?, ?)",
                      (key, interaction, delegation, title or short_title(request) or request, continued, result))
    store.progress(key, "request", request)


def test_a_follow_up_is_never_named_after_the_chat_thread_it_continued(service):
    """The thread's own name (an older, unrelated conversation) is never "the task it continues"."""
    import json
    store = service.store
    thread = json.dumps({"session_id": "s_old", "label": 'Discord "Stop greeting visitors as Clippy"'})
    _row(store, "k_a", "When someone onboards the kiosk, where does the sign-in come from", continued=thread,
         result=json.dumps({"spoken": "From the badge reader.", "question": {"question": "Fix the gaps?"}}))
    store.set_title("k_a", "Kiosk sign-in during onboarding")
    store.set_title("k_a", "Stop greeting visitors as Clippy", thread=True)  # the chat's name shown on the row
    _row(store, "k_b", "Fix and ship", delegation="answer_1", continued=thread)
    assert store.parent_name("k_a") == "Kiosk sign-in during onboarding"
    assert store.title_context("k_b") == ("Kiosk sign-in during onboarding",
                                          "When someone onboards the kiosk, where does the sign-in come from")


def test_the_sweep_uses_only_the_task_a_row_really_answers_or_joined(service):
    import json
    store = service.store
    _row(store, "k_1", "Plan the garden beds for spring",
         result=json.dumps({"spoken": "Two layouts.", "question": {"question": "Which layout?"}}))
    store.set_title("k_1", "Plan the spring garden beds")
    _row(store, "k_2", "Draft an email to the landlord about the broken boiler")
    store.set_title("k_2", "Draft the landlord boiler email")
    _row(store, "k_3", "Fix and ship", delegation="answer_2")              # tapped on k_1's question
    _row(store, "k_4", "I wasn't done about the other stuff")              # routed as a new task: no parent
    _row(store, "k_5", "For what")                                         # joined k_4 while it ran
    store.progress("k_4", "milestone", "You added: For what")
    assert store.title_context("k_3")[0] == "Plan the spring garden beds"
    assert store.title_context("k_4") == (None, None)
    assert store.title_context("k_5")[1] == "I wasn't done about the other stuff"
    seen = {}
    service.rt.title_call = lambda text: seen.setdefault(text.split("\n", 1)[0], text) and None
    service.sweep_titles()
    assert "Plan the spring garden beds" in seen["Request: Fix and ship"]
    assert "landlord" not in seen["Request: Fix and ship"]
    assert "It continues or answers" not in seen["Request: I wasn't done about the other stuff"]


def test_a_recorded_parent_wins_over_position(service):
    store = service.store
    _row(store, "k_p", "Plan the garden beds for spring")
    store.set_title("k_p", "Plan the spring garden beds")
    _row(store, "k_q", "Draft an email to the landlord about the broken boiler")
    _row(store, "k_r", "Do both")
    store.set_parent("k_r", "k_p")
    assert store.title_context("k_r")[0] == "Plan the spring garden beds"


def test_a_meta_parent_name_is_not_passed_on_but_its_request_is(service):
    store = service.store
    _row(store, "k_m", "I wasn't done about the other stuff")
    store.set_title("k_m", "Continue previous discussion")
    _row(store, "k_n", "For what")
    store.set_parent("k_n", "k_m")
    assert store.title_context("k_n") == (None, "I wasn't done about the other stuff")


@pytest.mark.parametrize("title, expected", [
    ("Push all five of Priya's PRs", "Push all five of my PRs"),
    ("Check tomorrow's weather for Juniper", "Check tomorrow's weather"),
    ("Juniper, book the dentist for Friday", "Book the dentist for Friday"),
    ("Ask Juniper to summarize the lease", "Summarize the lease"),
    ("Reorder Priya's coffee beans", "Reorder my coffee beans"),
    ("Order a cake for Priyanka", "Order a cake for Priyanka"),  # only whole names
])
def test_names_never_appear_in_a_title(title, expected):
    from speakeasy.text import drop_names
    assert drop_names(title, "Priya", "Juniper") == expected


def test_the_titler_is_told_the_names_and_a_named_title_is_retried():
    text = title_input("Push all five of them", names=("Priya", "Juniper"))
    assert text.endswith("Names to leave out of the title: Priya, Juniper")
    title, why = router.clean_task_title('{"title": "Push all five of Priya\'s PRs"}', text)
    assert title is None and "Priya" in why


def test_a_live_title_never_carries_the_names(server, service, hermes):
    from test_routing import start_call, tasks
    service.settings.patch({"user_name": "Priya", "assistant_name": "Juniper"})
    seen = []
    service.rt.title_call = lambda text: seen.append(text) or "Push all five of Priya's PRs"
    _, worker = start_call(server, service)
    worker.delegate("call_n", "Juniper, push all five of the open pull requests on the garden repo")
    wait_for(lambda: any(t.get("title") == "Push all five of my PRs" for t in tasks(server)), timeout=10)
    assert "Names to leave out of the title: Priya, Juniper" in seen[0]


def test_the_sweep_renames_surviving_stand_ins_with_context(service):
    store = service.store
    for key, rowid_req in (("k_1", "Research the best standing desks under five hundred dollars"), ("k_2", "Fix and ship")):
        store._db.execute("INSERT INTO runs (idem_key, interaction_id, delegation_id, revision, status, title, updated) "
                          "VALUES (?, 'vi_s', ?, 1, 'completed', ?, 2)", (key, "call_" + key, short_title(rowid_req)))
        store.progress(key, "request", rowid_req)
    store._db.execute("UPDATE runs SET title='Research standing desks under 500' WHERE idem_key='k_1'")
    store.set_parent("k_2", "k_1")  # it answered k_1 (recorded when it started)
    seen = []
    service.rt.title_call = lambda text: seen.append(text) or "Order the chosen standing desk"
    assert service.sweep_titles() == 1
    assert store.title("k_2") == "Order the chosen standing desk"
    assert "Research standing desks under 500" in seen[0]
    assert service.sweep_titles() == 0  # nothing left, and a named task is never renamed
