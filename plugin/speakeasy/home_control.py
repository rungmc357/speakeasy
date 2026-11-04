"""Instant home control (optional): spoken home requests go straight to Home Assistant during a call.

Offered automatically when this Hermes profile already has Home Assistant set up (Hermes' own
``HASS_URL`` / ``HASS_TOKEN``) and Home Assistant answers. When it is on, a handoff that is clearly
about the user's devices ("lights off except the bedroom, den to 72, fan on") is planned by ONE
call to the user's own fast routing model (Hermes auxiliary task ``speakeasy_router``) against the
list of devices the user chose, checked, and sent to Home Assistant. That takes about a second
instead of a full Hermes run. Multi-step requests work when every step is a plain device command.

Anything else goes to Hermes unchanged: timing or conditions ("at 11", "if nobody's home"),
routines, anything the planner isn't sure about, any device not on the user's list, and every lock,
alarm, garage door or other security device (never offered here at all).

Safety: the planner can only name devices from the list; every call is checked against that list,
an allowlist of services, and each device's own limits before anything is sent. Success is only
spoken after Home Assistant accepts every call. The token is read per use from Hermes and is never
stored, logged or spoken.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Hermes' own Home Assistant integration reads these (tools/homeassistant_tool.py).
URL_ENV = "HASS_URL"
TOKEN_ENV = "HASS_TOKEN"
DEFAULT_URL = "http://homeassistant.local:8123"

# What the fast path may touch, and how. Security devices are never offered.
SERVICES: dict[str, set[str]] = {
    "light": {"turn_on", "turn_off", "toggle"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "fan": {"turn_on", "turn_off", "toggle", "set_percentage", "oscillate"},
    "climate": {"set_temperature", "set_hvac_mode", "turn_on", "turn_off"},
    "media_player": {"media_play", "media_pause", "media_play_pause", "volume_set", "volume_mute",
                     "turn_on", "turn_off", "media_next_track", "media_previous_track"},
    "cover": {"open_cover", "close_cover", "stop_cover", "set_cover_position"},
    "scene": {"turn_on"},
}
DOMAINS = tuple(SERVICES)
# On by default when the device list is first built; the rest are listed but left unticked.
DEFAULT_DOMAINS = {"light", "climate", "fan"}
# Garage doors and gates are covers too: never offered, whatever their domain.
SECURITY_CLASSES = {"garage", "gate", "door", "lock"}
DATA_KEYS = {
    "light": {"brightness_pct", "color_temp_kelvin", "color_name", "rgb_color", "transition"},
    "fan": {"percentage", "oscillating"},
    "climate": {"temperature", "hvac_mode", "target_temp_low", "target_temp_high"},
    "media_player": {"volume_level", "is_volume_muted"},
    "cover": {"position"},
}
MAX_DEVICES = 150
MAX_CALLS = 8
CACHE_S = 30.0
TIMEOUT_S = 4.0
PLAN_TIMEOUT_S = 4.0
MAX_REQUEST = 300

# Hand these to Hermes without asking the planner: timing, conditions, routines and memory.
TIMING = re.compile(
    r"(?i)\b(?:at \d+|at (?:noon|midnight)|in \d+ (?:mins?|minutes?|hours?|seconds?)|in (?:a|an|half an) (?:minute|hour)|"
    r"tonight|tomorrow|every |each (?:day|night|morning)|when (?:i|we|it|the)|whenever|if (?:i|we|it|nobody|no one|the)|"
    r"unless|until|after (?:i|we|sunset|sunrise)|before (?:i|we|bed)|remind|schedule|automation|routine|"
    r"sunset|sunrise|later)\b")
# Words that make a request worth asking the planner about (plus the user's own device and area names).
HOME_WORDS = re.compile(
    r"(?i)\b(?:lights?|lamps?|bulbs?|dim|brighter|brightness|thermostats?|temperature|heat(?:ing)?|cool(?:ing)?|"
    r"a/?c|air ?con(?:ditioning|ditioner)?|hvac|degrees?|fans?|switch(?:es)?|plugs?|blinds?|shades?|curtains?|"
    r"volume|music|speakers?|tv|scene|warmer|colder|cooler|hotter)\b")


# A short request that only makes sense after a home command: "turn them back on", "a bit dimmer".
FOLLOW_ON = re.compile(r"(?i)\b(?:them|those|these|it|that|again|back on|back off|on|off|more|less|bit|little|"
                       r"brighter|dimmer|warmer|cooler|up|down|percent|half|all the way)\b")


def distinctive_words(devices: list["Device"]) -> set[str]:
    """Words that pick out a device on their own: in a device name, 4+ letters, not a room or kind
    word shared by many devices, and not generic."""
    counts: dict[str, int] = {}
    for d in devices:
        for w in set(re.findall(r"[a-z0-9]+", d.name.lower())):
            counts[w] = counts.get(w, 0) + 1
    limit = max(2, len(devices) // 4)
    return {w for w, n in counts.items() if len(w) >= 4 and n <= limit and w not in GENERIC and w not in KIND_WORDS}


class HomeAssistantError(Exception):
    def __init__(self, kind: str, status: int | None = None, sent: bool = False):
        super().__init__(kind)
        self.kind, self.status, self.sent = kind, status, sent


def credentials(hermes_home: Path) -> tuple[str, str] | None:
    """(url, token) from this profile, the way Hermes' Home Assistant tool reads them; None when the
    token is missing. Values are never logged."""
    from .settings import hermes_secret
    token = hermes_secret(hermes_home, TOKEN_ENV)
    if not token:
        return None
    url = (hermes_secret(hermes_home, URL_ENV) or DEFAULT_URL).rstrip("/")
    if not re.match(r"^https?://[^\s/]+", url):
        return None
    return url, token


class HomeAssistantClient:
    """Minimal REST client: read states and config, call one checked service."""

    def __init__(self, url: str, token: str, timeout: float = TIMEOUT_S):
        self._url, self._token, self._timeout = url.rstrip("/"), token, timeout

    def __repr__(self) -> str:  # never expose the token
        return "HomeAssistantClient(<configured>)"

    def _request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self._url + "/api/" + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read(8 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise HomeAssistantError("auth" if exc.code in (401, 403) else "http", status=exc.code) from None
        except (TimeoutError, OSError, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(reason)
            raise HomeAssistantError("timeout" if timed_out else "unreachable", sent=timed_out and method == "POST") from None
        try:
            return json.loads(raw or b"null")
        except ValueError:
            raise HomeAssistantError("http", status=200) from None

    def states(self) -> list[dict[str, Any]]:
        data = self._request("GET", "states")
        return data if isinstance(data, list) else []

    def config(self) -> dict[str, Any]:
        data = self._request("GET", "config")
        return data if isinstance(data, dict) else {}

    def call_service(self, domain: str, service: str, data: dict[str, Any]) -> Any:
        return self._request("POST", f"services/{domain}/{service}", data)


@dataclasses.dataclass(frozen=True)
class Device:
    entity_id: str
    name: str
    state: str
    attrs: dict[str, Any]

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    def line(self) -> str:
        """One line of the planner's device list: id | name | state (+ what matters for control)."""
        a = self.attrs
        extra = ""
        if self.domain == "climate":
            modes = ",".join(str(m) for m in a.get("hvac_modes") or [])
            extra = f" | set to {a.get('temperature')} | modes {modes} | range {a.get('min_temp')}-{a.get('max_temp')}"
        elif self.domain == "light" and self.state == "on" and a.get("brightness") is not None:
            extra = f" | brightness {round(int(a['brightness']) / 2.55)}%"
        elif self.domain == "cover" and a.get("current_position") is not None:
            extra = f" | position {a.get('current_position')}"
        return f"{self.entity_id} | {self.name} | {self.state}{extra}"


