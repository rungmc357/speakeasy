"""A running task's live line: what it is doing now and what it has done so far, from what it really did.

A task's opening plan is a guess made at handoff ("Find the cause / Trace the stuck state / Fix and
test / Your review"); the work rarely follows it, so a card driven by it sat on one step for half an
hour while the task edited files and ran tests. This module reads the task's activity feed instead
(activity.py: every tool call it made, already in plain words and redacted) and says:

- ``now``: a short plain-English line for the last couple of minutes ("Editing store.py and
  router.py", "Running the tests"); a single call running a long time says so ("Waiting on the
  tests · 4 min"); nothing at all for over two minutes says that honestly ("No new steps · 4 min").
- ``so_far``: one growing recap of the whole run, oldest first ("Read the code, edited 3 files, ran
  the tests"), rebuilt from the activity every time, never from the opening plan.

``deterministic`` needs no model. ``Summarizer`` can add the plugin's side model on top (Hermes'
auxiliary client, router.aux_call), but it is off unless the user picked their own model for
Speakeasy's side tasks (``auxiliary.speakeasy_router``): left on "auto" that is their MAIN model, and
asking it on every read of a running task (the app's liveness loop included) cost one main-model
call per task every 40 s. When on, it is asked only when someone really opened the task
(``ask=True``: the task view or its session page), once per phase (``PHASE_STEPS`` new steps since
the last ask, at least ``MODEL_EVERY_S`` apart) and at most ``HOURLY_CAP`` times an hour in all;
its words are vetted and used while fresh by every read, else the deterministic line.
Never a step count, never a raw tool name, never a path beyond a file's own name.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

WINDOW_S = 120.0          # "now" reads the last two minutes of steps
IDLE_AFTER_S = 120.0      # no step for this long: say it's quiet, with for how long
LONG_CALL_S = 60.0        # one call running this long: "Waiting on ..."
HANDOFF_STALE_S = 60.0    # the handoff guess stops being shown as "now" after this
MODEL_EVERY_S = 60.0      # at most one side-model summary per task this often
PHASE_STEPS = 3           # ... and only once this many new steps happened since the last ask
HOURLY_CAP = 20           # side-model summaries in all, per rolling hour
MODEL_FRESH_S = 150.0     # a model summary older than this (or behind the feed) isn't used
NOW_CHARS = 90
SO_FAR_CHARS = 160

# The activity feed's past-tense openers, in the present ("Ran the tests" -> "Running the tests").
_ING = {
    "read": "Reading", "searched": "Searching", "ran": "Running", "edited": "Editing", "looked": "Looking",
    "checked": "Checking", "built": "Building", "fetched": "Fetching", "wrote": "Writing", "made": "Making",
    "used": "Using", "updated": "Updating", "installed": "Installing", "asked": "Asking", "sent": "Sending",
    "handed": "Handing", "recorded": "Recording", "worked": "Working", "opened": "Opening", "created": "Creating",
    "deleted": "Deleting", "moved": "Moving", "started": "Starting", "thinking": "Thinking",
}
# A long wait on one call, by what it is ("Waiting on the tests").
_WAIT_ON = (
    (re.compile(r"\btests?\b", re.I), "the tests"),
    (re.compile(r"\bbuil[dt]\b", re.I), "the build"),
    (re.compile(r"\bbackground job\b", re.I), "a background job"),
    (re.compile(r"\binstall", re.I), "an install"),
    (re.compile(r"\bweb\b|\bpage\b|\bbrowser\b", re.I), "a web page"),
    (re.compile(r"\bhelper\b", re.I), "a helper"),
    (re.compile(r"\bapproval\b|\bquestion\b", re.I), "you"),
)
_BAD = re.compile(r"[`{}<>$|\\]|https?://|~/|/Users/|\b[a-z_]+\(", re.I)
_TOOLISH = re.compile(r"\b[a-z]+_[a-z_]+\b")  # raw tool names (read_file, web_search)
# The agent talking to itself about a hiccup ("Using patch instead", "That didn't work, trying
# again", "Hmm, wrong file"): its own words, but never what the job is doing.
RECOVERY = re.compile(
    r"\binstead\b|\b(?:try|trying|tried) (?:again|another|a different)|\bretry(?:ing)?\b|\bdidn'?t work\b"
    r"|\bdoesn'?t work\b|\bthat failed\b|^(?:hmm|oops|whoops|sorry|ah|actually|wait|my mistake|apologies)\b"
    r"|\bwrong (?:file|path|command|tool|place)\b|\btypo\b|\bfix(?:ing)? (?:my|the) (?:mistake|typo|command|call)\b",
    re.I)


def recovery_talk(text: Any) -> bool:
    return isinstance(text, str) and bool(RECOVERY.search(" ".join(text.split())))


def _clean(text: Any, limit: int) -> str | None:
    if not isinstance(text, str):
        return None
    text = " ".join(text.split()).strip(" .")
    if not text or _BAD.search(text) or _TOOLISH.search(text):
        return None
    if len(text) > limit:
        text = text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;: ") + "\u2026"
    return text


def _minutes(seconds: float) -> str:
    m = max(1, int(round(seconds / 60.0)))
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def present(text: str) -> str:
    """"Ran the tests" -> "Running the tests"; anything else unchanged."""
    first, _, rest = text.partition(" ")
    ing = _ING.get(first.lower())
    return f"{ing} {rest}".strip() if ing else text


# R3: a status line names the site ("Reading Yelp"), never its bare domain ("Reading www.yelp.com/biz/…").
# Brands whose name isn't their domain label in title case.
SITE_NAMES = {
    "github": "GitHub", "gitlab": "GitLab", "youtube": "YouTube", "linkedin": "LinkedIn", "tripadvisor": "Tripadvisor",
    "opentable": "OpenTable", "stackoverflow": "Stack Overflow", "stackexchange": "Stack Exchange", "ebay": "eBay",
    "imdb": "IMDb", "rei": "REI", "bbc": "BBC", "cnn": "CNN", "nytimes": "The New York Times", "wsj": "WSJ",
    "npr": "NPR", "espn": "ESPN", "hackernews": "Hacker News", "ycombinator": "Hacker News", "openai": "OpenAI",
    "chatgpt": "ChatGPT", "airbnb": "Airbnb", "doordash": "DoorDash", "ubereats": "Uber Eats", "x": "X",
    "twitter": "X", "weather": "Weather.com", "accuweather": "AccuWeather", "wikipedia": "Wikipedia",
    "bestbuy": "Best Buy", "homedepot": "Home Depot", "walmart": "Walmart", "costco": "Costco", "etsy": "Etsy",
    "google": "Google", "apple": "Apple", "icloud": "iCloud", "npmjs": "npm", "pypi": "PyPI", "vercel": "Vercel",
    "netlify": "Netlify", "cloudflare": "Cloudflare", "sentry": "Sentry", "linear": "Linear", "notion": "Notion",
    "figma": "Figma", "reddit": "Reddit", "yelp": "Yelp", "amazon": "Amazon", "resy": "Resy", "kayak": "Kayak",
    "booking": "Booking.com", "expedia": "Expedia", "zillow": "Zillow", "redfin": "Redfin", "craigslist": "Craigslist",
    "spotify": "Spotify", "medium": "Medium", "substack": "Substack", "arxiv": "arXiv", "nasa": "NASA",
    "noaa": "NOAA", "usps": "USPS", "ups": "UPS", "fedex": "FedEx", "dhl": "DHL", "irs": "IRS", "dmv": "DMV",
}
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu"}
_TLDS = ("com|org|net|io|co|dev|ai|gov|edu|us|uk|ca|au|de|fr|jp|tv|news|info|biz|gg|fm|xyz|blog|shop|eu|nz|ie")
_DOMAIN = re.compile(rf"(?i)(?<![@\w.-])(?:https?://)?(?:www\d?\.)?((?:[a-z0-9-]+\.)+(?:{_TLDS}))\b(?![@\w-])(?:/[^\s,;)]*)?")


def site_name(host: str) -> str:
    """"www.yelp.com" -> "Yelp", "news.ycombinator.com" -> "Hacker News", "acme-tools.co.uk" -> "Acme Tools"."""
    parts = [p for p in host.lower().strip(".").split(".") if p]
    if parts and re.fullmatch(r"www\d?", parts[0]):
        parts = parts[1:]
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL and len(parts[-1]) == 2:
        label = parts[-3]
    elif len(parts) >= 2:
        label = parts[-2]
    else:
        label = parts[0] if parts else host
    if label in SITE_NAMES:
        return SITE_NAMES[label]
    return " ".join(w[:1].upper() + w[1:] for w in label.split("-") if w) or host


def sited(line: str) -> str:
    """A status line with every bare domain (and its path) said as the site's name."""
    return _DOMAIN.sub(lambda m: site_name(m.group(1)), line or "")


