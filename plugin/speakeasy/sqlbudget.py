"""Bounded queries for a request that must answer (SPK-465).

Home's build (``GET /voice/now``, ``/voice/home/stack``) once sat inside a single query for minutes,
with no error and no log line, so a paired app never got a Home and looked like it couldn't connect.
Inside :func:`budget`, every query this thread runs on a guarded connection (the plugin store, and
the Hermes state DB connections ``history`` opens) is watched by SQLite's progress handler: one that
runs longer than ``per_statement`` seconds, or any query once the block has run ``total`` seconds,
is stopped and raises :class:`SlowStatement`, and the stopped query is named in the log with its
string values masked. Queries on other threads, and queries outside a budget, are never touched.
"""
from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Iterator

logger = logging.getLogger("speakeasy.store")

# A healthy Home build of a store with thousands of tasks takes well under a second.
SLOW_STATEMENT_S = 5.0
SLOW_BUILD_S = 20.0
CHECK_OPS = 20_000   # SQLite VM steps between checks (each check is a thread-local read)
_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_local = threading.local()


def masked(sql: str) -> str:
    """A query's text for the log: string values masked (they may carry what the user said)."""
    return " ".join(_STRING_RE.sub("'…'", str(sql or "")).split())[:400]


class SlowStatement(Exception):
    """A query inside a budget ran too long and was stopped."""

    def __init__(self, label: str, sql: str, seconds: float):
        super().__init__(f"{label}: a query ran over {seconds:.0f} s and was stopped: {sql}")
        self.label, self.sql, self.seconds = label, sql, seconds


def active() -> dict[str, Any] | None:
    return getattr(_local, "record", None)


def _trace(sql: str) -> None:
    record = active()
    if record is not None:
        record["sql"], record["start"] = sql, time.monotonic()
        if record["log"] is not None:
            record["log"].append(sql)


def _progress() -> int:
    record = active()
    if record is None or record["sql"] is None:
        return 0
    now = time.monotonic()
    if now - record["start"] <= record["per"] and now - record["began"] <= record["total"]:
        return 0
    if record["tripped"] is None:
        record["tripped"] = masked(record["sql"])
    return 1


def guard(db: sqlite3.Connection) -> sqlite3.Connection:
    """Let budgets watch this connection's queries (a no-op for any thread outside a budget)."""
    db.set_trace_callback(_trace)
    db.set_progress_handler(_progress, CHECK_OPS)
    return db


@contextlib.contextmanager
def budget(label: str, per_statement: float | None = None, total: float | None = None,
           log: list[str] | None = None) -> Iterator[dict[str, Any]]:
    """Bound the guarded queries this thread runs inside the block. Yields the block's record:
    ``tripped`` is the stopped query (masked) once one was stopped, even if a caller swallowed the
    error. *log*, when given, collects every statement run (tests)."""
    outer = active()
    if log is None and outer is not None:
        log = outer["log"]   # a nested budget keeps collecting for the outer one
    record: dict[str, Any] = {
        "label": label, "sql": None, "start": 0.0, "began": time.monotonic(), "tripped": None, "log": log,
        "per": float(SLOW_STATEMENT_S if per_statement is None else per_statement),
        "total": float(SLOW_BUILD_S if total is None else total)}
    _local.record = record
    try:
        yield record
    except sqlite3.OperationalError as exc:
        if record["tripped"] and "interrupt" in str(exc).lower():
            raise SlowStatement(label, record["tripped"], record["per"]) from exc
        raise
    finally:
        _local.record = outer
        if record["tripped"]:
            logger.warning("speakeasy: %s stopped a query that ran too long (over %.0f s, or the whole "
                           "build over %.0f s): %s", label, record["per"], record["total"], record["tripped"])