def offerable(state: dict[str, Any]) -> bool:
    """A device the fast path may ever touch: an allowed kind, and never a security device."""
    entity_id = str(state.get("entity_id") or "")
    domain = entity_id.split(".", 1)[0]
    if domain not in SERVICES or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity_id):
        return False
    attrs = state.get("attributes") or {}
    if str(attrs.get("device_class") or "").lower() in SECURITY_CLASSES:
        return False
    return str(state.get("state")) != "unavailable" or domain == "scene"


def device_from_state(state: dict[str, Any]) -> Device:
    attrs = state.get("attributes") or {}
    name = str(attrs.get("friendly_name") or state["entity_id"]).strip()[:80]
    return Device(state["entity_id"], name, str(state.get("state")), dict(attrs))


def default_selection(states: list[dict[str, Any]]) -> list[str]:
    """What is ticked when home control is first turned on: lights, thermostats and fans."""
    out = []
    for s in states:
        if not offerable(s) or s["entity_id"].split(".", 1)[0] not in DEFAULT_DOMAINS:
            continue
        attrs = s.get("attributes") or {}
        # Beds, water heaters and the like show up as "climate" too (often heat_cool only); a
        # thermostat can heat or cool.
        if s["entity_id"].startswith("climate.") and not set(attrs.get("hvac_modes") or []) & {"heat", "cool"}:
            continue
        out.append(s["entity_id"])
    return sorted(out)[:MAX_DEVICES]


