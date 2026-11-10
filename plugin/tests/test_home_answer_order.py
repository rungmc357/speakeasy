"""SPK-314: 'your responses in the Home chat thread show up above a message I sent before'.

Drives the real HomeChat turn and the real Home now/stream builder (service.now -> NowModel._stream ->
stream.build_stream) on one fake clock. Only the coordinator model (HomeChat._call) and the job
backend (service.start_job / service.task_message) are faked: a "job" is a real run row in the store
(test_now.task), created at the fake clock and finished by store.transition. Invented data only.

Expectation (the user's): in the final stream (oldest first) the newest answer the user received
comes after every message the user sent before it. The job's card keeps its place (it never jumps on
progress or completion); a job that finishes after a newer message also says its answer at the bottom,
as a reply tied to its card (``d:<run>``, ``item_id``)."""
from __future__ import annotations

import json
import threading

from test_history import state  # noqa: F401
from test_home_stream import NOON
from test_now import NY, task


class Clock:
    def __init__(self, at: float):
        self.t = at

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float = 1.0) -> float:
        self.t += s
        return self.t


def rig(service, clock):
    """One clock for Home's chat and Home's cards; jobs are run rows created on that clock."""
    service.now_model.clock = clock
    service.home_chat.clock = clock
    service.store.set_meta("now_baseline_at", repr(clock() - 30 * 86400))   # not a first use
    jobs: list[str] = []

    def start_job(body, device_id=None, never_wait=False):
        clock.tick()                                   # the run is reserved just after the message
        key = f"k{len(jobs) + 1}"
        run = task(service.store, key, "running", session="s_" + key, request=body["text"], created=clock())
        jobs.append(key)
        return {"run_id": run, "session_id": "s_" + key, "task_id": key}

    steered: list[tuple[str, str]] = []

    def task_message(run_id, body, device_id=None):
        steered.append((run_id, body["text"]))          # a steer to a running job: same run
        return {"run_id": run_id, "delivered_as": "steer"}

    service.start_job = start_job
    service.task_message = task_message
    return jobs, steered


def script(service, *answers, gate=None):
    queue = list(answers)

    def call(messages):
        if gate is not None:
            gate(messages)
        nxt = queue.pop(0)
        if isinstance(nxt, dict) and "tool" not in nxt:
            nxt = json.dumps(nxt)
        return nxt, {"model": "fast-chat-1", "label": "Fast chat"}
    service.home_chat._call = call


def finish(service, clock, key, headline):
    clock.tick(30)
    service.store.transition(key, "completed", result={"spoken": headline, "headline": headline})
    with service.store._lock, service.store._db:
        service.store._db.execute("UPDATE runs SET settled_at=? WHERE idem_key=?", (clock(), key))
    return clock()


def entries(service):
    return service.now({"tz": NY})["stream"]["entries"]


def rows(ent):
    return [(e["kind"], e.get("role") or e.get("state"), round(e["at"] - NOON, 1),
             (e.get("text") or e.get("item_id") or "")[:40]) for e in ent]


def assert_answer_after_sent(ent, answer_id, answered_at):
    """The answer (entry id) the user got at *answered_at* sits after every message they sent before."""
    ids = [e["id"] for e in ent]
    assert answer_id in ids, f"answer {answer_id} not in stream:\n" + "\n".join(map(str, rows(ent)))
    where = ids.index(answer_id)
    later = [e for e in ent[where + 1:] if e["kind"] == "message" and e.get("role") == "user"
             and e["at"] < answered_at]
    order = "\n".join(f"  {i}: {r}" for i, r in enumerate(rows(ent)))
    assert not later, (f"answer {answer_id} (answered at +{answered_at - NOON:.0f}s) is drawn ABOVE "
                       f"{len(later)} user message(s) sent before it: {[e['text'] for e in later]}\n"
                       f"stream (kind, role/state, at-NOON, text):\n{order}")


# -- control ------------------------------------------------------------------------------------

