"""Is a thread task's Hermes turn actually still running?

A thread task only shows life when its session writes a new message, and Hermes writes one per
finished step. One long step (a slow command, a subagent, a long think) leaves minutes without a
row, so the card used to flip to "Status unconfirmed" while the work was fine. The gateway knows
better: it holds the running agent for every live turn, and the agent reports its last activity and
the tool it is in. This asks the gateway directly, so "still working" is a fact, not a guess, and a
turn that really died still goes stale.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# A running agent whose own last activity is older than this is not vouched for: it may be wedged.
QUIET_LIMIT_S = 15 * 60
# When the turn's own words on the card are older than this, show what it is doing instead.
QUIET_SAY_S = 75

TOOL_WORDS = {
    "terminal": "running a command", "execute_code": "running code", "delegate_task": "waiting on a helper",
    "browser_exec": "working in the browser", "mcp__browser_exec": "working in the browser",
    "web_search": "searching the web", "web_extract": "reading a page", "read_file": "reading files",
    "write_file": "writing a file", "patch": "editing a file", "search_files": "searching files",
}


def _runner() -> Any:
    """The gateway runner, from the adapter module (never swapped by a live reload)."""
    try:
        from . import adapter
        runner = getattr(adapter, "GATEWAY_RUNNER", None)
        if runner is not None:
            return runner
    except Exception:  # outside the gateway (tests, CLI)
        pass
    try:
        from gateway import run_api  # type: ignore
        return getattr(run_api, "_ACTIVE_RUNNER", None)
    except Exception:
        return None


def _agents(runner: Any) -> list[Any]:
    snap = getattr(runner, "_snapshot_running_agents", None)
    if callable(snap):
        try:
            return list(snap().values())
        except Exception:
            logger.debug("speakeasy: running-agent snapshot failed", exc_info=True)
    return []


def session_activity(session_id: str | None, runner: Any = None) -> dict[str, Any] | None:
    """The live agent's activity snapshot for ``session_id``, or None when no turn is running there
    (or the gateway can't be asked). Only an agent that is in the gateway's running set counts."""
    if not session_id:
        return None
    runner = runner if runner is not None else _runner()
    if runner is None:
        return None
    for agent in _agents(runner):
        if getattr(agent, "session_id", None) != session_id:
            continue
        summary: dict[str, Any] = {}
        get = getattr(agent, "get_activity_summary", None)
        if callable(get):
            try:
                summary = dict(get() or {})
            except Exception:
                logger.debug("speakeasy: activity summary failed", exc_info=True)
        return summary
    return None


def alive(summary: dict[str, Any] | None) -> bool:
    """Running, and the agent itself moved recently enough to vouch for it."""
    if summary is None:
        return False
    quiet = summary.get("seconds_since_activity")
    return quiet is None or float(quiet) < QUIET_LIMIT_S


def doing(summary: dict[str, Any] | None) -> str:
    """A short plain line for the card: what the turn is in the middle of."""
    if not summary:
        return "Still working"
    tool = str(summary.get("current_tool") or "")
    if tool:
        word = TOOL_WORDS.get(tool) or TOOL_WORDS.get(tool.split("__")[-1])
        return f"Still working: {word}" if word else "Still working"
    return "Still working: thinking"


def watch(session_of: Callable[[], str | None], on_alive: Callable[[dict[str, Any]], None],
          stop: Any, every_s: float = 20.0, runner: Any = None,
          activity: Callable[[str | None], dict[str, Any] | None] | None = None) -> None:
    """Blocking loop (run it in a thread): every ``every_s`` until ``stop`` is set, call
    ``on_alive(summary)`` when the session's turn is verifiably running. Silent otherwise, so a dead
    turn still goes stale on its own. ``activity``: the host's check (host.Host.session_activity);
    None = Hermes' gateway, as before."""
    while not stop.wait(every_s):
        try:
            sid = session_of()
            summary = activity(sid) if activity is not None and runner is None else session_activity(sid, runner)
            if alive(summary):
                on_alive(summary or {})
        except Exception:
            logger.debug("speakeasy: liveness check failed", exc_info=True)
