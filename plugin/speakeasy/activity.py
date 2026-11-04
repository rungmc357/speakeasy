"""Live job activity: what a running task is doing, has found and has touched (docs/API.md "Live job
activity", capability ``job_activity``).

Everything here comes from the run's own events, never from a guess:

- ``tool.started`` / ``tool.completed`` {tool, preview, tool_call_id, error} -> ``activity`` items and
  ``touched`` (files, URLs, commands). ``preview`` is Hermes' redacted one-line argument summary on
  start and a bounded, redacted result on completion; it is humanized into one short sentence here and
  redacted again (tokens, keys, env values, emails, query strings; paths become their basename).
- ``message.interim`` {text} (the agent's commentary beside its tool calls) -> ``findings`` when a
  sentence states something learned (or an explicit ``FINDING:`` line), and a ``message`` item for an
  explicit ``STATUS:`` line.
- ``files.changed`` {files: [{path, added, removed}], diff} and a ``patch`` result's diff -> +/- counts.
- ``reasoning.available`` -> one "Thinking it over" item (never its text); ``subagent.start`` /
  ``subagent.complete`` -> a helper item; ``approval.request`` -> "Asked for your approval".

``hero`` is derived only when the data clearly supports it: ``trials`` from an explicit
"31/50 passed"-style count the run itself reported, ``diff`` from counted line changes, ``sources``
from two or more pages read with nothing edited, else ``steps`` once there are a few items.
``series`` is never derived: Hermes doesn't report data points on its event stream.

State is kept per task (``StateStore.set_activity``) and written at most once a second while the
run works (``FLUSH_EVERY_S``), immediately on its terminal event; clients read it from the task's
work object, so a reopened app shows the same feed.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from typing import Any

from .text import EMAIL_RE, OPAQUE_RE, SECRET_FILE_RE, SENSITIVE_TEXT_RE, URL_SECRET_RE

MAX_ACTIVITY = 100        # kept per task; GET /voice/work/{run_id} returns them all, with details
MAX_STREAM_ACTIVITY = 40  # the newest this many ride the event stream / task feed, without details
ARGS_CHARS = 300          # a tool call's argument preview (Hermes' own, redacted again here)
RESULT_CHARS = 500        # its result preview
BLOCK_LINES = 16
MAX_FINDINGS = 8
MAX_TOUCHED = 20
MAX_SOURCES = 6
TEXT_CHARS = 80
DETAIL_CHARS = 100
FINDING_CHARS = 120
LABEL_CHARS = 48
PREVIEW_LINES = 6
FLUSH_EVERY_S = 1.0
STEPS_AFTER = 3  # a "steps" hero only once there is a path to show
KINDS = ("read", "edit", "run", "search", "browse", "think", "message", "tool")
MINUS = "\u2212"

# -- redaction -------------------------------------------------------------------------------------

_URL_RE = re.compile(r"\bhttps?://[^\s'\"<>()\[\]{},]+", re.I)
_TOKEN_RES = (
    re.compile(r"(?i)\b(?:authorization|proxy-authorization)\s*:\s*\S+(?:\s+\S+)?"),
    re.compile(r"(?i)\bbearer\s+\S+"),
    re.compile(r"\b(?:sk|pk|rk)-(?:[A-Za-z0-9]+-)?[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{10,}|\bgithub_pat_[A-Za-z0-9_]{10,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]+)?"),
)
# KEY=value (env assignments, --flag=value for secret-ish flags, key: value for secret-ish keys)
_ENV_RE = re.compile(r"\b([A-Z][A-Z0-9_]{1,63})=(\"[^\"]*\"|'[^']*'|\S+)")
_SECRET_FLAG_RE = re.compile(
    r"(?i)(--?[a-z0-9-]*(?:token|key|secret|passw(?:or)?d|auth|credential|cookie)[a-z0-9-]*)(?:[= ]+)(\"[^\"]*\"|'[^']*'|\S+)")
_SECRET_KV_RE = re.compile(
    r"(?i)\b([a-z0-9_]*(?:token|api[_-]?key|secret|passw(?:or)?d|credential|cookie)[a-z0-9_]*)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)")
# The same, quoted (JSON arguments and results): "api_key": "value"
_SECRET_JSON_RE = re.compile(
    r"(?i)([\"']?[a-z0-9_-]*(?:token|api[_-]?key|secret|passw(?:or)?d|credential|cookie|authorization)[a-z0-9_-]*[\"']?)"
    r"(\s*[:=]\s*)(\"(?:[^\"\\\\]|\\\\.)*\"|'[^']*'|[^\s,}\]]+)")
_PATH_RE = re.compile(r"(?<![\w/.:-])(?:~|\.{1,2})?(?:/[^\s/'\"<>|;,)]+){1,}/?|(?<![\w/.:~-])[\w.-]+(?:/[\w.@+-]+){2,}/?"
                      r"|(?<![\w/.:~-])[A-Za-z_][\w.-]*/[\w@+-][\w.@+-]*\.[A-Za-z][A-Za-z0-9]{0,7}\b")
_ELLIPSIS_LEAD_RE = re.compile(r"^(?:\.\.\.|…)\s*")


def _basename(path: str) -> str:
    path = path.rstrip("/")
    name = path.rsplit("/", 1)[-1] if "/" in path else path
    if not name or name in {"~", ".", ".."}:
        return "a folder"
    if SECRET_FILE_RE.search(name):
        return "a private file"
    return name


def _short_url(url: str) -> tuple[str, str | None] | None:
    """(label, https link) for one URL, dropping the query and fragment; None when it isn't web."""
    try:
        parts = urllib.parse.urlsplit(url.rstrip(".,;:!?"))
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", host):
        return None
    host = host[4:] if host.startswith("www.") else host
    segments = [s for s in parts.path.split("/") if s]
    if any(OPAQUE_RE.search(s) for s in segments):
        segments = segments[:1] if segments and not OPAQUE_RE.search(segments[0]) else []
    path = "/".join(segments)
    label = host + ("/" + path if path else "")
    if len(label) > LABEL_CHARS:
        tail = segments[-1] if segments else ""
        label = (host + "/…/" + tail) if tail and len(host) + len(tail) < LABEL_CHARS - 3 else host
    link = None
    if parts.scheme == "https" and not parts.username and not parts.password:
        link = "https://" + (parts.hostname or "") + (("/" + "/".join(segments)) if segments else "")
    return label, link


