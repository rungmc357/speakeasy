"""Hermes skills as slash commands in the app's message box.

``GET /voice/skills`` lists the user's installed skills (``catalog``) so typing ``/`` in a composer
offers them. A typed message that starts with ``/<skill>`` (or several stacked: ``/a /b do this``)
runs as a real skill invocation (``invocation``), built by Hermes' own ``agent.skill_commands`` the
same way the Telegram/Discord gateway expands ``/skill`` — so the task loads the skill's full
instructions, while the session page still shows only the words the user typed.

Everything here degrades quietly: an older Hermes without the module, a skill that fails to load
or a ``/word`` that isn't a skill leaves the message as plain text.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

PLATFORM = "api_server"  # the skills Hermes enables for its API server (skills.platform_disabled)
CATALOG_TTL_S = 30.0
MAX_DESCRIPTION = 160
_TOKEN = re.compile(r"/([A-Za-z0-9][A-Za-z0-9_.:-]{0,79})(?=\s|$)")

_lock = threading.Lock()
_cache: tuple[float, list[dict[str, Any]]] | None = None


def _commands() -> dict[str, dict[str, Any]]:
    """Hermes' skill command table ({"/slug": {name, description, ...}}), or {} without it."""
    try:
        from agent.skill_commands import get_skill_commands
        return dict(get_skill_commands() or {})
    except Exception as exc:  # an older Hermes, or a scan that failed
        logger.debug("speakeasy: skill commands unavailable (%s)", type(exc).__name__)
        return {}


def _disabled() -> set[str]:
    try:
        from agent.skill_utils import get_disabled_skill_names
        return set(get_disabled_skill_names(platform=PLATFORM) or ())
    except Exception:
        return set()


def _category(info: dict[str, Any]) -> str:
    """The skill's folder under the skills dir (``devops/foo`` → ``devops``), or ""."""
    for key in ("skill_identifier", "skill_dir"):
        value = str(info.get(key) or "").replace("\\", "/").rstrip("/")
        if "/skills/" in value:
            parts = value.split("/skills/", 1)[1].split("/")
            return parts[0] if len(parts) > 1 else ""
        if key == "skill_identifier" and "/" in value and not value.startswith("/"):
            return value.split("/", 1)[0]
    return ""


def _clean_description(text: Any) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= MAX_DESCRIPTION else words[:MAX_DESCRIPTION - 1].rstrip() + "…"


def catalog(*, fresh: bool = False) -> list[dict[str, Any]]:
    """Every skill the user can invoke here, sorted by command: ``[{command, name, description,
    category}]`` (``command`` is what goes after the slash). Cached briefly: a composer asks once per
    page and Hermes rescans when skills change on disk anyway."""
    global _cache
    now = time.monotonic()
    with _lock:
        if not fresh and _cache is not None and now - _cache[0] < CATALOG_TTL_S:
            return [dict(item) for item in _cache[1]]
    disabled = _disabled()
    items: list[dict[str, Any]] = []
    for key, info in _commands().items():
        command = str(key or "").lstrip("/")
        if not command or not isinstance(info, dict):
            continue
        name = str(info.get("name") or command)
        if name in disabled or command in disabled:
            continue
        items.append({"command": command, "name": name, "description": _clean_description(info.get("description")),
                      "category": _category(info)})
    items.sort(key=lambda item: item["command"].lower())
    with _lock:
        _cache = (now, items)
    return [dict(item) for item in items]


def leading_commands(text: str) -> tuple[list[str], str]:
    """The skill commands a message starts with and the words after them: ``("/a /b do it")`` →
    (["/a", "/b"], "do it"). ([], text) when it doesn't start with a known skill."""
    if not isinstance(text, str) or not text.lstrip().startswith("/"):
        return [], text
    try:
        from agent.skill_commands import resolve_skill_command_key
    except Exception:
        return [], text
    disabled = _disabled()
    table = _commands()
    keys: list[str] = []
    rest = text.lstrip()
    while len(keys) < 5:
        match = _TOKEN.match(rest)
        if not match:
            break
        try:
            key = resolve_skill_command_key(match.group(1))
        except Exception:
            key = None
        if key is None or key in keys or str((table.get(key) or {}).get("name") or "") in disabled:
            break
        keys.append(key)
        rest = rest[match.end():].lstrip()
    return (keys, rest) if keys else ([], text)


def invocation(text: str, task_id: str | None = None) -> str:
    """The skill block a task's prompt carries when *text* starts with ``/<skill>`` (Hermes'
    ``build_skill_invocation_message`` / its stacked form, with the user's words as the instruction),
    or "" when it doesn't, or the skill can't load."""
    keys, rest = leading_commands(text)
    if not keys:
        return ""
    try:
        from agent import skill_commands as sc
        if len(keys) > 1 and hasattr(sc, "build_stacked_skill_invocation_message"):
            built = sc.build_stacked_skill_invocation_message(keys, rest, task_id=task_id)
            return built[0] if built else ""
        return sc.build_skill_invocation_message(keys[0], rest, task_id=task_id) or ""
    except Exception as exc:
        logger.warning("speakeasy: a /skill message could not load its skill (%s)", type(exc).__name__)
        return ""


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None
