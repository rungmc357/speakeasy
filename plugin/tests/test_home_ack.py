"""home-ack (Geo, 11-11: "it said thinking for like almost a minute before it said anything back"):
every Home turn shows words within the deadline. An answer the coordinator has in time comes alone;
otherwise an acknowledgement for this ask goes up first (the fast model's, else one derived from the
ask), the status capsule shows under it, and the answer/job follows in the same exchange without
repeating it. The coordinator and the acknowledgement writer are scripted. Invented data only."""
from __future__ import annotations

import json
import re
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
    # ack-context: a neutral beat, never the ask read back ("Let me look into your flight info...").
    assert body["reply"]["part"] == "ack" and said in HC.NEUTRAL_ACKS and "Lisbon" not in said
    gate.set()
    wait_for(lambda: len([m for m in messages(server) if m["role"] == "assistant"]) == 2)


def test_a_slow_ack_model_is_not_waited_for(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Done.", "action": "none"}, gate=gate, ack="Too late to matter.", ack_delay=3.0)
    t0 = time.monotonic()
    _, body = post(server, {"text": "how is the patio quote going", "client_id": "c_slowack"})
    assert time.monotonic() - t0 < 0.9
    assert body["reply"]["text"] != "Too late to matter." and body["reply"]["text"] in HC.NEUTRAL_ACKS
    gate.set()


def test_no_home_turn_goes_past_the_deadline_without_words(server, service, hermes):
    """The hard server-side deadline at its real value: a coordinator that hangs for 6 s still has
    words in the thread by the fallback mark (ACK_FALLBACK_SHARE of ACK_DEADLINE_S, counted from the
    request's arrival), so they reach the phone inside ACK_DEADLINE_S."""
    gate = threading.Event()
    script(service, {"reply": "Here.", "action": "none"}, gate=gate, ack=None, deadline=HC.ACK_DEADLINE_S)
    assert 2.0 <= HC.ACK_DEADLINE_S <= 3.0
    t0 = time.monotonic()
    _, body = post(server, {"text": "summarize what came in from the contractor", "client_id": "c_hard"})
    took = time.monotonic() - t0
    hard = HC.ACK_DEADLINE_S * HC.ACK_FALLBACK_SHARE
    assert hard - 0.1 <= took < HC.ACK_DEADLINE_S, took
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


def test_fallback_ack_never_reads_the_ask_back():
    """ack-context: the line without a model is a neutral beat or names the job a follow-up is about;
    it never rebuilds the user's words (the old topic templates mangled them)."""
    assert HC.fallback_ack("why is the build failing on the release branch") in HC.NEUTRAL_ACKS
    assert HC.fallback_ack("can you check my flight info for japan") in HC.NEUTRAL_ACKS
    b = HC.fallback_ack("check my flight info for japan")
    assert HC.fallback_ack("check my flight info for japan", avoid=b) != b
    assert HC.fallback_ack("", files=[{"kind": "picture"}]) == "Let me take a look at the picture."
    assert HC.fallback_ack("that's a fun idea honestly") in HC.CHAT_ACKS
    assert HC.fallback_ack("thanks!") in HC.CHAT_ACKS
    said = HC.fallback_ack("submit it", job="Opt out of the people-search sites")
    assert said.startswith("Back on \u201cOpt out of the people-search") and said.endswith("\u201d.")
    assert HC.fallback_ack("any update", job="Paint search") == "Back on the paint search."
    assert HC.fallback_ack("ha nice", job="Paint search", chat=True) in HC.CHAT_ACKS
    assert HC.fallback_ack("ok send it", job="A job") in HC.NEUTRAL_ACKS     # a placeholder title isn't a name


INVENTED_ASKS = [
    "will the new pillow actually help my neck",
    "is a rail pass worth it for the Kyushu loop",
    "a checked bag works",
    "honestly that layover was brutal",
    "swiping to dismiss the reminder also flips the page on build 9",
    "can you draft a note to the invented landlord about the leak",
    "why is the patio quote so high",
    "what's my flight info for the Lisbon trip",
    "I think the green one looks better",
    "find me a rain jacket under $100",
]


def test_fallback_never_echoes_or_mangles_the_users_words():
    for ask in INVENTED_ASKS:
        for job in (None, "Fence repair estimate"):
            line = HC.fallback_ack(ask, job=job)
            words = [w for w in re.findall(r"[a-z$0-9']+", ask.lower())]
            trigrams = {" ".join(words[i:i + 3]) for i in range(len(words) - 2)}
            flat = " ".join(re.findall(r"[a-z$0-9']+", line.lower()))
            assert not any(t in flat for t in trigrams), (ask, line)
            assert "look into" not in line.lower() and "think about" not in line.lower(), (ask, line)
            assert len(line.split()) <= 8, line


def test_remarks_never_get_a_look_into_line():
    for remark in ("honestly that layover was brutal", "I think the green one looks better", "lol", "fair enough",
                   "that's a fun idea honestly", "ugh mondays"):
        assert HC.fallback_ack(remark) in HC.CHAT_ACKS


def test_after_ack_never_repeats_the_acknowledgement():
    assert HC.after_ack("Looking into the swipe bug now.", "start", "Let me look at the swipe bug.") == ""
    assert HC.after_ack("On it. I'll also check the build after.", "start", "Checking the swipe.") == \
        "I'll also check the build after."
    assert HC.after_ack("Your dentist visit is Thursday.", "none", "Let me look at your calendar.") == \
        "Your dentist visit is Thursday."
    assert HC.after_ack("Let me look at your calendar. It's Thursday.", "none", "Let me look at your calendar.") == \
        "It's Thursday."


def test_clean_ack_rejects_read_backs_and_skip():
    assert HC.clean_ack("SKIP", "hi") is None and HC.clean_ack("Ha, fair point.", "hermes is great") == "Ha, fair point."
    assert HC.clean_ack("I'll dig into the swipe bug. It might be the gesture.", "swipe bug") == "I'll dig into the swipe bug."
    assert HC.clean_ack("Pulling up your Japan flight details", "flight?") == "Pulling up your Japan flight details."


def test_clean_ack_strips_the_kind_tag():
    assert HC.ack_tag("CHAT: Ha, layovers are the worst.") == ("CHAT", "Ha, layovers are the worst.")
    assert HC.ack_tag("JOB - Looking for a rain jacket in budget.")[0] == "JOB"
    assert HC.ack_tag("Looking now.") == (None, "Looking now.")
    assert HC.clean_ack("JOB: Submitting the opt-out now.", "submit it") == "Submitting the opt-out now."
    assert HC.clean_ack("CHAT: SKIP", "lol") is None


# -- ack-context: the writer sees the conversation ------------------------------------------------

def capture(service, answer, gate=None, ack="JOB: Back on the fence quote.", deadline=0.4):
    """Like ``script`` but records what the acknowledgement writer was sent, and when."""
    seen: list = []
    calls = script(service, answer, gate=gate, ack=ack, deadline=deadline)
    inner = service.home_chat._ack_call

    def write(messages):
        seen.append((time.monotonic(), messages))
        return inner(messages)
    service.home_chat._ack_call = write
    return seen, calls


def test_the_ack_writer_sees_the_last_messages_but_not_old_acks(server, service, hermes):
    chat = service.home_chat
    uid = chat._add("user", "make a packing list for the invented Bergen trip", chat.clock() - 30)
    chat._add("assistant", "Let me look into that.", chat.clock() - 29, reply_to=uid, part="ack")
    chat._add("assistant", "Here's the list. Carry-on only, or are you checking a bag?", chat.clock() - 20,
              reply_to=uid, part="answer")
    gate = threading.Event()
    seen, _ = capture(service, {"reply": "Then the rain shell and boots both fit.", "action": "none"}, gate=gate,
                      ack="CHAT: Good, a checked bag means the boots can come.")
    _, body = post(server, {"text": "a checked bag works", "client_id": "c_ctx"})
    content = seen[0][1][-1]["content"]
    assert "packing list for the invented Bergen trip" in content and "checking a bag?" in content
    assert "Let me look into that." not in content           # earlier filler acks are left out
    assert content.rstrip().endswith("Newest message: a checked bag works")
    assert "JOB" in seen[0][1][0]["content"] and "CHAT" in seen[0][1][0]["content"]
    assert len(content) < 2500                               # small prompt: it races the clock
    gate.set()
    wait_for(lambda: not now_body(server)["stream"]["current"]["pending"])
    texts = [m["text"] for m in messages(server) if m["role"] == "assistant"]
    assert texts[-1] == "Then the rain shell and boots both fit."


def test_the_ack_writer_keeps_only_the_last_six_messages():
    from types import SimpleNamespace
    chat = HC.HomeChat.__new__(HC.HomeChat)
    chat.svc = SimpleNamespace(settings=SimpleNamespace(get=lambda: {"assistant_name": "Todd"}))
    history = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"invented line {i} " + "x" * 400}
               for i in range(10)]
    msgs = chat.ack_messages("and the other one?", history)
    content = msgs[-1]["content"]
    assert "invented line 3 " not in content and "invented line 4 " in content and "invented line 9 " in content
    assert all(len(line) <= HC.ACK_LINE_CHARS + 10 for line in content.splitlines())
    assert msgs[0]["content"].startswith("You are Todd")