def redact(value: Any) -> str:
    """One line of display copy with secrets, tokens, env values, emails and query strings taken out
    and every path shortened to its basename."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    urls: list[str] = []

    def keep_url(m: re.Match[str]) -> str:
        short = _short_url(m.group(0))
        urls.append(short[0] if short else "a link")
        return f"\x00{len(urls) - 1}\x00"

    text = _URL_RE.sub(keep_url, text)
    for pattern in _TOKEN_RES:
        text = pattern.sub("…", text)
    text = _SECRET_JSON_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}…", text)
    text = _ENV_RE.sub(lambda m: f"{m.group(1)}=…", text)
    text = _SECRET_FLAG_RE.sub(lambda m: f"{m.group(1)} …", text)
    text = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=…", text)
    text = EMAIL_RE.sub("an email address", text)
    text = _PATH_RE.sub(lambda m: _basename(m.group(0)), text)
    text = OPAQUE_RE.sub("…", text)
    text = re.sub(r"\x00(\d+)\x00", lambda m: urls[int(m.group(1))], text)
    text = re.sub(r"[\"']…[\"']?|…[\"']", "…", text)
    return re.sub(r"(?:…\s*){2,}", "… ", text).strip()


_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def tool_name(value: Any) -> str | None:
    """The raw tool name (``terminal``, ``mcp__tracker__create_issue``), or None when it isn't one."""
    return value.strip() if isinstance(value, str) and _TOOL_NAME_RE.fullmatch(value.strip()) else None