def test_control_plain_chat_reply_lands_at_the_bottom(service):
    clock = Clock(NOON)
    rig(service, clock)
    script(service, "Hey! What's up?", "Paris.")
    service.home_chat.message({"text": "yo", "client_id": "c_1"})
    clock.tick(10)
    got = service.home_chat.message({"text": "capital of france?", "client_id": "c_2"})
    ent = entries(service)
    print("\n".join(map(str, rows(ent))))
    assert ent[-1]["id"] == got["reply"]["id"]
    assert_answer_after_sent(ent, got["reply"]["id"], got["reply"]["at"])


# -- H1: a Home job's answer lands in its card at the job's start ----------------------------------

def test_h1_job_answer_after_a_later_chat_message_lands_at_the_bottom(service):
    clock = Clock(NOON)
    jobs, _ = rig(service, clock)
    script(service, {"reply": "Looking for a quiet cafe.", "action": "start", "request": "Find a quiet cafe"},
           "About 384,400 km on average.")
    first = service.home_chat.message({"text": "find me a quiet cafe near the river", "client_id": "c_a"})
    run = first["run_id"]
    clock.tick(20)
    service.home_chat.message({"text": "how far is the moon?", "client_id": "c_b"})   # while the job runs
    done = finish(service, clock, jobs[0], "Riverside Roasters opens at 7.")
    ent = entries(service)
    (card,) = [e for e in ent if e["kind"] == "card"]
    assert card["state"] in {"new_answer", "seen_answer"} and card["id"] == f"c:{run}"
    print("\n".join(map(str, rows(ent))))
    ids = [e["id"] for e in ent]
    moon = next(e["id"] for e in ent if e.get("text") == "how far is the moon?")
    assert ids.index(card["id"]) < ids.index(moon)          # the card stays where it was asked
    line = ent[-1]
    assert line["id"] == f"d:{run}" and line["item_id"] == card["item_id"] and line["at"] == done
    assert line["kind"] == "message" and line["role"] == "assistant" and line["answer"] is True
    assert line["text"] == "Riverside Roasters opens at 7."
    assert_answer_after_sent(ent, line["id"], done)


# -- H2: a follow-up to a running job --------------------------------------------------------------

def test_h2_follow_up_answer_lands_below_the_follow_up(service):
    clock = Clock(NOON)
    jobs, steered = rig(service, clock)
    script(service, {"reply": "Checking the forecast.", "action": "start", "request": "Weather for Saturday"},
           {"reply": "Asking it for Sunday too.", "action": "follow_up", "job": "j1", "request": "Also check Sunday"})
    first = service.home_chat.message({"text": "weather saturday?", "client_id": "c_w1"})
    clock.tick(20)
    follow = service.home_chat.message({"text": "and sunday", "client_id": "c_w2"})
    assert follow["action"] == "follow_up" and steered == [(first["run_id"], "Also check Sunday")]
    done = finish(service, clock, jobs[0], "Saturday sunny, Sunday rain.")
    ent = entries(service)
    print("\n".join(map(str, rows(ent))))
    assert ent[-3]["id"] == follow["message"]["id"] and ent[-2]["id"] == follow["reply"]["id"]  # ack, then the answer
    (card,) = [e for e in ent if e["kind"] == "card"]
    assert card["state"] in {"new_answer", "seen_answer"}
    assert ent[-1]["id"] == f"d:{first['run_id']}" and ent[-1]["text"] == "Saturday sunny, Sunday rain."
    assert_answer_after_sent(ent, ent[-1]["id"], done)


# -- H3: B posted while A's coordinator call is still running --------------------------------------