# -- planning ---------------------------------------------------------------------------------------

PLANNER_RULES = """You turn one spoken home request into Home Assistant service calls.
- Use ONLY entity_ids from the device list. Never invent one.
- Follow exclusions exactly ("except the bedroom" means leave every bedroom device alone).
- "Thermostat", "heat", "AC" mean HVAC climate devices only. A bed, mattress or water heater is never a thermostat.
- Temperatures: give the number the user said and its unit ("F" or "C"; if they gave no unit, use {unit}). Never convert.
- Brightness is brightness_pct 0-100. Fan speed is percentage 0-100. Volume is volume_level 0-1.
- "Off"/"on" for a room means that room's devices of the kind asked; "the lights" with no room means every light in the list.
- A question about the devices ("is the bedroom light on?", "what's the den set to?"): answer it from the list's
  current states with {{"answer": "one short sentence"}} and no calls.
- All or nothing: never do part of a request.
- Needs timing, a condition, a routine or scene you'd have to invent, or anything but direct device commands:
  return {{"handoff": true}}.
{ask_rule}
Reply with JSON only: {{"calls": [{{"service": "light.turn_off", "entity_id": ["light.x"], "data": {{}}}}], "say": "short past-tense confirmation, e.g. Kitchen lights are off."}}
or {{"answer": "The den is set to 72."}}{ask_shape} or {{"handoff": true}}."""

ASK_RULE = """- A named room means all of that room's devices of the kind asked ("dim the kitchen" = every kitchen light); never
  ask which one within a room.
- Unclear only about WHICH listed device(s) (e.g. "the thermostat" with several thermostats and no room) or a
  missing value ("dim the kitchen" -> "How dim?"): ask ONE short spoken question instead, naming up to three choices by their
  plain names: {{"ask": "Den, bedroom, or office?"}}. Never ask to confirm, never ask about timing or scenes, and
  never ask when the request is already clear enough to do.
- Otherwise unclear: {{"handoff": true}}."""
NO_ASK_RULE = """- This is their answer to a question you already asked. If it is still unclear, return {{"handoff": true}};
  do not ask again. If the answer is really a new home request, plan that instead."""


@dataclasses.dataclass(frozen=True)
class Call:
    domain: str
    service: str
    entity_ids: tuple[str, ...]
    data: dict[str, Any]


@dataclasses.dataclass(frozen=True)
class Plan:
    calls: tuple[Call, ...]
    say: str
    ask: bool = False  # say is a clarifying question; nothing runs


