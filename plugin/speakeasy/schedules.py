"""Hermes scheduled jobs (cron), seen and managed from the apps (docs/API.md "Schedules";
capability ``schedules``).

Everything goes through Hermes' own cron API, scoped to this profile's HERMES_HOME:

* reads: ``cron.jobs.list_jobs`` / ``get_job`` (the job store), ``cron.executions.list_executions``
  (one ledger row per fire), ``SessionDB.list_cron_job_runs`` (each run's session, so a run opens in
  its session page) and the run's output document (``cron/output/<job>/<time>.md``) for its result;
* writes: ``tools.cronjob_tools.cronjob`` (the same entry point ``hermes cron`` and the agent's
  cron tool use: prompt safety scan, schedule parsing, provider notification) for create, update,
  pause, resume and remove; ``cron.jobs.trigger_job`` for Run now (the gateway's ticker fires it on
  its next tick, inside the gateway with the live delivery adapters, the same as a scheduled fire).

Schedules are shown in plain English ("Weekdays at 8:00 AM"). A new schedule is written as a
sentence ("every weekday at 8 summarize my inbox"): ``parse_request`` splits it into when and what,
Hermes' own ``parse_schedule`` validates the when, and the app shows a preview the person confirms
before anything is saved.
"""
from __future__ import annotations

import contextlib
import dataclasses
import logging
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

MAX_PROMPT = 4000
MAX_SCHEDULE = 200
MAX_NAME = 80
RESULT_CHARS = 160         # the list's short result
RUN_RESULT_CHARS = 1200    # one run's result on the detail page
RUNS = 15                  # recent runs on the detail page
DOC_TAIL_BYTES = 96 * 1024  # an output doc's response is at its end; never read a huge prompt
SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

PLATFORM_LABELS = {"discord": "Discord", "telegram": "Telegram", "slack": "Slack", "whatsapp": "WhatsApp",
                   "signal": "Signal", "matrix": "Matrix", "email": "Email", "sms": "SMS", "voice": "Speakeasy",
                   "bot-chat": "Bot Chat", "webhook": "Webhook", "local": "Saved only"}


class ScheduleError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# -- plain English ----------------------------------------------------------------------------------

_DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_DOW_NAMES = {n[:3].lower(): i for i, n in enumerate(_DAY_NAMES)}
_MON_NAMES = {m.lower(): i + 1 for i, m in enumerate(_MONTHS)}


def clock(hour: int, minute: int) -> str:
    """``8:00 AM``, ``12:30 PM``, ``12:00 AM`` (Apple's short time, en-US)."""
    suffix = "AM" if hour < 12 else "PM"
    h = hour % 12 or 12
    return f"{h}:{minute:02d} {suffix}"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _expand(field: str, lo: int, hi: int, names: dict[str, int] | None = None) -> list[int] | None:
    """A cron field's values (``*`` is None). Raises ValueError on anything unexpected."""
    if field == "*":
        return None
    out: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
            if step < 1:
                raise ValueError(field)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _num(x, names), _num(y, names)
        else:
            a = b = _num(part, names)
            if step != 1:
                b = hi
        if a > b:
            raise ValueError(field)
        out.update(range(a, b + 1, step))
    if names is _DOW_NAMES and 7 in out:  # 7 is Sunday too
        out.discard(7)
        out.add(0)
    if not out or min(out) < lo or max(out) > (7 if names is _DOW_NAMES else hi):
        raise ValueError(field)
    return sorted(out)


def _num(token: str, names: dict[str, int] | None) -> int:
    token = token.strip().lower()
    if names and token[:3] in names and not token.isdigit():
        return names[token[:3]]
    return int(token)


def _days_phrase(dows: list[int] | None) -> str:
    if dows is None or sorted(dows) == list(range(7)):
        return "Every day"
    s = sorted(dows)
    if s == [1, 2, 3, 4, 5]:
        return "Weekdays"
    if s == [0, 6]:
        return "Weekends"
    if len(s) >= 3 and s == list(range(s[0], s[-1] + 1)):
        return f"{_DAY_NAMES[s[0]]} to {_DAY_NAMES[s[-1]]}"
    # Monday first (a week reads Mon…Sun).
    ordered = sorted(s, key=lambda d: (d == 0, d))
    return _join([_DAY_NAMES[d] + "s" for d in ordered])


def _times_phrase(hours: list[int], minutes: list[int]) -> str:
    times = [clock(h, m) for h in hours for m in minutes]
    if len(times) <= 4:
        return "at " + _join(times)
    return f"{len(times)} times a day, {times[0]} to {times[-1]}"


def _step(values: list[int] | None, lo: int, hi: int) -> int | None:
    """The step of an evenly spaced full-range field (``*/15`` → 15), else None."""
    if values is None:
        return 1
    if len(values) < 2 or values[0] != lo:
        return None
    step = values[1] - values[0]
    if values == list(range(lo, hi + 1, step)) and step > 1:
        return step
    return None


