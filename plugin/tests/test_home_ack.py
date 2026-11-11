"""home-ack (Geo, 11-11: "it said thinking for like almost a minute before it said anything back"):
every Home turn shows words within the deadline. An answer the coordinator has in time comes alone;
otherwise an acknowledgement for this ask goes up first (the fast model's, else one derived from the
ask), the status capsule shows under it, and the answer/job follows in the same exchange without
repeating it. The coordinator and the acknowledgement writer are scripted. Invented data only."""
from __future__ import annotations

import json
import threading
import time

from fakes import http, wait_for
from speakeasy import home_chat as HC
from test_routing import tasks


def post(server, body):
    return http(server.base_url, "POST", "/voice/home/message", body, server.token)


def now_body(server):
    return http(server.base_url, "GET", "/voice/now?tz=America/New_York", None, server.token)[1]


def messages(server):
    return [e for e in now_body(server)["stream"]["entries"] if e["kind"] == "message"]


def script(service, answer, delay=0.0, ack=None, ack_delay=0.0, deadline=0.4, gate=None):
    """The coordinator answers *answer* after *delay* s (or when *gate* is set); the ack writer
    answers *ack* after *ack_delay* s (an Exception instance raises)."""
    chat = service.home_chat
    chat.deadline_s = deadline
    calls = {"coord": 0, "ack": 0}

    def coord(_messages):
        calls["coord"] += 1
        if gate is not None:
            gate.wait(10)
        else:
            time.sleep(delay)
        nxt = answer
        if isinstance(nxt, dict) and "tool" not in nxt:
            nxt = json.dumps(nxt)
        return nxt, {"model": "fast-chat-1", "label": "Fast chat"}

    def write(_messages):
        calls["ack"] += 1
        time.sleep(ack_delay)
        if isinstance(ack, Exception):
            raise ack
        return ack, {"model": "fast-chat-1", "label": "Fast chat"}
    chat._call = coord
    chat._ack_call = write
    return calls


def test_an_instant_answer_has_no_acknowledgement_in_front(server, service, hermes):
    script(service, {"reply": "It's 3:10 PM.", "action": "none"}, delay=0.0, ack="Let me check the time.")
    t0 = time.monotonic()
    status, body = post(server, {"text": "what time is it", "client_id": "c_inst"})
    assert status == 200 and time.monotonic() - t0 < 0.4
    assert body["action"] == "none" and body["reply"]["text"] == "It's 3:10 PM." and "part" not in body["reply"]
    msgs = messages(server)
    assert [m.get("role") for m in msgs] == ["user", "assistant"]
    cur = now_body(server)["stream"]["current"]
    assert [p["part"] for p in cur["parts"]] == ["reply"] and cur["pending"] is False


def test_a_slow_turn_shows_the_models_acknowledgement_by_the_deadline(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Checking the build logs.", "action": "start", "request": "Why does the beta build fail"},
           gate=gate, ack="I'll dig into why the beta build keeps failing.", ack_delay=0.05)
    t0 = time.monotonic()
    status, body = post(server, {"text": "why does the beta build keep failing", "client_id": "c_slow"})
    took = time.monotonic() - t0
    assert status == 200 and took < 0.4 + 0.5, took          # the request returns at the deadline
    assert body["pending"] is True and body["action"] == "pending"
    assert body["reply"]["text"] == "I'll dig into why the beta build keeps failing." and body["reply"]["part"] == "ack"
    cur = now_body(server)["stream"]["current"]
    assert cur["pending"] is True and [p["part"] for p in cur["parts"]] == ["ack"]
    user = [m for m in messages(server) if m["role"] == "user"][0]
    assert user["pending"] is True                           # the capsule shows under the ack
    gate.set()
    wait_for(lambda: hermes.calls)
    cur = wait_for(lambda: (c := now_body(server)["stream"]["current"]) and not c["pending"] and c["card_ids"] and c)
    # The job's lead sentence was its acknowledgement: nothing repeats; the card follows the ack.
    msgs = messages(server)
    assert [m["text"] for m in msgs if m["role"] == "assistant"] == ["I'll dig into why the beta build keeps failing."]
    assert [p["part"] for p in cur["parts"]] == ["ack"] and cur["run_ids"] and len(tasks(server)) == 1
    assert msgs[1]["run_id"] == cur["run_ids"][0]           # the ack belongs to the job it announced


