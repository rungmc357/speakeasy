"""Speakeasy's agent slot: which agent does the work, what it can do, and how it is named.

The agent in the slot is any ``backends.TaskBackend`` (Hermes' ``HermesAPI`` today). Its
``Capabilities`` decide which of Speakeasy's features exist on this server:

- the UX flags in ``GET /voice/status`` are the server's own flags with every feature the agent
  lacks switched off (``capabilities``), so the apps hide it the way they already hide what an
  older server lacks;
- the optional features (past conversations, helpers, questions) come from the agent through
  ``session_catalog`` / ``subagent_source``; an agent without one gets a stand-in that offers
  nothing instead of reading Hermes' files;
- ``agent_info`` tells the apps which agent they are talking to (old apps ignore it);
- ``named`` puts the agent's own name into generic wording that used to say "Hermes".

With Hermes in the slot every flag, feature and sentence is exactly what it was before.
"""
from __future__ import annotations

import re
from typing import Any

DEFAULT_NAME = "Hermes"

# Optional agent feature -> the UX flags in GET /voice/status that depend on it.
FEATURE_FLAGS: dict[str, tuple[str, ...]] = {
    "history": ("history",),
    "subagents": ("job_subagents",),
    "questions": ("questions", "question_actions"),  # question_actions: decision-card dismiss/undo (SPK-68)
}
FLAGS_OF_FEATURES = frozenset(flag for flags in FEATURE_FLAGS.values() for flag in flags)


def agent_caps(agent: Any) -> Any:
    """The agent's Capabilities, or None when it can't say (a stand-in in a test)."""
    fn = getattr(agent, "capabilities", None)
    if not callable(fn):
        return None
    try:
        return fn()
    except Exception:
        return None


def display_name(agent: Any) -> str:
    caps = agent_caps(agent)
    name = getattr(caps, "display_name", "") if caps is not None else ""
    return name if isinstance(name, str) and name.strip() else DEFAULT_NAME


def has(agent: Any, feature: str) -> bool:
    """Whether the agent offers an optional feature. An agent that can't describe itself is
    treated as Hermes was before the slot existed: everything on."""
    caps = agent_caps(agent)
    return True if caps is None else bool(getattr(caps, feature, False))


def capabilities(static: dict[str, Any], agent: Any) -> dict[str, Any]:
    """The server's UX flags with every feature the agent lacks switched off."""
    out = dict(static)
    for feature, flags in FEATURE_FLAGS.items():
        on = has(agent, feature)
        for flag in flags:
            if flag in out:
                out[flag] = bool(out[flag]) and on
    return out


def agent_info(agent: Any, host: Any = None) -> dict[str, Any]:
    """``agent`` in GET /voice/status and the pair response: which agent this server talks to."""
    caps = agent_caps(agent)
    kind = getattr(caps, "kind", "") if caps is not None else ""
    version = getattr(caps, "version", "") if caps is not None else ""
    return {"kind": kind or "hermes", "display_name": display_name(agent),
            "version": version if isinstance(version, str) else "",
            "host": getattr(host, "kind", "") or "hermes"}


# -- optional features -------------------------------------------------------------------------

class NoSessions:
    """Stand-in SessionCatalog for an agent without past conversations: nothing to list or read."""

    def __init__(self, name: str = DEFAULT_NAME):
        self.name = name

    def list_sessions(self, **_kwargs: Any) -> dict[str, Any]:
        from .calls import ServiceError
        raise ServiceError(404, f"{self.name} doesn't keep past conversations Speakeasy can read.")

    def session_meta(self, _session_id: str) -> dict[str, Any] | None:
        return None

    def session_messages(self, _session_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"entries": [], "has_more": False, "before": None}

    def fingerprint(self, _session_ids: list[str] | None = None) -> Any:
        return None

    def message_time(self, _row_id: int | None) -> float | None:
        return None

    def linked_context(self, _session_id: str, _title: str, _label: str, _text: str) -> str:
        return ""


def session_catalog(agent: Any, state_db: Any = None) -> Any:
    """The agent's past conversations (``history``), else a stand-in with none. ``state_db``: the
    Hermes profile's database, used only for a stand-in agent that can't describe itself."""
    if has(agent, "history"):
        own = getattr(agent, "session_catalog", None)
        found = own() if callable(own) else None
        if found is None and agent_caps(agent) is None and state_db is not None:
            from .backends.hermes import HermesSessionCatalog
            found = HermesSessionCatalog(state_db)
        if found is not None:
            return found
    return NoSessions(display_name(agent))


def subagent_source(agent: Any, state_db: Any = None) -> Any:
    """Where the agent's helpers can be followed (``subagents``), else None. ``state_db`` as above."""
    if not has(agent, "subagents"):
        return None
    own = getattr(agent, "subagent_source", None)
    found = own() if callable(own) else None
    if found is None and agent_caps(agent) is None and state_db is not None:
        from .backends.hermes import HermesSubagentSource
        found = HermesSubagentSource(state_db)
    return found


# -- wording -------------------------------------------------------------------------------------

_NAME_RE = re.compile(r"\bHermes\b")


def named(text: str, name: str) -> str:
    """Generic wording with the agent's name in place of "Hermes" (unchanged for Hermes).
    Only for sentences true of any agent; Hermes-only features keep their own wording."""
    if not name or name == DEFAULT_NAME:
        return text
    return _NAME_RE.sub(name, text)