def describe_cron(expr: str) -> str:
    """A 5-field cron expression in plain English; ``Custom schedule (…)`` when it's unusual."""
    parts = expr.split()
    fallback = f"Custom schedule ({expr.strip()})"
    if len(parts) != 5:
        return fallback
    m_f, h_f, dom_f, mon_f, dow_f = parts
    try:
        minutes = _expand(m_f, 0, 59)
        hours = _expand(h_f, 0, 23)
        doms = _expand(dom_f, 1, 31)
        months = _expand(mon_f, 1, 12, _MON_NAMES)
        dows = _expand(dow_f, 0, 6, _DOW_NAMES)
    except (ValueError, IndexError):
        return fallback
    if doms is not None and dows is not None:
        return fallback  # cron ORs the two; too clever to say simply
    days = _days_phrase(dows)
    if doms is not None and months is None and dows is None and _step(doms, 1, 31) and len(doms) > 2:
        n = _step(doms, 1, 31)
        days = "Every other day" if n == 2 else f"Every {n} days"
        doms = None
    if doms is not None:
        on = "on the " + _join([_ordinal(d) for d in doms])
        if months is None:
            days = f"Monthly {on}"
        elif len(months) == 1 and len(doms) == 1:
            days = f"Every year on {_MONTHS[months[0] - 1]} {doms[0]}"
        else:
            days = f"{on[0].upper()}{on[1:]} of {_join([_MONTHS[m - 1] for m in months])}"
    elif months is not None:
        days = f"{days} in {_join([_MONTHS[m - 1] for m in months])}"
    lower = days if days.startswith(tuple(_DAY_NAMES)) else days[0].lower() + days[1:]

    many = hours is not None and minutes is not None and len(hours) * len(minutes) > 4
    minute_step = _step(minutes, 0, 59) if minutes is not None and len(minutes) > 1 else None
    stepped = many and ((_step(hours, 0, 23) and len(minutes) == 1) or minute_step)
    if minutes is not None and hours is not None and not stepped:
        if many:
            times = [clock(h, m) for h in hours for m in minutes]
            when = f"{len(times)} times a day, {times[0]} to {times[-1]}"
            return when[0].upper() + when[1:] if days == "Every day" else f"{days}, {when}"
        return f"{days} {_times_phrase(hours, minutes)}"
    if hours is None or _step(hours, 0, 23):
        every_h = _step(hours, 0, 23)
        if minutes is None:
            prefix = "Every minute"
        elif _step(minutes, 0, 59) and len(minutes) > 1:
            prefix = f"Every {_step(minutes, 0, 59)} minutes"
        elif len(minutes) == 1:
            unit = "Every hour" if every_h == 1 else f"Every {every_h} hours"
            prefix = f"{unit}, on the hour" if minutes[0] == 0 else f"{unit} at :{minutes[0]:02d}"
            return prefix if dows is None and doms is None and months is None else f"{prefix}, {lower}"
        else:
            return fallback
        if every_h != 1:
            prefix += f", every {every_h} hours"
        return prefix if dows is None and doms is None and months is None else f"{prefix}, {lower}"
    # Minutes repeat within a block of hours ("*/15 9-17 * * 1-5").
    window = f"{clock(hours[0], 0)} to {clock(hours[-1], 59)}" if hours == list(range(hours[0], hours[-1] + 1)) else None
    step = _step(minutes, 0, 59) if minutes is not None else 1
    if window and step:
        what = "Every minute" if step == 1 else f"Every {step} minutes"
        tail = "" if dows is None and doms is None and months is None else f", {lower}"
        return f"{what}, {window}{tail}"
    return fallback


def describe_interval(minutes: int) -> str:
    if minutes <= 1:
        return "Every minute"
    if minutes % 1440 == 0:
        n = minutes // 1440
        return "Every day" if n == 1 else f"Every {n} days"
    if minutes % 60 == 0:
        n = minutes // 60
        return "Every hour" if n == 1 else f"Every {n} hours"
    return f"Every {minutes} minutes"


def describe_once(run_at: str | None, now: datetime | None = None) -> str:
    dt = _parse_iso(run_at)
    if dt is None:
        return "Once"
    now = now or datetime.now(dt.tzinfo)
    when = clock(dt.hour, dt.minute)
    day = dt.date()
    today = now.astimezone(dt.tzinfo).date() if dt.tzinfo else now.date()
    if day == today:
        return f"Once, today at {when}"
    if day == today + timedelta(days=1):
        return f"Once, tomorrow at {when}"
    label = f"{_DAY_NAMES[(dt.weekday() + 1) % 7][:3]}, {_MONTHS[dt.month - 1]} {dt.day}"
    if dt.year != now.year:
        label += f", {dt.year}"
    return f"Once, {label} at {when}"


def describe_schedule(schedule: Any, now: datetime | None = None) -> str:
    """A stored Hermes schedule (``{"kind": "cron"|"interval"|"once", …}``) in plain English."""
    if not isinstance(schedule, dict):
        return "Custom schedule"
    kind = schedule.get("kind")
    if kind == "cron":
        return describe_cron(str(schedule.get("expr") or ""))
    if kind == "interval":
        try:
            return describe_interval(int(schedule.get("minutes") or 0))
        except (TypeError, ValueError):
            return "Custom schedule"
    if kind == "once":
        return describe_once(schedule.get("run_at"), now)
    return f"Custom schedule ({schedule.get('display') or kind})"


def describe_delivery(deliver: Any, origin: Any = None) -> str:
    """Where a job's result goes: ``Telegram``, ``Discord · #garden``, ``Saved only``."""
    raw = deliver if isinstance(deliver, str) else ",".join(map(str, deliver)) if isinstance(deliver, list) else ""
    parts = [p.strip() for p in raw.split(",") if p.strip()] or ["origin" if origin else "local"]
    origin = origin if isinstance(origin, dict) else {}
    labels: list[str] = []
    for part in parts:
        platform = part.split(":", 1)[0].lower()
        if platform == "origin":
            platform = str(origin.get("platform") or "local").lower()
            label = PLATFORM_LABELS.get(platform, platform.replace("_", " ").title())
            chat = str(origin.get("chat_name") or "").strip()
            if chat and platform not in ("local", "voice"):
                chat = chat if chat.startswith(("#", "@")) or platform != "discord" else "#" + chat.split("/")[-1].strip()
                label = f"{label} · {chat[:40]}"
        else:
            label = PLATFORM_LABELS.get(platform, platform.replace("_", " ").replace("-", " ").title())
        if label not in labels:
            labels.append(label)
    if len(labels) > 1 and "Saved only" in labels:
        labels.remove("Saved only")
    return " + ".join(labels)


