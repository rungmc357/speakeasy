"""Instant answers computed on this machine: clock, date and arithmetic. No search, no model.

"What time is it in Nairobi", "what's the date", "what's 18 percent of 240", "what's 12 times 7".
Each returns a finished spoken sentence, or None when the question isn't one of these.
"""
from __future__ import annotations

import ast
import json
import logging
import operator
import re
import urllib.parse
import urllib.request
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable
from zoneinfo import ZoneInfo, available_timezones

logger = logging.getLogger(__name__)

GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
TIMEOUT_S = 2.0
_LEAD = r"(?i)^\s*(?:(?:hey|so|ok|okay|um|uh|quick question|quickly)[,\s]+)*(?:(?:can|could) you tell me\s+|do you know\s+)?"
TIME_IN = re.compile(_LEAD + r"what(?:'s|s| is)? (?:the )?(?:time|current time)(?: is it)?(?: right now| now)?"
                     r"(?: (?:in|over in|out in|for) (?P<place>[a-z .'\-]+?))?(?: right now| now| today)?[?.!]*\s*$")
DATE = re.compile(_LEAD + r"(?:what(?:'s|s| is) (?:the )?(?:date|day)(?: is it)?(?: today)?|what(?:'s|s| is) today(?:'s date)?|"
                  r"what day is (?:it|today)(?: today)?|what's today)[?.!]*\s*$")
PERCENT = re.compile(_LEAD + r"(?:what(?:'s|s| is)\s+)?(?P<p>[\d.,]+)\s*(?:percent|%)\s+of\s+(?P<n>[\d.,]+)[?.!]*\s*$")
ARITH = re.compile(_LEAD + r"(?:what(?:'s|s| is)|calculate|compute)\s+(?P<expr>[\d.,\s()+\-*/x^]+?|.*?(?:plus|minus|times|"
                   r"multiplied by|divided by|over|squared|cubed|to the power of).*?)[?.!]*\s*$")
WORDS = [(r"\bmultiplied by\b", "*"), (r"\bdivided by\b", "/"), (r"\bto the power of\b", "**"), (r"\btimes\b", "*"),
         (r"\bplus\b", "+"), (r"\bminus\b", "-"), (r"\bover\b", "/"), (r"\bsquared\b", "**2"), (r"\bcubed\b", "**3"),
         (r"(?<=\d)\s*x\s*(?=\d)", "*"), (r"\^", "**"), (r"(?<=\d),(?=\d{3})", "")]
OPS: dict[type, Callable[..., Any]] = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
                                     ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
                                     ast.UAdd: operator.pos}
Fetch = Callable[[str], Any]


def _get(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.7.1", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as response:
        return json.loads(response.read(500_000) or b"{}")


def number(value: float) -> str:
    if abs(value - round(value)) < 1e-9 and abs(value) < 1e15:
        return f"{int(round(value)):,}"
    text = f"{value:,.4f}".rstrip("0").rstrip(".")
    return text


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 12 or abs(left) > 1e6):
            raise ValueError("too big")
        return OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPS:
        return OPS[type(node.op)](_eval(node.operand))
    raise ValueError("not arithmetic")