def preview_block(value: Any, limit: int) -> str | None:
    """A tool call's argument or result preview as a few monospaced lines: every line through the
    same redactor as the feed (secrets, tokens, env values, emails, query strings out; paths to
    their basename), lines that still look secret dropped, then cut to ``limit`` characters.
    Redacted before the cut, so a cut never leaves a secret's prefix."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return None
    lines: list[str] = []
    for raw in value.replace("\r", "").split("\n")[: BLOCK_LINES * 4]:
        indent = min(len(raw) - len(raw.lstrip(" \t")), 8)
        line = redact(raw)
        if not line:
            continue
        probe = re.sub(r"\s*[=:]\s*…", " ", line)
        if SENSITIVE_TEXT_RE.search(probe) or URL_SECRET_RE.search(probe):
            line = "…"
        lines.append(" " * indent + line)
        if len(lines) >= BLOCK_LINES:
            break
    text = "\n".join(lines).strip("\n")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text if re.search(r"[A-Za-z0-9]", text) else None


def _unsafe(text: str) -> bool:
    text = re.sub(r"\s*[=:]\s*…", " ", text)  # an already-redacted "KEY=…" names a key, not its value
    return bool(SENSITIVE_TEXT_RE.search(text) or URL_SECRET_RE.search(text) or "```" in text
                or re.search(r"[{}\[\]]\s*\"", text))  # raw JSON


def clip(text: str, limit: int) -> str:
    """At most ``limit`` characters, cut at a word with an ellipsis."""
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def plain(value: Any, limit: int) -> str | None:
    """Redacted, bounded display copy, or None when nothing safe is left."""
    text = clip(redact(value), limit)
    if not text or _unsafe(text) or not re.search(r"[A-Za-z0-9]", text):
        return None
    return text


# -- humanizing one tool call ---------------------------------------------------------------------

_TEST_RE = re.compile(r"(?i)\b(?:pytest|jest|vitest|mocha|rspec|go test|cargo test|swift test|xcodebuild test|"
                      r"(?:npm|pnpm|yarn|bun)(?: run)? test|playwright test|cypress run|phpunit|tox|ctest)\b")
_CHECK_RE = re.compile(r"(?i)\b(?:tsc --noEmit|mypy|pyright|eslint|ruff|flake8|pylint|swiftlint|golangci-lint|"
                       r"(?:npm|pnpm|yarn|bun)(?: run)? (?:lint|typecheck|check)|cargo (?:check|clippy))\b")
_BUILD_RE = re.compile(r"(?i)\b(?:(?:npm|pnpm|yarn|bun)(?: run)? build|make|cargo build|swift build|xcodebuild|go build|tsc|gradle|mvn)\b")
_INSTALL_RE = re.compile(r"(?i)\b(?:(?:npm|pnpm|yarn|bun) (?:install|add|ci)|pip3? install|uv (?:pip install|add|sync)|brew install|cargo add)\b")
_GIT_RE = re.compile(r"(?i)^\s*git\s+(status|diff|log|show|blame|checkout|switch|commit|add|stash|branch|pull|fetch|grep)\b")
_GIT_WORDS = {"status": "Checked git status", "diff": "Looked at the changes", "log": "Read the history",
              "show": "Looked at a commit", "blame": "Checked who changed a line", "checkout": "Switched branch",
              "switch": "Switched branch", "commit": "Committed the changes", "add": "Staged changes",
              "stash": "Stashed changes", "branch": "Checked branches", "pull": "Pulled the latest changes",
              "fetch": "Fetched the latest changes", "grep": "Searched the code"}

_SIMPLE = {  # tool -> (kind, sentence) where the argument says nothing worth showing (or is private)
    "browser_click": ("browse", "Clicked on the page"),
    "browser_type": ("browse", "Typed into the page"),   # never what was typed
    "browser_press": ("browse", "Pressed a key on the page"),
    "browser_scroll": ("browse", "Scrolled the page"),
    "browser_back": ("browse", "Went back a page"),
    "browser_snapshot": ("browse", "Looked at the page"),
    "browser_vision": ("browse", "Looked at the page"),
    "browser_get_images": ("browse", "Looked at the page's images"),
    "browser_console": ("browse", "Checked the page's console"),
    "browser_exec": ("browse", "Worked in the browser"),
    "vision_analyze": ("read", "Looked at an image"),
    "image_generate": ("tool", "Made an image"),
    "video_generate": ("tool", "Made a video"),
    "text_to_speech": ("tool", "Recorded audio"),
    "todo_list": ("think", "Updated its to-do list"),
    "todo": ("think", "Updated its to-do list"),
    "memory": ("think", "Updated its notes"),
    "session_search": ("search", "Looked through past conversations"),
    "chat_history_lookup": ("search", "Looked through past conversations"),
    "skills_list": ("read", "Checked its skills"),
    "clarify": ("message", "Asked you a question"),
    "send_message": ("message", "Sent a message"),
    "process_manage": ("run", "Checked a background job"),
    "cronjob_manage": ("tool", "Updated a scheduled job"),
    "tool_search": ("tool", "Looked for the right tool"),
    "tool_describe": ("tool", "Looked up how a tool works"),
    "execute_code": ("run", "Ran a script"),
}


def _preview(value: Any) -> str:
    text = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
    return _ELLIPSIS_LEAD_RE.sub("", text)


def _file_arg(preview: str) -> str | None:
    """The file a read/write/patch preview names (Hermes sends the path, or for reads
    ``<basename> L10-40``), as its basename."""
    first = preview.split(" ")[0] if preview else ""
    if not first:
        return None
    name = _basename(first)
    return name if name and plain(name, LABEL_CHARS) else None


def _tool_words(tool: str) -> str:
    """A tool's name in words: ``mcp__linear__create_issue`` -> "linear create issue"."""
    name = re.sub(r"^mcp__", "", tool)
    words = re.sub(r"[_\W]+", " ", name).strip().lower()
    return clip(words, 40) or "a tool"