def test_h3_two_messages_in_flight(service):
    clock = Clock(NOON)
    rig(service, clock)
    a_in, b_done = threading.Event(), threading.Event()
    answers = {"first?": "Answer to A.", "second?": "Answer to B."}

    def call(messages):
        asked = messages[-1]["content"]
        if asked == "first?":
            a_in.set()
            assert b_done.wait(10)     # A's model call is still running while B is sent and answered
        return answers[asked], {"model": "fast-chat-1", "label": "Fast chat"}
    service.home_chat._call = call
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(a=service.home_chat.message({"text": "first?", "client_id": "c_A"})))
    t.start()
    assert a_in.wait(10)
    clock.tick(5)
    out["b"] = service.home_chat.message({"text": "second?", "client_id": "c_B"})
    clock.tick(5)
    b_done.set()
    t.join(10)
    ent = entries(service)
    print("\n".join(map(str, rows(ent))))
    a, b = out["a"], out["b"]
    for got in (a, b):
        assert_answer_after_sent(ent, got["reply"]["id"], got["reply"]["at"])
    ids = [e["id"] for e in ent]
    # Note the order the user sees (printed above): A's reply relative to B.
    print("A reply index", ids.index(a["reply"]["id"]), "B msg index", ids.index(b["message"]["id"]),
          "B reply index", ids.index(b["reply"]["id"]))


# -- no extra line when nothing came after the job -------------------------------------------------

def test_a_job_that_finishes_with_nothing_newer_adds_no_line(service):
    clock = Clock(NOON)
    jobs, _ = rig(service, clock)
    script(service, {"reply": "Looking for a quiet cafe.", "action": "start", "request": "Find a quiet cafe"})
    first = service.home_chat.message({"text": "find me a quiet cafe near the river", "client_id": "c_a"})
    finish(service, clock, jobs[0], "Riverside Roasters opens at 7.")
    ent = entries(service)
    assert not [e for e in ent if e["id"].startswith("d:")]
    assert ent[-1]["id"] == f"c:{first['run_id']}"          # its card is already the newest thing


def test_a_message_sent_after_the_job_finished_adds_no_line(service):
    clock = Clock(NOON)
    jobs, _ = rig(service, clock)
    script(service, {"reply": "Looking for a quiet cafe.", "action": "start", "request": "Find a quiet cafe"},
           "Thanks!")
    service.home_chat.message({"text": "find me a quiet cafe near the river", "client_id": "c_a"})
    finish(service, clock, jobs[0], "Riverside Roasters opens at 7.")
    clock.tick(5)
    service.home_chat.message({"text": "nice", "client_id": "c_b"})
    ent = entries(service)
    assert not [e for e in ent if e["id"].startswith("d:")]   # you saw the answer before you wrote


def test_a_running_job_gets_no_line_and_its_card_never_moves(service):
    clock = Clock(NOON)
    rig(service, clock)
    script(service, {"reply": "Looking for a quiet cafe.", "action": "start", "request": "Find a quiet cafe"},
           "About 384,400 km on average.")
    first = service.home_chat.message({"text": "find me a quiet cafe near the river", "client_id": "c_a"})
    clock.tick(20)
    service.home_chat.message({"text": "how far is the moon?", "client_id": "c_b"})
    ent = entries(service)
    assert not [e for e in ent if e["id"].startswith("d:")]
    ids = [e["id"] for e in ent]
    assert ids.index(f"c:{first['run_id']}") < ids.index(next(e["id"] for e in ent if e.get("text") == "how far is the moon?"))


def test_replying_to_the_answer_line_follows_that_job(service):
    clock = Clock(NOON)
    jobs, steered = rig(service, clock)
    script(service, {"reply": "Looking for a quiet cafe.", "action": "start", "request": "Find a quiet cafe"},
           "About 384,400 km on average.",
           {"reply": "Asking it.", "action": "follow_up", "job": "j1", "request": "Does it have wifi?"})
    first = service.home_chat.message({"text": "find me a quiet cafe near the river", "client_id": "c_a"})
    clock.tick(20)
    service.home_chat.message({"text": "how far is the moon?", "client_id": "c_b"})
    finish(service, clock, jobs[0], "Riverside Roasters opens at 7.")
    line = entries(service)[-1]
    assert line["id"] == f"d:{first['run_id']}"
    got = service.home_chat.message({"text": "wifi there?", "client_id": "c_c", "reply_to": line["id"]})
    assert got["message"]["quote"]["run_id"] == first["run_id"]   # the quote is that job's card
