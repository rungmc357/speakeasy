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
EVENT = {"start": S, "end": S, "title": S, "place": S, "all_day": B, "color": S, "url": URL}
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
    "status": {"scope": S, "items": ({"name": S, "role": S, "state": S, "value": S, "unit": S, "note": S,
                                      "history": (N, 24), "url": URL}, 12)},
    "draft": {"channel": S, "to": S, "subject": S, "body": S, "send_as": S, "thread": S, "revision": N,
              "changes": S},
    # P2: what happens when (a trip by day, a calendar, a release's stages, a package's scans).
    "timeline": {"title": S, "days": ({"label": S, "sleep": S, "items": ({"when": S, "what": S, "where": S, "note": S,
                                                                         "state": S, "url": URL, "address": S,
                                                                         "lat": N, "lon": N}, 10)}, 10)},
    # P4: rows of things to act on (pull requests, inbox triage, to-dos, bills, alerts).
    "rows": {"title": S, "entity": S, "items": ({"ref": S, "name": S, "state": S, "tone": S, "note": S, "when": S,
                                                 "amount": S, "url": URL}, 12)},
    # P6: one number that matters, which way it moves, and the series behind it.
    "metric": {"label": S, "value": S, "unit": S, "delta": S, "good": B,
               "points": ({"label": S, "value": N, "mark": S}, 31),
               "threshold": N, "threshold_label": S, "source": S, "as_of": S},
    # P7: one thing to open (an album, an article, a paper, a contract, a file) with a short list.
    "preview": {"entity": S, "title": S, "creator": S, "sub": S, "year": S, "photo_url": URL, "url": URL,
                "media_kind": S, "ids": {"apple_music": S, "spotify": S}, "summary": S, "list_label": S,
                "list": (S, 10), "flags": (S, 4)},
}

# One thing in an explore section (a place, a stay, an album, a pull request…). The app draws the
# layout and builds the deep links from `ids` / `url` / `lat`+`lon` / `address`; the agent never
# names a scheme.
ENTITIES = {"place", "stay", "reservation", "flight", "event", "product", "media", "person", "article", "pr",
            "listing", "generation", "task"}
ENTITY_ITEM = {"name": S, "sub": S, "when": S, "facts": (S, 3), "price": S, "rating": N, "state": S,
               "photo_url": URL, "url": URL, "address": S, "city": S, "lat": N, "lon": N, "phone": S,
               "creator": S, "media_kind": S, "ids": {"apple_music": S, "spotify": S}, "day": S}
CATALOG["explore"]["sections"][0]["items"] = (ENTITY_ITEM, 10)
ARTIFACTS = {"explore", "compare", "status", "draft", "timeline", "rows", "metric", "preview"}
ROW_ENTITIES = {"pr", "issue", "email", "todo", "bill", "package", "alert", "note", "item", "task", "post"}
PREVIEW_ENTITIES = {"media", "article", "paper", "file", "document", "contract", "page", "image", "recipe"}
TIMELINE_STATES = {"done", "now", "next"}
STATUS_STATES = {"ok", "warn", "bad", "off", "on", "needs_you", "info"}
DRAFT_CHANNELS = {"email", "imessage", "sms", "slack", "discord", "telegram", "x", "linkedin", "whatsapp"}
MEDIA_KINDS = {"album", "song", "playlist", "podcast", "artist", "show", "book"}
MAX_ARTIFACTS = 2

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
        return {**body, "items": items} if body.get("scope") and items else {}
    if kind == "draft":
        channel = str(body.get("channel", "")).lower()
        if channel not in DRAFT_CHANNELS or not body.get("to") or not body.get("body"):
            return {}
        return {**body, "channel": channel}
    if kind == "timeline":
        days = []
        for day in body.get("days") or []:
            items = [_coords(i) for i in day.get("items") or [] if i.get("when") and i.get("what")]
            items = [i if str(i.get("state", "")).lower() in TIMELINE_STATES else {k: v for k, v in i.items() if k != "state"}
                     for i in items]
            items = [{**i, "state": i["state"].lower()} if "state" in i else i for i in items]
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
        return out
    if kind == "metric":
        if not body.get("label") or not body.get("value"):
            return {}
        out = dict(body)
        points = out.get("points") or []
        if points and (len(points) < 2 or any("label" not in pt or "value" not in pt for pt in points)):
            out.pop("points")   # a series is all of it or none of it, never a chart cut short
        return out
    if kind == "preview":
        entity = str(body.get("entity", "")).lower()
        if entity not in PREVIEW_ENTITIES or not body.get("title"):
            return {}
        return _entity_item(entity, {**body, "entity": entity})
    return body


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


