"""Visual cards ("views") the apps draw next to an answer.

A view is a small typed JSON object: {"kind": "quote", ...}. Quick answers build them from the live data
they already fetched (prices, weather, games, clocks); full Hermes tasks can add them in a fenced
`speakeasy-views` block, which is validated here and stripped from the text. Apps ignore kinds they
don't know, so new kinds never break an older app.

Every field is checked against the catalog below: strings are clipped, URLs must be https, lists are
capped, unknown fields are dropped. Nothing here fetches anything.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_VIEWS = 6
S, N, B, URL, ANY_LIST, IMG = "str", "num", "bool", "url", "list", "img"

# Row shapes reused by several kinds.
EVENT = {"start": S, "end": S, "title": S, "place": S, "all_day": B, "color": S, "url": URL, "join_url": URL}
GAME_SIDE = {"name": S, "abbr": S, "score": S, "logo_url": URL, "winner": B, "home": B, "record": S}
DAY = {"label": S, "high": N, "low": N, "rain": N, "condition": S, "icon": S}
HOUR = {"label": S, "temp": N, "rain": N, "icon": S}
ROW_KV = {"label": S, "value": S}

# kind -> fields. A tuple value is (row shape, max rows) for a list of objects; ("str", n) a list of strings;
# ("num", n) a list of numbers.
CATALOG: dict[str, dict[str, Any]] = {
    # Markets
    "quote": {"asset": S, "symbol": S, "name": S, "price": N, "currency": S, "change": N, "change_pct": N,
              "previous_close": N, "as_of": S, "exchange": S, "logo_url": URL, "points": (N, 120),
              "range_label": S},
    # Weather
    "weather": {"place": S, "temp": N, "feels_like": N, "unit": S, "condition": S, "icon": S, "high": N, "low": N,
                "rain": N, "wind": S, "sunrise": S, "sunset": S, "hours": (HOUR, 12), "days": (DAY, 7)},
    # Sports
    "game": {"league": S, "status": S, "detail": S, "when": S, "venue": S, "broadcast": S,
             "teams": (GAME_SIDE, 2)},
    "games": {"title": S, "league": S, "games": ({"status": S, "when": S, "teams": (GAME_SIDE, 2)}, 16)},
    "standings": {"title": S, "rows": ({"rank": N, "name": S, "logo_url": URL, "record": S, "note": S}, 16)},
    # Time
    "clock": {"zones": ({"place": S, "time": S, "day": S, "offset": S, "zone": S, "here": B}, 6)},
    "timer": {"label": S, "ends_at": S, "seconds": N},
    "countdown": {"label": S, "date": S, "days": N, "detail": S},
    "calendar_day": {"date": S, "title": S, "events": (EVENT, 16), "free": (S, 8)},
    "agenda": {"title": S, "days": ({"date": S, "events": (EVENT, 10)}, 7)},
    "reminder": {"title": S, "due": S, "list": S, "done": B, "notes": S},
    # Places and getting around
    "place": {"name": S, "category": S, "address": S, "rating": N, "reviews": N, "price": S, "open_now": B,
              "hours_today": S, "phone": S, "url": URL, "photo_url": URL, "lat": N, "lon": N, "distance": S},
    "places": {"title": S, "items": ({"name": S, "category": S, "rating": N, "price": S, "open_now": B,
                                      "distance": S, "url": URL, "photo_url": URL, "lat": N, "lon": N}, 8)},
    "route": {"origin": S, "destination": S, "mode": S, "minutes": N, "distance": S, "leave_by": S, "arrive_by": S,
              "traffic": S, "steps": (S, 8), "lat": N, "lon": N, "dest_lat": N, "dest_lon": N},
    "transit": {"station": S, "status": S, "departures": ({"line": S, "color": S, "destination": S, "minutes": N,
                                                         "time": S, "note": S}, 10)},
    "flight": {"number": S, "airline": S, "origin": S, "destination": S, "departs": S, "arrives": S,
               "status": S, "gate": S, "terminal": S, "progress": N, "delay": S},
    "package": {"carrier": S, "item": S, "status": S, "eta": S, "url": URL,
                "steps": ({"when": S, "text": S, "done": B}, 8)},
    # People and messages
    "contact": {"name": S, "subtitle": S, "phone": S, "email": S, "photo_url": URL, "birthday": S, "note": S},
    "message_draft": {"channel": S, "to": S, "subject": S, "body": S, "status": S, "draft_id": S},
    "message": {"from": S, "channel": S, "when": S, "subject": S, "preview": S},
    "inbox": {"title": S, "items": ({"from": S, "subject": S, "preview": S, "when": S, "unread": B}, 8)},
    # Media
    "media": {"kind_label": S, "title": S, "subtitle": S, "year": S, "art_url": URL, "rating": S, "runtime": S,
              "where": (S, 6), "overview": S, "url": URL, "playing": B},
    "now_playing": {"title": S, "artist": S, "album": S, "art_url": URL, "playing": B, "position": N, "duration": N},
    # Knowledge
    "fact": {"title": S, "value": S, "unit": S, "subtitle": S, "image_url": URL, "source": S},
    "entity": {"title": S, "subtitle": S, "image_url": URL, "summary": S, "facts": (ROW_KV, 8), "url": URL},
    "definition": {"word": S, "phonetic": S, "part": S, "meanings": (S, 4), "example": S},
    "conversion": {"from_value": S, "from_unit": S, "to_value": S, "to_unit": S, "note": S},
    "math": {"expression": S, "result": S, "steps": (S, 6)},
    "translation": {"source_lang": S, "target_lang": S, "source": S, "text": S, "phonetic": S},
    "recipe": {"title": S, "image_url": URL, "time": S, "serves": S, "ingredients": (S, 20), "steps": (S, 12),
               "url": URL},
    "nutrition": {"title": S, "serving": S, "calories": N, "rows": (ROW_KV, 10)},
    # Comparing and lists
    "comparison": {"title": S, "columns": (S, 4), "rows": ({"label": S, "values": (S, 4)}, 10), "winner": S},
    "list": {"title": S, "items": ({"text": S, "done": B, "detail": S, "url": URL}, 20)},
    "steps": {"title": S, "items": (S, 12)},
    "stats": {"title": S, "items": ({"label": S, "value": S, "delta": S, "good": B}, 8)},
    # metric: what the numbers are; sort: high_first | low_first | as_listed (order_by names what
    # decided a listed order); source / as_of / delayed only when the data says so (never guessed).
    "chart": {"title": S, "style": S, "unit": S, "labels": (S, 60), "series": ({"name": S, "values": (N, 60)}, 4),
              "metric": S, "sort": S, "order_by": S, "source": S, "as_of": S, "delayed": B},
    "progress": {"label": S, "value": N, "total": N, "unit": S, "detail": S},
    "news": {"title": S, "items": ({"headline": S, "source": S, "when": S, "url": URL, "image_url": URL}, 6)},
    # Home
    "home": {"title": S, "devices": ({"name": S, "kind": S, "on": B, "level": N, "color": S, "state": S,
                                       "entity_id": S}, 16)},
    "thermostat": {"name": S, "current": N, "target": N, "mode": S, "unit": S, "humidity": N},
    "camera": {"name": S, "image_url": URL, "when": S},
    # Explaining a job: what ran when, what changed, where things go, how a fleet is doing.
    "lanes": {"title": S, "start_label": S, "end_label": S, "ticks": (S, 6), "note": S,
              "lanes": ({"label": S, "segments": ({"from": N, "to": N, "tone": S, "label": S}, 8)}, 6)},
    "before_after": {"title": S, "before": {"label": S, "text": S, "image": IMG},
                     "after": {"label": S, "text": S, "image": IMG}},
    "flow": {"title": S, "nodes": ({"label": S, "detail": S, "tone": S}, 5)},
    "devices": {"title": S, "items": ({"name": S, "value": S, "detail": S, "tone": S, "history": (S, 6)}, 8)},
    # A decision the task needs from the user; the app shows numbered answers (tap, 1-4, or say it).
    # Options are strings, or objects (OPTION below) when the model explained them; checked in clean_options.
    "question": {"question": S, "recommended": N, "task_id": S, "images": (N, 4),
                 "context": S, "why": S, "affects": S, "expires_in": N},
    # Artifacts (v1): the agent fills typed fields, never layout; `shape` checks each against the
    # answer and drops a card rather than show a wrong one. Apps that don't know these skip them.
    "explore": {"title": S, "sections": ({"label": S, "entity": S, "items": (None, 10)}, 6)},
    "compare": {"title": S, "options": ({"name": S, "sub": S, "price": S, "why": S, "url": URL, "photo_url": URL,
                                         "facts": (S, 3)}, 6),
                "winner": N, "why": S, "reasons": (S, 3), "caveat": S},
    "status": {"source": S, "scope": S, "items": ({"name": S, "role": S, "state": S, "value": S, "unit": S, "note": S,
                                      "history": (N, 24), "url": URL}, 12)},
    "draft": {"channel": S, "to": S, "subject": S, "body": S, "send_as": S, "thread": S, "revision": N,
              "changes": S},
    # P2: what happens when (a trip by day, a calendar, a release's stages, a package's scans).
    "timeline": {"title": S, "days": ({"label": S, "sleep": S, "items": ({"when": S, "what": S, "where": S, "note": S,
                                                                         "state": S, "url": URL, "address": S,
                                                                         "lat": N, "lon": N}, 10)}, 10)},
    # P4: rows of things to act on (pull requests, inbox triage, to-dos, bills, alerts).
    "rows": {"source": S, "title": S, "entity": S, "items": ({"ref": S, "name": S, "state": S, "tone": S, "note": S, "when": S,
                                                 "amount": S, "url": URL}, 12)},
    # P6: one number that matters, which way it moves, and the series behind it.
    "metric": {"label": S, "value": S, "unit": S, "delta": S, "good": B,
               "points": ({"label": S, "value": N, "mark": S}, 31),
               "threshold": N, "threshold_label": S, "source": S, "as_of": S},
    # P7: one thing to open (an album, an article, a paper, a contract, a file) with a short list.
    "preview": {"entity": S, "title": S, "creator": S, "sub": S, "year": S, "photo_url": URL, "url": URL,
                "media_kind": S, "ids": {"apple_music": S, "spotify": S}, "summary": S, "list_label": S,
                "list": (S, 10), "flags": (S, 4)},
    # Work cards (Home v7). The agent sends facts; the app draws the layout, the server sets buttons.
    # One issue from a tracker, with its mockup or screenshot as its own picture.
    "issue": {"source": S, "ref": S, "title": S, "state": S, "tone": S, "assignee": S, "priority": S,
              "labels": (S, 4), "note": S, "updated": S, "image": IMG, "url": URL},
    # One pull request explained: where it stands and why, in one plain line, with its checks.
    "pr": {"repo": S, "number": N, "title": S, "state": S, "reason": S, "files": N, "checks": (
        {"name": S, "state": S, "note": S}, 40), "when": S, "url": URL, "diff_url": URL},
    # Work that is running: one quiet status line each, what needs the user first.
    "jobs": {"title": S, "items": ({"name": S, "status": S, "elapsed": S, "needs_you": B, "ask": S,
                                    "task_id": S}, 8)},
    # A website or preview deploy: the page's screenshot at its own shape, and one Open.
    "site": {"url": URL, "title": S, "sub": S, "image": IMG},
    # Screens the task designed, to swipe through, and up to two next steps the user can tap.
    "mockups": {"title": S, "note": S, "items": ({"image": IMG, "caption": S}, 8), "next": (S, 2)},
}

# One thing in an explore section (a place, a stay, an album, a pull request…). The app draws the
# layout and builds the deep links from `ids` / `url` / `lat`+`lon` / `address`; the agent never
# names a scheme.
ENTITIES = {"place", "stay", "reservation", "flight", "event", "product", "media", "person", "article", "pr",
            "listing", "generation", "task"}
ENTITY_ITEM = {"name": S, "sub": S, "when": S, "facts": (S, 3), "price": S, "rating": N, "state": S,
               "photo_url": URL, "image": IMG, "url": URL, "address": S, "city": S, "lat": N, "lon": N, "phone": S,
               "creator": S, "media_kind": S, "ids": {"apple_music": S, "spotify": S}, "day": S}
CATALOG["explore"]["sections"][0]["items"] = (ENTITY_ITEM, 10)
ARTIFACTS = {"explore", "compare", "status", "draft", "timeline", "rows", "metric", "preview",
             "issue", "pr", "jobs", "site", "mockups"}
ROW_ENTITIES = {"pr", "issue", "email", "todo", "bill", "package", "alert", "note", "item", "task", "post"}
PREVIEW_ENTITIES = {"media", "article", "paper", "file", "document", "contract", "page", "image", "recipe"}
TIMELINE_STATES = {"done", "now", "next"}
STATUS_STATES = {"ok", "warn", "bad", "off", "on", "needs_you", "info"}
DRAFT_CHANNELS = {"email", "imessage", "sms", "slack", "discord", "telegram", "x", "linkedin", "whatsapp"}
MEDIA_KINDS = {"album", "song", "playlist", "podcast", "artist", "show", "book"}
MAX_ARTIFACTS = 2
ISSUE_SOURCES = {"linear", "github", "jira", "other"}
PR_STATES = {"open", "draft", "conflicts", "failing", "passing", "approved", "merged", "closed"}
CHECK_STATES = {"passed", "failed", "skipped", "running", "pending"}

TONES = {"good", "warn", "bad", "info", "neutral"}
CHART_SORTS = {"high_first", "low_first", "as_listed"}
TAG_TONES = {"good", "warn", "bad", "neutral"}
RISKS = {"low", "medium", "high"}
OPTION = {"text": S, "detail": S, "tags": ({"text": S, "tone": S}, 4), "risk": S, "reversible": B,
          "link": S, "facts": (S, 3)}  # link: the https page of a thing to pick; facts: price, size... (Home)

# A decision is one-tap "Approve" only when nothing in it spends money, speaks for the user,
# publishes, or deletes. Plain words, checked on the question and the recommended option; a false
# alarm only costs the user a look at the options.
APPROVE_GUARD = {
    "money": r"[$€£¥]\s?\d|\b(?:pay|paid|payment|purchase|buy|bought|order|checkout|charge|refund|invoice|"
             r"subscri\w*|price|cost|fee|dollars?|euros?|card|wire|transfer|billing|spend|spent|deposit|"
             r"withdraw|trade|sell|sold|donate|tip)(?:s|es|ed|ing)?\b",
    "speaking as the user": r"\b(?:send|sends|sending|sent)\s+(?:(?:it|this|that|the|an?|your|my|our|them)\s+)?"
                            r"(?:email|e-mail|message|reply|note|invite|text|dm|letter|post|tweet)|"
                            r"\b(?:email|e-mail|reply|replies|message|messages|dm|tweet|rsvp|invite)\b|"
                            r"on your behalf|in your name|as you\b|from your (?:account|address|number)",
    "publishing": r"\b(?:publish|post|share|public|announce|launch|release|deploy|go live|push|merge|ship)"
                  r"(?:s|es|ed|ing|d)?\b",
    "deleting": r"\b(?:delete|remove|erase|wipe|destroy|purge|uninstall|overwrite|discard|cancel|unsubscribe|"
                r"revoke|drop)(?:s|es|ed|ing|d|ped|ping)?\b",
}
APPROVE_GUARD_RE = re.compile("(?i)" + "|".join(f"(?:{p})" for p in APPROVE_GUARD.values()))


def _str(value: Any, limit: int = 240) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:limit] if text else None


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _url(value: Any) -> str | None:
    text = _str(value, 600)
    if not text or not text.lower().startswith("https://") or " " in text:
        return None
    return text


def _image(value: Any) -> Any:
    """A picture on a card: a result image's card number, an https URL, or a file path the task
    made (split_result vets that path like any result image and swaps in its card number)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 1 else None
    text = _str(value, 600)
    if text and (text.startswith("https://") or text.startswith("/") or text.startswith("~/")) and "\n" not in text:
        return text
    return None


