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

``deterministic`` needs no model. ``Summarizer`` adds the plugin's side model on top (Hermes'
auxiliary client, router.aux_call): at most one call per task every ``MODEL_EVERY_S`` and only when
something new happened; its words are vetted and used while fresh, else the deterministic line.
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
MODEL_EVERY_S = 40.0      # at most one side-model summary per task this often
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
    found = [t for t in (_clean(f.get("text"), 80) for f in findings if isinstance(f, dict)) if t]
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
            line = f"Waiting on {target}" if target else present(text)
            return {"now": f"{line} \u00b7 {_minutes(ran)}", "so_far": so_far, "quiet_s": None,
                    "source": "activity", "basis": basis}
    quiet = now - last_at
    if running is None and quiet >= IDLE_AFTER_S:
        return {"now": f"No new steps \u00b7 {_minutes(quiet)}", "so_far": so_far, "quiet_s": int(quiet),
                "source": "idle", "basis": basis}
    recent = [i for i in items if _ended(i) >= now - WINDOW_S] or items[-1:]
    return {"now": _clean(_now_line(recent), NOW_CHARS) or present(str(items[-1]["text"]))[:NOW_CHARS],
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
    now = _clean(data.get("now"), NOW_CHARS)
    if not now or re.search(r"\bstep \d|\d+ of \d+", now, re.I) or not 2 <= len(now.split()) <= 16:
        return None
    so_far = _clean(data.get("so_far"), SO_FAR_CHARS)
    return {"now": now, "so_far": so_far or ""}


class Summarizer:
    """The side model's live line per task: asked off the request path, rate-limited per task,
    used only while fresh and not behind the feed. ``call`` is router.aux_call-shaped."""

    def __init__(self, call: Callable[[list[dict[str, str]]], str | None] | None,
                 clock: Callable[[], float] = time.time, spawn: Callable[[Callable[[], None]], Any] | None = None):
        self.call = call
        self.clock = clock
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._asked: dict[str, tuple[float, int]] = {}
        self._busy: set[str] = set()

    def line(self, key: str, state: dict[str, Any] | None, *, request: str | None, started: float | None,
             on_ready: Callable[[], None] | None = None) -> dict[str, Any] | None:
        now = self.clock()
        base = deterministic(state, now=now, started=started)
        if base is None or base["source"] != "activity":
            return base  # long waits and quiet spells are said plainly, never by the model
        with self._lock:
            cached = self._cache.get(key)
            asked = self._asked.get(key)
            due = (self.call is not None and key not in self._busy
                   and (asked is None or (base["basis"] != asked[1] and now - asked[0] >= MODEL_EVERY_S)))
            if due:
                self._busy.add(key)
                self._asked[key] = (now, base["basis"])
        if due:
            items = list((state or {}).get("items") or [])
            findings = list((state or {}).get("findings") or [])
            self.spawn(lambda: self._ask(key, request, items, findings, base["basis"], on_ready))
        if cached and now - cached["at"] <= MODEL_FRESH_S and base["basis"] - cached["basis"] <= 3:
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