# -- a sentence -> when + what ----------------------------------------------------------------------

_DOW_WORDS = r"(?:mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|rsday|urday)?s?"
_DOW_MAP = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}
_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "ten": 10,
              "fifteen": 15, "twenty": 20, "thirty": 30, "forty-five": 45, "half an": 0.5, "a half": 0.5}
_TIME = (r"(?P<time>noon|midday|midnight|(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?\s?m\.?|p\.?\s?m\.?|am|pm)?"
         r"(?:\s*o'?clock)?)")
_AT_TIME = re.compile(r"\b(?:at|@)\s+" + _TIME + r"(?![\w:])", re.I)
_BARE_TIME = re.compile(r"(?<![\w:])" + r"(?P<time>noon|midnight|(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?|am|pm))(?![\w])", re.I)
_PART = re.compile(r"\b(morning|afternoon|evening|night|tonight)s?\b", re.I)
_PART_DEFAULT = {"morning": (8, 0), "afternoon": (14, 0), "evening": (18, 0), "night": (21, 0), "tonight": (21, 0)}


@dataclasses.dataclass
class Parsed:
    schedule: str          # a schedule Hermes' parse_schedule takes (cron expr, "every 30m", ISO time)
    prompt: str
    kind: str              # cron | interval | once
    assumed: str | None = None  # a part we filled in ("8:00 AM", since no time was said)


def _clock_from(match: re.Match, part: str | None) -> tuple[int, int] | None:
    t = match.group("time").lower()
    if t in ("noon", "midday"):
        return 12, 0
    if t == "midnight":
        return 0, 0
    h, m = int(match.group("h")), int(match.group("m") or 0)
    ap = (match.group("ap") or "").replace(".", "").replace(" ", "").lower()
    if m > 59:
        return None
    if ap:
        if not 1 <= h <= 12:
            return None
        return h % 12 + (12 if ap == "pm" else 0), m
    if h > 23:
        return None
    if h >= 13 or h == 0:
        return h, m
    if part in ("afternoon", "evening", "night", "tonight"):
        return (h % 12) + 12 if not (part in ("night", "tonight") and h == 12) else 0, m
    if part == "morning":
        return h % 12, m
    # A bare hour with no AM/PM: 7–11 reads as morning, 12 as noon, 1–6 as afternoon/evening. The
    # preview shows the result, so a wrong guess is caught before anything is saved.
    if h == 12:
        return 12, m
    return (h, m) if 7 <= h <= 11 else (h + 12, m)


def _dows_in(text: str) -> list[int]:
    found: list[int] = []
    for word in re.findall(_DOW_WORDS, text, re.I):
        d = _DOW_MAP.get(word.lower()[:3])
        if d is not None and d not in found:
            found.append(d)
    return found


def _cut(text: str, spans: list[tuple[int, int]]) -> str:
    out, last = [], 0
    for a, b in sorted(spans):
        if a < last:
            a = last
        out.append(text[last:a])
        last = max(last, b)
    out.append(text[last:])
    return " ".join("".join(out).split())


def _clean_prompt(text: str) -> str:
    text = re.sub(r"^[\s,.;:–—-]+|[\s,;:–—-]+$", "", text)
    text = re.sub(r"^(?:please\s+|can you\s+|could you\s+|i want you to\s+|i'd like you to\s+)", "", text, flags=re.I)
    text = re.sub(r"^(?:and\s+|then\s+|to\s+)", "", text, flags=re.I)
    text = re.sub(r"\s+(?:please)$", "", text, flags=re.I)
    text = text.strip(" ,;:")
    return text[:1].upper() + text[1:] if text else ""