@dataclasses.dataclass(frozen=True)
class Reply:
    ok: bool
    spoken: str
    ask: bool = False  # spoken is a question for the user; no device moved
    touched: tuple[str, ...] = dataclasses.field(default=(), compare=False)  # entity ids it moved (the card's device buttons)


TOGGLEABLE = {"light", "switch", "fan"}  # what a card's on/off button may flip
MAX_CARD_DEVICES = 4


def device_view(device: Device) -> dict[str, Any]:
    """A home card for one device, with an on/off button (docs/API.md "Card actions")."""
    on = device.state == "on"
    row: dict[str, Any] = {"name": device.name[:60], "kind": device.domain, "on": on, "state": device.state,
                           "entity_id": device.entity_id}
    action: dict[str, Any] = {"id": "toggle", "label": "Turn off" if on else "Turn on", "style": "primary",
                              "confirm": False}
    if device.domain == "light":
        if on and device.attrs.get("brightness") is not None:
            row["level"] = round(int(device.attrs["brightness"]) / 2.55)
        modes = device.attrs.get("supported_color_modes") or []
        if any(m != "onoff" for m in modes) or device.attrs.get("brightness") is not None:
            action["value"] = "brightness"  # the button takes a 0-100 value too
    return {"kind": "home", "title": device.name[:60], "devices": [row], "actions": [action]}


def planner_messages(request: str, devices: list[Device], unit: str, notes: list[str] | None = None,
                     may_ask: bool = True) -> list[dict[str, str]]:
    listing = "\n".join(d.line() for d in devices)
    rules = PLANNER_RULES.format(unit=unit, ask_rule=(ASK_RULE if may_ask else NO_ASK_RULE).format(),
                                 ask_shape=' or {{"ask": "Den or bedroom?"}}'.format() if may_ask else "")
    if notes:
        rules += ("\n\nEarlier on this call they answered these; apply them the same way unless they say otherwise:\n"
                  + "\n".join(f"- {n}" for n in notes[-MAX_NOTES:]))
    return [{"role": "system", "content": rules + "\n\nDevices (entity_id | name | state):\n" + listing},
            {"role": "user", "content": request}]


MAX_NOTES = 5
ASK_WINDOW_S = 120  # an answer after this long is treated as a new request


def _json_object(text: Any) -> dict[str, Any] | None:
    if not isinstance(text, str):
        return None
    text = text.strip()
    fence = re.search(r"\{.*\}", text, re.S)
    if not fence:
        return None
    try:
        data = json.loads(fence.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9 * 2) / 2  # nearest half degree, the way thermostats step


def c_to_f(c: float) -> float:
    return round(c * 9 / 5 + 32)


EXCEPT = re.compile(r"(?i)\b(?:except(?: for)?|but not|other than|besides|apart from|excluding)\s+(.+?)(?:,|;|\band\b|\bthen\b|$)")
GENERIC = {"the", "my", "a", "an", "all", "ones", "one", "light", "lights", "fan", "fans", "thermostat", "thermostats",
           "in", "of", "room", "stuff", "things", "those", "that", "this", "it"}


KIND_WORDS = {"light": "light", "lights": "light", "lamp": "light", "lamps": "light", "fan": "fan", "fans": "fan",
              "thermostat": "climate", "thermostats": "climate", "heat": "climate", "ac": "climate",
              "blinds": "cover", "shades": "cover", "speaker": "media_player", "speakers": "media_player", "tv": "media_player"}


def exclusions(request: str) -> list[tuple[set[str], set[str]]]:
    """(excluded words, domains that clause is about; empty = any) per "except ..." in the request."""
    out = []
    for m in EXCEPT.finditer(request or ""):
        clause = re.split(r",|;|\band\b|\bthen\b", request[:m.start()])[-1]
        domains = {KIND_WORDS[w] for w in re.findall(r"[a-z]+", clause.lower()) if w in KIND_WORDS}
        words = excluded_words(m.group(0))
        if words:
            out.append((words, domains))
    return out