def _ended(item: dict[str, Any]) -> float:
    return float(item.get("ended_at") or item.get("at") or 0)


def _files(items: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for item in items:
        m = re.match(r"(?:Edited|Read) (\S+\.[A-Za-z0-9]{1,8})$", str(item.get("text") or ""))
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def _join(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _now_line(recent: list[dict[str, Any]]) -> str:
    """The last couple of minutes in one line: the newest kind of work, with the files it named."""
    newest = recent[-1]
    kind = newest.get("kind")
    same = [i for i in recent if i.get("kind") == kind]
    if kind == "edit":
        files = _files(same)
        if files:
            shown = files[-2:]
            more = len(files) - len(shown)
            return "Editing " + _join(shown) + (f" and {more} more" if more > 0 else "")
    if kind == "read" and len(same) >= 3 and not _files(same[-1:]):
        return "Reading through the code"
    line = present(str(newest.get("text") or ""))
    # What came just before, when it was a different kind of work ("Running the tests after editing store.py").
    before = next((i for i in reversed(recent[:-1]) if i.get("kind") != kind), None)
    if before is not None and kind == "run" and before.get("kind") == "edit":
        files = _files([i for i in recent if i.get("kind") == "edit"])
        if files:
            line += " after editing " + _join(files[-2:])
    return line


def _so_far(items: list[dict[str, Any]], findings: list[dict[str, Any]]) -> str | None:
    """Oldest first, one phrase per kind of work: "Read the code, edited 3 files, ran the tests"."""
    order: list[str] = []
    count: dict[str, int] = {}
    tests = 0
    for item in items:
        kind = str(item.get("kind") or "tool")
        text = str(item.get("text") or "")
        if kind == "run" and re.search(r"\btests?\b", text, re.I):
            kind = "test"
            tests += 1
        if kind not in count:
            order.append(kind)
            count[kind] = 0
        count[kind] += 1
    edited = _files([i for i in items if i.get("kind") == "edit"])
    phrase = {
        "read": "read the code" if count.get("read", 0) > 2 else "read a file",
        "search": "searched the code",
        "edit": (f"edited {_join(edited)}" if 0 < len(edited) <= 2 else
                 f"edited {len(edited)} files" if edited else "made edits"),
        "test": "ran the tests" + (f" {tests} times" if tests > 1 else ""),
        "run": "ran commands" if count.get("run", 0) > 1 else "ran a command",
        "browse": "looked on the web",
        "think": "thought it over",
        "message": "sent a message",
        "tool": "used its tools",
    }
    parts = [phrase.get(k, "") for k in order if phrase.get(k)]
    found = [t for t in (_clean(sited(str(f.get("text") or "")), 80) for f in findings if isinstance(f, dict)) if t]
    if found:
        parts.insert(0, "found " + found[-1][0].lower() + found[-1][1:])
    if not parts:
        return None
    text = ", ".join(parts)
    return _clean(text[0].upper() + text[1:], SO_FAR_CHARS)


def deterministic(state: dict[str, Any] | None, *, now: float, started: float | None = None) -> dict[str, Any] | None:
    """The live line from the activity feed alone. None when the task has no activity yet and
    started under ``HANDOFF_STALE_S`` ago (the handoff line is still honest then)."""
    items = [i for i in ((state or {}).get("items") or []) if isinstance(i, dict) and i.get("text")]
    findings = list((state or {}).get("findings") or [])
    if not items:
        if started and now - started >= HANDOFF_STALE_S:
            return {"now": f"No steps reported yet \u00b7 {_minutes(now - started)}", "so_far": None,
                    "quiet_s": int(now - started), "source": "idle", "basis": 0}
        return None
    basis = len(items)
    running = next((i for i in reversed(items) if i.get("status") == "running"), None)
    last_at = max(_ended(i) for i in items)
    so_far = _so_far(items, findings)
    if running is not None:
        ran = now - float(running.get("started_at") or running.get("at") or now)
        if ran >= LONG_CALL_S:
            text = str(running.get("text") or "")
            target = next((label for pattern, label in _WAIT_ON if pattern.search(text)), None)
            line = f"Waiting on {target}" if target else sited(present(text))
            return {"now": f"{line} \u00b7 {_minutes(ran)}", "so_far": so_far, "quiet_s": None,
                    "source": "activity", "basis": basis}
    quiet = now - last_at
    if running is None and quiet >= IDLE_AFTER_S:
        return {"now": f"No new steps \u00b7 {_minutes(quiet)}", "so_far": so_far, "quiet_s": int(quiet),
                "source": "idle", "basis": basis}
    recent = [i for i in items if _ended(i) >= now - WINDOW_S] or items[-1:]
    return {"now": _clean(sited(_now_line(recent)), NOW_CHARS) or sited(present(str(items[-1]["text"])))[:NOW_CHARS],
            "so_far": so_far, "quiet_s": None, "source": "activity", "basis": basis}


# -- the side model ------------------------------------------------------------------------------

SYSTEM = (
    "You write the live status line of a background task for its owner's phone. You get the task's "
    "request and its recent steps (plain words, oldest first). Reply with JSON only: "
    "{\"now\": \"...\", \"so_far\": \"...\"}. now: what it is doing right now, present tense, 4 to 12 "
    "plain words, specific (\"Reading the session page code to find why old messages show last\"). "
    "so_far: one short line of what it has done, oldest first, under 20 words (\"Found the cause; "
    "fixing it; tests next\"). No step numbers, no tool names, no file paths, no code, no quotes."
)


def model_messages(request: str | None, items: list[dict[str, Any]], findings: list[dict[str, Any]]) -> list[dict[str, str]]:
    steps = []
    for i in items[-14:]:
        line = str(i.get("text") or "")
        if i.get("status") == "running":
            line += " (still running)"
        elif i.get("status") == "failed":
            line += " (failed)"
        steps.append(line)
    found = [str(f.get("text") or "") for f in findings[-3:] if isinstance(f, dict)]
    user = json.dumps({"request": (request or "")[:300], "steps": steps, "found": found}, ensure_ascii=False)
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]


def parse_model(reply: Any) -> dict[str, str] | None:
    if not isinstance(reply, str):
        return None
    m = re.search(r"\{.*\}", reply, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    now = _clean(sited(data["now"]) if isinstance(data.get("now"), str) else None, NOW_CHARS)
    if not now or re.search(r"\bstep \d|\d+ of \d+", now, re.I) or not 2 <= len(now.split()) <= 16:
        return None
    so_far = _clean(data.get("so_far"), SO_FAR_CHARS)
    return {"now": now, "so_far": so_far or ""}


class Summarizer:
    """The side model's live line per task: asked off the request path, rate-limited per task,
    used only while fresh and not behind the feed. ``call`` is router.aux_call-shaped."""

    def __init__(self, call: Callable[[list[dict[str, str]]], str | None] | None,
                 clock: Callable[[], float] = time.time, spawn: Callable[[Callable[[], None]], Any] | None = None,
                 enabled: Callable[[], bool] | None = None):
        self.call = call
        self.clock = clock
        # Off unless told otherwise: whether the user picked a side model of their own (service.py).
        self.enabled = enabled or (lambda: False)
        self._spent: list[float] = []  # when each side-model ask went out, the last hour
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._asked: dict[str, tuple[float, int]] = {}
        self._busy: set[str] = set()

    def _allowed(self) -> bool:
        try:
            return bool(self.enabled())
        except Exception:
            return False

    def line(self, key: str, state: dict[str, Any] | None, *, request: str | None, started: float | None,
             on_ready: Callable[[], None] | None = None, ask: bool = False) -> dict[str, Any] | None:
        """The task's live line. *ask*: someone opened the task just now (not a background or
        liveness read), so a fresh side-model line may be asked for if the side model is on."""
        now = self.clock()
        base = deterministic(state, now=now, started=started)
        if base is None or base["source"] != "activity":
            return base  # long waits and quiet spells are said plainly, never by the model
        with self._lock:
            cached = self._cache.get(key)
            asked = self._asked.get(key)
            self._spent = [t for t in self._spent if now - t < 3600.0]
            due = (ask and self.call is not None and key not in self._busy and len(self._spent) < HOURLY_CAP
                   and (asked is None or (base["basis"] - asked[1] >= PHASE_STEPS
                                          and now - asked[0] >= MODEL_EVERY_S)))
            due = due and self._allowed()
            if due:
                self._busy.add(key)
                self._asked[key] = (now, base["basis"])
                self._spent.append(now)
        if due:
            items = list((state or {}).get("items") or [])
            findings = list((state or {}).get("findings") or [])
            self.spawn(lambda: self._ask(key, request, items, findings, base["basis"], on_ready))
        if cached and now - cached["at"] <= MODEL_FRESH_S and base["basis"] - cached["basis"] <= PHASE_STEPS:
            return {**base, "now": cached["now"], "so_far": cached["so_far"] or base["so_far"], "source": "model"}
        return base

    def forget(self, key: str) -> None:
        """Drop the task's last line (its request was just corrected): the next one is asked afresh."""
        with self._lock:
            self._cache.pop(key, None)
            self._asked.pop(key, None)

    def _ask(self, key: str, request: str | None, items: list[dict[str, Any]], findings: list[dict[str, Any]],
             basis: int, on_ready: Callable[[], None] | None) -> None:
        try:
            got = parse_model(self.call(model_messages(request, items, findings)) if self.call else None)
        except Exception:
            logger.debug("speakeasy: live summary model call failed", exc_info=True)
            got = None
        with self._lock:
            self._busy.discard(key)
            if got:
                self._cache[key] = {**got, "at": self.clock(), "basis": basis}
                while len(self._cache) > 200:
                    self._cache.pop(next(iter(self._cache)))
        if got and on_ready is not None:
            try:
                on_ready()
            except Exception:
                pass


# -- Home's step capsule (build 73 home-fixes) ---------------------------------------------------
#
# Home draws a running job as a small capsule under the reply: a glyph for the KIND of thing the
# job is doing and its current step. ``step_kind`` names that kind from the job's newest activity
# item (the tool it called and the words activity.py already made of it), never from a guess.
# ``START_LINE`` is the step a job shows from the moment its run is under way until its first tool
# call lands: the model is working on its first turn then (build 73 data: a typed-chat job's first
# tool call came 35-38 s after it started, and until then Home had no step at all).

START_LINE = "Thinking it over"
STEP_KINDS = ("mail", "calendar", "web", "terminal", "doc", "edit", "search", "think", "message", "helper", "tool")
HOME_STEPS = 8   # the newest steps a running Home item carries (the capsule's expanded view)

_MAIL_RE = re.compile(r"(?i)\b(?:g?mail|e-?mails?|inbox|himalaya|agentmail|messages? from)\b|gmail")
_CAL_RE = re.compile(r"(?i)\b(?:calendars?|gcal|ical|agenda|schedule[ds]?)\b")
_FILE_TOOLS = frozenset({"read_file", "search_files", "write_file", "patch", "edit_file", "skill_view", "skill_manage"})
_WEB_TOOL_RE = re.compile(r"(?i)^(?:browser_|web_|fetch$|mcp__aside|mcp__browser)")


def step_kind(item: dict[str, Any] | None) -> str | None:
    """The kind of one activity item for Home's glyph: what the call touches first (mail, a
    calendar), else what kind of work it is (activity.py's ``kind``). None for no item."""
    if not isinstance(item, dict) or not item.get("text"):
        return None
    tool = str(item.get("tool") or item.get("_tool") or "")
    kind = str(item.get("kind") or "tool")
    text = str(item.get("text") or "")
    hay = f"{tool} {text}"
    if tool in _FILE_TOOLS:
        pass  # reading notes on "email-operations" or a file named calendar.md is still reading a file
    elif _MAIL_RE.search(hay):
        return "mail"
    elif _CAL_RE.search(hay):
        return "calendar"
    if kind == "browse" or _WEB_TOOL_RE.search(tool) or text.startswith(("Searched the web", "Fetched from the web")):
        return "web"
    if tool == "delegate_task" or tool == "subagent":
        return "helper"
    return {"read": "doc", "edit": "edit", "run": "terminal", "search": "search", "think": "think",
            "message": "message"}.get(kind, "tool")


def home_steps(activity: list[dict[str, Any]] | None, limit: int = HOME_STEPS) -> list[dict[str, Any]]:
    """The newest *limit* steps, oldest first, as Home's expanded capsule lists them: the words
    activity.py already wrote (past tense, plain, redacted), the kind, when, and whether it is the
    call still running. No arguments, no results, no raw tool names."""
    out: list[dict[str, Any]] = []
    for item in [i for i in (activity or []) if isinstance(i, dict) and i.get("text")][-limit:]:
        step: dict[str, Any] = {"text": _clean(str(item["text"]), 80) or str(item["text"])[:80],
                                "kind": step_kind(item) or "tool"}
        at = item.get("started_at") or item.get("at")
        if isinstance(at, (int, float)):
            step["at"] = float(at)
        if item.get("status") == "running":
            step["running"] = True
        out.append(step)
    return out