def parse_request(text: str, now: datetime) -> Parsed:
    """Split "every weekday at 8 summarize my inbox" into a schedule and a prompt.

    ``now`` is Hermes' clock (its configured zone); one-time schedules become an ISO time in it.
    Raises ScheduleError(400) with a plain sentence when the when or the what is missing.
    """
    original = " ".join((text or "").split())
    if not original:
        raise ScheduleError(400, "Say when it should run and what it should do, like \u201Cevery weekday at 8 AM summarize my inbox\u201D.")
    if len(original) > MAX_PROMPT:
        raise ScheduleError(400, "That's too long for one schedule.")
    spans: list[tuple[int, int]] = []
    lower = original.lower()

    def take(m: re.Match | None) -> re.Match | None:
        if m:
            spans.append(m.span())
        return m

    def find_time(part: str | None) -> tuple[int, int] | None:
        m = take(_AT_TIME.search(original)) or take(_BARE_TIME.search(original))
        return _clock_from(m, part) if m else None

    # 1. Every N minutes/hours/days ("every 30 minutes", "every 2 hours", "hourly", "every half hour").
    m = re.search(r"\b(?:every|each)\s+(?P<n>\d+|" + "|".join(map(re.escape, _NUM_WORDS)) +
                  r")\s+(?P<unit>min(?:ute)?s?|hours?|hrs?|days?)\b", lower)
    if m:
        n = m.group("n")
        n = float(n) if n.isdigit() else _NUM_WORDS[n]
        unit = m.group("unit")
        minutes = int(round(n * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1)))
        spans.append(m.span())
        if unit.startswith("d") and minutes % 1440 == 0 and (t := find_time(None)):
            # "every 2 days at 9": an interval can't hold a clock time; every N days at a time ≈ */N.
            expr = f"{t[1]} {t[0]} */{minutes // 1440} * *"
            return Parsed(expr, _need_prompt(_clean_prompt(_cut(original, spans))), "cron")
        return Parsed(f"every {minutes}m", _need_prompt(_clean_prompt(_cut(original, spans))), "interval")
    m = re.search(r"\b(?:every|each)\s+half(?:\s+an)?\s+hour\b|\bevery\s+half-hour\b", lower)
    if m:
        spans.append(m.span())
        return Parsed("every 30m", _need_prompt(_clean_prompt(_cut(original, spans))), "interval")
    m = re.search(r"\b(?:every|each)\s+(?P<unit>minute|hour)\b|\b(?P<ly>hourly)\b", lower)
    if m and not re.search(r"\bevery\s+hour\s+(?:from|between)\b", lower):
        spans.append(m.span())
        unit = m.group("unit") or "hour"
        return Parsed("every 1m" if unit == "minute" else "every 60m",
                      _need_prompt(_clean_prompt(_cut(original, spans))), "interval")

    # 2. Recurring days ("every weekday", "weekdays", "every monday and thursday", "every morning",
    #    "daily", "monthly on the 1st").
    dow: str | None = None
    dom: str | None = None
    part: str | None = None
    m = re.search(r"\b(?:every|each|on)\s+(?:week\s?day|work\s?day|business\s+day)s?\b|\bweekdays\b|"
                  r"\bmon(?:day)?\s*(?:-|–|to|through|thru)\s*fri(?:day)?\b", lower)
    if m:
        dow = "1-5"
        spans.append(m.span())
    if dow is None and (m := re.search(r"\b(?:every|each|on)\s+weekends?\b|\bweekends\b", lower)):
        dow = "0,6"
        spans.append(m.span())
    if dow is None:
        m = re.search(r"\b(?:every|each|on)\s+(?P<days>" + _DOW_WORDS + r"(?:\s*(?:,|and|&|,\s*and)\s*" + _DOW_WORDS + r")*)\b"
                      r"|\b(?P<plural>(?:mon|tues|wednes|thurs|fri|satur|sun)days(?:\s*(?:,|and|&|,\s*and)\s*(?:mon|tues|wednes|thurs|fri|satur|sun)days)*)\b", lower)
        if m and (m.group("days") is None or lower[m.start():m.start() + 2] != "on" or re.search(r"s\b", m.group("days"))):
            days = _dows_in(m.group("days") or m.group("plural"))
            if days:
                dow = ",".join(str(d) for d in sorted(days))
                spans.append(m.span())
    m = re.search(r"\b(?:every|each)\s+(?:single\s+)?(?:day|morning|afternoon|evening|night)\b|\b(?:daily|nightly)\b", lower)
    if m:
        if dow is None:
            dow = "*"
        word = re.findall(r"morning|afternoon|evening|night", m.group(0))
        part = word[0] if word else ("night" if "nightly" in m.group(0) else None)
        spans.append(m.span())
    m = re.search(r"\b(?:monthly|every\s+month|each\s+month|of\s+(?:every|each|the)\s+month)\b", lower)
    if m:
        spans.append(m.span())
        d = re.search(r"\b(?:on\s+)?(?:the\s+)?(?P<d>\d{1,2})(?:st|nd|rd|th)(?:\s+of)?\b", lower)
        if d and 1 <= int(d.group("d")) <= 31:
            dom = d.group("d")
            spans.append(d.span())
        else:
            dom = "1"
        dow = None
    if dow is not None or dom is not None:
        if part is None and (pm := _PART.search(original)):
            part = pm.group(1).lower()
            spans.append(pm.span())
        t = find_time(part)
        assumed = None
        if t is None:
            t = _PART_DEFAULT.get(part or "", (9, 0))
            assumed = clock(*t)
        expr = f"{t[1]} {t[0]} {dom or '*'} * {dow if dom is None else '*'}"
        return Parsed(expr, _need_prompt(_clean_prompt(_cut(original, spans))), "cron", assumed)

    # 3. Once: "in 20 minutes", "tomorrow at 9", "on friday at 3pm", "tonight", "at 5pm".
    m = re.search(r"\bin\s+(?P<n>\d+|" + "|".join(map(re.escape, _NUM_WORDS)) + r")\s+(?P<unit>min(?:ute)?s?|hours?|hrs?|days?)\b", lower)
    if m:
        n = m.group("n")
        n = float(n) if n.isdigit() else _NUM_WORDS[n]
        unit = m.group("unit")
        minutes = n * (1440 if unit.startswith("d") else 60 if unit.startswith("h") else 1)
        spans.append(m.span())
        at = (now + timedelta(minutes=minutes)).replace(microsecond=0)
        return Parsed(at.isoformat(), _need_prompt(_clean_prompt(_cut(original, spans))), "once")
    day: datetime | None = None
    if m := re.search(r"\btomorrow\b", lower):
        day = now + timedelta(days=1)
        spans.append(m.span())
    elif m := re.search(r"\btoday\b|\btonight\b|\bthis\s+(?:morning|afternoon|evening)\b", lower):
        day = now
        spans.append(m.span())
        if "tonight" in m.group(0):
            part = "night"
    elif m := re.search(r"\b(?:on\s+|next\s+|this\s+)?(?P<d>" + _DOW_WORDS + r")\b", lower):
        days = _dows_in(m.group("d"))
        if days:
            ahead = (days[0] - (now.weekday() + 1) % 7) % 7 or 7
            day = now + timedelta(days=ahead)
            spans.append(m.span())
    if part is None and (pm := _PART.search(original)):
        part = pm.group(1).lower()
        spans.append(pm.span())
    t = find_time(part)
    if day is not None or t is not None:
        assumed = None
        if t is None:
            t = _PART_DEFAULT.get(part or "", (9, 0))
            assumed = clock(*t)
        base = day or now
        at = base.replace(hour=t[0], minute=t[1], second=0, microsecond=0)
        if at <= now:
            if day is not None and day.date() != now.date():
                pass
            else:
                at += timedelta(days=1)
        if at <= now:
            raise ScheduleError(400, "That time has already passed.")
        return Parsed(at.isoformat(), _need_prompt(_clean_prompt(_cut(original, spans))), "once", assumed)
    raise ScheduleError(400, "I couldn't tell when it should run. Try \u201Cevery weekday at 8 AM\u201D, "
                             "\u201Cevery 2 hours\u201D or \u201Ctomorrow at 9\u201D.")