def _command_label(preview: str) -> str | None:
    return plain(preview, LABEL_CHARS)


def humanize(tool: Any, preview: Any) -> dict[str, Any] | None:
    """One tool call -> {kind, text, detail?, touched?}, or None when there is no tool name. The
    sentence only says what the call itself shows (the tool and its argument preview)."""
    if not isinstance(tool, str) or not tool.strip() or tool in {"_thinking", "thread"}:
        return None
    tool = tool.strip()
    arg = _preview(preview)
    out: dict[str, Any]
    if tool == "read_file":
        name = _file_arg(arg)
        out = {"kind": "read", "text": f"Read {name}" if name else "Read a file"}
        if name:
            out["touched"] = {"kind": "file", "label": name}
    elif tool in {"write_file", "patch", "edit_file", "apply_patch", "str_replace_editor"}:
        name = _file_arg(arg)
        verb = "Wrote" if tool == "write_file" else "Edited"
        out = {"kind": "edit", "text": f"{verb} {name}" if name else f"{verb} a file"}
        if name:
            out["touched"] = {"kind": "file", "label": name, "edited": True}
    elif tool in {"search_files", "grep", "glob"}:
        q = plain(arg, 40)
        out = {"kind": "search", "text": f"Searched the files for “{q}”" if q else "Searched the files"}
    elif tool == "web_search":
        q = plain(arg, 50)
        out = {"kind": "search", "text": f"Searched the web for “{q}”" if q else "Searched the web"}
    elif tool in {"web_extract", "browser_navigate", "web_fetch", "fetch"}:
        match = _URL_RE.search(arg)
        short = _short_url(match.group(0)) if match else None
        verb = "Opened" if tool == "browser_navigate" else "Read"
        out = {"kind": "browse", "text": f"{verb} {short[0]}" if short else f"{verb} a web page"}
        if short:
            out["touched"] = {"kind": "url", "label": short[0], **({"url": short[1]} if short[1] else {})}
            out["source"] = {"site": short[0].split("/", 1)[0], "label": short[0]}
    elif tool in {"terminal", "bash", "shell", "run_command", "exec_command"}:
        out = _humanize_command(arg)
    elif tool == "skill_view":
        name = plain(arg, 40)
        out = {"kind": "read", "text": f"Read its notes on {name}" if name else "Read its notes"}
    elif tool == "skill_manage":
        out = {"kind": "think", "text": "Updated its notes"}
    elif tool == "delegate_task":
        goal = plain(arg, DETAIL_CHARS)
        out = {"kind": "tool", "text": "Handed part of the work to a helper", **({"detail": goal} if goal else {})}
    elif tool == "tool_call":
        name = plain(arg.split(" ")[0], 40) if arg else None
        out = {"kind": "tool", "text": f"Used {_tool_words(name)}" if name else "Used a tool"}
    elif tool in _SIMPLE:
        kind, text = _SIMPLE[tool]
        out = {"kind": kind, "text": text}
    else:
        out = {"kind": "tool", "text": f"Used {_tool_words(tool)}"}
    out["text"] = clip(out["text"], TEXT_CHARS)
    return out


def _humanize_command(command: str) -> dict[str, Any]:
    label = _command_label(command)
    out: dict[str, Any] = {"kind": "run"}
    git = _GIT_RE.match(command)
    if git:
        out["text"] = _GIT_WORDS[git.group(1).lower()]
    elif _TEST_RE.search(command):
        out["text"] = "Ran the tests"
    elif _CHECK_RE.search(command):
        out["text"] = "Checked the code"
    elif _BUILD_RE.search(command):
        out["text"] = "Built the project"
    elif _INSTALL_RE.search(command):
        out["text"] = "Installed packages"
    elif re.match(r"(?i)\s*(?:ls|find|tree|fd)\b", command):
        out.update(kind="search", text="Looked through the folders")
    elif re.match(r"(?i)\s*(?:cat|head|tail|less|sed -n|bat)\b", command):
        out.update(kind="read", text="Read a file")
    elif re.match(r"(?i)\s*(?:rg|grep|ag|ack)\b", command):
        out.update(kind="search", text="Searched the code")
    elif re.match(r"(?i)\s*(?:curl|wget|http)\b", command):
        out["text"] = "Fetched from the web"
    else:
        out["text"] = f"Ran {label}" if label and len(label) <= 40 else "Ran a command"
    detail = plain(command, DETAIL_CHARS)
    if detail:
        out["detail"] = detail
    if label:
        out["touched"] = {"kind": "command", "label": label}
    return out


# -- counts in a tool's result --------------------------------------------------------------------

