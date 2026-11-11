"""The view fast path: "show me build 75 tickets" answered with a native list widget in seconds.

A request that only wants to SEE a list (tickets in a build, a saved Linear view, a search, a Kanban
board) never becomes a full agent task. Measured before this existed (11-11): the task agent took
12 model turns, loaded two skills (~116k chars) to list issues, wandered into Kanban and git, and
hand-drew an HTML/PNG "widget"; 558 s end to end.

Here instead: one call to the coordinator's fast model with a small prompt and two read-only tools
(``speakeasy_linear_view``, ``speakeasy_kanban_view``), exactly one tool call, then the result drawn
as a ``rows`` widget (views.py) by code, never by a model. No skills, no Hermes session, no images.
Each step is timed into ``runs.timings`` (``view_*_ms``).

Linear is read through ``<hermes_home>/scripts/linear.py`` (its own token handling; nothing here
reads, prints or stores a key). A Hermes without that script simply has no Linear tool, and a
request that can't be served here goes to the agent as before.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

LINEAR_TOOL = "speakeasy_linear_view"
KANBAN_TOOL = "speakeasy_kanban_view"
MAX_ROWS = 12          # views.CATALOG["rows"] cap
FETCH = 50             # issues read per list (the widget shows MAX_ROWS, the total says the rest)
MODEL_TIMEOUT_S = 15.0
MAX_TOKENS = 200

# "show me / list / what's in / pull up" + something listable. Anything that changes things, or asks
# why/how, is real work and goes to the agent.
_VERB = re.compile(r"(?i)^\s*(?:(?:hey|ok|okay|so|um|uh|can you|could you|please|todd)[,\s]+)*"
                   r"(?:show(?:\s+me)?|list|pull\s+up|bring\s+up|display|open|give\s+me|let\s+me\s+see|see|"
                   r"what(?:'s|s|\s+is|\s+are|'re)\s+(?:in|on|left\s+in|open\s+in)|which\s+(?:tickets|issues))\b")
_OBJECT = re.compile(r"(?i)\b(?:tickets?|issues?|build[\s_-]*\d+(?:\.\d+)?|linear|backlog|kanban|board|"
                     r"(?:saved\s+)?view|cards?|SPK-\d+)\b")
_WORK = re.compile(r"(?i)\b(?:fix|create|file|make|add|move|close|assign|reassign|update|change|merge|ship|delete|"
                   r"remove|comment|label|relabel|write|draft|start|stop|why|how|explain|summari[sz]e|review|"
                   r"compare|prioriti[sz]e|triage|plan|send|email|post)\b")
_BUILD = re.compile(r"(?i)\bbuild[\s_-]*(\d+(?:\.\d+)?)\b")

SYSTEM = ("You fetch a list for a phone app that draws it as a native widget. Call exactly one tool, once, "
          "then stop. speakeasy_linear_view: tickets / issues / a build / a Linear view / a search. "
          "speakeasy_kanban_view: a Kanban board of agent cards. Pass the user's own words for what to show "
          "(\"build 75\", \"Build 76 view\", \"swipe reply\"); never invent filters. Never answer in text.")

TOOLS = [
    {"type": "function", "function": {
        "name": LINEAR_TOOL,
        "description": "Read-only. Linear issues as a list widget: a build (label or view), a saved view by name, "
                       "or a free-text search.",
        "parameters": {"type": "object", "properties": {
            "filter": {"type": "string", "description": "What to show, e.g. 'build 75', 'Build 76', 'swipe reply'."},
            "kind": {"type": "string", "enum": ["build", "view", "query"],
                     "description": "build = a build number; view = a saved Linear view; query = search words."}},
            "required": ["filter"]}}},
    {"type": "function", "function": {
        "name": KANBAN_TOOL,
        "description": "Read-only. One Hermes Kanban board's cards as a list widget, needs-you first.",
        "parameters": {"type": "object", "properties": {
            "board": {"type": "string", "description": "Board name or slug; empty = the busiest board."}},
            "required": []}}},
]


def wanted(request: str | None) -> bool:
    """Only wants to see a list: "show me build 75 tickets", "what's in build 76", "pull up the
    speakeasy board". Never anything that changes something or asks for reasoning."""
    text = " ".join((request or "").split())
    if not text or len(text.split()) > 20 or _WORK.search(text):
        return False
    return bool(_VERB.search(text) and _OBJECT.search(text))