def _need_prompt(prompt: str) -> str:
    if not prompt or not re.search(r"\w{2,}", prompt):
        raise ScheduleError(400, "What should it do? Add that after the time, like \u201C\u2026 summarize my inbox\u201D.")
    return prompt


def parse_schedule_only(text: str, now: datetime) -> Parsed:
    """Just a when ("weekdays at 9", "every 2 hours", "0 9 * * 1"): for editing a schedule."""
    clean = " ".join((text or "").split())
    if not clean:
        raise ScheduleError(400, "Say when it should run.")
    if len(clean) > MAX_SCHEDULE:
        raise ScheduleError(400, "That schedule is too long.")
    try:
        return parse_request(clean + " — run", now)
    except ScheduleError:
        pass
    return Parsed(clean, "", "")  # a raw cron expression, "every 45m", an ISO time: Hermes decides


def name_for(prompt: str) -> str:
    words = re.sub(r"[`*_#>\[\]]", "", prompt).split()
    name = " ".join(words[:7])
    if len(words) > 7 or len(name) > MAX_NAME:
        name = name[:MAX_NAME - 1].rstrip(" ,.;:") + "…"
    return name.rstrip(" .") or "Scheduled job"


# -- Hermes ------------------------------------------------------------------------------------------

def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        return None


def _iso_epoch(value: Any) -> float | None:
    dt = _parse_iso(value)
    return dt.timestamp() if dt else None