def diff_counts(diff: str) -> tuple[int, int]:
    added = removed = 0
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _diff_preview(diff: str) -> str | None:
    lines = []
    for line in diff.splitlines():
        if (line.startswith("+") or line.startswith("-")) and not line.startswith(("+++", "---")):
            safe = plain(line[0] + " " + line[1:].strip(), 80) if line[1:].strip() else None
            if safe:
                lines.append(line[0] + safe[1:] if safe[0] in "+-" else safe)
        if len(lines) >= PREVIEW_LINES:
            break
    return "\n".join(lines) or None


def patch_result(preview: Any) -> dict[str, Any] | None:
    """A patch tool's result (``{"success": true, "diff": "..."}``), only when it arrived whole:
    Hermes cuts long results at 500 characters, and a cut diff would give wrong counts."""
    if not isinstance(preview, str) or not preview.strip().startswith("{"):
        return None
    try:
        data = json.loads(preview)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("success") or not isinstance(data.get("diff"), str):
        return None
    diff = data["diff"]
    files = [f for k in ("files_modified", "files_created") for f in (data.get(k) or []) if isinstance(f, str)]
    added, removed = diff_counts(diff)
    return {"files": files, "added": added, "removed": removed, "diff": diff}


_TRIALS_RE = re.compile(
    r"(?i)\b(\d{1,6})\s*(?:/|out of|of)\s*(\d{1,6})\s*(?:runs?|trials?|attempts?|iterations?|times?|reruns?|repeats?)?"
    r"\s*(?:have\s+|so far\s+)?(passed|passing|succeeded|green|ok|failed|failing|flaked|red)\b")
_TRIALS_AND_RE = re.compile(r"(?i)\b(\d{1,6})\s+(passed|failed)\b")


def trials_in(text: Any) -> dict[str, int] | None:
    """An explicit repeated-run count the run reported ("31/50 passed", "4 of 50 runs failed",
    "Run 50 of 50: 46 passed, 4 failed"); None otherwise. ``failed`` only when stated."""
    if not isinstance(text, str):
        return None
    match = None
    for match in _TRIALS_RE.finditer(text):
        pass
    if match is None:
        return None
    n, total, word = int(match.group(1)), int(match.group(2)), match.group(3).lower()
    if not 2 <= total <= 100000 or n > total:
        return None
    good = word in {"passed", "passing", "succeeded", "green", "ok"}
    out = {"total": total, "passed": n} if good else {"total": total, "failed": n}
    # the other side named next to it ("…, 4 failed")
    rest = text[match.end(): match.end() + 40]
    other = _TRIALS_AND_RE.search(rest)
    if other and other.group(2).lower() == ("failed" if good else "passed"):
        out["failed" if good else "passed"] = int(other.group(1))
    if out.get("passed", 0) + out.get("failed", 0) > total:
        return None
    return out


# -- findings from the agent's commentary --------------------------------------------------------

_INTENT_RE = re.compile(
    r"(?i)^(?:let me|let's|lets|i'll|i will|i'm going|i am going|going to|now\b|next\b|first\b|then\b|"
    r"ok\b|okay\b|alright\b|sure\b|great\b|perfect\b|good\b|checking|looking|starting|trying|running|reading|"
    r"i need to|i should|i want|i'd like|i'm\s+\w+ing|i am\s+\w+ing|time to|on it|working on)")
_EXPLICIT_FINDING_RE = re.compile(r"(?im)^[ \t]*(?:FINDING|FOUND|LEARNED|NOTE)S?:[ \t]*(.+?)[ \t]*$")
_DIRECTIVE_LINE_RE = re.compile(r"(?im)^[ \t]*(?:STATUS|DETAIL|MEDIA|DONE|SPOKEN|FINDING|FOUND|LEARNED|NOTE)S?:.*$")


def findings_in(text: Any) -> list[str]:
    """Things the agent says it has learned, in its own words: explicit ``FINDING:`` lines, else the
    statements in its commentary that aren't about what it will do next."""
    if not isinstance(text, str) or not text.strip():
        return []
    explicit = [m.group(1) for m in _EXPLICIT_FINDING_RE.finditer(text)]
    if explicit:
        candidates = explicit
    else:
        body = _DIRECTIVE_LINE_RE.sub("", text)
        body = re.sub(r"```.*?```", " ", body, flags=re.S)
        candidates = []
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", body):
            sentence = sentence.strip(" -*•\t")
            if len(sentence.split()) < 4 or _INTENT_RE.match(sentence) or sentence.endswith("?"):
                continue
            if sentence.endswith(":"):
                continue
            candidates.append(sentence)
    out = []
    for raw in candidates[:2]:
        safe = plain(raw.rstrip("."), FINDING_CHARS)
        if safe and safe not in out:
            out.append(safe)
    return out


