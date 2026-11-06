"""Which model ran a task and how full its context window got: recorded live, as Hermes works.

Hermes never saves the context figure (the "model · 22%" footer chat platforms can show is built
from the running agent and thrown away). It does call ``post_api_request`` after every model call,
in the gateway process the plugin lives in, with the session, the model that answered, the tokens
sent (``usage.prompt_tokens``: everything the model read, cache included) and the model's context
length. ``after_api_request`` keeps the newest figure per Hermes session (plus its peak) in memory
and, once the service is running, in the plugin's own store so it survives a restart.

``describe`` phrases one record for a task card or session page ("Claude Opus 5.5 · about 22% of
context used · 3 min"). The share is what the provider reported for the last call over the context
length Hermes uses for the model: always labelled an estimate.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_SESSIONS = 2000   # sessions kept in memory (the store keeps the rest)
NOTE = "An estimate from the token counts the provider reported for the latest model call."

_LOCK = threading.Lock()
_MEM: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_SINK: dict[str, Any] = {"record": None, "lookup": None}


def attach(record: Callable[[str, dict[str, Any]], None] | None,
           lookup: Callable[[str], dict[str, Any] | None] | None) -> None:
    """The service's store: where records are kept across restarts (None detaches)."""
    _SINK["record"], _SINK["lookup"] = record, lookup


def detach(record: Callable[..., Any] | None = None) -> None:
    if record is None or _SINK["record"] == record:
        _SINK["record"] = _SINK["lookup"] = None


def _int(value: Any) -> int:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def after_api_request(**kwargs: Any) -> None:
    """``post_api_request`` hook. Never raises: a failed record must not touch the user's turn."""
    try:
        sid = kwargs.get("session_id")
        raw_usage = kwargs.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        tokens = _int(usage.get("prompt_tokens")) or (
            _int(usage.get("input_tokens")) + _int(usage.get("cache_read_tokens")) + _int(usage.get("cache_write_tokens")))
        if not isinstance(sid, str) or not sid or not tokens:
            return
        model = kwargs.get("response_model") or kwargs.get("model") or ""
        asked = kwargs.get("model") or ""
        rec = {"model": str(model)[:200], "asked": str(asked)[:200], "provider": str(kwargs.get("provider") or "")[:80],
               "tokens": tokens, "window": _int(kwargs.get("context_length")) or None, "at": time.time()}
        with _LOCK:
            prev = _MEM.pop(sid, None) or {}
            rec["peak"] = max(tokens, _int(prev.get("peak")))
            rec["calls"] = _int(prev.get("calls")) + 1
            _MEM[sid] = rec
            while len(_MEM) > MAX_SESSIONS:
                _MEM.popitem(last=False)
        sink = _SINK["record"]
        if sink is not None:
            sink(sid, dict(rec))
    except Exception:
        logger.debug("speakeasy: context use not recorded", exc_info=True)


def latest(session_id: str | None) -> dict[str, Any] | None:
    """The newest record for one Hermes session (memory first, then the store), or None."""
    if not session_id:
        return None
    with _LOCK:
        rec = _MEM.get(session_id)
    if rec is not None:
        return dict(rec)
    lookup = _SINK["lookup"]
    if lookup is None:
        return None
    try:
        got = lookup(session_id)
    except Exception:
        return None
    return dict(got) if isinstance(got, dict) else None


def stamp(session_ids: list[str]) -> float:
    """Changes when one of these sessions makes a model call (a cached page's freshness key)."""
    with _LOCK:
        return max((_MEM[s].get("at") or 0 for s in session_ids if s in _MEM), default=0.0)


def newest(session_ids: list[str]) -> dict[str, Any] | None:
    """The most recent record among several sessions (a chat's compression segments, linked tasks)."""
    found = [r for r in (latest(s) for s in dict.fromkeys(i for i in session_ids if i)) if r]
    return max(found, key=lambda r: r.get("at") or 0) if found else None


def _tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1000:
        return f"{round(n / 1000)}K"
    return f"{n:,}"


def _percent(tokens: int, window: int | None) -> int | None:
    if not window:
        return None
    return max(0, min(100, round(tokens * 100 / window)))


def duration_text(seconds: float | None) -> str | None:
    if seconds is None or seconds < 1:
        return None
    if seconds < 60:
        return f"{round(seconds)} s"
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    hours, rest = divmod(round(seconds / 60), 60)
    return f"{hours} h {rest} min" if rest else f"{hours} h"


def describe(rec: dict[str, Any] | None, model_name: str | None = None, seconds: float | None = None,
             name_for: Callable[[str], str] | None = None) -> dict[str, Any] | None:
    """``context`` for a task card / session page. ``model_name``: the plugin's own name for the model
    (models.describe), else the answering model's id run through ``name_for``. ``seconds``: how long a
    finished task took, added to the line like the chat-platform footer."""
    if not rec or not _int(rec.get("tokens")):
        return None
    tokens, window = _int(rec["tokens"]), _int(rec.get("window")) or None
    peak = max(tokens, _int(rec.get("peak")))
    raw = rec.get("model") or ""
    name = model_name or (name_for(raw) if name_for and raw else raw) or None
    pct, peak_pct = _percent(tokens, window), _percent(peak, window)
    use = f"about {pct}% of context used" if pct is not None else f"about {_tokens(tokens)} tokens of context"
    took = duration_text(seconds)
    line = " · ".join(p for p in (name, use, took) if p)
    if window:
        detail = f"About {_tokens(tokens)} of {_tokens(window)} tokens in the latest model call"
        if peak_pct is not None and peak_pct > (pct or 0):
            detail += f" (peak about {peak_pct}%)"
    else:
        detail = f"About {_tokens(tokens)} tokens in the latest model call; the model's context size isn't known"
    out: dict[str, Any] = {"estimate": True, "line": line, "use": use, "detail": detail + ". " + NOTE,
                           "tokens": tokens, "window": window, "percent": pct, "peak_tokens": peak,
                           "peak_percent": peak_pct, "model": name, "model_calls": _int(rec.get("calls")) or None,
                           "at": rec.get("at")}
    if took:
        out["seconds"] = round(seconds or 0)
        out["took"] = took
    return out