def test_a_reply_to_a_card_gives_the_writer_the_card_and_a_job_named_fallback(server, service, hermes):
    script(service, {"reply": "Let me look for a fence quote.", "action": "start",
                     "request": "Get a quote for the invented fence repair"})
    first = post(server, {"text": "get a quote for the fence repair", "client_id": "c_card1"})[1]
    run_id = first["run_id"]
    assert run_id
    title = HC.HomeChat._card_quote(service.home_chat, run_id)["text"]
    gate = threading.Event()
    seen, _ = capture(service, {"reply": "Sending it.", "action": "follow_up", "job": "j1",
                                "request": "Submit the quote request"}, gate=gate, ack=RuntimeError("down"))
    _, body = post(server, {"text": "submit it", "client_id": "c_card2", "reply_to": "r:" + run_id})
    content = seen[0][1][-1]["content"]
    assert f"replying to the job card \u201c{title}" in content
    said = body["reply"]["text"]
    assert body["reply"]["part"] == "ack" and said.startswith("Back on ") and "submit" not in said.lower()
    gate.set()


def test_the_ack_writer_starts_the_moment_the_message_arrives(server, service, hermes):
    gate = threading.Event()
    seen, _ = capture(service, {"reply": "Here.", "action": "none"}, gate=gate)
    t0 = time.monotonic()
    post(server, {"text": "how is the fence quote coming along", "client_id": "c_fast"})
    assert seen and seen[0][0] - t0 < 0.15, seen[0][0] - t0
    gate.set()