def excluded_words(request: str) -> set[str]:
    """Words naming what the user said to leave alone ("except the bedroom lamps" -> bedroom, lamp)."""
    words: set[str] = set()
    for m in EXCEPT.finditer(request or ""):
        for w in re.findall(r"[a-z0-9']+", m.group(1).lower()):
            w = w.removesuffix("'s")
            w = w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
            if w not in GENERIC and len(w) >= 3:
                words.add(w)
    return words


def breaks_exclusion(plan: "Plan", request: str, by_id: dict[str, Device]) -> bool:
    """True when a call touches a device whose name or id carries a word the user excluded. Checked in
    code because planners get exclusions wrong; a false alarm only costs a trip to Hermes."""
    rules = exclusions(request)
    for call in plan.calls:
        words = set().union(*[w for w, domains in rules if not domains or call.domain in domains])
        if not words:
            continue
        for eid in call.entity_ids:
            d = by_id.get(eid)
            hay = set(re.findall(r"[a-z0-9]+", f"{d.name if d else ''} {eid}".lower()))
            hay |= {h[:-1] for h in hay if len(h) > 3 and h.endswith("s")}
            if words & hay:
                return True
    return False


def parse_plan(text: Any, by_id: dict[str, Device], unit: str, may_ask: bool = False) -> Plan | None:
    """The planner's JSON, checked against the chosen devices; None = hand to Hermes."""
    data = _json_object(text)
    if not data or data.get("handoff"):
        return None
    ask = data.get("ask")
    say_raw = data.get("say")
    if not (isinstance(ask, str) and ask.strip()) and data.get("calls") and isinstance(say_raw, str) and "?" in say_raw:
        # Did part and asked about the rest. All or nothing: run none of it, just ask; the answer
        # re-plans the whole request.
        asked = [q.strip() for q in re.findall(r"[^.?!]*\?", say_raw) if q.strip()]
        ask, data = (asked[-1] if asked else ""), {}
    if isinstance(ask, str) and ask.strip() and not data.get("calls"):
        question = " ".join(ask.split())
        if not may_ask or len(question) > 160 or not question.endswith("?"):
            return None  # asked twice, or not a real short question: Hermes takes it
        return Plan((), question, ask=True)
    answer = data.get("answer")
    if isinstance(answer, str) and answer.strip() and not data.get("calls"):
        if "?" in answer:
            return None  # a question dressed as an answer
        return Plan((), " ".join(answer.split())[:200])  # a question, answered from current states
    if not isinstance(data.get("calls"), list) or not data["calls"]:
        return None
    calls: list[Call] = []
    for raw in data["calls"][:MAX_CALLS + 1]:
        call = check_call(raw, by_id, unit)
        if call is None:
            return None  # one bad step: the whole request goes to Hermes, never half of it
        calls.append(call)
    if len(calls) > MAX_CALLS:
        return None
    say = data.get("say")
    say = " ".join(say.split())[:200] if isinstance(say, str) and say.strip() else "Done."
    if "?" in say:
        return None  # did part and asked about the rest: all or nothing, so Hermes takes it
    return Plan(tuple(calls), say)