# -- one task's state ----------------------------------------------------------------------------

def new_state() -> dict[str, Any]:
    return {"v": 1, "next": 1, "next_f": 1, "items": [], "findings": [], "touched": [], "edits": {},
            "sources": [], "trials": None, "diff_preview": None}


def _status_for(terminal: str) -> str:
    return "done" if terminal == "completed" else "failed"


class JobActivity:
    """The activity of one task, built from its run's events. Not thread-safe on its own; the
    module-level ``observe`` serializes access per store."""

    def __init__(self, state: dict[str, Any] | None = None):
        self.state = state if isinstance(state, dict) and state.get("v") == 1 else new_state()
        self.dirty = False
        self.flushed_at = 0.0
        self.timer: threading.Timer | None = None

    # -- recording --
    def _add(self, kind: str, text: str, now: float, *, detail: str | None = None, status: str = "done",
             call_id: str | None = None, tool: str | None = None, raw_tool: str | None = None,
             args: str | None = None) -> dict[str, Any]:
        items = self.state["items"]
        last = items[-1] if items else None
        if last and last["text"] == text and last.get("detail") == detail and last["status"] != "running" \
                and status != "running":
            last["at"] = now
            return last
        item = {"id": f"a{self.state['next']}", "at": round(now, 3), "kind": kind, "text": text, "status": status}
        if detail:
            item["detail"] = detail
        if call_id:
            item["_call"] = call_id
        if tool:
            item["_tool"] = tool
        if raw_tool:  # a real tool call: its name, when it started, and what it was given
            item["tool"] = raw_tool
            item["started_at"] = round(now, 3)
            if args:
                item["args"] = args
        self.state["next"] += 1
        items.append(item)
        del items[:-MAX_ACTIVITY]
        return item

    def _touch(self, entry: dict[str, Any]) -> None:
        touched = self.state["touched"]
        key = (entry["kind"], entry["label"])
        for existing in touched:
            if (existing["kind"], existing["label"]) == key:
                if entry.get("edited"):
                    existing["edited"] = True
                touched.remove(existing)
                touched.append(existing)  # most recent last
                return
        touched.append({k: v for k, v in entry.items() if k in {"kind", "label", "url", "edited"}})
        del touched[:-MAX_TOUCHED]

    def _count_edit(self, name: str, added: int, removed: int, *, absolute: bool = False) -> None:
        edits = self.state["edits"]
        prev = edits.get(name, [0, 0])
        edits[name] = [added, removed] if absolute else [prev[0] + added, prev[1] + removed]
        self._touch({"kind": "file", "label": name, "edited": True})

    def _finding(self, text: str, now: float) -> bool:
        findings = self.state["findings"]
        if any(f["text"] == text for f in findings):
            return False
        findings.append({"id": f"f{self.state['next_f']}", "at": round(now, 3), "text": text})
        self.state["next_f"] += 1
        del findings[:-MAX_FINDINGS]
        return True

    def _trials(self, text: Any) -> bool:
        trials = trials_in(text)
        if not trials:
            return False
        prev = self.state.get("trials") or {}
        if prev.get("total") == trials["total"]:
            trials = {**prev, **trials}
        if trials == prev:
            return False
        self.state["trials"] = trials
        return True

    def _find_running(self, call_id: Any, tool: Any) -> dict[str, Any] | None:
        for item in reversed(self.state["items"]):
            if item["status"] != "running":
                continue
            if call_id and item.get("_call") == call_id:
                return item
            if not call_id and tool and item.get("_tool") == tool:
                return item
        return None

    def observe(self, event: dict[str, Any], now: float | None = None) -> bool:
        """Take one run event; True when the public activity may have changed."""
        if not isinstance(event, dict):
            return False
        now = time.time() if now is None else now
        kind = event.get("event")
        changed = False
        if kind in {"tool.started", "tool.start"}:
            human = humanize(event.get("tool"), event.get("preview"))
            if human:
                self._add(human["kind"], human["text"], now, detail=human.get("detail"), status="running",
                          call_id=event.get("tool_call_id") if isinstance(event.get("tool_call_id"), str) else None,
                          tool=event.get("tool"), raw_tool=tool_name(event.get("tool")),
                          args=preview_block(event.get("preview"), ARGS_CHARS))
                if human.get("touched"):
                    self._touch(human["touched"])
                if human.get("source"):
                    sources = self.state["sources"]
                    if all(s["label"] != human["source"]["label"] for s in sources):
                        sources.append(human["source"])
                        del sources[:-MAX_SOURCES]
                changed = True
        elif kind in {"tool.completed", "tool.complete", "tool.failed"}:
            tool = event.get("tool")
            failed = kind == "tool.failed" or bool(event.get("error"))
            item = self._find_running(event.get("tool_call_id"), tool)
            if item is not None:
                item["status"] = "failed" if failed else "done"
                if "tool" in item:
                    item["ended_at"] = round(now, 3)
                    result = preview_block(event.get("preview"), RESULT_CHARS)
                    if result:
                        item["result"] = result
                changed = True
            if not failed and tool in {"patch", "edit_file", "apply_patch"}:
                result = patch_result(event.get("preview"))
                if result:
                    names = [_basename(f) for f in result["files"]] or (
                        [item["text"].split(" ", 1)[1]] if item and " " in item["text"] else [])
                    if len(names) == 1 and (result["added"] or result["removed"]):
                        self._count_edit(names[0], result["added"], result["removed"])
                        self.state["diff_preview"] = _diff_preview(result["diff"]) or self.state.get("diff_preview")
                        changed = True
            if tool in {"terminal", "execute_code", "bash", "shell"}:
                changed = self._trials(event.get("preview")) or changed
        elif kind == "files.changed":
            files = event.get("files") if isinstance(event.get("files"), list) else []
            for f in files[:MAX_TOUCHED]:
                if not isinstance(f, dict) or not isinstance(f.get("path"), str):
                    continue
                added, removed = f.get("added"), f.get("removed")
                if isinstance(added, int) and isinstance(removed, int) and added >= 0 and removed >= 0:
                    self._count_edit(_basename(f["path"]), added, removed, absolute=True)  # cumulative
                    changed = True
            if isinstance(event.get("diff"), str):
                self.state["diff_preview"] = _diff_preview(event["diff"]) or self.state.get("diff_preview")
        elif kind == "message.interim":
            text = event.get("text")
            status = re.search(r"(?im)^[ \t]*STATUS:[ \t]*(.+?)[ \t]*$", text) if isinstance(text, str) else None
            if status:
                line = plain(status.group(1), TEXT_CHARS)
                detail = re.search(r"(?im)^[ \t]*DETAIL:[ \t]*(.+?)[ \t]*$", text)
                if line:
                    self._add("message", line, now, detail=plain(detail.group(1), DETAIL_CHARS) if detail else None)
                    changed = True
            for finding in findings_in(text):
                if not trials_in(finding):  # a running count is the hero, not something learned
                    changed = self._finding(finding, now) or changed
            changed = self._trials(text) or changed
        elif kind == "reasoning.available":
            items = self.state["items"]
            if not items or items[-1]["kind"] != "think" or items[-1]["text"] != "Thinking it over":
                self._add("think", "Thinking it over", now)
                changed = True
        elif kind == "subagent.start":
            goal = plain(event.get("goal") or event.get("preview"), DETAIL_CHARS)
            self._add("tool", "Handed part of the work to a helper", now, detail=goal, status="running",
                      tool="subagent", raw_tool="delegate_task",
                      args=preview_block(event.get("goal") or event.get("preview"), ARGS_CHARS))
            changed = True
        elif kind == "subagent.complete":
            item = self._find_running(None, "subagent")
            if item is not None:
                item["status"] = "failed" if str(event.get("status") or "").lower() in {"failed", "error"} else "done"
                item["ended_at"] = round(now, 3)
                changed = True
        elif kind == "approval.request":
            self._add("message", "Asked for your approval", now)
            changed = True
        elif isinstance(kind, str) and kind.startswith("run."):
            end = kind[4:]
            for item in self.state["items"]:
                if item["status"] == "running":
                    item["status"] = _status_for(end)
                    if "tool" in item:
                        item["ended_at"] = round(now, 3)
                    changed = True
            changed = changed or end in {"completed", "failed", "cancelled", "stopped", "interrupted"}
        self.dirty = self.dirty or changed
        return changed


