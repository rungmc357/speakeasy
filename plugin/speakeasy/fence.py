"""Same-project fence: two voice tasks must not edit one repo or project at the same time.

Deliberately conservative. A false fence (holding back work that would never have collided) is worse
than a miss, so a task is only held when all of these hold:

- the new request asks for a change (fix, edit, refactor, bump, commit, ...) and is not a question,
  lookup or status ask;
- it names its project explicitly: a path on this machine inside a git checkout, or "the <name>
  repo/project/codebase";
- a running task is editing that same project: its own request names it and asks for a change, or it
  has written files there (``write_file``/``patch`` tool calls, or a terminal step that ``cd``s into it).

Projects are compared as keys: ``path:<checkout root>`` and ``name:<checkout folder or spoken name>``.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Iterable

_EDIT = re.compile(
    r"(?i)\b(?:fix|fixes|fixing|change|edit|modify|refactor|rewrite|implement|add|remove|delete|rename|update|"
    r"upgrade|bump|commit|merge|rebase|patch|migrate|port|wire\s+up|clean\s+up|restructure|ship|release|"
    r"write|create|build\s+(?:out|in)|replace|move|revert|land|redesign|reorganize|format)\b")
# Questions, lookups and status asks never fence, whatever they name.
_READ_ONLY = re.compile(
    r"(?i)^\W*(?:(?:hey|ok|okay|so|and|um+|uh+)\W+)*(?:what|what's|whats|how|how's|why|when|where|who|which|"
    r"is|are|was|were|does|do|did|can\s+you\s+(?:tell|show|explain|check|look|find|see)|could\s+you\s+(?:tell|show|explain|check|look)|"
    r"tell\s+me|show\s+me|explain|summari[sz]e|review|read|look\s+(?:at|into|up)|check|find|search|list|status|describe)\b")
_NAMED = (
    re.compile(r"(?i)\b(?:the\s+|my\s+|our\s+)?([a-z0-9][\w.-]{1,40})\s+(?:repo|repository|codebase|project|app|plugin)\b"),
    re.compile(r"(?i)\b(?:repo|repository|codebase|project)\s+(?:called\s+|named\s+)?([a-z0-9][\w.-]{1,40})\b"),
)
_NOT_NAMES = {
    "the", "this", "that", "my", "our", "your", "a", "an", "same", "new", "other", "whole", "side", "main", "old",
    "first", "second", "next", "last", "big", "small", "open", "source", "personal", "work", "web", "mobile", "ios",
    "mac", "it", "its", "one", "any", "some", "which", "what", "every", "each", "test", "demo", "sample", "pet",
    "voice", "hermes", "called", "named", "of", "in", "on", "for", "to", "into", "from", "and", "or", "with",
}
_PATH = re.compile(r"(?<![\w/])((?:~|/(?:Users|home|opt|srv|var|tmp|private|Volumes|workspace|code|src))/[\w.@+-][\w./@+-]*)")
_CD = re.compile(r"(?:^|[;&|]\s*|\s)(?:cd|pushd)\s+([~/][^\s;&|]+)|git\s+-C\s+([~/][^\s;&|]+)")
EDIT_TOOLS = {"write_file", "patch", "edit_file", "str_replace_editor", "apply_patch"}
MAX_DEPTH = 8


# Proposals, plans and research change nothing yet ("propose a fix", "plan the refactor", "research
# how to migrate"): they never fence either, even though they name an edit.
_NOT_YET = re.compile(
    r"(?i)^\W*(?:(?:hey|ok|okay|so|and|please|um+|uh+)\W+)*(?:(?:can|could|would)\s+you\s+)?(?:propose|suggest|"
    r"recommend|research|investigate|plan|outline|brainstorm|compare|draft\s+a\s+plan|think\s+(?:about|through)|"
    r"come\s+up\s+with|give\s+me\s+(?:a\s+)?(?:plan|proposal|options|ideas))\b")


def is_edit_request(text: str) -> bool:
    said = " ".join((text or "").split())
    if not said or _READ_ONLY.search(said) or _NOT_YET.search(said) or said.rstrip().endswith("?"):
        return False
    return bool(_EDIT.search(said))


def checkout_root(path: str) -> Path | None:
    """The git checkout a path is in, or None (a path outside any checkout, or not on this machine)."""
    try:
        here = Path(os.path.expanduser(path.rstrip(".,;:)'\""))).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if not here.is_absolute() or here == Path(here.anchor):
        return None
    for candidate in [here, *here.parents][:MAX_DEPTH]:
        if candidate == Path.home() or candidate == Path(candidate.anchor):
            return None
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError:
            return None
    return None


def _root_keys(root: Path) -> set[str]:
    return {f"path:{root}", f"name:{root.name.lower()}"}


def keys_in_text(text: str) -> set[str]:
    """Project keys a request names explicitly: checkout paths, and "the <name> repo"."""
    keys: set[str] = set()
    for match in _PATH.finditer(text or ""):
        root = checkout_root(match.group(1))
        if root is not None:
            keys |= _root_keys(root)
    for pattern in _NAMED:
        for match in pattern.finditer(text or ""):
            name = match.group(1).lower().strip(".-")
            if name and name not in _NOT_NAMES and not name.isdigit():
                keys.add(f"name:{name}")
    return keys


def keys_from_tool(tool: str | None, preview: str | None) -> tuple[set[str], bool]:
    """(project keys, whether it edits) from one ``tool.started`` event: a file write or patch names
    the file (an edit there); a terminal step that changes into a checkout names that checkout."""
    tool, preview = str(tool or ""), str(preview or "")
    if tool in EDIT_TOOLS and preview:
        root = checkout_root(preview.split()[0])
        return (_root_keys(root), True) if root else (set(), False)
    if tool == "terminal" and preview:
        keys: set[str] = set()
        for match in _CD.finditer(preview):
            root = checkout_root(match.group(1) or match.group(2))
            if root is not None:
                keys |= _root_keys(root)
        return keys, False
    return set(), False


def overlap(new: Iterable[str], running: Iterable[str]) -> str | None:
    """The shared project, as words to say ("the speakeasy repo"), or None. A path match wins."""
    shared = set(new) & set(running)
    if not shared:
        return None
    path = next((k for k in sorted(shared) if k.startswith("path:")), None)
    if path:
        return Path(path[5:]).name
    return sorted(shared)[0][5:]