def check_call(raw: Any, by_id: dict[str, Device], unit: str) -> Call | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("service"), str):
        return None
    domain, _, service = raw["service"].partition(".")
    if service not in SERVICES.get(domain, set()):
        return None
    ids = raw.get("entity_id")
    ids = [ids] if isinstance(ids, str) else ids
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
        return None
    if any(i not in by_id or by_id[i].domain != domain for i in ids):
        return None
    data = raw.get("data") or {}
    if not isinstance(data, dict) or not set(data) <= DATA_KEYS.get(domain, set()) | {"unit"}:
        return None
    data = dict(data)
    said_unit = str(data.pop("unit", "") or "").upper()[:1]
    if domain == "light" and "brightness_pct" in data:
        if not _num(data["brightness_pct"], 0, 100):
            return None
    if domain == "fan" and "percentage" in data and not _num(data["percentage"], 0, 100):
        return None
    if domain == "media_player" and "volume_level" in data and not _num(data["volume_level"], 0, 1):
        return None
    if domain == "cover" and "position" in data and not _num(data["position"], 0, 100):
        return None
    if domain == "climate":
        for key in ("temperature", "target_temp_low", "target_temp_high"):
            if key in data:
                value = data[key]
                if not _num(value, -50, 150):
                    return None
                # The user's number, in the home's unit. No unit said: in a Celsius home a number
                # above 45 can only be Fahrenheit (people say "72" either way); else the home's unit.
                if not said_unit:
                    said_unit = "F" if unit == "C" and float(value) > 45 else unit
                if said_unit == "F" and unit == "C":
                    value = f_to_c(float(value))
                elif said_unit == "C" and unit == "F":
                    value = c_to_f(float(value))
                for i in ids:
                    lo, hi = by_id[i].attrs.get("min_temp"), by_id[i].attrs.get("max_temp")
                    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and not lo <= value <= hi:
                        return None
                data[key] = value
        if service == "set_hvac_mode":
            mode = data.get("hvac_mode")
            if not all(mode in (by_id[i].attrs.get("hvac_modes") or []) for i in ids):
                return None
        if service == "set_temperature" and not data:
            return None
    return Call(domain, service, tuple(ids), data)


def _num(value: Any, lo: float, hi: float) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and lo <= value <= hi


# -- the controller ----------------------------------------------------------------------------------

@dataclasses.dataclass
class Snapshot:
    at: float
    states: list[dict[str, Any]]
    unit: str


