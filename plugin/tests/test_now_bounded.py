"""SPK-465: Home (``GET /voice/now`` and ``/voice/home/stack``) always answers, quickly, on a store with
thousands of rows. A paired app that never gets Home looks like it can't connect. Invented data only."""
from __future__ import annotations

import logging
import random
import re
import sqlite3
import threading
import time

import pytest

from fakes import http
from speakeasy import history as H
from speakeasy import sqlbudget as B
from speakeasy import store as S
from test_now import task

RUNAWAY = ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
           "SELECT count(*) FROM c WHERE x != 'a private phrase'")


def fill(service, runs=650, steps=3300, seed=11):
    """A store the size of a well-used install, in the shapes that cost the most to build Home from:
    most tasks typed on Home (one shared interaction), follow-ups asked as "(following up: ...)",
    parents and job links, questions, and Home messages that started jobs."""
    store, rng, now = service.store, random.Random(seed), time.time()
    keys: list[str] = []
    for i in range(runs):
        key = f"t{i:04d}"
        status = rng.choice(["completed"] * 6 + ["failed", "cancelled", "running"])
        request = f"Compare three standing desks, round {i}"
        if keys and rng.random() < 0.4:
            request += f" (following up: Compare three standing desks, round {i - 1})"
        result = None
        if status == "completed":
            result = {"spoken": f"The oak desk wins round {i}.", "full": f"The oak desk wins round {i}."}
            if i % 9 == 0:
                result["question"] = {"question": "Which one should I order?",
                                      "options": [{"text": "Oak"}, {"text": "Birch"}]}
        task(store, key, status, session=f"sess_{i % 120}", result=result,
             settled=now - rng.random() * 2 * 86400, created=now - rng.random() * 2 * 86400,
             interaction="vi_typed" if i % 3 else f"vi_call{i // 6}", request=request)
        if keys and rng.random() < 0.35:
            parent = rng.choice(keys)
            store.set_parent(key, parent)
            store.link_job(key, parent)
        keys.append(key)
    for n in range(max(0, steps - runs)):
        store.progress(rng.choice(keys), rng.choice(["tool", "milestone"]), f"You added: note {n}")
    chat = service.home_chat
    for i in range(0, runs, 3):
        mid = chat._add("user", f"desk question {i}", now - 600 + i)
        chat._link(mid, "run_" + keys[i], "job")
    return keys


def _home(service):
    started = time.monotonic()
    body = service.now({"tz": "America/New_York"})
    return body, time.monotonic() - started


def test_home_builds_well_under_a_second_on_a_store_with_thousands_of_rows(service):
    fill(service)
    with service.store._lock:
        counts = {t: service.store._db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("runs", "work_events", "home_messages")}
    assert counts["runs"] >= 650 and counts["work_events"] >= 3000, counts
    _home(service)   # first build records Home's baseline
    body, took = _home(service)
    assert body["items"] and "stack" in body
    assert took < 1.0, f"Home took {took:.2f} s to build"
    started = time.monotonic()
    service.home_stack({"tz": "America/New_York"})
    assert time.monotonic() - started < 1.0


def test_no_query_home_repeats_reads_the_whole_task_or_step_table(service):
    """A query Home runs once per card must be an index lookup: a full read of ``runs`` or
    ``work_events`` per card is what turns a few thousand rows into a build that never ends."""
    fill(service)
    seen: list[str] = []
    store = service.store
    with store.statement_budget("test", log=seen):
        service.now({})
    counts: dict[str, int] = {}
    first: dict[str, str] = {}

    def shape(sql: str) -> str:
        return re.sub(r"'(?:[^']|'')*'", "?", re.sub(r"\b\d+(?:\.\d+)?\b", "N", sql))

    for sql in seen:
        if sql.lstrip().upper().startswith(("SELECT", "WITH")):
            counts[shape(sql)] = counts.get(shape(sql), 0) + 1
            first.setdefault(shape(sql), sql)
    repeated = [k for k, n in counts.items() if n > 3]   # per card, not a once-per-build read
    assert repeated, "the trace saw Home's per-card queries"
    scans = []
    with store._lock:
        for k in repeated:
            plan = " | ".join(r[3] for r in store._db.execute("EXPLAIN QUERY PLAN " + first[k]).fetchall())
            if re.search(r"\bSCAN (runs|work_events|r|e)\b", plan):
                scans.append(f"{counts[k]}x {k[:160]} -> {plan}")
    assert not scans, "\n".join(scans)


def test_a_runaway_query_is_stopped_and_named_in_the_log(service, caplog):
    store = service.store
    caplog.set_level(logging.WARNING, logger="speakeasy.store")
    started = time.monotonic()
    with pytest.raises(S.SlowStatement) as stopped:
        with store.statement_budget("Home's build", per_statement=0.3):
            with store._lock:
                store._db.execute(RUNAWAY).fetchall()
    assert time.monotonic() - started < 3
    assert "WITH RECURSIVE" in stopped.value.sql
    assert "a private phrase" not in stopped.value.sql   # values masked
    logged = [r.getMessage() for r in caplog.records if "ran too long" in r.getMessage()]
    assert logged and "WITH RECURSIVE" in logged[0] and "a private phrase" not in logged[0]
    # The store is fine afterwards, and statements outside a budget run unbounded as before.
    with store._lock:
        assert store._db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    assert B.active() is None


def test_a_budget_on_one_thread_never_stops_another_threads_query(service):
    store = service.store
    other: dict[str, object] = {}
    slow = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 3000000) SELECT count(*) FROM c"

    def run():
        try:
            other["n"] = store._db.execute(slow).fetchone()[0]   # its own statement, no budget
        except sqlite3.Error as exc:   # pragma: no cover - the failure being guarded against
            other["error"] = exc

    with store.statement_budget("Home's build", per_statement=0.01, total=0.01):
        t = threading.Thread(target=run)
        t.start()
        t.join(60)
    assert other == {"n": 3000000}


def test_home_answers_503_instead_of_hanging_when_its_build_runs_away(service, server, monkeypatch):
    monkeypatch.setattr(B, "SLOW_STATEMENT_S", 0.3)

    def runaway(tz=None, **_):
        with service.store._lock:
            service.store._db.execute(RUNAWAY).fetchall()
        raise AssertionError("not reached")

    monkeypatch.setattr(service.now_model, "build", runaway)
    for path in ("/voice/now", "/voice/home/stack"):
        started = time.monotonic()
        status, body = http(server.base_url, "GET", path, token=server.token)
        assert status == 503, (path, status, body)
        assert time.monotonic() - started < 5


def test_a_runaway_query_on_the_hermes_state_db_is_stopped_too(tmp_path):
    """Home also reads Hermes' own conversations (history.py opens its own connection)."""
    path = tmp_path / "state.db"
    sqlite3.connect(path).execute("CREATE TABLE t (x)").connection.commit()
    db = H._open(path)
    started = time.monotonic()
    with pytest.raises(B.SlowStatement):
        with B.budget("Home's build", per_statement=0.3):
            db.execute(RUNAWAY).fetchall()
    assert time.monotonic() - started < 3
    assert db.execute("SELECT count(*) FROM t").fetchone()[0] == 0   # outside a budget: as before
    db.close()
