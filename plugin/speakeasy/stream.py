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
    """open (waiting on you) -> confirmed (you answered, the work goes on) -> done (it finished);
    replaced: nobody answered it and a newer question took over (it stays, closed, saying when)."""
    if item.get("replaced"):
        return "replaced"
    kind = item.get("kind")
    if kind == "question":
        return "open"
    if not answered:
        return None
    return "done" if kind in {"new_answer", "seen_answer"} else "confirmed" if kind == "working" else None


def _elsewhere(item: dict[str, Any]) -> bool:
    """A Hermes conversation Speakeasy didn't start: no task of ours behind it."""
    return str(item.get("id") or "").startswith("s:") and not item.get("run_id")


ANSWERED = {"new_answer", "seen_answer", "stopped"}


def _answer_lines(items: list[dict[str, Any]], folded: set[str],
                  messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """SPK-314: a card stays where it was asked, so a job that finishes after the user sent something
    newer would fill in a card above that message and nothing would arrive at the bottom (it reads as
    out of order, or missing). Such a job also says its answer at the bottom, at the time it finished:
    a reply entry tied to its card (``item_id``) so apps draw it as a line that opens the card. Older
    apps draw it as a plain reply with the answer's words. Only while the card itself is on Home."""
    sent = [float(m["at"]) for m in messages if m.get("kind") == "message" and m.get("role") == "user"]
    out: list[dict[str, Any]] = []
    if not sent:
        return out
    for item in items:
        run = item.get("run_id")
        finished = item.get("finished_at")
        if (not run or item["id"] in folded or item.get("kind") not in ANSWERED or item.get("canceled")
                or item.get("call_id") or finished is None):
            continue
        started = float(item.get("started_at") or finished)
        if not any(started < at < float(finished) for at in sent):
            continue   # nothing of yours came after it started: the card is already the latest thing
        line = " ".join(str(item.get("line") or "").split())
        title = " ".join(str(item.get("title") or "").split())
        if not line or line == "Done.":
            line = f"{title} is done." if title else "Done."
        out.append({"id": f"d:{run}", "kind": "message", "role": "assistant", "text": _clip(line, ASK_CHARS),
                    "at": float(finished), "run_id": run, "item_id": item["id"], "answer": True})
    return out


def build_stream(items: list[dict[str, Any]], asks: dict[str, str], answered: set[str], now: float,
                 tz: dt.tzinfo, messages: list[dict[str, Any]] | None = None,
                 from_chat: set[str] | None = None,
                 results: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """*items* are Home's final cards, *asks* maps item id -> what the user asked, *answered* the item
    ids whose question the user already answered. *messages*: Home's chat (home_chat.py), each
    already a ``message`` entry; *from_chat*: run ids a chat message started or followed up, whose
    card needs no "you asked" bubble (the message itself shows). *results*: run id -> ``{id, at}``
    for jobs whose result (an email draft) was posted as its own message: it lands as a ``result``
    entry where it was posted (the bottom of the thread when the job finished), and the job's card,
    which stays where it was asked, carries ``result_id`` so a tap scrolls down to it."""
    from_chat = from_chat or set()
    results = results or {}
    # Conversations Speakeasy didn't start (a Discord chat, a CLI session) stay in the Work strip and
    # Recent: Home's thread is the user's own conversation, and other chats enter it only when they
    # need the user. ``items`` keeps them for the work counts and older apps.
    items = [i for i in items if not _elsewhere(i) or i.get("kind") == "question"]
    folds: dict[str, list[dict[str, Any]]] = {}
    labels: dict[str, str] = {}
    for item in items:
        if (item.get("kind") == SEEN and not item.get("call_id") and now - _at(item) >= COLLAPSE_AFTER_S
                and str(item.get("run_id") or "") not in from_chat   # a chat's job stays beside its messages
                and str(item.get("run_id") or "") not in results):   # its posted result links back to it
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
        if ask and str(item.get("run_id") or "") not in from_chat:
            entries.append({"id": f"a:{thread_key(item)}", "kind": "ask", "at": started, "text": _clip(ask, ASK_CHARS),
                            "item_id": item["id"]})
        # The card belongs to the ask, not to the latest progress tick. Otherwise each refresh moves
        # old work past newer asks, and answering a question jumps it to the bottom of the thread.
        entry: dict[str, Any] = {"id": f"c:{thread_key(item)}", "kind": "card", "at": started,
                                "item_id": item["id"], "state": item.get("kind")}
        widget = widget_state(item, item.get("_run_key") in answered or item["id"] in answered)
        if widget:
            entry["widget"] = widget
            if widget == "replaced":
                entry["replaced_at"] = item["replaced"].get("at")
                entry["replaced_by"] = item["replaced"].get("by")
        posted = results.get(str(item.get("run_id") or ""))
        if posted:
            entry["result_id"] = posted["id"]
            entries.append({"id": posted["id"], "kind": "result", "at": posted["at"], "item_id": item["id"],
                            "run_id": item["run_id"], "card_id": entry["id"]})
        entries.append(entry)
    # SPK-407: a job Home's chat started posts its own finish line (a stored message with ``answer``).
    # It opens its card like SPK-314's line (``item_id``, while the card is on Home), and it is that
    # job's one line at the bottom: no made-up ``d:<run>`` line beside it.
    card_of = {str(i["run_id"]): i["id"] for i in items if i.get("run_id")}   # folded ones open too
    said: set[str] = set()
    # A job whose email draft was posted as its result (even after its line went up) keeps that one.
    messages = [m for m in messages or [] if not (m.get("answer") and str(m.get("run_id") or "") in results)]
    for m in messages:
        if m.get("answer") and m.get("run_id"):
            said.add(str(m["run_id"]))
            if str(m["run_id"]) in card_of:
                m["item_id"] = card_of[str(m["run_id"])]
    entries.extend(messages or [])
    # A job whose result was already posted at the bottom (an email draft) needs no answer line too:
    # the result entry is what arrives at the bottom for it (b73 integration of email-card73 + SPK-314).
    entries.extend(a for a in _answer_lines(items, folded, messages or [])
                   if str(a.get("run_id") or "") not in results and str(a.get("run_id") or "") not in said)
    # An ask (or the chat message and reply that started a job) sorts just before its card even
    # when they share a second.
    entries.sort(key=lambda e: (e["at"], 0 if e["kind"] in {"ask", "message"} else 1))
    return {"entries": entries,
            "needs_you": [i["id"] for i in items if i.get("kind") == "question"],
            "folded": sorted(folded)}