def _field(shape: Any, value: Any) -> Any:
    if shape == S:
        return _str(value, 1200)
    if shape == N:
        return _num(value)
    if shape == B:
        return value if isinstance(value, bool) else None
    if shape == URL:
        return _url(value)
    if shape == IMG:
        return _image(value)
    if isinstance(shape, dict):
        return _object(shape, value) or None
    if isinstance(shape, tuple) and isinstance(value, list):
        row, cap = shape
        out = []
        for item in value[:cap]:
            clean = _object(row, item) if isinstance(row, dict) else _field(row, item)
            if clean not in (None, {}):
                out.append(clean)
        return out or None
    return None


def _object(shape: dict[str, Any], raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, kind in shape.items():
        if key in raw:
            value = _field(kind, raw[key])
            if value is not None:
                out[key] = value
    return out


ACTION_STYLES = {"primary", "secondary", "destructive"}
MAX_ACTIONS = 4


def clean_actions(raw: Any) -> list[dict[str, Any]]:
    """A card's buttons (docs/API.md "Card actions"): ``id`` and ``label`` required; the server
    decides what an id does when it is pressed."""
    out: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        action_id, label = item.get("id"), _str(item.get("label"), 24)
        if not isinstance(action_id, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,23}", action_id) or not label:
            continue
        style = str(item.get("style") or "secondary").lower()
        action = {"id": action_id, "label": label, "style": style if style in ACTION_STYLES else "secondary",
                  "confirm": item.get("confirm") is True}
        if item.get("value") == "brightness":
            action["value"] = "brightness"
        out.append(action)
        if len(out) == MAX_ACTIONS:
            break
    return out


def clean(raw: Any, running: bool = False) -> dict[str, Any] | None:
    """One validated view, or None for an unknown kind or an empty view. Views are a task's answer, so
    they belong to a finished task unless ``running`` says otherwise: a progress bar is only kept for
    work still under way; on a finished task it becomes a plain count (``settled``)."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("kind")
    if kind == "confirm":
        from .confirms import clean as clean_confirm
        return clean_confirm(raw)
    shape = CATALOG.get(kind) if isinstance(kind, str) else None
    if shape is None:
        return None
    body = _tidy(kind, _object(shape, raw))
    if body and (actions := clean_actions(raw.get("actions"))):
        body["actions"] = actions
    view = {"kind": kind, **body} if body else None
    if view is None or running:
        return view
    view = settled(view)
    return view if len(view) > 1 else None


def _count(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else f"{n:g}"


def settled(view: dict[str, Any]) -> dict[str, Any]:
    """A finished task's card. A progress bar means "x of y done so far" and only while the work runs;
    a count or breakdown ("5 of 13 crashes had reports") on a done task is a fact, never a bar: the
    label stays the title, the numbers read "5 of 13" with the unit said once, no percent. Other kinds
    pass through; the card count never changes (card buttons are addressed by position)."""
    if view.get("kind") != "progress":
        return view
    value, total = _num(view.get("value")), _num(view.get("total"))
    fact: dict[str, Any] = {"kind": "fact"}
    if _str(view.get("label")):
        fact["title"] = _str(view["label"])
    if value is not None:
        fact["value"] = f"{_count(value)} of {_count(total)}" if total and total > 0 else _count(value)
    if value is not None and _str(view.get("unit")):
        fact["unit"] = _str(view["unit"])
    if _str(view.get("detail")):
        fact["subtitle"] = _str(view["detail"])
    return fact


def settled_all(views: Any) -> Any:
    """``settled`` over a stored result's views (results saved before this kept their progress bars)."""
    if not isinstance(views, list):
        return views
    return [settled(x) if isinstance(x, dict) else x for x in views]


def _tone(row: dict[str, Any], allowed: set[str] = TONES) -> dict[str, Any]:
    """Drop a tone the apps don't draw (they show a missing tone as neutral)."""
    if "tone" in row and str(row["tone"]).lower() not in allowed:
        row.pop("tone")
    elif "tone" in row:
        row["tone"] = str(row["tone"]).lower()
    return row


def _segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lane segments sit on a 0..1 timeline: clamp both ends, drop empty or backwards ones."""
    out = []
    for seg in segments:
        if "from" not in seg or "to" not in seg:
            continue
        start, end = min(max(seg["from"], 0.0), 1.0), min(max(seg["to"], 0.0), 1.0)
        if end <= start:
            continue
        out.append(_tone({**seg, "from": start, "to": end}))
    return out


def _tidy(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """Rules a field shape can't say: allowed tones, and lane segments on a 0..1 timeline."""
    if kind == "lanes":
        lanes = [{**lane, "segments": _segments(lane.get("segments") or [])} for lane in body.get("lanes") or []]
        lanes = [lane if lane["segments"] else {k: v for k, v in lane.items() if k != "segments"} for lane in lanes]
        lanes = [lane for lane in lanes if lane.get("segments") or lane.get("label")]
        body.pop("lanes", None)
        if lanes:
            body["lanes"] = lanes
    for rows in ("nodes", "items"):
        if kind in ("flow", "devices") and body.get(rows):
            body[rows] = [_tone(row) for row in body[rows]]
    if kind == "chart" and "sort" in body:
        word = str(body["sort"]).lower().replace("-", "_")
        if word not in CHART_SORTS:
            body.pop("sort")
        else:
            body["sort"] = word
    if kind == "before_after" and not ("before" in body or "after" in body):
        return {}
    if kind == "stats":
        # "Shared notes crash (FIELDNOTE-IOS-3KD)": the tracker's key is not a heading; and a word
        # value reads as a word ("Fatal", not "fatal"), like the label above it.
        if isinstance(body.get("title"), str):
            body["title"] = _TRACKER_KEY.sub("", body["title"]).strip() or body["title"]
        for item in body.get("items") or []:
            v = item.get("value")
            if isinstance(v, str) and re.fullmatch(r"[a-z][a-z ]*", v):
                item["value"] = _cap_first(v.replace("_", " "))
    if kind == "weather":
        # The model writes the sky in words ("Light drizzle") and rarely the icon: without one every
        # row drew the same cloud, "Clear" included.
        for row in [body, *(body.get("hours") or []), *(body.get("days") or [])]:
            if isinstance(row, dict) and not row.get("icon") and isinstance(row.get("condition"), str):
                if icon := condition_icon(row["condition"]):
                    row["icon"] = icon
        # A row that is only a label ("6am" with no temperature) draws an empty bar: no row
        # beats an empty one, and an hour or day list of nothing but labels is left out.
        for rows in ("hours", "days"):
            if rows in body:
                kept = [row for row in body[rows] if set(row) - {"label", "icon"}]
                if kept:
                    body[rows] = kept
                else:
                    body.pop(rows)
    if kind in ARTIFACTS:
        return _artifact(kind, body)
    return body


def _artifact(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """The artifact kinds' required fields and bounds (catalog.md). A failed item is dropped, and a
    card left without its required parts is dropped whole: no card beats a wrong card."""
    if kind == "explore":
        sections = []
        for sec in body.get("sections") or []:
            if sec.get("entity") not in ENTITIES or not sec.get("label"):
                continue
            items = [_entity_item(sec["entity"], it) for it in sec.get("items") or [] if it.get("name")]
            if items:
                sections.append({**sec, "items": items})
        return {**body, "sections": sections} if sections else {}
    if kind == "compare":
        options = [o for o in body.get("options") or [] if o.get("name")]
        winner = body.get("winner")
        if not 2 <= len(options) <= 6 or winner is None or not float(winner).is_integer() \
                or not 0 <= int(winner) < len(options) or not body.get("why") or len(body["why"]) > 80:
            return {}
        return {**body, "options": options, "winner": int(winner)}
    if kind == "status":
        items = [{**i, "state": i["state"].lower()} for i in body.get("items") or []
                 if i.get("name") and str(i.get("state", "")).lower() in STATUS_STATES]
        for i in items:
            role = i.get("role")
            if isinstance(role, str) and len(role) > 24 and " \u00b7 " in role:
                i["role"] = role.split(" \u00b7 ")[0]   # "Production · pg 8.13 bump" cut to "Production · pg 8.13 b…"
            if isinstance(i.get("note"), str) and _COMMAND_NOTE.search(i["note"]):
                i.pop("note")
            elif isinstance(i.get("note"), str):
                i["note"] = _first_clause(i["note"])
            name = i["name"].strip()
            if re.fullmatch(r"[a-z0-9]+(?:[-_][a-z0-9]+){2,}", name):
                # "wiki-session-writeback-curator": a job's file name, read as words. Two-part slugs
                # ("fieldnote-api") are the names people use for projects and stay as written.
                name = re.sub(r"[-_]", " ", name)
            i["name"] = name[0].upper() + name[1:] if name[:1].islower() and " " in name else name
        if isinstance(body.get("scope"), str):
            # "Scheduled jobs (partial list, ~3 AM snapshot)": the aside is the agent's caveat; the answer says it.
            body = {**body, "scope": _PAREN_ASIDE.sub("", body["scope"]).strip() or body["scope"]}
        return {**body, "items": items} if body.get("scope") and items else {}
    if kind == "draft":
        channel = str(body.get("channel", "")).lower()
        if channel not in DRAFT_CHANNELS or not body.get("to") or not body.get("body"):
            return {}
        # "Priya Raman (#fieldnote-ios, thread reply)": the aside is the agent's note about where it
        # goes (the thread field says that); the recipient is the name before it.
        to = re.sub(r"\s*\([^()]*\)\s*$", "", body["to"]).strip() or body["to"]
        return {**body, "channel": channel, "to": to}
    if kind == "timeline":
        days = []
        for day in body.get("days") or []:
            items = [_coords(i) for i in day.get("items") or [] if i.get("when") and i.get("what")]
            items = [i if str(i.get("state", "")).lower() in TIMELINE_STATES else {k: v for k, v in i.items() if k != "state"}
                     for i in items]
            items = [{**i, "state": i["state"].lower()} if "state" in i else i for i in items]
            for i in items:
                # "SPK-208 Home is now a chat": a step reads as what happened; the key is tracker filing.
                what = re.sub(r"^(?:[A-Z][A-Z0-9]*-\d+|#\d+)[:\s\u2014-]+(?=\S+\s+\S)", "", i["what"].strip())
                i["what"] = _cap_first(what) if what != i["what"].strip() else what
                # A note that is a list of keys ("Includes SPK-232, SPK-233, SPK-241") says nothing to read.
                if isinstance(i.get("note"), str) and len(re.findall(r"\b[A-Z][A-Z0-9]*-\d+\b|#\d+", i["note"])) >= 3:
                    i.pop("note")
            if day.get("label") and items:
                days.append({**day, "items": items})
        return {**body, "days": days} if days else {}
    if kind == "rows":
        items = [_tone(dict(i), TAG_TONES) for i in body.get("items") or [] if i.get("name")]
        if not items:
            return {}
        out = {**body, "items": items}
        if str(out.get("entity", "")).lower() in ROW_ENTITIES:
            out["entity"] = out["entity"].lower()
        else:
            out.pop("entity", None)
        for i in items:
            # A worker's or database id ("t_98ab845a") is not a reference anyone uses; the row keeps
            # its name and loses the ref rather than show machine text.
            ref = i.get("ref")
            if isinstance(ref, str) and _INTERNAL_ID.fullmatch(ref.strip()):
                i.pop("ref")
            elif isinstance(ref, str) and not re.search(r"\d", ref):
                # A sender or owner ("Dana Whitfield") in the ref slot: it's who, not which. It leads
                # the note instead, so the row's mono reference column holds only numbers.
                i.pop("ref")
                i["note"] = f"{ref.strip()} · {i['note']}" if i.get("note") else ref.strip()
        _fill_ref_urls(items)
        states = {str(i.get("state", "")).strip().lower() for i in items}
        if len(items) >= 3 and len(states) == 1 and "" not in states:
            # "Open" on every row says nothing a row can't: the title can say it once.
            for i in items:
                i.pop("state")
        if out.get("entity") in ("pr", "issue"):
            for i in items:
                i["name"] = _cap_first(_work_title(i["name"]))
                if i.get("state"):
                    i["state"] = _cap_first(i["state"])
        return out
    if kind == "metric":
        if not body.get("label") or not body.get("value"):
            return {}
        out = dict(body)
        out["label"] = _TRACKER_KEY.sub("", out["label"]).strip() or out["label"]   # no "(FIELDNOTE-IOS-3KD)"
        points = out.get("points") or []
        if points and (len(points) < 2 or any("label" not in pt or "value" not in pt for pt in points)):
            out.pop("points")   # a series is all of it or none of it, never a chart cut short
        return out
    if kind == "preview":
        entity = str(body.get("entity", "")).lower()
        if entity not in PREVIEW_ENTITIES or not body.get("title"):
            return {}
        out = _entity_item(entity, {**body, "entity": entity})
        said = _norm(out.get("summary") or "")
        if isinstance(out.get("list"), list):
            # A key point that only restates the summary ("Goal: drafts keep attachments") is said twice.
            kept = [x for x in out["list"] if not (isinstance(x, str) and _restates(x, said))]
            out["list"] = kept
            if len(kept) == 1 and isinstance(kept[0], str) and out.get("summary"):
                # One numbered "key point" is a list of one: it reads as the summary's next sentence.
                point = kept[0].strip().rstrip(".")
                out["summary"] = out["summary"].rstrip().rstrip(".") + ". " + point[0].upper() + point[1:] + "."
                kept = []
            if not kept:
                out.pop("list")
                out.pop("list_label", None)
        if isinstance(out.get("flags"), list):
            listed = _norm(" ".join(x for x in out.get("list") or [] if isinstance(x, str)))
            out["flags"] = [f for f in out["flags"] if not (isinstance(f, str) and _restates(f, listed))]
            if not out["flags"]:
                out.pop("flags")
        return out
    if kind == "issue":
        if not body.get("title"):
            return {}
        out = _tone(dict(body), TAG_TONES)
        source = str(out.get("source", "")).lower()
        out["source"] = source if source in ISSUE_SOURCES else "other"
        out["title"] = _work_title(out["title"])
        if "priority" in out:
            word = _priority(out["priority"])
            out.pop("priority")
            if word:
                out["priority"] = word
        if out.get("state"):
            out["state"] = _cap_first(out["state"])
        if "labels" in out:
            labels = _topic_labels(out["labels"], out.get("state"), out.get("priority"))
            out.pop("labels")
            if labels:
                out["labels"] = labels
        return out
    if kind == "pr":
        state = str(body.get("state", "")).lower()
        if not body.get("title") and body.get("checks") and body.get("number"):
            # Asked only about CI ("how did CI do on 147753"), the agent read the checks, not the PR:
            # the card is that PR's checks, named by its number, with no state it didn't see.
            body = {**body, "title": f"Checks on #{int(body['number'])}"}
            if state not in PR_STATES:
                state = "failing" if any(str(c.get("state", "")).lower() == "failed" for c in body["checks"]) else ""
        if not body.get("title") or not body.get("reason") or state not in PR_STATES and not (state == "" and body.get("checks")):
            return {}
        out = {**body, "reason": body["reason"][:160], "title": _work_title(body["title"])}
        if state:
            out["state"] = state
        else:
            out.pop("state", None)
        repo = str(out.get("repo") or "")
        if not out.get("url") and out.get("number") and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            # owner/repo and a number name exactly one GitHub page: the card can open it.
            out["url"] = f"https://github.com/{repo}/pull/{int(out['number'])}"
        if "number" in out and not (float(out["number"]).is_integer() and out["number"] > 0):
            out.pop("number")
        elif "number" in out:
            out["number"] = int(out["number"])
        checks = [{**c, "state": c["state"].lower()} for c in out.get("checks") or []
                  if c.get("name") and str(c.get("state", "")).lower() in CHECK_STATES]
        for c in checks:
            if c.get("note") and _MACHINE_NOTE.search(c["note"]):
                c.pop("note")   # the state already says it; raw CI words are worse than none
        notes = [c.get("note") for c in checks]
        if len(checks) > 1 and notes[0] and all(n == notes[0] for n in notes):
            for c in checks:   # one reason on every check ("Needs a maintainer to approve") is the card's reason
                c.pop("note")
        out.pop("checks", None)
        if checks:
            # What failed or is still going first, six at most (older apps list them all); the card
            # counts the rest from checks_total ("9 more skipped"), so fifteen skips aren't a wall.
            order = {"failed": 0, "running": 1, "pending": 2, "passed": 3, "skipped": 4}
            ranked = [c for _, c in sorted(enumerate(checks), key=lambda p: (order.get(p[1]["state"], 5), p[0]))]
            out["checks"] = ranked[:6]
            if len(ranked) > 6:
                out["checks_total"] = len(ranked)
                rest = {c["state"] for c in ranked[6:]}
                if len(rest) == 1:
                    out["checks_rest"] = rest.pop()
            # The agent often lists a few checks and counts the rest in words ("All 15 checks were
            # skipped"): the card's "2 more" then undercounts. Take the count the reason gives.
            said = re.search(r"\b(all )?(\d{1,3}) (?:CI )?checks\b", out.get("reason") or "", re.I)
            if said and len(ranked) < int(said.group(2)) <= 300:
                out["checks_total"] = int(said.group(2))
                states = {c["state"] for c in ranked}
                if said.group(1) and len(states) == 1:
                    out["checks_rest"] = states.pop()
        return out
    if kind == "jobs":
        items = [i for i in body.get("items") or [] if i.get("name") and i.get("status")]
        for i in items:
            if "ask" in i:
                i["ask"] = _label(i["ask"])
            if not i.get("needs_you"):
                i.pop("ask", None)
            if isinstance(i.get("elapsed"), str):
                # "about 19 min" pushed the name onto two lines; a card corner is exact enough as "19 min".
                i["elapsed"] = re.sub(r"(?i)^(?:about|around|roughly|approx\.?|~)\s*", "", i["elapsed"].strip())
        return {**body, "items": items} if items else {}
    if kind == "site":
        return body if body.get("url") else {}
    if kind == "mockups":
        items = [i for i in body.get("items") or [] if i.get("image") is not None]
        if not items:
            return {}
        out = {**body, "items": items}
        if isinstance(out.get("title"), str):
            out["title"] = _PAREN_ASIDE.sub("", out["title"]).strip() or out["title"]
        if isinstance(out.get("note"), str) and _restates(out["note"], _norm(out.get("title") or "")):
            out.pop("note")
        nxt = [_label(n) for n in out.get("next") or [] if n]
        out.pop("next", None)
        if nxt:
            out["next"] = nxt
        return out
    return body


# A commit-style prefix ("fix(gateway): ", "feat: ", "chore(deps)!: ") is how a branch is filed, not
# what the change is; the card shows the words after it, capitalized like a title.
_COMMIT_PREFIX = re.compile(r"^(?:feat|fix|chore|docs|refactor|perf|test|tests|build|ci|style|revert)"
                            r"(?:\([^)]{1,40}\))?!?:\s+(?=\S)", re.I)
_PRIORITY_WORDS = {"0": None, "1": "Urgent", "2": "High", "3": "Medium", "4": "Low",
                   "no priority": None, "none": None, "p0": "Urgent", "p1": "High", "p2": "Medium", "p3": "Low"}


# Worker/database ids, commit SHAs, UUIDs and CI run ids (8+ digits; PR and issue numbers stay shorter).
_INTERNAL_ID = re.compile(r"(?i)#?(?:[a-z]{1,4}_[0-9a-f]{6,}|[0-9a-f]{12,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f-]{20,}|\d{8,})")
# Machine words in a check's note: CI states in snake_case and bare commit SHAs ("action_required on 05396a4").
_MACHINE_NOTE = re.compile(r"\b[a-z]+_[a-z_]+\b|\b[0-9a-f]{7,40}\b")
# A command line in a note ("npm run build exited 1"): the state already says it failed.
_COMMAND_NOTE = re.compile(r"(?i)\bexit(?:ed)?(?: with)?(?: code| status)? \d+\b|^(?:npm|yarn|pnpm|bun|make|cargo|go|pip|uv) \w")


_SKY_WORDS = (   # first match wins: the strongest weather in the words
    (r"thunder|storm|lightning", "storm"), (r"sleet|freezing|ice|hail", "sleet"), (r"snow|flurr", "snow"),
    (r"heavy rain|downpour|pouring", "heavy_rain"), (r"drizzle|sprinkle", "drizzle"), (r"rain|shower", "rain"),
    (r"fog|mist|haze|smoke", "fog"), (r"partly|mostly sunny|some sun|few clouds|scattered clouds|mix of sun", "cloud_sun"),
    (r"overcast|cloud|grey|gray", "cloud"), (r"clear|sunny|fair|sun", "sun"),
)


def condition_icon(condition: str) -> str | None:
    """The weather card's icon word for a sky written in words ("Rain showers" -> rain), or None."""
    low = condition.lower()
    return next((icon for pattern, icon in _SKY_WORDS if re.search(pattern, low)), None)


def _fill_ref_urls(items: list[dict[str, Any]]) -> None:
    """Rows the agent linked for some refs but not others ("SPK-196" linked, "SPK-207" not): when a
    linked row's URL holds its own ref, the others get the same URL with theirs, so every row opens."""
    template = None
    for i in items:
        ref, url = i.get("ref"), i.get("url")
        if isinstance(ref, str) and isinstance(url, str) and url.startswith("https://"):
            m = re.match(r"(https://[^?#]*/)" + re.escape(ref.strip()) + r"(?=$|[/?#])", url)
            if m:
                template = m.group(1)
                break
    if not template:
        return
    for i in items:
        ref = i.get("ref")
        if not i.get("url") and isinstance(ref, str) and re.fullmatch(r"[A-Z][A-Z0-9]*-\d+", ref.strip()):
            i["url"] = template + ref.strip()


_TRACKER_KEY = re.compile(r"\s*\((?:[A-Z][A-Z0-9]*-)+[A-Z0-9]+\)\s*$")
_PAREN_ASIDE = re.compile(r"\s*\([^()]{1,40}\)")


def _first_clause(note: str, limit: int = 44) -> str:
    """A tile's note as one short clause: "Very busy; 18.0 GB free, 206 GB disk free" -> "Very busy"."""
    note = note.strip()
    if len(note) <= limit:
        return note
    head = re.split(r"\s*[;\u2014]\s*|\.\s+", note)[0].strip()
    if len(head) <= limit:
        return head
    parts = [p.strip() for p in head.split(",")]
    out = parts[0]
    for p in parts[1:]:
        if len(out) + len(p) + 2 > limit:
            break
        out += ", " + p
    return out


def _restates(text: str, said: str) -> bool:
    """Most of *text*'s content words already appear in *said* (a normalized string)."""
    words = [w for w in _norm(re.sub(r"^\w+:\s*", "", text)).split() if len(w) > 2 and w not in _LABEL_STOP]
    if not words or said.strip() == "":
        return False
    return sum(f" {w} " in said for w in words) * 4 >= len(words) * 3


def _cap_first(text: str) -> str:
    first = text.split(" ", 1)[0] if text else ""
    if any(ch.isupper() for ch in first[1:]):
        return text   # "iPhone Duo side panel", "macOS menu": the name's own casing
    return text[:1].upper() + text[1:] if text and text[:1].islower() else text


def _work_title(title: str, long: int = 56) -> str:
    """An issue or pull request title as people read it: no commit-style prefix; and a long one
    ("Build 70 iPhone: Jump to work shows while the work is on screen; new replies stop
    auto-scrolling", three lines on a phone card) loses its scope prefix and its second clause
    ("Jump to work shows while the work is on screen"). The tracker keeps the whole title."""
    text = title.strip()
    bare = _COMMIT_PREFIX.sub("", text)
    out = _cap_first(bare) if bare != text else text
    if len(out) > long:
        head, sep, rest = out.partition(": ")
        if sep and len(head.split()) <= 4 and len(rest.split()) >= 3:
            out = _cap_first(rest.strip())
    if len(out) > long:
        clause = re.split(r"\s*(?:;|\s[\u2014\u2013]\s|\s-\s)\s*", out)[0].strip()
        if len(clause.split()) >= 3:
            out = clause
    return out


def _priority(value: Any) -> str | None:
    """Tracker priority in words: Linear sends 0-4 (0 none, 1 urgent ... 4 low); a word passes through
    without a trailing 'priority' (the card adds it). None: no priority worth a line."""
    raw = str(value).strip()
    key = raw.lower().removesuffix(" priority").strip()
    key = key[:-2] if key.endswith(".0") else key
    if key in _PRIORITY_WORDS:
        return _PRIORITY_WORDS[key]
    return _cap_first(raw[:-len(" priority")] if raw.lower().endswith(" priority") else raw) or None


def _norm_label(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


def _topic_labels(labels: list[Any], state: Any, priority: Any) -> list[str]:
    """Labels that say what the issue is about: one that only repeats the state or priority
    ("ready-to-ship" beside Ready to ship) is already on the card."""
    said = {_norm_label(state), _norm_label(priority), ""}
    out: list[str] = []
    for label in labels:
        if re.fullmatch(r"(?i)(?:build|release|cycle|sprint|milestone|v)[\s_-]?\d+(?:\.\d+)*", str(label).strip()):
            continue   # "build-70": a release bucket, not what the issue is about
        if _norm_label(label) not in said and _norm_label(label) not in {_norm_label(x) for x in out}:
            out.append(str(label))
    return out


def _coords(item: dict[str, Any]) -> dict[str, Any]:
    """Coordinates come as a valid pair or not at all."""
    if "lat" in item and not -90 <= item["lat"] <= 90 or "lon" in item and not -180 <= item["lon"] <= 180 \
            or ("lat" in item) != ("lon" in item):
        item = {k: v for k, v in item.items() if k not in ("lat", "lon")}
    return item


def _entity_item(entity: str, item: dict[str, Any]) -> dict[str, Any]:
    """Ids that look wrong are dropped (the app then falls back to a search link)."""
    ids = dict(item.get("ids") or {})
    if ids.get("apple_music") and not re.fullmatch(r"\d{4,15}", ids["apple_music"]):
        ids.pop("apple_music")
    if ids.get("spotify") and not re.fullmatch(r"[A-Za-z0-9]{22}", ids["spotify"]):
        ids.pop("spotify")
    out = {k: v for k, v in item.items() if k != "ids"}
    if ids and entity == "media":
        out["ids"] = ids
    if "media_kind" in out and (entity != "media" or out["media_kind"].lower() not in MEDIA_KINDS):
        out.pop("media_kind")
    elif "media_kind" in out:
        out["media_kind"] = out["media_kind"].lower()
    return _coords(out)


# -- shaping artifacts against the answer -----------------------------------------------------------

def _norm(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "


_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    out = set()
    for raw in _NUM_RE.findall(text or ""):
        n = raw.replace(",", "")
        out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def _grounded_name(name: str, said: str) -> bool:
    return bool(name) and _norm(name).strip() != "" and _norm(name) in said


def _grounded_value(value: Any, said_numbers: set[str]) -> bool:
    return _numbers(str(value)) <= said_numbers


_LABEL_STOP = set("""a an the and or of to for in on at by with from via is are was be your you my our its it this that
these those as into over under up out off""".split())


def _grounded_label(name: str, said: str, said_numbers: set[str], half: bool = False) -> bool:
    """A status tile's or row's name is a short label the agent writes for one line of its answer
    ("Mac tests", "Package needs a signature"), rarely that answer's exact words. It counts as said when
    the exact phrase is there, or when every number in it is said and at least two thirds of its words
    are (a one-word name must be said exactly). Values, refs and amounts stay exact (SPK-236)."""
    if _grounded_name(name, said):
        return True
    head = re.split(r"\s+[\u2014\u2013+]\s+|\s+-\s+|\s*\(", name, maxsplit=1)[0].strip()
    if head != name and len(_norm(head).split()) >= 2 and _grounded_label(head, said, said_numbers, half):
        return True   # "Wallboard updater \u2014 daytime": the job is named; the qualifier is its detail
    words = [w for w in _norm(name).split() if w not in _LABEL_STOP]
    if len(words) < 2 or not _numbers(name) <= said_numbers:
        return False
    hits = sum(f" {w} " in said for w in words)
    return hits * 2 >= len(words) if half else hits * 3 >= len(words) * 2


def grounded(view: dict[str, Any], answer: str) -> bool:
    """Every name and number on an artifact appears in the answer text. One miss and the card goes
    (it is never repaired: repaired cards were how wrong content got through). Status and rows names
    are labels, checked word by word (``_grounded_label``); entity names stay exact."""
    said, nums = _norm(answer), _numbers(answer)
    # "six pieces of work" says the 6 on the card (spoken-style answers spell small numbers out).
    from .home_words import digits
    nums |= _numbers(digits(answer or ""))
    kind = view.get("kind")
    names: list[str] = []
    labels: list[str] = []
    loose: list[str] = []
    values: list[Any] = []
    if kind == "explore":
        for sec in view["sections"]:
            for it in sec["items"]:
                names.append(it["name"])
                values += [it.get(k) for k in ("price", "rating") if it.get(k) is not None]
    elif kind == "compare":
        for o in view["options"]:
            names.append(o["name"])
            values += [o.get("price")] if o.get("price") else []
    elif kind == "status":
        for i in view["items"]:
            labels.append(i["name"])
            values += [i.get("value")] if i.get("value") else []
    elif kind == "draft":
        # A recipient's full name ("Priya Raman") is usually said by first name in the answer.
        (loose if len(_norm(view["to"]).split()) > 1 else names).append(view["to"])
    elif kind == "timeline":
        for day in view["days"]:
            values.append(day["label"])
            for it in day["items"]:
                values.append(it["when"])
                names += [it["where"]] if it.get("where") else []
    elif kind == "rows":
        for i in view["items"]:
            # A row pinned by a ticket ref the answer says ("SPK-196") is that ticket; its name is the
            # agent's short description of it, so half its words are enough.
            ref = i.get("ref")
            if ref and _numbers(str(ref)) and _grounded_name(str(ref), said):
                loose.append(i["name"])
            else:
                labels.append(i["name"])
            values += [i[k] for k in ("ref", "amount") if i.get(k)]
    elif kind == "metric":
        # The line itself prints no numbers; only marked points show theirs, so only those are checked.
        values += [view["value"]] + [view[k] for k in ("delta", "threshold") if view.get(k) is not None]
        values += [pt["value"] for pt in view.get("points") or [] if pt.get("mark")]
    elif kind == "preview":
        names += [view["title"]] + ([view["creator"]] if view.get("creator") else [])
    elif kind in ("issue", "pr"):
        # The ticket is pinned by its ref or number; its title, copied from the tracker, is rarely
        # quoted whole in the answer, so with the ref said it is checked as a label (SPK-231 reps).
        ref = view.get("ref") if kind == "issue" else view.get("number")
        if ref is not None and _numbers(str(ref)) and _grounded_value(ref, nums) and _grounded_name(str(ref), said):
            labels.append(view["title"])
        else:
            names.append(view["title"])
        values += [ref] if ref is not None else []
    elif kind == "jobs":
        # Job names come from the board and the answer says them in its own words: labels.
        labels += [i["name"] for i in view["items"]]
    elif kind == "site":
        names += [view["title"]] if view.get("title") else []
    # mockups: the pictures are the task's own renders and their captions describe them; nothing to check.
    if kind == "explore":
        values += [it["day"] for sec in view["sections"] for it in sec["items"] if it.get("day")]
    return (all(_grounded_name(n, said) for n in names) and all(_grounded_label(n, said, nums) for n in labels)
            and all(_grounded_label(n, said, nums, half=True) for n in loose)
            and all(_grounded_value(v, nums) for v in values))


def _artifact_actions(view: dict[str, Any]) -> dict[str, Any]:
    """Buttons are set here, never by the agent: Send on a draft and Go with on a compare reach the
    task as a follow-up, and both always confirm first."""
    view.pop("actions", None)
    if view["kind"] == "draft":
        view["actions"] = [{"id": "send", "label": "Send", "style": "primary", "confirm": True}]
    elif view["kind"] == "compare":
        label = _str("Go with " + view["options"][view["winner"]]["name"], 24) or "Go with this"
        view["actions"] = [{"id": "go", "label": label, "style": "primary", "confirm": True}]
    elif view["kind"] == "pr" and view.get("state") in ("conflicts", "failing"):
        label = "Fix conflicts" if view["state"] == "conflicts" else "Fix the checks"
        view["actions"] = [{"id": "fix", "label": label, "style": "primary", "confirm": False}]
    elif view["kind"] == "mockups" and view.get("next"):
        view["actions"] = [{"id": f"next_{i}", "label": n, "style": "secondary", "confirm": False}
                           for i, n in enumerate(view["next"])]
    return view


def _label(text: str, limit: int = 24) -> str:
    """A button's words, cut at a word so a label never ends mid-word."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit + 1].rsplit(" ", 1)[0].strip()
    return cut or text[:limit]


def _when(value: Any) -> Any:
    """An event time as a datetime when it is a machine timestamp ("2026-11-09T13:00"), else None."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if "T" in str(value) else None
    except ValueError:
        return None


def day_overlaps(view: dict[str, Any]) -> list[tuple[int, int]]:
    """Pairs of a day's timed events that overlap (index of the earlier, index of the later). The
    apps draw the same pairs from the same times; the server only uses them to offer Move."""
    times = []
    for i, e in enumerate(view.get("events") or []):
        start, end = _when(e.get("start")), _when(e.get("end"))
        if start and end and end > start and not e.get("all_day"):
            times.append((start, end, i))
    times.sort()
    pairs = []
    for a in range(len(times)):
        for b in range(a + 1, len(times)):
            try:
                if times[b][0] < times[a][1]:
                    pairs.append((times[a][2], times[b][2]))
            except TypeError:   # one time with a zone, one without: not comparable, not drawn
                continue
    return pairs


def _day_actions(view: dict[str, Any]) -> dict[str, Any]:
    """A day with an overlap offers to move the later event (the agent does the moving)."""
    pairs = day_overlaps(view)
    if pairs and not view.get("actions"):
        later = view["events"][pairs[0][1]].get("title") or "the later one"
        label = "Move " + later
        while len(label) > 24 and len(label.split()) > 2:
            label = label.rsplit(" ", 1)[0]   # "Move Investor update", never "Move Investor update cal"
        view["actions"] = [{"id": "move", "label": label if len(label) <= 24 else "Move it", "style": "secondary",
                            "confirm": False}]
    free = view.get("free")
    if isinstance(free, list) and free:
        # "Before 9:30 AM" and "After 7:00 PM" are the whole morning and evening, not a gap worth
        # naming; and ":00" is noise ("1:30\u20136:00 PM" reads "1:30\u20136 PM").
        inner = [f for f in free if isinstance(f, str) and not re.match(r"(?i)(?:before|after|until|from)\b", f)]
        view["free"] = [re.sub(r":00(?=\s*(?:[AaPp][Mm]|\u2013|-|$))", "", f) for f in (inner or free) if isinstance(f, str)]
    return view


def _unsaid(view: dict[str, Any], answer: str) -> dict[str, Any]:
    """A card line that repeats the answer's opening (the reply sits right above the card on Home)
    is said twice: a PR's reason or an issue's note that restates it goes; the reply keeps it."""
    opening = _norm(" ".join(re.split(r"(?<=[.!?])\s+", (answer or "").strip())[:2]))
    for key in {"pr": ("reason",), "issue": ("note",)}.get(view.get("kind"), ()):
        if isinstance(view.get(key), str) and _restates(view[key], opening):
            view = {k: v for k, v in view.items() if k != key}
    return view


def shape(views: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
    """Artifacts (the v1 kinds) kept only when grounded in the answer text; at most two, in the
    order given. Other kinds pass through unchanged (older apps keep their cards)."""
    out, kept = [], 0
    for view in views:
        if view.get("kind") == "calendar_day":
            out.append(_day_actions(view))
            continue
        if view.get("kind") not in ARTIFACTS:
            out.append(view)
            continue
        if kept >= MAX_ARTIFACTS or not grounded(view, answer):
            continue
        out.append(_artifact_actions(_unsaid(view, answer)))
        kept += 1
    return out


def follow_up(view: dict[str, Any], action: str) -> str | None:
    """What an artifact's button says to its task, in plain words (the agent does the sending)."""
    if view.get("kind") == "draft" and action == "send":
        channel = {"email": "email", "x": "post"}.get(view["channel"], "message")
        return f"Send the {channel} to {view['to']} exactly as drafted on the card."
    if view.get("kind") == "compare" and action == "go":
        return f"Go with {view['options'][view['winner']]['name']}."
    if view.get("kind") == "pr" and action == "fix":
        name = " ".join(x for x in (view.get("repo"), f"#{view['number']}" if view.get("number") else None) if x)
        what = "merge conflicts" if view.get("state") == "conflicts" else "failing checks"
        return f"Fix the {what} on the pull request {name} ({view['title']}).".replace("  ", " ")
    if view.get("kind") == "mockups" and action.startswith("next_"):
        nxt = view.get("next") or []
        i = action[5:]
        return f"{nxt[int(i)]}." if i.isdigit() and int(i) < len(nxt) else None
    if view.get("kind") == "calendar_day" and action == "move":
        pairs = day_overlaps(view)
        if pairs:
            first, later = (view["events"][j].get("title") or "the event" for j in pairs[0])
            return f"Move {later} so it no longer overlaps {first}. Show me the new time before changing it."
    return None


def view_images(views: list[dict[str, Any]]) -> list[str]:
    """Picture references use result-image vetting, never client-visible server paths."""
    return [part["image"] for part in _image_parts(views) if isinstance(part.get("image"), str)]


def _image_parts(views: list[dict[str, Any]]):
    for view in views:
        if view.get("kind") == "before_after":
            yield from (view.get(side) or {} for side in ("before", "after")
                        if _is_path((view.get(side) or {}).get("image")))
        elif view.get("kind") in ("issue", "site") and isinstance(view.get("image"), str):
            yield view
        elif view.get("kind") == "mockups":
            yield from (item for item in view.get("items") or [] if isinstance(item.get("image"), str))
        elif view.get("kind") == "explore":
            for section in view.get("sections", []):
                for item in section.get("items", []):
                    # A catalog photo goes through the same bounded authenticated route as a render.
                    if item.get("photo_url") and not item.get("image"):
                        item["image"] = item["photo_url"]
                    item.pop("photo_url", None)
                    yield item


def _is_path(value: Any) -> bool:
    return isinstance(value, str) and not value.startswith("https://")


def number_view_images(views: list[dict[str, Any]], number: Any) -> None:
    """Swap each before/after file path for its card number (``number(path)`` -> int, 0 when the
    image was rejected). A rejected path is dropped, so no server path ever reaches a client."""
    for part in _image_parts(views):
        if isinstance(part.get("image"), str):
            n = number(part["image"])
            if n:
                part["image"] = n
            else:
                part.pop("image")


def clean_all(raw: Any) -> list[dict[str, Any]]:
    items = raw if isinstance(raw, list) else [raw]
    return [v for v in (clean(x) for x in items[:MAX_VIEWS * 2]) if v][:MAX_VIEWS]


def extract(output: str) -> tuple[str, list[dict[str, Any]]]:
    """Strip an optional ```speakeasy-views block from a task's final text; return (text, views)."""
    match = re.search(r"(?s)\n?```speakeasy-views\s*\n(.*?)\n```", output or "")
    if not match:
        return output, []
    try:
        views = clean_all(json.loads(match.group(1)))
    except ValueError:
        views = []
    return (output[:match.start()] + output[match.end():]).strip(), views


def clean_option(raw: Any) -> str | dict[str, Any] | None:
    """One answer: a short string, or an object {text, detail, tags, risk, reversible} when the task
    explained it. An object without text is dropped; one with only text stays a plain string."""
    if not isinstance(raw, dict):
        return _str(raw, 1200)
    opt = _object(OPTION, raw)
    if not opt.get("text"):
        return None
    if "tags" in opt:
        opt["tags"] = [_tone(t, TAG_TONES) for t in opt["tags"] if t.get("text")] or None
        if opt["tags"] is None:
            opt.pop("tags")
    if str(opt.get("risk", "")).lower() in RISKS:
        opt["risk"] = str(opt["risk"]).lower()
    else:
        opt.pop("risk", None)
    return opt if len(opt) > 1 else opt["text"]


def option_text(option: Any) -> str:
    """The words of an answer, whichever form it came in (answers are sent back as this text)."""
    return option.get("text", "") if isinstance(option, dict) else str(option or "")


def approvable(view: dict[str, Any]) -> bool:
    """One-tap Approve, decided here and never by the model: the recommended answer must be low risk
    and reversible, and nothing in the question or that answer may spend money, speak for the user,
    publish, or delete."""
    rec = view.get("recommended")
    if not isinstance(rec, int):
        return False
    option = view["options"][rec]
    if not isinstance(option, dict) or option.get("risk") != "low" or option.get("reversible") is not True:
        return False
    words = " ".join([view.get("question", ""), view.get("affects", ""), option_text(option), option.get("detail", ""),
                      *(t.get("text", "") for t in option.get("tags") or [])])
    return not APPROVE_GUARD_RE.search(words)


def extract_question(output: str) -> tuple[str, dict[str, Any] | None]:
    """Strip an optional ```speakeasy-question block: {"question", "options": [2-4], "recommended": i}.
    Options are strings or objects (text, detail, tags, risk, reversible, image); the question can
    carry context, why and affects. ``approvable`` is always computed here."""
    match = re.search(r"(?s)\n?```speakeasy-question\s*\n(.*?)\n```", output or "")
    if not match:
        return output, None
    rest = (output[:match.start()] + output[match.end():]).strip()
    try:
        raw = json.loads(match.group(1))
    except ValueError:
        return rest, None
    if not isinstance(raw, dict):
        return rest, None
    view = clean({**raw, "kind": "question"}) or {"kind": "question"}
    pairs = []   # (clean option, raw image) for each answer that survived
    for opt in (raw.get("options") if isinstance(raw.get("options"), list) else [])[:4]:
        cleaned = clean_option(opt)
        if cleaned:
            image = opt.get("image") if isinstance(opt, dict) and isinstance(opt.get("image"), str) else ""
            pairs.append((cleaned, image.strip()))
    if not view.get("question") or len(pairs) < 2:
        return rest, None
    view["options"] = [o for o, _ in pairs]
    images = [i for _, i in pairs]
    if any(images):
        view["_option_images"] = images
    rec = view.get("recommended")
    if isinstance(rec, (int, float)) and 0 <= int(rec) < len(view["options"]):
        view["recommended"] = int(rec)
    else:
        view.pop("recommended", None)
    view.pop("approvable", None)
    if approvable(view):
        view["approvable"] = True
    return rest, view


# Words that start a sentence but are not names: these lose their capital mid-sentence
# ("Quick question: which ..."). Anything else (Misi, Lilia, PR 147753) keeps its casing.
_PLAIN_LEADS = frozenset("""
a an the this that these those it its my your our their one two three four five first second last
which what when where who whom whose why how should shall would will could can do does did is are was were
have has had may might must want need yes no not none neither either both all any each every some other
keep skip stop start wait go shoot lock leave use pick try ship hold move cancel send buy book check fix
ask show make take add drop merge pull push run build open close approve reject accept decline retry
just only now later today tonight tomorrow
""".split())


def soft_lower(text: str) -> str:
    """Lowercase a sentence-initial word for mid-sentence use, unless it is (or may be) a name."""
    word = re.split(r"[\s,.;:!?]", text, maxsplit=1)[0]
    if word and word.lower() in _PLAIN_LEADS and word[1:] == word[1:].lower():
        return text[0].lower() + text[1:]
    return text


def spoken_question(view: dict[str, Any], lead: str = "") -> str:
    """How the voice reads a question card: the question, the options, and the suggestion.

    The suggestion is skipped when the lead sentence already named it ("My pick is Misi.")."""
    opts = [option_text(o).rstrip(".") for o in view["options"]]
    said = view["question"].rstrip()
    if not said.endswith("?"):
        said = said.rstrip(".") + "?"
    listed = ", ".join(opts[:-1]) + f", or {opts[-1]}" if len(opts) > 2 else f"{opts[0]} or {opts[1]}"
    out = f"Quick question: {soft_lower(said)} {listed}."
    if any(view.get("images") or []):
        out = out[:-1] + ". They're on screen."
    if "recommended" in view:
        pick = opts[view["recommended"]]
        if pick.lower() not in lead.lower():
            out += f" I'd go with {soft_lower(pick)}."
    return out