def _iso(epoch: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


_MD = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[([^\]]+)\]\([^)]*\)|[*_`>#]+|^\s*[-•]\s+", re.M)


def plain(text: str, limit: int) -> str:
    text = _MD.sub(lambda m: m.group(1) or "", text or "")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit * 0.6 else cut).rstrip(" ,.;:") + "…"


def _response_of(doc: str) -> str:
    """The run's answer from a Hermes output document: ``## Response`` (agent runs) or
    ``## Script Output`` (script jobs); a silent run reads as nothing to report."""
    for heading in ("\n## Response\n", "\n## Script Output\n", "\n## Output\n"):
        i = doc.rfind(heading)
        if i >= 0:
            body = doc[i + len(heading):].strip()
            break
    else:
        status = re.search(r"^\*\*Status:\*\*\s*(.+)$", doc, re.M)
        if status and re.match(r"(silent|no_change)", status.group(1), re.I):
            return "Nothing to report."
        body = re.sub(r"^# Cron Job:.*?\n", "", doc, count=1)
        body = re.sub(r"^\*\*[A-Za-z ]+:\*\*[^\n]*\n?", "", body, flags=re.M)
        body = body.strip()
        if not body and status:
            return status.group(1).strip().capitalize()
    if body.upper().startswith("[SILENT]"):
        return "Nothing to report."
    return body


class Schedules:
    """This profile's Hermes cron jobs. Every call runs inside the profile's home scope."""

    def __init__(self, home: Path, state_db: Path | None = None):
        self.home = Path(home)
        self.state_db = state_db or self.home / "state.db"

    # -- scope ---------------------------------------------------------------------------------
    @contextlib.contextmanager
    def scope(self) -> Iterator[None]:
        """Hermes' cron store and config for this profile (HTTP threads carry no profile context)."""
        try:
            from hermes_constants import reset_hermes_home_override, set_hermes_home_override
            from cron.jobs import use_cron_store
        except Exception as exc:  # pragma: no cover - a Hermes too old for this
            raise ScheduleError(503, "This Hermes can't share its schedules yet. Update Hermes.") from exc
        token = set_hermes_home_override(str(self.home))
        try:
            with use_cron_store(self.home):
                yield
        finally:
            reset_hermes_home_override(token)

    def _now(self) -> datetime:
        try:
            from hermes_time import now
            return now()
        except Exception:
            return datetime.now().astimezone()

    def _tz_name(self) -> str | None:
        try:
            from hermes_time import get_timezone_name
            return get_timezone_name() or None
        except Exception:
            return None

    @staticmethod
    def _tool(**kwargs: Any) -> dict[str, Any]:
        import json
        from tools.cronjob_tools import cronjob
        result = json.loads(cronjob(**kwargs))
        if not result.get("success", False):
            raise ScheduleError(400, _friendly(str(result.get("error") or "Hermes refused that.")))
        return result

    def _job(self, job_id: str) -> dict[str, Any]:
        if not SAFE_JOB_ID.match(job_id or ""):
            raise ScheduleError(404, "I couldn't find that schedule.")
        from cron.jobs import get_job
        job = get_job(job_id)
        if not job:
            raise ScheduleError(404, "I couldn't find that schedule.")
        return job

    # -- reading -------------------------------------------------------------------------------
    def list(self) -> dict[str, Any]:
        with self.scope():
            from cron.jobs import list_jobs
            jobs = list_jobs(include_disabled=True)
            live = self._live([j["id"] for j in jobs])
            now = self._now()
            rows = [self._row(j, live.get(j["id"]), now) for j in jobs]
            tz = self._tz_name()
        rows.sort(key=_order)
        return {"schedules": rows, "timezone": tz}

    def detail(self, job_id: str) -> dict[str, Any]:
        with self.scope():
            job = self._job(job_id)
            live = self._live([job["id"]]).get(job["id"])
            now = self._now()
            row = self._row(job, live, now)
            runs = self._runs(job, live)
            tz = self._tz_name()
        skills = job.get("skills") or ([job["skill"]] if job.get("skill") else [])
        return {**row, "prompt": str(job.get("prompt") or ""), "skills": [str(s) for s in skills][:8],
                "schedule_text": _editable(job.get("schedule")), "runs": runs, "timezone": tz,
                "script_only": bool(job.get("no_agent")),
                "created_at": job.get("created_at")}

    def _live(self, job_ids: list[str]) -> dict[str, dict[str, Any]]:
        try:
            from cron.executions import live_executions
            return live_executions(job_ids) or {}
        except Exception:
            return {}

    def _row(self, job: dict[str, Any], live: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
        from cron.jobs import effective_job_state
        state = effective_job_state(job) if job else "scheduled"
        paused = state == "paused" or (not job.get("enabled", True) and state != "completed")
        status = job.get("last_status")
        last_status = None if not job.get("last_run_at") else ("ok" if status == "ok" else "failed" if status else None)
        if status == "cancelled":
            last_status = "cancelled"
        result = ""
        if last_status == "failed":
            result = plain(str(job.get("last_error") or "It didn't finish."), RESULT_CHARS)
        elif job.get("last_run_at"):
            result = plain(self._latest_result(job["id"]), RESULT_CHARS)
        delivery_problem = job.get("last_delivery_error")
        return {
            "id": job["id"],
            "name": str(job.get("name") or name_for(str(job.get("prompt") or "")))[:MAX_NAME],
            "schedule_label": describe_schedule(job.get("schedule"), now),
            "kind": (job.get("schedule") or {}).get("kind"),
            "next_run_at": None if paused or state == "completed" else job.get("next_run_at"),
            "last_run_at": job.get("last_run_at"),
            "last_status": "running" if live else last_status,
            "last_result": result,
            "delivery_problem": plain(str(delivery_problem), RESULT_CHARS) if delivery_problem else None,
            "enabled": not paused and state != "completed",
            "state": "running" if live else ("paused" if paused else state),
            "running": bool(live),
            "delivery_label": describe_delivery(job.get("deliver"), job.get("origin")),
        }

    def _docs(self, job_id: str, limit: int) -> list[tuple[float, Path]]:
        from cron.jobs import get_cron_output_dir
        folder = get_cron_output_dir() / job_id
        try:
            files = sorted((p for p in folder.glob("*.md") if p.is_file()), key=lambda p: p.name, reverse=True)[:limit]
        except OSError:
            return []
        out = []
        tz = None
        with contextlib.suppress(Exception):
            from hermes_time import get_timezone
            tz = get_timezone()
        for p in files:
            try:
                naive = datetime.strptime(p.stem, "%Y-%m-%d_%H-%M-%S")
                ts = naive.replace(tzinfo=tz).timestamp() if tz else naive.astimezone().timestamp()
            except ValueError:
                ts = p.stat().st_mtime
            out.append((ts, p))
        return out

    @staticmethod
    def _read_doc(path: Path) -> str:
        try:
            with path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - DOC_TAIL_BYTES))
                raw = f.read().decode("utf-8", errors="replace")
            if size > DOC_TAIL_BYTES:
                raw = "\n" + raw.split("\n", 1)[-1]
            return _response_of(raw)
        except OSError:
            return ""

    def _latest_result(self, job_id: str) -> str:
        docs = self._docs(job_id, 1)
        return self._read_doc(docs[0][1]) if docs else ""

    def _sessions(self, job_id: str) -> list[dict[str, Any]]:
        if not self.state_db.exists():
            return []
        try:
            from hermes_state import SessionDB
            db = SessionDB(db_path=self.state_db, read_only=True)
            try:
                return db.list_cron_job_runs(job_id, limit=RUNS * 2)
            finally:
                with contextlib.suppress(Exception):
                    db.close()
        except Exception:
            logger.debug("speakeasy: cron run sessions unreadable", exc_info=True)
            return []

    def _runs(self, job: dict[str, Any], live: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Recent runs, newest first: status, result, and the session each one ran in."""
        job_id = job["id"]
        try:
            from cron.executions import list_executions
            execs = list_executions(job_id=job_id, limit=RUNS)
        except Exception:
            execs = []
        sessions = self._sessions(job_id)
        docs = self._docs(job_id, RUNS * 2)
        by_exec = {s.get("cron_execution_id"): s for s in sessions if s.get("cron_execution_id")}
        used_docs: set[Path] = set()
        used_sessions: set[str] = set()

        def doc_between(start: float, end: float | None) -> Path | None:
            for ts, p in docs:
                if p in used_docs:
                    continue
                if ts + 0.001 >= math.floor(start) and (end is None or ts <= end + 300):
                    used_docs.add(p)
                    return p
            return None

        def session_between(start: float, end: float | None) -> dict[str, Any] | None:
            for s in sessions:
                if s["id"] in used_sessions or s.get("cron_execution_id"):
                    continue
                st = float(s.get("started_at") or 0)
                if st >= start - 5 and (end is None or st <= end + 5):
                    return s
            return None

        runs: list[dict[str, Any]] = []
        for e in execs:
            status = str(e.get("status") or "")
            if status == "unknown":
                continue
            start = _iso_epoch(e.get("claimed_at")) or 0.0
            end = _iso_epoch(e.get("finished_at"))
            session = by_exec.get(e.get("id")) or session_between(start, end)
            if session:
                used_sessions.add(session["id"])
            doc = doc_between(start, end) if status not in ("claimed", "running") else None
            st = {"completed": "ok", "failed": "failed", "cancelled": "cancelled", "missed": "missed",
                  "claimed": "running", "running": "running"}.get(status, status or "ok")
            if st == "ok" and str(e.get("delivery_outcome") or "") == "failed":
                st = "ok"
            error = str(e.get("error") or "").strip()
            if error in ("None", "null"):
                error = ""
            result = self._read_doc(doc) if doc else ""
            if st in ("failed", "cancelled", "missed") and error:
                result = result or error
            runs.append({"id": str(e.get("id")), "started_at": e.get("started_at") or e.get("claimed_at"),
                         "finished_at": e.get("finished_at"), "status": st,
                         "result": plain(result, RUN_RESULT_CHARS), "error": plain(error, 300) or None,
                         "session_id": session["id"] if session else None})
        if not execs:
            for s in sessions:
                start = float(s.get("started_at") or 0)
                end = float(s["ended_at"]) if s.get("ended_at") else None
                doc = doc_between(start, end)
                text = self._read_doc(doc) if doc else str(s.get("preview") or "")
                reason = str(s.get("end_reason") or "")
                st = "running" if end is None else ("failed" if "fail" in reason or "error" in reason else "ok")
                runs.append({"id": s["id"], "started_at": _iso(start), "finished_at": _iso(end) if end else None,
                             "status": st, "result": plain(text, RUN_RESULT_CHARS), "error": None,
                             "session_id": s["id"]})
            if not sessions:
                for ts, p in docs[:RUNS]:
                    raw = self._read_doc(p)
                    head = ""
                    with contextlib.suppress(OSError):
                        with p.open("rb") as f:
                            head = f.read(200).decode("utf-8", errors="replace")
                    failed = "(FAILED)" in head.split("\n", 1)[0]
                    runs.append({"id": f"doc_{p.stem}", "started_at": _iso(ts), "finished_at": _iso(ts),
                                 "status": "failed" if failed else "ok", "result": plain(raw, RUN_RESULT_CHARS),
                                 "error": None, "session_id": None})
        if live and not any(r["status"] == "running" for r in runs):
            runs.insert(0, {"id": str(live.get("id") or "live"), "started_at": live.get("claimed_at"),
                            "finished_at": None, "status": "running", "result": "", "error": None,
                            "session_id": None})
        runs.sort(key=lambda r: _iso_epoch(r.get("started_at")) or 0, reverse=True)
        return runs[:RUNS]

    # -- writing -------------------------------------------------------------------------------
    def pause(self, job_id: str) -> dict[str, Any]:
        with self.scope():
            job = self._job(job_id)
            self._tool(action="pause", job_id=job["id"], reason="Paused from Speakeasy")
        return self.detail(job_id)

    def resume(self, job_id: str) -> dict[str, Any]:
        with self.scope():
            job = self._job(job_id)
            try:
                self._tool(action="resume", job_id=job["id"])
            except ValueError as exc:
                raise ScheduleError(400, _friendly(str(exc))) from None
        return self.detail(job_id)

    def run_now(self, job_id: str) -> dict[str, Any]:
        """Queue one run now: the gateway's ticker fires it on its next tick (within a minute),
        with its normal delivery. A paused job is resumed by this (Hermes' own rule for Run now)."""
        with self.scope():
            job = self._job(job_id)
            from cron.jobs import is_terminal_job, trigger_job
            if is_terminal_job(job):
                raise ScheduleError(409, "This one-time schedule already ran. Make a new one to run it again.")
            if self._live([job["id"]]).get(job["id"]):
                raise ScheduleError(409, "It's running right now.")
            was_paused = not job.get("enabled", True) or job.get("state") == "paused"
            try:
                trigger_job(job["id"])
            except ValueError as exc:
                raise ScheduleError(409, _friendly(str(exc))) from None
            with contextlib.suppress(Exception):
                from tools.cronjob_tools import _notify_provider_jobs_changed_safe
                _notify_provider_jobs_changed_safe()
        out = self.detail(job_id)
        out["queued"] = True
        out["resumed"] = was_paused
        return out

    def delete(self, job_id: str) -> dict[str, Any]:
        with self.scope():
            job = self._job(job_id)
            self._tool(action="remove", job_id=job["id"])
        return {"ok": True, "id": job_id}

    def update(self, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """``{schedule?, prompt?, name?}``: the schedule in words ("weekdays at 9") or Hermes' forms."""
        if not isinstance(body, dict):
            raise ScheduleError(400, "Nothing to change.")
        with self.scope():
            job = self._job(job_id)
            kwargs: dict[str, Any] = {}
            if body.get("schedule") is not None:
                kwargs["schedule"] = self._validated(str(body["schedule"]))[0]
            if body.get("prompt") is not None:
                prompt = " ".join(str(body["prompt"]).split()) if "\n" not in str(body["prompt"]) else str(body["prompt"]).strip()
                if not prompt:
                    raise ScheduleError(400, "Say what it should do.")
                if len(prompt) > MAX_PROMPT:
                    raise ScheduleError(400, "That's too long for one schedule.")
                kwargs["prompt"] = prompt
            if body.get("name") is not None:
                name = " ".join(str(body["name"]).split())[:MAX_NAME]
                if name:
                    kwargs["name"] = name
            if not kwargs:
                raise ScheduleError(400, "Nothing to change.")
            self._tool(action="update", job_id=job["id"], **kwargs)
        return self.detail(job_id)

    def _validated(self, text: str) -> tuple[str, dict[str, Any], str | None]:
        """A schedule in words or Hermes' forms -> (Hermes schedule string, parsed, assumed)."""
        from cron.jobs import compute_next_run, parse_schedule
        parsed = parse_schedule_only(text, self._now())
        try:
            sched = parse_schedule(parsed.schedule)
        except ValueError:
            raise ScheduleError(400, "I couldn't read that schedule. Try \u201Cweekdays at 9 AM\u201D or \u201Cevery 2 hours\u201D.") from None
        if sched.get("kind") == "once" and compute_next_run(sched) is None:
            raise ScheduleError(400, "That time has already passed.")
        return parsed.schedule, sched, parsed.assumed

    def preview(self, body: dict[str, Any]) -> dict[str, Any]:
        """``{text}`` (one sentence) or ``{schedule, prompt}``: what would be saved. Saves nothing."""
        if not isinstance(body, dict):
            raise ScheduleError(400, "Say when it should run and what it should do.")
        with self.scope():
            from cron.jobs import compute_next_run, parse_schedule
            now = self._now()
            if body.get("text"):
                parsed = parse_request(str(body["text"]), now)
                schedule_text, prompt, assumed = parsed.schedule, parsed.prompt, parsed.assumed
                try:
                    sched = parse_schedule(schedule_text)
                except ValueError:
                    raise ScheduleError(400, "I couldn't tell when it should run.") from None
            else:
                prompt = str(body.get("prompt") or "").strip()
                if not prompt:
                    raise ScheduleError(400, "Say what it should do.")
                if len(prompt) > MAX_PROMPT:
                    raise ScheduleError(400, "That's too long for one schedule.")
                schedule_text, sched, assumed = self._validated(str(body.get("schedule") or ""))
            next_runs = _next_runs(sched, compute_next_run, now)
            if not next_runs:
                raise ScheduleError(400, "That time has already passed.")
            tz = self._tz_name()
            targets = self._targets()
        return {"schedule": schedule_text, "schedule_label": describe_schedule(sched, now),
                "kind": sched.get("kind"), "prompt": prompt, "name": name_for(prompt),
                "next_run_at": next_runs[0], "next_runs": next_runs, "assumed": assumed,
                "timezone": tz, "deliver": "local", "delivery_label": describe_delivery("local"),
                "delivery_targets": targets}

    def _targets(self) -> list[dict[str, str]]:
        targets = [{"id": "local", "label": "Saved only"}]
        with contextlib.suppress(Exception):
            from cron.scheduler_delivery import cron_delivery_targets
            for t in cron_delivery_targets():
                tid = str(t.get("id") or "")
                if tid and t.get("home_target_set", True) and not tid.startswith("bot-chat"):
                    targets.append({"id": tid, "label": describe_delivery(tid)})
        return targets

    def create(self, body: dict[str, Any]) -> dict[str, Any]:
        """``{schedule, prompt, name?, deliver?}`` from a confirmed preview. Hermes scans the prompt."""
        if not isinstance(body, dict) or not body.get("confirmed"):
            raise ScheduleError(400, "Preview the schedule and confirm it first.")
        prompt = str(body.get("prompt") or "").strip()
        if not prompt:
            raise ScheduleError(400, "Say what it should do.")
        if len(prompt) > MAX_PROMPT:
            raise ScheduleError(400, "That's too long for one schedule.")
        with self.scope():
            schedule_text, _sched, _ = self._validated(str(body.get("schedule") or ""))
            deliver = str(body.get("deliver") or "local")
            if deliver not in {t["id"] for t in self._targets()}:
                raise ScheduleError(400, "Pick where the results should go.")
            name = " ".join(str(body.get("name") or "").split())[:MAX_NAME] or name_for(prompt)
            result = self._tool(action="create", prompt=prompt, schedule=schedule_text, name=name, deliver=deliver)
            job_id = str(result.get("job_id") or "")
        return self.detail(job_id)


def _next_runs(sched: dict[str, Any], compute_next_run: Any, now: datetime, count: int = 3) -> list[str]:
    first = compute_next_run(sched)
    if not first:
        return []
    out = [first]
    if sched.get("kind") == "cron":
        with contextlib.suppress(Exception):
            from croniter import croniter
            it = croniter(sched["expr"], _parse_iso(first))
            for _ in range(count - 1):
                out.append(it.get_next(datetime).isoformat())
    elif sched.get("kind") == "interval":
        dt = _parse_iso(first)
        step = timedelta(minutes=int(sched.get("minutes") or 0))
        if dt and step:
            out += [(dt + step * (i + 1)).isoformat() for i in range(count - 1)]
    return out


def _editable(schedule: Any) -> str:
    """The schedule as text the edit field starts with (Hermes takes it back as is)."""
    if not isinstance(schedule, dict):
        return ""
    kind = schedule.get("kind")
    if kind == "cron":
        return str(schedule.get("expr") or "")
    if kind == "interval":
        return f"every {int(schedule.get('minutes') or 0)}m"
    if kind == "once":
        return str(schedule.get("run_at") or "")
    return str(schedule.get("display") or "")


def _order(row: dict[str, Any]) -> tuple:
    """Running and active first (soonest next run), then paused, then finished one-time jobs."""
    group = 0 if row["running"] else 1 if row["enabled"] else 2 if row["state"] == "paused" else 3
    nxt = _iso_epoch(row.get("next_run_at")) or float("inf")
    return (group, nxt if group <= 1 else -(_iso_epoch(row.get("last_run_at")) or 0), row["name"].lower())


def _friendly(error: str) -> str:
    """Hermes' errors are written for a terminal; keep the first sentence, drop CLI hints."""
    if "threat pattern" in error or "Blocked:" in error:
        return "Hermes won't save that prompt: it looks like it's trying to change its instructions or reach secrets."
    if "Invalid schedule" in error or "Invalid cron" in error:
        return "I couldn't read that schedule. Try \u201Cweekdays at 9 AM\u201D or \u201Cevery 2 hours\u201D."
    if "in the past" in error:
        return "That time has already passed."
    first = error.strip().split("\n", 1)[0]
    first = re.sub(r"'hermes cron [^']*'", "", first)
    return first[:200] or "Hermes refused that."