class HomeControl:
    """Checks, plans and runs home requests for one Hermes profile. Stateless apart from a short cache."""

    def __init__(self, settings_fn: Callable[[], dict[str, Any]], creds_fn: Callable[[], tuple[str, str] | None],
                 plan_call: Callable[[list[dict[str, str]]], str | None] | None = None,
                 client_factory: Callable[[str, str], Any] = HomeAssistantClient):
        self._settings_fn, self._creds_fn = settings_fn, creds_fn
        self._plan_call = plan_call
        self._client_factory = client_factory
        self._lock = threading.Lock()
        self._snap: Snapshot | None = None

    # settings
    def _cfg(self) -> dict[str, Any]:
        return self._settings_fn().get("home_control") or {}

    @property
    def enabled(self) -> bool:
        return bool(self._cfg().get("enabled"))

    def client(self) -> Any:
        creds = self._creds_fn()
        if creds is None:
            raise HomeAssistantError("not_set_up")
        return self._client_factory(*creds)

    def snapshot(self, fresh: bool = False) -> Snapshot:
        with self._lock:
            if not fresh and self._snap and time.monotonic() - self._snap.at < CACHE_S:
                return self._snap
        client = self.client()
        states = client.states()
        unit = "C"
        try:
            temp = str((client.config().get("unit_system") or {}).get("temperature") or "")
            unit = "F" if "F" in temp.upper() else "C"
        except HomeAssistantError:
            pass
        snap = Snapshot(time.monotonic(), states, unit)
        with self._lock:
            self._snap = snap
        return snap

    def detect(self) -> dict[str, Any]:
        """Is Home Assistant set up in Hermes and answering? Never raises; never returns the token."""
        if self._creds_fn() is None:
            return {"available": False, "reason": "Hermes doesn't have Home Assistant set up (HASS_TOKEN)."}
        try:
            snap = self.snapshot(fresh=True)
        except HomeAssistantError as exc:
            return {"available": False, "reason": _problem(exc)}
        return {"available": True, "reason": "", "offerable": sum(1 for s in snap.states if offerable(s)),
                "unit": snap.unit}

    def overview(self) -> dict[str, Any]:
        """Everything Settings shows: whether it's on, and every device it could use, ticked or not."""
        cfg = self._cfg()
        found = self.detect()
        out: dict[str, Any] = {"enabled": bool(cfg.get("enabled")), "configured": cfg.get("entities") is not None,
                               **found, "devices": []}
        if not found["available"]:
            return out
        chosen = cfg.get("entities")
        snap = self.snapshot()
        selected = set(chosen if chosen is not None else default_selection(snap.states))
        devices = [device_from_state(s) for s in snap.states if offerable(s)]
        devices.sort(key=lambda d: (DOMAINS.index(d.domain), d.name.lower()))
        out["devices"] = [{"entity_id": d.entity_id, "name": d.name, "kind": d.domain, "state": d.state,
                           "included": d.entity_id in selected} for d in devices[:400]]
        out["missing"] = sorted(selected - {d.entity_id for d in devices})
        return out

    def chosen(self, snap: Snapshot) -> list[Device]:
        chosen = self._cfg().get("entities")
        selected = set(chosen if chosen is not None else default_selection(snap.states))
        return [device_from_state(s) for s in snap.states if s.get("entity_id") in selected and offerable(s)][:MAX_DEVICES]

    # the call path
    def wants(self, request: str, devices: list[Device], following: bool = False) -> bool:
        """Worth asking the planner: mentions home things, a chosen device by name or by the
        distinctive word of its name ("the sconces" for "Hallway Brass Sconces"), or, right after
        a home command, a short follow-up that leans on it ("turn them back on", "a bit more"). No timing."""
        text = (request or "").strip()
        if not text or len(text) > MAX_REQUEST or TIMING.search(text):
            return False
        if HOME_WORDS.search(text):
            return True
        low = text.lower()
        if any(len(d.name) >= 3 and re.search(r"\b" + re.escape(d.name.lower()) + r"\b", low) for d in devices):
            return True
        said = set(re.findall(r"[a-z0-9]+", low))
        if said & distinctive_words(devices):
            return True
        return following and len(said) <= 8 and bool(FOLLOW_ON.search(text))

    def respond(self, request: str, notes: list[str] | None = None, answering: tuple[str, str] | None = None,
                following: bool = False) -> Reply | None:
        """The spoken result, or None to hand the request to Hermes unchanged.

        ``notes``: answers given earlier on this call. ``answering``: (original request, question) when
        this request answers a question just asked; the planner may not ask a second time."""
        if not self.enabled or self._plan_call is None:
            return None
        try:
            snap = self.snapshot()
        except HomeAssistantError as exc:
            logger.info("speakeasy: home control unavailable (%s); using Hermes", exc.kind)
            return None
        devices = self.chosen(snap)
        if not devices:
            return None
        if answering:
            original, question = answering
            if TIMING.search(request or "") or len(request or "") > MAX_REQUEST:
                return None
            request = (f"{original}\n(I asked: {question} They answered: {request}. The answer only fills in what I "
                       "asked; keep every other part of the original request, including which devices, unchanged.)")
        elif not self.wants(request, devices, following=following):
            return None
        may_ask = not answering
        started = time.monotonic()
        try:
            text = self._plan_call(planner_messages(request, devices, snap.unit, notes, may_ask))
        except Exception as exc:
            logger.info("speakeasy: home planner unavailable (%s); using Hermes", type(exc).__name__)
            return None
        plan = parse_plan(text, {d.entity_id: d for d in devices}, snap.unit, may_ask)
        planned_ms = int((time.monotonic() - started) * 1000)
        if plan is None:
            logger.info("speakeasy: home request handed to Hermes after %d ms", planned_ms)
            return None
        if plan.ask:
            logger.info("speakeasy: home control asked which one after %d ms", planned_ms)
            return Reply(True, plan.say, ask=True)
        if breaks_exclusion(plan, request.split("\n(I asked:")[0], {d.entity_id: d for d in devices}):
            logger.info("speakeasy: home plan touched an excluded device; using Hermes")
            return None
        reply = self.run(plan)
        logger.info("speakeasy: home control %s in %d ms (%d calls)", "done" if reply.ok else "failed",
                    int((time.monotonic() - started) * 1000), len(plan.calls))
        return reply

    def run(self, plan: Plan) -> Reply:
        if not plan.calls:
            return Reply(True, plan.say)
        client = self.client()
        done = 0
        touched = tuple(dict.fromkeys(i for call in plan.calls for i in call.entity_ids))
        for call in plan.calls:
            try:
                client.call_service(call.domain, call.service, {"entity_id": list(call.entity_ids), **call.data})
            except HomeAssistantError as exc:
                with self._lock:
                    self._snap = None
                if done == 0 and not exc.sent:
                    return Reply(False, f"I couldn't reach your home: {_problem(exc)} Nothing changed.")
                return Reply(False, f"Only part of that went through: {_problem(exc)} Check the app to see what changed.")
            done += 1
        with self._lock:
            self._snap = None  # states changed: re-read next time
        return Reply(True, plan.say, touched=touched)

    # cards
    def device_views(self, entity_ids: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
        """Cards with on/off buttons for the devices a command moved (lights, switches, fans), as
        they are now. Empty when there are none or Home Assistant can't be read."""
        try:
            snap = self.snapshot(fresh=True)
        except HomeAssistantError:
            return []
        by_id = {d.entity_id: d for d in self.chosen(snap)}
        devices = [by_id[i] for i in entity_ids if i in by_id and by_id[i].domain in TOGGLEABLE]
        return [device_view(d) for d in devices[:MAX_CARD_DEVICES]]

    def toggle(self, entity_id: str, value: Any = None,
               want_on: bool | None = None) -> tuple[Reply, dict[str, Any] | None]:
        """A card's on/off button: flip the device, or with ``value`` (0-100) set a light's brightness.
        Only a device home control may touch. Returns the reply and the device's card as it is now.

        ``want_on`` is what the pressed button said ("Turn off" means False). A card can be stale
        (the device moved since, by voice, another app, or the same card tapped on a second device),
        and a blind flip would then do the opposite of its label; a device already in that state is
        left alone and the card is refreshed."""
        if not self.enabled:
            return Reply(False, "Home control is off in Speakeasy settings."), None
        try:
            snap = self.snapshot(fresh=True)
        except HomeAssistantError as exc:
            return Reply(False, _problem(exc)), None
        device = next((d for d in self.chosen(snap) if d.entity_id == entity_id), None)
        if device is None or device.domain not in TOGGLEABLE:
            return Reply(False, "That device isn't one Speakeasy can switch."), None
        name = device.name
        verb = "are" if re.search(r"(?i)[^s]s$", name) else "is"  # "Kitchen Lights are on"
        if value is not None and device.domain == "light":
            if not _num(value, 0, 100):
                return Reply(False, "Brightness must be 0 to 100."), None
            plan = Plan((Call("light", "turn_on" if value > 0 else "turn_off", (entity_id,),
                              {"brightness_pct": int(value)} if value > 0 else {}),),
                        f"{name} {verb} at {int(value)}%." if value > 0 else f"{name} {verb} off.")
        else:
            on = device.state == "on"
            if want_on is not None and on == want_on:
                views = self.device_views((entity_id,))
                return Reply(True, f"{name} {verb} already {'on' if on else 'off'}."), (views[0] if views else None)
            plan = Plan((Call(device.domain, "turn_off" if on else "turn_on", (entity_id,), {}),),
                        f"{name} {verb} {'off' if on else 'on'}.")
        reply = self.run(plan)
        card = None
        if reply.ok:
            views = self.device_views((entity_id,))
            card = views[0] if views else None
        return reply, card


def _problem(exc: HomeAssistantError) -> str:
    return {
        "not_set_up": "Home Assistant isn't set up in Hermes.",
        "auth": "Home Assistant refused Hermes' token.",
        "timeout": "Home Assistant didn't answer in time.",
        "unreachable": "Home Assistant couldn't be reached.",
    }.get(exc.kind, f"Home Assistant returned an error ({exc.status}).")
