"""Home as one stream (SPK-208): the same cards ``now.py`` already chose, laid out as a single
thread in time order. Your ask comes first, then its card; a card keeps one id, so it updates in
place when its state changes. A question, confirm or checkout is a widget in the thread (never a
pinned panel); older finished stretches fold into one row ("This morning · 6 done"). Additive: an
app that does not know ``stream`` keeps drawing ``items``.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

COLLAPSE_AFTER_S = 2 * 3600     # a finished, seen answer this old may fold into its stretch of the day
COLLAPSE_MIN = 3                # a stretch of fewer seen answers than this stays as cards
ASK_CHARS = 280
SEEN = "seen_answer"
STRETCHES = ((5, "Overnight", "night"), (12, "This morning", "morning"), (17, "This afternoon", "afternoon"),
             (21, "This evening", "evening"), (24, "Tonight", "late"))


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _at(item: dict[str, Any]) -> float:
    return float(item.get("finished_at") or item.get("updated_at") or item.get("started_at") or 0)


def stretch(at: float, now: float, tz: dt.tzinfo) -> tuple[str, str]:
    """(label, key) for the part of the day *at* falls in, by the user's calendar: today's parts
    read "This morning", an earlier day reads "Yesterday" or the weekday."""
    when, today = dt.datetime.fromtimestamp(at, tz), dt.datetime.fromtimestamp(now, tz)
    days = (today.date() - when.date()).days
    part = next((lab, key) for hour, lab, key in STRETCHES if when.hour < hour)
    if days <= 0:
        return part
    day = "Yesterday" if days == 1 else when.strftime("%A")
    return day, when.date().isoformat()


def thread_key(item: dict[str, Any]) -> str:
    """One stable key per job: Home's own item id changes with the card's kind (``q:`` while it asks,
    ``s:`` after), the run does not, so the thread entry keeps one id from question to answer."""
    return str(item.get("run_id") or item["id"])


def widget_state(item: dict[str, Any], answered: bool) -> str | None:
    """open (waiting on you) -> confirmed (you answered, the work goes on) -> done (it finished)."""
    kind = item.get("kind")
    if kind == "question":
        return "open"
    if not answered:
        return None
    return "done" if kind in {"new_answer", "seen_answer"} else "confirmed" if kind == "working" else None


def build_stream(items: list[dict[str, Any]], asks: dict[str, str], answered: set[str], now: float,
                 tz: dt.tzinfo) -> dict[str, Any]:
    """*items* are Home's final cards, *asks* maps item id -> what the user asked, *answered* the item
    ids whose question the user already answered."""
    folds: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    for item in items:
        if item.get("kind") == SEEN and not item.get("call_id") and now - _at(item) >= COLLAPSE_AFTER_S:
            label, key = stretch(_at(item), now, tz)
            folds.setdefault(key, []).append(item)
            labels[key] = label
    folded: set[str] = set()
    entries: list[dict[str, Any]] = []
    for key, group in folds.items():
        if len(group) < COLLAPSE_MIN:
            continue
        folded |= {i["id"] for i in group}
        entries.append({"id": f"g:{key}", "kind": "group", "at": max(_at(i) for i in group),
                        "label": labels[key], "done": len(group),
                        "line": f"{labels[key]} \u00b7 {len(group)} done",
                        "item_ids": [i["id"] for i in group]})
    for item in items:
        if item["id"] in folded:
            continue
        started = float(item.get("started_at") or _at(item))
        ask = asks.get(item["id"])
        if ask:
            entries.append({"id": f"a:{thread_key(item)}", "kind": "ask", "at": started, "text": _clip(ask, ASK_CHARS),
                            "item_id": item["id"]})
        # The card belongs to the ask, not to the latest progress tick. Otherwise each refresh moves
        # old work past newer asks, and answering a question jumps it to the bottom of the thread.
        entry: dict[str, Any] = {"id": f"c:{thread_key(item)}", "kind": "card", "at": started,
                                "item_id": item["id"], "state": item.get("kind")}
        widget = widget_state(item, item.get("_run_key") in answered or item["id"] in answered)
        if widget:
            entry["widget"] = widget
        entries.append(entry)
    # An ask sorts just before its card even when they share a second.
    entries.sort(key=lambda e: (e["at"], 0 if e["kind"] == "ask" else 1, e["id"]))
    return {"entries": entries,
            "needs_you": [i["id"] for i in items if i.get("kind") == "question"],
            "folded": sorted(folded)}