# -- the public shape ----------------------------------------------------------------------------

def _counts(edit: list[int] | None) -> str | None:
    if not edit or (not edit[0] and not edit[1]):
        return None
    return f"+{edit[0]} {MINUS}{edit[1]}"


def hero(state: dict[str, Any]) -> dict[str, Any] | None:
    trials = state.get("trials")
    if isinstance(trials, dict) and trials.get("total"):
        out = {"type": "trials", "total": trials["total"]}
        for key in ("passed", "failed"):
            if isinstance(trials.get(key), int):
                out[key] = trials[key]
        return out
    edits = {k: v for k, v in (state.get("edits") or {}).items() if v and (v[0] or v[1])}
    if edits:
        out = {"type": "diff", "files": len(edits), "added": sum(v[0] for v in edits.values()),
               "removed": sum(v[1] for v in edits.values())}
        if state.get("diff_preview"):
            out["preview"] = state["diff_preview"]
        return out
    sources = state.get("sources") or []
    edited = any(t.get("edited") for t in state.get("touched") or [])
    if len(sources) >= 2 and not edited:
        return {"type": "sources", "items": [{"title": s["label"], "site": s["site"]} for s in sources[:MAX_SOURCES]]}
    if len(state.get("items") or []) >= STEPS_AFTER:
        return {"type": "steps"}
    return None


