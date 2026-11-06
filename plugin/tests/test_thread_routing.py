"""Finding the conversation work was going on in, by what was said there, not by its name."""
from __future__ import annotations

import json
import sqlite3
import time

from speakeasy import continuity, router
from speakeasy.calls import SidebandWorker as CallWorker
from speakeasy.store import StateStore as Store

GUILD = "My Server"


def state_db(path, sessions, messages, fts=True):
    """sessions: (id, chat_id, title, ended); messages: (session_id, role, content, age_s)."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_type TEXT, thread_id TEXT, "
               "origin_json TEXT, title TEXT, started_at REAL, ended_at REAL)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, "
               "content TEXT, tool_calls TEXT, timestamp REAL)")
    now = time.time()
    for sid, chat_id, title, ended in sessions:
        origin = {"platform": "discord", "chat_id": chat_id, "chat_type": "thread", "thread_id": chat_id,
                  "chat_name": f"{GUILD} / #build / {title}", "user_id": "42", "parent_chat_id": "111"}
        db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                   (sid, "discord", "thread", chat_id, json.dumps(origin), title, now - 7200,
                    now - 60 if ended else None))
    for sid, role, content, age in messages:
        db.execute("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)",
                   (sid, role, content, None, now - age))
    if fts:
        db.execute("CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='id')")
        db.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    db.commit()
    db.close()
    return path


FILLER = [(f"s_f{i}", str(950 + i), f"Other chat {i}", False) for i in range(8)]
FILLER_TALK = [(f"s_f{i}", "user", f"[sam] please check the thing number {i} again", 4000 + i) for i in range(8)]


def workspace(tmp_path, fts=True):
    # The build thread's title is stale ("Gateway tests exit code 1") but the talk is about routing.
    return state_db(tmp_path / "state.db",
                    [("s_build", "900", "Gateway tests exit code 1", False),
                     ("s_trip", "901", "Lisbon trip", False),
                     ("s_new", "902", "Halloween costume ideas", False), *FILLER],
                    [("s_build", "user", "[Triggering message id: `1` — use as `message_id`]\n\n[sam] the thread "
                                         "routing is still hit or miss finding the right thread", 600),
                     ("s_build", "assistant", "Thread routing matches titles only; routing needs content.", 590),
                     ("s_build", "user", "[sam] make speakeasy routing use what was said in each thread", 500),
                     ("s_trip", "user", "[sam] find a hotel in Lisbon near the river", 3000),
                     ("s_new", "user", "[sam] halloween costume research please", 30), *FILLER_TALK], fts=fts)


def test_candidates_come_from_what_was_said_not_the_title(tmp_path):
    db = workspace(tmp_path)
    found = continuity.conversations_with_context(db, "keep going on the thread routing fix")
    by_id = {c.conv.session_id: c for c in found}
    assert by_id["s_build"].hits >= continuity.MIN_SCORE and by_id["s_trip"].hits == 0
    assert found[0].conv.session_id == "s_new"  # the most recently active chat is always offered
    # the user's own lines, without gateway scaffolding
    assert by_id["s_build"].snippets[0] == "make speakeasy routing use what was said in each thread"
    assert all("Triggering" not in s and "[sam]" not in s for c in found for s in c.snippets)
    asked = "continue the thread routing work on speakeasy"
    found = continuity.conversations_with_context(db, asked)
    assert continuity.best_by_content(asked, found).session_id == "s_build"
    assert continuity.best_by_content("what's the weather", found) is None  # no continuation cue


def test_candidates_work_without_a_full_text_index(tmp_path):
    db = workspace(tmp_path, fts=False)
    found = continuity.conversations_with_context(db, "keep going on the thread routing fix")
    assert {c.conv.session_id: c.hits for c in found}["s_build"] >= continuity.MIN_SCORE


def test_a_thread_whose_session_was_reset_continues_in_its_current_session(tmp_path):
    db = state_db(tmp_path / "state.db",
                  [("s_old", "900", "Old title", True), ("s_cur", "900", "Old title", False)],
                  [("s_old", "user", "[sam] routing work", 5000), ("s_cur", "user", "[sam] still routing", 100)])
    # Speakeasy recorded the old session when it first sent work there.
    assert continuity.conversation_by_session(db, "s_old").session_id == "s_cur"


def test_voice_placements_rank_first_and_survive_session_swaps(tmp_path):
    db = state_db(tmp_path / "state.db",
                  [("s_old", "900", "Gateway tests", True), ("s_cur", "900", "Gateway tests", False),
                   ("s_busy", "905", "Lots of chatter", False)],
                  [("s_old", "user", "[sam] x", 9000), ("s_cur", "user", "[sam] y", 4000),
                   ("s_busy", "user", "[sam] newest chat", 10)])
    found = continuity.conversations_with_context(db, "keep going")
    assert found[0].conv.session_id == "s_busy"  # most recent activity first, before placements
    ranked = continuity.with_placements(db, found, [{"session_id": "s_old", "request": "fix thread routing",
                                                     "at": time.time() - 300}])
    assert ranked[0].conv.session_id == "s_cur" and ranked[0].voice_request == "fix thread routing"


def test_store_remembers_where_voice_work_went_across_calls(tmp_path):
    store = Store(tmp_path / "voice.db")
    store.reserve_run("k1", "call_a", "d1", 1)
    store.progress("k1", "request", "fix the thread routing")
    store.set_continued("k1", "s_build", 'Discord "Voice build"')
    store.reserve_run("k2", "call_b", "d2", 1)  # a later call, not placed anywhere
    assert store.recent_placements() == [
        {"session_id": "s_build", "request": "fix the thread routing", "at": store.recent_placements()[0]["at"]}]


CHATS = [router.Chat("c1", 'Discord "Halloween costume ideas"', ("halloween costume research please",), "", 30),
         router.Chat("c2", 'Discord "Gateway tests exit code 1"', ("make routing use what was said",),
                     "fix the thread routing", 500)]


def test_the_model_sees_what_was_said_and_picks_by_content():
    seen = []

    def model(messages):
        seen.append(messages[1]["content"])
        return '{"follow_up_task_id": null, "conversation": "c2", "parts": ["x"], "channel": null}'

    d = router.decide("keep going on the routing stuff", [], None, [], model, chats=CHATS)
    assert d.source == "model" and d.conversation == "c2" and d.parts == [router.Part(router.NEW,
                                                                                   "keep going on the routing stuff")]
    prompt = seen[0]
    assert 'user said: "make routing use what was said"' in prompt
    assert 'you last sent work there by voice: "fix the thread routing"' in prompt
    assert "last active 8 min ago" in prompt


def test_the_model_can_decline_and_bad_refs_fall_back():
    none = router.decide("what's 2 plus 2", [], None, [],
                         lambda m: '{"follow_up_task_id": null, "conversation": null, "parts": ["x"], "channel": null}',
                         chats=CHATS)
    assert none.source == "model" and none.conversation is None
    ghost = router.decide("keep going", [], None, [], lambda m: '{"conversation": "c9", "parts": ["x"]}', chats=CHATS)
    assert ghost.source == "fallback" and ghost.conversation is None


def test_the_model_is_asked_whenever_there_are_conversations():
    asked = []
    router.decide("What time is it in Nairobi?", [], None, [], lambda m: asked.append(1) or None, chats=CHATS)
    assert asked == [1]
    router.decide("What time is it in Nairobi?", [], None, [], lambda m: asked.append(2) or None, chats=[])
    assert asked == [1]  # nothing to decide: stays instant


def test_pick_conversation_maps_the_ref_and_trusts_a_model_no(tmp_path):
    db = workspace(tmp_path)
    cands = continuity.conversations_with_context(db, "continue the thread routing work on speakeasy")
    chats = [router.Chat(f"c{i + 1}", c.conv.where) for i, c in enumerate(cands)]
    ref = next(ch.ref for ch, c in zip(chats, cands) if c.conv.session_id == "s_trip")
    picked = CallWorker.pick_conversation("x", router.Decision([], conversation=ref, source="model"), cands, chats)
    assert picked.session_id == "s_trip"
    # the model said no conversation: a keyword match must not override it
    assert CallWorker.pick_conversation("continue the thread routing work on speakeasy",
                                        router.Decision([], source="model"), cands, chats) is None
    # no model (timed out / unavailable): never guess a thread from shared words; start new work
    assert CallWorker.pick_conversation("continue the thread routing work on speakeasy",
                                        router.Decision([], source="fallback"), cands, chats) is None


def test_the_model_gets_the_call_so_far_to_resolve_references():
    seen = []
    router.decide("make the palma one cheaper", [], None, [], lambda m: seen.append(m[1]["content"]) or None,
                  chats=CHATS, call_so_far="User: find me a phone-sized e-reader with an eSIM\nAssistant: The Boox Palma...")
    assert "The call so far" in seen[0] and "phone-sized e-reader" in seen[0]
    seen.clear()
    router.decide("x and y", [], None, [], lambda m: seen.append(m[1]["content"]) or None, call_so_far="User: hi")
    assert "The call so far" not in seen[0]  # only when there are conversations or open tasks to tell apart
    seen.clear()
    task = router.OpenTask("t1", "organize my dock", "completed", "Done: dock matches.")
    router.decide("no, the other apps too", [task], None, [], lambda m: seen.append(m[1]["content"]) or None,
                  call_so_far="Assistant: Your dock matches the laptop.")
    assert "The call so far" in seen[0]  # pushback on an answer needs the answer


def test_a_routing_timeout_never_lands_work_in_an_existing_thread(tmp_path):
    """Live bug: the routing model timed out and the keyword fallback sent an hourly-reminder request
    into an unrelated thread that happened to share words with it. Without the model's pick it is
    new work, even when the request says "continue" and one chat clearly shares its words."""
    db = workspace(tmp_path)
    asked = "continue the thread routing work on speakeasy"
    cands = continuity.conversations_with_context(db, asked)
    chats = [router.Chat(f"c{i + 1}", c.conv.where) for i, c in enumerate(cands)]
    def timed_out(messages):
        raise TimeoutError
    slow = router.decide(asked, [], None, [], timed_out, chats=chats)
    assert slow.source == "fallback" and slow.conversation is None
    assert CallWorker.pick_conversation(asked, slow, cands, chats) is None


def test_the_content_search_is_bounded_so_routing_is_not_kept_waiting(tmp_path, monkeypatch):
    """The word search runs before the routing model on every request; on a big history it took
    7-20 s. It stops after a budget, keeping the rarest words it already searched."""
    db = workspace(tmp_path)
    real = continuity._term_sessions
    seen = []

    def slow(*a, **k):
        seen.append(a[1])
        time.sleep(0.2)
        return real(*a, **k)
    monkeypatch.setattr(continuity, "_term_sessions", slow)
    monkeypatch.setattr(continuity, "SEARCH_BUDGET_S", 0.3)
    started = time.monotonic()
    continuity.conversations_with_context(db, "keep going on the thread routing fix for speakeasy tomorrow please")
    assert time.monotonic() - started < 1.0 and 1 <= len(seen) <= 3
    assert seen[0] == max(seen, key=len)  # longest (rarest) words first


def test_the_model_call_gets_the_whole_routing_budget(monkeypatch):
    """The default model call must not give up before routing does (it used to stop at 3 s while
    routing waited 5 s whenever conversations were in play)."""
    seen = []
    monkeypatch.setattr(router, "aux_call", lambda messages, timeout=router.ROUTE_TIMEOUT_S: seen.append(timeout) or None)
    router.decide("keep going on that", [], None, [], None, chats=CHATS)
    assert seen == [router.CHAT_ROUTE_TIMEOUT_S]