def grounded(view: dict[str, Any], answer: str) -> bool:
    """Every name and number on an artifact appears in the answer text. One miss and the card goes
    (it is never repaired: repaired cards were how wrong content got through)."""
    said, nums = _norm(answer), _numbers(answer)
    kind = view.get("kind")
    names: list[str] = []
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
            names.append(i["name"])
            values += [i.get("value")] if i.get("value") else []
    elif kind == "draft":
        names.append(view["to"])
    elif kind == "timeline":
        for day in view["days"]:
            values.append(day["label"])
            for it in day["items"]:
                values.append(it["when"])
                names += [it["where"]] if it.get("where") else []
    elif kind == "rows":
        for i in view["items"]:
            names.append(i["name"])
            values += [i[k] for k in ("ref", "amount") if i.get(k)]
    elif kind == "metric":
        # The line itself prints no numbers; only marked points show theirs, so only those are checked.
        values += [view["value"]] + [view[k] for k in ("delta", "threshold") if view.get(k) is not None]
        values += [pt["value"] for pt in view.get("points") or [] if pt.get("mark")]
    elif kind == "preview":
        names += [view["title"]] + ([view["creator"]] if view.get("creator") else [])
    if kind == "explore":
        values += [it["day"] for sec in view["sections"] for it in sec["items"] if it.get("day")]
    return all(_grounded_name(n, said) for n in names) and all(_grounded_value(v, nums) for v in values)


def _artifact_actions(view: dict[str, Any]) -> dict[str, Any]:
    """Buttons are set here, never by the agent: Send on a draft and Go with on a compare reach the
    task as a follow-up, and both always confirm first."""
    view.pop("actions", None)
    if view["kind"] == "draft":
        view["actions"] = [{"id": "send", "label": "Send", "style": "primary", "confirm": True}]
    elif view["kind"] == "compare":
        label = _str("Go with " + view["options"][view["winner"]]["name"], 24) or "Go with this"
        view["actions"] = [{"id": "go", "label": label, "style": "primary", "confirm": True}]
    return view


def shape(views: list[dict[str, Any]], answer: str) -> list[dict[str, Any]]:
    """Artifacts (the v1 kinds) kept only when grounded in the answer text; at most two, in the
    order given. Other kinds pass through unchanged (older apps keep their cards)."""
    out, kept = [], 0
    for view in views:
        if view.get("kind") not in ARTIFACTS:
            out.append(view)
            continue
        if kept >= MAX_ARTIFACTS or not grounded(view, answer):
            continue
        out.append(_artifact_actions(view))
        kept += 1
    return out


def follow_up(view: dict[str, Any], action: str) -> str | None:
    """What an artifact's button says to its task, in plain words (the agent does the sending)."""
    if view.get("kind") == "draft" and action == "send":
        channel = {"email": "email", "x": "post"}.get(view["channel"], "message")
        return f"Send the {channel} to {view['to']} exactly as drafted on the card."
    if view.get("kind") == "compare" and action == "go":
        return f"Go with {view['options'][view['winner']]['name']}."
    return None


def view_images(views: list[dict[str, Any]]) -> list[str]:
    """Every file path a before/after card named, in order (they go through the result-image vetting)."""
    return [v[side]["image"] for v in views if v.get("kind") == "before_after"
            for side in ("before", "after") if _is_path((v.get(side) or {}).get("image"))]


def _is_path(value: Any) -> bool:
    return isinstance(value, str) and not value.startswith("https://")


def number_view_images(views: list[dict[str, Any]], number: Any) -> None:
    """Swap each before/after file path for its card number (``number(path)`` -> int, 0 when the
    image was rejected). A rejected path is dropped, so no server path ever reaches a client."""
    for v in views:
        for side in ("before", "after") if v.get("kind") == "before_after" else ():
            part = v.get(side) or {}
            if _is_path(part.get("image")):
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


def spoken_question(view: dict[str, Any]) -> str:
    """How the voice reads a question card: the question, the options, and the suggestion."""
    opts = [option_text(o).rstrip(".") for o in view["options"]]
    said = view["question"].rstrip()
    if not said.endswith("?"):
        said = said.rstrip(".") + "?"
    listed = ", ".join(opts[:-1]) + f", or {opts[-1]}" if len(opts) > 2 else f"{opts[0]} or {opts[1]}"
    out = f"Quick question: {said[0].lower() + said[1:]} {listed}."
    if any(view.get("images") or []):
        out = out[:-1] + ". They're on screen."
    if "recommended" in view:
        out += f" I'd go with {opts[view['recommended']].lower()}."
    return out