_DETAIL_KEYS = ("args", "result")


def public(state: dict[str, Any] | None, *, detail: bool = False) -> dict[str, Any]:
    """The contract fields for one task's work object; empty ones are left out.

    ``detail`` (``GET /voice/work/{run_id}``): every kept item (at most ``MAX_ACTIVITY``) with its
    ``args`` and ``result`` previews. Otherwise (the event stream, the task feed, ``/latest``): the
    newest ``MAX_STREAM_ACTIVITY`` without them, so the stream stays small. Both carry
    ``activity_total``, how many items the task has kept."""
    if not isinstance(state, dict) or state.get("v") != 1:
        return {}
    out: dict[str, Any] = {}
    kept = (state.get("items") or [])[-MAX_ACTIVITY:]
    shown = kept if detail else kept[-MAX_STREAM_ACTIVITY:]
    items = [{k: v for k, v in item.items() if not k.startswith("_") and (detail or k not in _DETAIL_KEYS)}
             for item in shown]
    if items:
        out["activity"] = items
        out["activity_total"] = len(kept)
    if state.get("findings"):
        out["findings"] = list(state["findings"][-MAX_FINDINGS:])
    edits = state.get("edits") or {}
    touched = []
    for t in (state.get("touched") or [])[-MAX_TOUCHED:]:
        entry = {"kind": t["kind"], "label": t["label"]}
        detail = _counts(edits.get(t["label"])) if t["kind"] == "file" else None
        if detail:
            entry["detail"] = detail
        elif t.get("edited"):
            entry["detail"] = "edited"
        if t.get("url"):
            entry["url"] = t["url"]
        touched.append(entry)
    if touched:
        out["touched"] = touched
    shown = hero(state)
    if shown:
        out["hero"] = shown
    return out


# -- per-task trackers, throttled writes ---------------------------------------------------------

_LOCK = threading.Lock()
MAX_TRACKERS = 200


def _trackers(store: Any) -> dict[str, JobActivity]:
    trackers = getattr(store, "_activity_trackers", None)
    if trackers is None:
        trackers = {}
        store._activity_trackers = trackers
    return trackers


def tracker(store: Any, key: str) -> JobActivity:
    """The task's tracker, restored from what was saved (a resumed call, a reloaded plugin)."""
    trackers = _trackers(store)
    found = trackers.get(key)
    if found is None:
        found = JobActivity(store.activity_state(key))
        trackers[key] = found
        while len(trackers) > MAX_TRACKERS:
            trackers.pop(next(iter(trackers)))
    return found


def observe(store: Any, key: str, event: dict[str, Any], on_flush: Any = None, now: float | None = None) -> bool:
    """Feed one run event to the task's activity. Saved at most once per FLUSH_EVERY_S (a terminal
    event saves at once); when held back, a timer saves it and calls ``on_flush`` (to publish).
    True when it was saved now."""
    terminal = str(event.get("event") or "").startswith("run.") if isinstance(event, dict) else False
    with _LOCK:
        job = tracker(store, key)
        if not job.observe(event, now):
            return False
        return _flush_locked(store, key, job, force=terminal, on_flush=on_flush, now=now)


def _flush_locked(store: Any, key: str, job: JobActivity, *, force: bool, on_flush: Any, now: float | None) -> bool:
    if not job.dirty:
        return False
    clock = time.monotonic() if now is None else now
    wait = FLUSH_EVERY_S - (clock - job.flushed_at)
    if force or wait <= 0:
        store.set_activity(key, job.state)
        job.dirty, job.flushed_at = False, clock
        if job.timer is not None:
            job.timer.cancel()
            job.timer = None
        return True
    if job.timer is None:
        def later() -> None:
            with _LOCK:
                job.timer = None
                saved = _flush_locked(store, key, job, force=True, on_flush=None, now=None)
            if saved and on_flush is not None:
                try:
                    on_flush()
                except Exception:
                    pass
        job.timer = threading.Timer(wait, later)
        job.timer.daemon = True
        job.timer.start()
    return False