def messages(request: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": " ".join(request.split())[:400]}]


def prompt_chars(request: str) -> int:
    """The whole prompt the model sees (system + user + tool schemas), for the size budget."""
    return sum(len(m["content"]) for m in messages(request)) + len(json.dumps(TOOLS))


# -- Linear ------------------------------------------------------------------------------------------

_ISSUE = ("identifier title url priority updatedAt state{name type} assignee{displayName} "
          "delegate{displayName} labels(first:6){nodes{name}}")
_STATE_ORDER = {"started": 0, "unstarted": 1, "triage": 2, "backlog": 3, "completed": 4, "canceled": 5}


class LinearSource:
    """Read-only Linear through the profile's ``scripts/linear.py`` (loaded once, in process: one
    GraphQL request per list, ~0.1-1 s). Its ``gql`` exits on errors; that becomes a LookupError."""

    def __init__(self, script: Path):
        self.script = Path(script)
        self._mod: Any = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.script.is_file()

    def _load(self) -> Any:
        with self._lock:
            if self._mod is None:
                spec = importlib.util.spec_from_file_location("speakeasy_linear_cli", self.script)
                if spec is None or spec.loader is None:
                    raise LookupError("no Linear script")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                self._mod = mod
        return self._mod

    def _gql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        self._load()
        try:
            return self._mod.gql(query, variables or {})
        except SystemExit as exc:   # linear.py reports errors by exiting; never let that end the gateway
            raise LookupError(str(exc)[:200]) from None

    def team(self) -> str:
        return str(getattr(self._load(), "TEAM", "") or "")

    def issues(self, filter: str = "", kind: str = "") -> dict[str, Any]:
        words = " ".join(str(filter or "").split())[:200]
        if not words:
            raise LookupError("nothing to show")
        team = self.team()
        team_filter = {"team": {"key": {"eq": team}}} if team else {}
        build = _BUILD.search(words)
        if build or kind == "build":
            number = build.group(1) if build else re.sub(r"[^\d.]", "", words)
            title = f"Build {number}"
            got = self._view(title)
            if got is not None:
                return {**got, "title": title}
            nodes = self._gql("query($f:IssueFilter){issues(first:%d,filter:$f){nodes{%s}}}" % (FETCH, _ISSUE),
                              {"f": {**team_filter, "labels": {"name": {"eqIgnoreCase": f"build-{number}"}}}})
            return {"title": title, "scope": f"label build-{number}", "issues": nodes["issues"]["nodes"]}
        if kind in ("view", ""):
            got = self._view(words, contains=True)
            if got is not None:
                return got
        if re.fullmatch(r"[\w.+-]{2,40}", words):   # "agent-ready", "plugin": a label first
            nodes = self._gql("query($f:IssueFilter){issues(first:%d,filter:$f){nodes{%s}}}" % (FETCH, _ISSUE),
                              {"f": {**team_filter, "labels": {"name": {"eqIgnoreCase": words}}}})["issues"]["nodes"]
            if nodes:
                return {"title": words, "scope": "label", "issues": nodes}
        nodes = self._gql("query($t:String!,$f:IssueFilter){searchIssues(term:$t,first:%d,filter:$f){nodes{%s}}}"
                          % (MAX_ROWS * 2, _ISSUE), {"t": words, "f": team_filter})
        return {"title": f"\u201c{words[:40]}\u201d", "scope": "search", "issues": nodes["searchIssues"]["nodes"]}

    def _view(self, name: str, contains: bool = False) -> dict[str, Any] | None:
        op = "containsIgnoreCase" if contains else "eqIgnoreCase"
        name = re.sub(r"(?i)\s*\b(?:view|tickets?|issues?|in linear|linear)\b\s*", " ", name).strip() or name
        got = self._gql("query($n:String!){customViews(first:1,filter:{name:{%s:$n}}){nodes{name issues(first:%d)"
                        "{nodes{%s}}}}}" % (op, FETCH, _ISSUE), {"n": name})
        nodes = got["customViews"]["nodes"]
        if not nodes:
            return None
        return {"title": nodes[0]["name"], "scope": "view", "issues": nodes[0]["issues"]["nodes"]}


def _stamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _ago(stamp: Any, now: float) -> str:
    then = _stamp(stamp)
    if not then:
        return ""
    s = max(0, int(now - then))
    return f"{s // 60}m ago" if s < 3600 else f"{s // 3600}h ago" if s < 86400 else f"{s // 86400}d ago"


def linear_rows(got: dict[str, Any], now: float | None = None) -> tuple[dict[str, Any], str]:
    """A ``rows`` widget (entity issue) and one plain line saying what's in it."""
    now = time.time() if now is None else now
    issues = [i for i in got.get("issues") or [] if isinstance(i, dict) and i.get("title")]
    issues.sort(key=lambda i: (_STATE_ORDER.get(((i.get("state") or {}).get("type") or ""), 9), -_stamp(i.get("updatedAt"))))
    title = str(got.get("title") or "Issues")
    counts: dict[str, int] = {}
    for i in issues:
        state = (i.get("state") or {}).get("name") or "No state"
        counts[state] = counts.get(state, 0) + 1
    items = []
    for i in issues[:MAX_ROWS]:
        labels = [str(l.get("name")) for l in ((i.get("labels") or {}).get("nodes") or [])
                  if isinstance(l, dict) and l.get("name") and not _BUILD.fullmatch(str(l["name"]).replace("-", " "))]
        who = (i.get("assignee") or {}).get("displayName") or (i.get("delegate") or {}).get("displayName") or ""
        stype = (i.get("state") or {}).get("type") or ""
        items.append({"ref": i.get("identifier") or "", "name": i["title"], "state": (i.get("state") or {}).get("name") or "",
                      "tone": "good" if stype == "completed" else "neutral", "who": who,
                      "when": _ago(i.get("updatedAt"), now), "note": " · ".join(labels[:3]),
                      "url": i.get("url") or ""})
    total = len(issues)
    open_n = sum(1 for i in issues if ((i.get("state") or {}).get("type") or "") not in ("completed", "canceled"))
    scope = f"{total} issue{'s' if total != 1 else ''} · {open_n} open" if total else "No issues"
    view = {"kind": "rows", "source": "Linear", "entity": "issue", "title": title, "scope": scope,
            "total": total, "items": items}
    if not total:
        return view, f"{title} has no issues."
    parts = ", ".join(f"{n} {s.lower()}" for s, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return view, f"{title}: {total} issue{'s' if total != 1 else ''} ({parts})."


# -- Kanban ------------------------------------------------------------------------------------------

_COLUMN_TONE = {"blocked": "warn", "running": "neutral", "ready": "neutral", "done": "good"}


def kanban_board(boards: Any, board: str = "") -> dict[str, Any]:
    listing = boards.list()
    rows = listing.get("boards") or []
    want = re.sub(r"[^a-z0-9]+", "", (board or "").lower())
    slug = None
    if want:
        for r in rows:
            if want in (re.sub(r"[^a-z0-9]+", "", r["slug"].lower()), re.sub(r"[^a-z0-9]+", "", str(r["name"]).lower())):
                slug = r["slug"]
                break
        if slug is None:
            slug = next((r["slug"] for r in rows if want in re.sub(r"[^a-z0-9]+", "", f"{r['slug']}{r['name']}".lower())), None)
    slug = slug or listing.get("suggested")
    if not slug:
        raise LookupError("no boards")
    return boards.board(slug)


def kanban_rows(got: dict[str, Any]) -> tuple[dict[str, Any], str]:
    items, counts = [], []
    for col in got.get("columns") or []:
        counts.append((col.get("title") or col.get("key"), int(col.get("count") or 0)))
        for c in col.get("cards") or []:
            items.append({"name": c.get("title") or "Untitled card", "state": col.get("title") or "",
                          "tone": _COLUMN_TONE.get(col.get("key") or "", "neutral"), "who": c.get("assignee") or "",
                          "note": c.get("reason") or c.get("note") or c.get("summary") or ""})
    total = sum(n for _, n in counts)
    name = str(got.get("name") or got.get("slug") or "Board")
    scope = " · ".join(f"{n} {str(t).lower()}" for t, n in counts if n) or "Empty"
    view = {"kind": "rows", "source": "Kanban", "entity": "task", "title": name, "scope": scope,
            "total": total, "items": items[:MAX_ROWS]}
    return view, f"The {name} board: {scope.replace(' · ', ', ')}." if total else f"The {name} board is empty."


# -- the one model turn ------------------------------------------------------------------------------

def tool_call(answer: Any) -> tuple[str, dict[str, Any]] | None:
    """The single tool call in a model answer (``{"tool", "args"}``, home_chat's shape), or None.
    A Claude subscription route names tools ``mcp__<name>`` on the wire; the name is the same tool."""
    name = str(answer.get("tool") or "") if isinstance(answer, dict) else ""
    name = name.removeprefix("mcp__").removeprefix("functions.")
    if isinstance(answer, dict) and name in (LINEAR_TOOL, KANBAN_TOOL):
        raw = answer.get("args")
        args: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        return name, args
    return None


class Sources:
    """The tools' data: Linear (None when this Hermes has no linear.py) and Kanban boards."""

    def __init__(self, linear: LinearSource | None = None, boards: Any = None):
        self.linear = linear if linear is not None and linear.available() else None
        self.boards = boards

    def run(self, tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if tool == LINEAR_TOOL:
            if self.linear is None:
                raise LookupError("no Linear here")
            return linear_rows(self.linear.issues(str(args.get("filter") or ""), str(args.get("kind") or "")))
        if tool == KANBAN_TOOL:
            if self.boards is None:
                raise LookupError("no boards here")
            return kanban_rows(kanban_board(self.boards, str(args.get("board") or "")))
        raise LookupError(f"unknown tool {tool}")

    def tools(self) -> list[dict[str, Any]]:
        return [t for t in TOOLS if (t["function"]["name"] != LINEAR_TOOL or self.linear is not None)
                and (t["function"]["name"] != KANBAN_TOOL or self.boards is not None)]


def serve(request: str, call: Callable[[list[dict[str, str]], list[dict[str, Any]]], Any], sources: Sources,
          mark: Callable[[str, int], None] | None = None) -> dict[str, Any] | None:
    """One model turn + one tool + render. Returns ``{"spoken", "view", "tool", "args", "model"}`` or
    None (no tools here, the model didn't call one, or the tool failed): the request then goes to the
    agent as before. *mark*(name, ms) records each step's time."""
    mark = mark or (lambda name, ms: None)
    tools = sources.tools()
    if not tools:
        return None
    started = time.monotonic()
    try:
        got = call(messages(request), tools)
    except Exception as exc:  # noqa: BLE001 - the fast path is a nicety; the agent still answers
        logger.info("speakeasy: view fast path model failed (%s)", type(exc).__name__)
        return None
    answer, model = got if isinstance(got, tuple) else (got, None)
    t_model = time.monotonic()
    mark("view_first_model_ms", int((t_model - started) * 1000))
    picked = tool_call(answer)
    if picked is None or picked[0] not in {t["function"]["name"] for t in tools}:
        logger.info("speakeasy: view fast path model called no view tool; to the agent")
        return None
    try:
        data_view, spoken = sources.run(*picked)
    except Exception as exc:  # noqa: BLE001
        logger.info("speakeasy: view fast path tool failed (%s)", type(exc).__name__)
        return None
    t_tool = time.monotonic()
    mark("view_tool_ms", int((t_tool - t_model) * 1000))
    from . import views as views_mod
    shown = views_mod.clean_all([data_view])
    mark("view_render_ms", int((time.monotonic() - t_tool) * 1000))
    if not shown:
        return None
    return {"spoken": spoken, "views": shown, "tool": picked[0], "args": picked[1], "model": model}