def test_a_quick_conversational_answer_gets_no_filler_line(server, service, hermes):
    """A remark answered by CHAT_SHARE of the deadline: the answer alone, even though it came after
    the point a job's acknowledgement would have gone up."""
    gate = threading.Event()
    script(service, {"reply": "Ha, nine hours in Doha would break anyone.", "action": "none"}, gate=gate,
           ack=RuntimeError("down"))
    t0 = time.monotonic()
    threading.Timer(0.34, gate.set).start()   # between the fallback mark (0.29 s) and the chat mark (0.48 s)
    _, body = post(server, {"text": "honestly that layover was brutal", "client_id": "c_chat1"})
    assert time.monotonic() - t0 > 0.4 * HC.ACK_FALLBACK_SHARE        # it came after a job's ack mark
    assert body["reply"]["text"] == "Ha, nine hours in Doha would break anyone." and "part" not in body["reply"]
    assert [m["role"] for m in messages(server)] == ["user", "assistant"]


def test_a_chat_tagged_question_waits_for_its_answer_too(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Yes, for that loop the pass pays for itself.", "action": "none"}, gate=gate,
           ack="CHAT: Ooh, the Kyushu loop.", ack_delay=0.0)
    threading.Timer(0.34, gate.set).start()
    _, body = post(server, {"text": "is a rail pass worth it for the Kyushu loop", "client_id": "c_chat2"})
    assert "part" not in body["reply"] and body["reply"]["text"].startswith("Yes, for that loop")


def test_a_slow_conversational_answer_gets_the_writers_beat_then_the_answer(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "A firmer pillow usually helps within a week.", "action": "none"}, gate=gate,
           ack="CHAT: Fingers crossed for your neck.")
    t0 = time.monotonic()
    _, body = post(server, {"text": "will the new pillow actually help my neck?", "client_id": "c_chat3"})
    took = time.monotonic() - t0
    assert 0.4 * HC.CHAT_SHARE - 0.05 <= took < 0.4 * HC.CHAT_SHARE + 0.4, took
    assert body["reply"]["part"] == "ack" and body["reply"]["text"] == "Fingers crossed for your neck."
    gate.set()
    wait_for(lambda: not now_body(server)["stream"]["current"]["pending"])


def test_a_slow_remark_with_no_model_line_gets_a_plain_beat(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Fair, it is a lot.", "action": "none"}, gate=gate, ack=RuntimeError("down"))
    _, body = post(server, {"text": "ugh airports are the worst", "client_id": "c_chat4"})
    assert body["reply"]["part"] == "ack" and body["reply"]["text"] in HC.CHAT_ACKS
    gate.set()


def test_a_job_tagged_line_still_goes_up_early(server, service, hermes):
    gate = threading.Event()
    script(service, {"reply": "Looking.", "action": "start", "request": "Find a rain jacket under $100"}, gate=gate,
           ack="JOB: Hunting for a rain jacket in budget.", ack_delay=0.05)
    t0 = time.monotonic()
    _, body = post(server, {"text": "find me a rain jacket under $100", "client_id": "c_job"})
    assert time.monotonic() - t0 < 0.4 * HC.ACK_FALLBACK_SHARE + 0.15
    assert body["reply"]["text"] == "Hunting for a rain jacket in budget."
    gate.set()
    wait_for(lambda: hermes.calls)