def test_the_answer_follows_the_ack_in_order_without_repeating_it(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Let me look at the calendar. Your dentist visit is Thursday at 9:30 AM.", "action": "none"},
           gate=gate, ack="Let me look at your calendar for the dentist.")
    _, body = post(server, {"text": "when's my dentist appointment", "client_id": "c_dent"})
    assert body["reply"]["part"] == "ack"
    gate.set()
    wait_for(lambda: not now_body(server)["stream"]["current"]["pending"])
    msgs = messages(server)
    assert [(m["role"], m.get("part"), m["text"]) for m in msgs] == [
        ("user", None, "when's my dentist appointment"),
        ("assistant", "ack", "Let me look at your calendar for the dentist."),
        ("assistant", "answer", "Your dentist visit is Thursday at 9:30 AM."),
    ]
    cur = now_body(server)["stream"]["current"]
    assert [p["part"] for p in cur["parts"]] == ["ack", "answer"] and cur["pending"] is False
    assert cur["reply_ids"] == [msgs[1]["id"], msgs[2]["id"]]


def test_a_failed_ack_model_still_lands_a_line_from_the_ask_inside_the_deadline(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Your flight is JL 5 at 1:05 PM.", "action": "none"}, gate=gate,
           ack=RuntimeError("provider down"))
    t0 = time.monotonic()
    _, body = post(server, {"text": "what's my flight info for the Lisbon trip", "client_id": "c_fail"})
    assert time.monotonic() - t0 < 0.9
    said = body["reply"]["text"]
    assert body["reply"]["part"] == "ack" and "flight info for the Lisbon trip" in said and said.endswith(".")
    gate.set()
    wait_for(lambda: len([m for m in messages(server) if m["role"] == "assistant"]) == 2)


def test_a_slow_ack_model_is_not_waited_for(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Done.", "action": "none"}, gate=gate, ack="Too late to matter.", ack_delay=3.0)
    t0 = time.monotonic()
    _, body = post(server, {"text": "how is the patio quote going", "client_id": "c_slowack"})
    assert time.monotonic() - t0 < 0.9
    assert body["reply"]["text"] != "Too late to matter." and "patio quote" in body["reply"]["text"]
    gate.set()


def test_no_home_turn_goes_past_the_deadline_without_words(server, service, hermes):
    """The hard server-side deadline at its real value: a coordinator that hangs for 6 s still has
    words in the thread by ACK_DEADLINE_S (plus scheduling slack)."""
    gate = threading.Event()
    script(service, {"reply": "Here.", "action": "none"}, gate=gate, ack=None, deadline=HC.ACK_DEADLINE_S)
    assert 2.0 <= HC.ACK_DEADLINE_S <= 3.0
    t0 = time.monotonic()
    _, body = post(server, {"text": "summarize what came in from the contractor", "client_id": "c_hard"})
    took = time.monotonic() - t0
    assert HC.ACK_DEADLINE_S - 0.1 <= took < HC.ACK_DEADLINE_S + 0.5, took
    assert body["reply"]["part"] == "ack" and body["reply"]["text"]
    gate.set()


def test_a_retry_of_a_pending_turn_replays_the_ack_and_starts_nothing_twice(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "On it.", "action": "start", "request": "Draft the fence quote"}, gate=gate,
           ack="Drafting the fence quote now.")
    first = post(server, {"text": "draft the fence quote", "client_id": "c_rp"})[1]
    again = post(server, {"text": "draft the fence quote", "client_id": "c_rp"})[1]
    assert again["replayed"] is True and again["reply"]["id"] == first["reply"]["id"]
    gate.set()
    wait_for(lambda: hermes.calls)
    time.sleep(0.2)
    assert len(tasks(server)) == 1