def arithmetic(question: str) -> str | None:
    m = PERCENT.match(question)
    if m:
        try:
            p, n = float(m["p"].replace(",", "")), float(m["n"].replace(",", ""))
        except ValueError:
            return None
        return f"{number(p)} percent of {number(n)} is {number(p * n / 100)}."
    m = ARITH.match(question)
    if not m:
        return None
    expr = m["expr"].lower()
    for pattern, symbol in WORDS:
        expr = re.sub(pattern, symbol, expr)
    if not re.fullmatch(r"[\d.\s()+\-*/]+", expr) or not re.search(r"\d\s*(?:\*\*|[+\-*/])\s*[\d(]", expr):
        return None
    try:
        value = _eval(ast.parse(expr.strip(), mode="eval"))
    except (ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        return None
    return f"That's {number(value)}."


@lru_cache(maxsize=1)
def _zone_names() -> dict[str, str]:
    out: dict[str, str] = {}
    for name in available_timezones():
        if "/" in name and not name.startswith(("Etc/", "SystemV/", "posix/", "right/")):
            out.setdefault(name.rsplit("/", 1)[1].replace("_", " ").lower(), name)
    return out


def zone_for(place: str, fetch: Fetch = _get) -> tuple[str, str] | None:
    """(IANA zone, display name) for a city or country, from the zone list then Open-Meteo geocoding."""
    place = re.sub(r"(?i)^the\s+", "", place.strip(" .?!")).strip()
    if not place:
        return None
    hit = _zone_names().get(place.lower())
    if hit:
        return hit, place.title()
    try:
        data = fetch(f"{GEOCODE}?{urllib.parse.urlencode({'name': place, 'count': 1})}")
    except Exception as exc:
        logger.info("speakeasy: place lookup failed (%s)", type(exc).__name__)
        return None
    results = data.get("results") or []
    if not results or not results[0].get("timezone"):
        return None
    r = results[0]
    return r["timezone"], r.get("name") or place.title()


def _clock(when: datetime) -> str:
    return when.strftime("%-I:%M %p")


def clock(question: str, now: datetime | None = None, fetch: Fetch = _get) -> str | None:
    m = TIME_IN.match(question)
    if m:
        local = (now or datetime.now()).astimezone()
        place = (m["place"] or "").strip()
        if not place or place in {"here", "my time"}:
            return f"It's {_clock(local)}."
        zone = zone_for(place, fetch)
        if zone is None:
            return None
        there = local.astimezone(ZoneInfo(zone[0]))
        diff = (there.utcoffset() - local.utcoffset()).total_seconds() / 3600
        if abs(diff) < 0.01:
            rel = "the same time as you"
        else:
            hours = number(abs(diff))
            rel = f"{hours} hour{'s' if hours != '1' else ''} {'ahead of' if diff > 0 else 'behind'} you"
        day = "" if there.date() == local.date() else (" tomorrow" if there.date() > local.date() else " yesterday")
        return f"It's {_clock(there)}{day}, {there:%A}, in {zone[1]}, {rel}."
    if DATE.match(question):
        local = (now or datetime.now()).astimezone()
        return f"It's {local:%A, %B} {local.day}."
    return None


def view(question: str, now: datetime | None = None, fetch: Fetch = _get) -> dict[str, Any] | None:
    """The card for an instant answer: a clock pair (there + here) or the arithmetic."""
    text = (question or "").strip()
    m = TIME_IN.match(text)
    if m:
        local = (now or datetime.now()).astimezone()
        place = (m["place"] or "").strip()
        here = {"place": "Here", "time": _clock(local), "day": f"{local:%A}", "zone": local.tzname() or "", "here": True}
        if not place or place in {"here", "my time"}:
            return {"kind": "clock", "zones": [here]}
        zone = zone_for(place, fetch)
        if zone is None:
            return None
        there = local.astimezone(ZoneInfo(zone[0]))
        diff = (there.utcoffset() - local.utcoffset()).total_seconds() / 3600
        offset = "same time" if abs(diff) < 0.01 else f"{'+' if diff > 0 else '−'}{number(abs(diff))}h"
        return {"kind": "clock", "zones": [{"place": zone[1], "time": _clock(there), "day": f"{there:%A}",
                                            "offset": offset, "zone": there.tzname() or ""}, here]}
    spoken = arithmetic(text)
    if spoken:
        m2 = PERCENT.match(text)
        expr = f"{m2['p']}% of {m2['n']}" if m2 else (ARITH.match(text)["expr"].strip() if ARITH.match(text) else text)
        result = spoken.replace("That's ", "").rstrip(".").split(" is ")[-1]
        return {"kind": "math", "expression": expr, "result": result}
    return None


def answer(question: str, now: datetime | None = None, fetch: Fetch = _get) -> str | None:
    text = (question or "").strip()
    if not text or len(text) > 160:
        return None
    return clock(text, now, fetch) or arithmetic(text)
