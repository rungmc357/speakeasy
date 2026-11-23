"""SPK-442: the first hand-off of a call waited 4.4-7.2 s on the chat lookup, later ones ~2 s.

Two causes, two checks: the word search's budget was only looked at between words, so one word on
cold pages ran for seconds by itself; and the request-independent part of the lookup (which chats
are live, where the window starts, their latest lines) was read on the first hand-off instead of
when the call started. Invented chats only."""
from __future__ import annotations

import sqlite3
import time

from fakes import SDP, wait_for
from speakeasy import continuity

from test_thread_routing import workspace


def _slow_rows(monkeypatch, per_row_s: float) -> None:
    """Every message row the word search looks at costs *per_row_s*: a history on cold disk pages."""
    real_open = continuity._open

    def slow_open(path):
        db = real_open(path)
        if db is not None:
            db.create_function("cold_page", 1, lambda _: time.sleep(per_row_s) or 1)
        return db
    monkeypatch.setattr(continuity, "_open", slow_open)
    monkeypatch.setattr(continuity, "_HUMAN", continuity._HUMAN + " AND cold_page(m.id) = 1")


def test_one_slow_word_cannot_overrun_the_search_budget(tmp_path, monkeypatch):
    """A single word's query is stopped mid-query at the budget; the request still gets its chats."""
    db = workspace(tmp_path)
    con = sqlite3.connect(db)  # a big recent history behind one word
    con.executemany("INSERT INTO messages (session_id, role, content, tool_calls, timestamp) VALUES (?,?,?,?,?)",
                    [("s_trip", "user", f"[sam] lisbonish errand number {i}", None, time.time() - 100 - i)
                     for i in range(400)])
    con.execute("INSERT INTO messages_fts(messages_fts) VALUES ('rebuild')")
    con.commit()
    con.close()
    _slow_rows(monkeypatch, 0.01)          # 400 rows -> ~4 s for the one word
    monkeypatch.setattr(continuity, "SEARCH_BUDGET_S", 0.3)
    started = time.monotonic()
    found = continuity.conversations_with_context(db, "lisbonish")
    took = time.monotonic() - started
    assert took < 1.0, f"the search ran {took:.2f}s past a 0.3 s budget"
    assert found and found[0].conv.session_id  # still the recent chats, just without word scores


def test_the_call_start_read_is_reused_by_the_first_request(tmp_path, monkeypatch):
    """prepare() at call start does the request-independent reads; the request then only searches
    its words: no second chat-list read, no second window bisect, no snippet reads for the recent chats."""
    db = workspace(tmp_path)
    base = continuity.prepare(db)
    assert base is not None and base.convs and base.snippets
    counts = {"recent": 0, "floor": 0, "snippets": 0}
    for name, key in (("recent_conversations", "recent"), ("_recent_floor", "floor"), ("_snippets", "snippets")):
        real = getattr(continuity, name)

        def counted(*a, _real=real, _key=key, **k):
            counts[_key] += 1
            return _real(*a, **k)
        monkeypatch.setattr(continuity, name, counted)
    warm = continuity.conversations_with_context(db, "keep going on the thread routing fix", base=base)
    assert counts == {"recent": 0, "floor": 0, "snippets": 0}
    cold = continuity.conversations_with_context(db, "keep going on the thread routing fix")
    assert [(c.conv.session_id, c.snippets, c.hits) for c in warm] == \
        [(c.conv.session_id, c.snippets, c.hits) for c in cold]  # same answer either way
    ranked = continuity.with_placements(db, warm, [{"session_id": "s_trip", "request": "hotel", "at": time.time()}],
                                        recent=base.convs)
    assert counts["recent"] == 1  # only the cold run above: placements reuse the call's chat list
    assert ranked[0].conv.session_id == "s_trip"


def test_a_new_call_reads_the_chat_list_before_anything_is_asked(service, monkeypatch):
    """create_session starts the read; the first hand-off then finds it ready (or waits on that same
    read) instead of starting its own."""
    reads = []
    real = continuity.prepare
    monkeypatch.setattr(continuity, "prepare", lambda *a, **k: reads.append(time.monotonic()) or real(*a, **k))
    created = service.create_session({"sdp": SDP}, "req_warm_1")
    worker = service.workers[-1]
    wait_for(lambda: reads, timeout=5)
    wait_for(lambda: worker.__dict__.get("cand_base") is not None, timeout=5)
    import asyncio
    asyncio.run_coroutine_threadsafe(worker.conversation_candidates("book the usual table for friday"),
                                     worker.loop).result(10)
    assert len(reads) == 1, "the first request read the chat list again"
    assert created["interaction_id"]