def test_a_new_message_rolls_the_ack_and_answer_into_the_transcript(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Your balance is $1,240.", "action": "none"}, gate=gate, ack="Checking your account balance.")
    post(server, {"text": "what's my checking balance", "client_id": "c_bal"})
    gate.set()
    wait_for(lambda: len(messages(server)) == 3)
    script(service, {"reply": "Hey!", "action": "none"})
    post(server, {"text": "thanks", "client_id": "c_thx"})
    body = now_body(server)
    cur = body["stream"]["current"]
    ids = {m["id"]: m for m in messages(server)}
    old = [m for m in ids.values() if m.get("part") in ("ack", "answer")]
    assert len(old) == 2 and not any(m.get("current") for m in old)   # transcript-only now, both kept in order
    assert [p["part"] for p in cur["parts"]] == ["reply"]


def test_the_twenty_minute_reset_clears_an_acked_exchange(server, service, hermes):
    from speakeasy.home_state import RESET_AFTER_S, current_exchange
    entries = [{"id": "m:hu_1", "kind": "message", "role": "user", "at": 100.0},
               {"id": "m:ha_1", "kind": "message", "role": "assistant", "at": 101.0, "part": "ack"},
               {"id": "m:ha_2", "kind": "message", "role": "assistant", "at": 140.0, "part": "answer"}]
    live = current_exchange([dict(e) for e in entries], 150.0)
    assert [p["part"] for p in live["parts"]] == ["ack", "answer"]
    assert current_exchange([dict(e) for e in entries], 140.0 + RESET_AFTER_S)["clean"] is True


def test_a_restart_mid_turn_keeps_the_ack_and_says_so(server, service, hermes):
    chat = service.home_chat
    uid = chat._add("user", "find the warranty for the dishwasher", chat.clock(), client_id="c_rs")
    chat._add("assistant", "Let me find the dishwasher warranty.", chat.clock(), reply_to=uid, part="ack")
    again = HC.HomeChat(service)            # the plugin coming back up
    rows = again._rows("WHERE reply_to=?", (uid,))
    texts = sorted((r["part"], r["text"]) for r in rows)
    assert texts[0] == ("ack", "Let me find the dishwasher warranty.")
    assert texts[1][0] == "answer" and "restarted" in texts[1][1]
    HC.HomeChat(service)                    # a second start adds nothing more
    assert len(again._rows("WHERE reply_to=?", (uid,))) == 2


def test_fallback_ack_is_specific_and_varies():
    a = HC.fallback_ack("why is the build failing on the release branch")
    assert a.startswith("Let me") and "why the build is failing" in a
    b = HC.fallback_ack("can you check my flight info for japan")
    assert "your flight info for japan" in b
    c = HC.fallback_ack("what's the weather in Lisbon this weekend")
    assert "weather in Lisbon this weekend" in c
    assert len({a, b, c}) == 3
    assert HC.fallback_ack("check my flight info for japan", avoid=b) != b
    assert HC.fallback_ack("", files=[{"kind": "picture"}]).startswith("Let me take a look at")


def test_after_ack_never_repeats_the_acknowledgement():
    assert HC.after_ack("Looking into the swipe bug now.", "start", "Let me look at the swipe bug.") == ""
    assert HC.after_ack("On it. I'll also check the build after.", "start", "Checking the swipe.") == \
        "I'll also check the build after."
    assert HC.after_ack("Your dentist visit is Thursday.", "none", "Let me look at your calendar.") == \
        "Your dentist visit is Thursday."
    assert HC.after_ack("Let me look at your calendar. It's Thursday.", "none", "Let me look at your calendar.") == \
        "It's Thursday."


def test_clean_ack_rejects_read_backs_and_skip():
    assert HC.clean_ack("SKIP", "hi") is None
    assert HC.clean_ack("I'll dig into the swipe bug. It might be the gesture.", "swipe bug") == "I'll dig into the swipe bug."
    assert HC.clean_ack("Pulling up your Japan flight details", "flight?") == "Pulling up your Japan flight details."
