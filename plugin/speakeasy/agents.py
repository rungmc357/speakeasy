"""Agents: which Hermes profile a Speakeasy chat talks to (docs/API.md "Agents").

A Hermes install can hold several profiles (``~/.hermes`` plus ``profiles/<name>``), each its own
agent with its own memory, skills, model and history. A gateway that multiplexes profiles serves
every one of them on the same API server at ``/p/<profile>/v1/...``; Speakeasy picks one per chat
by sending that chat's runs there.

- **Allowlist.** Only profiles the user allowed on the Mac (settings ``agents.allowed``) are
  offered. Nothing is allowed by default: the picker stays hidden and every chat runs on the
  profile Speakeasy lives in (its own agent), exactly as before.
- **Per chat.** An agent is chosen when a chat starts (New chat); the chat lives in that profile,
  so its later messages, follow-ups and linked tasks keep going there. Calls and chats with no
  choice use Speakeasy's own profile.
- **Its own model.** A chat on another agent runs on that agent's configuration: Speakeasy's model
  picks come from its own profile's catalog and are not sent there.

Other profiles' files are only read (their name, description and state.db), never written.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .profiles import hermes_root, profile_name

logger = logging.getLogger(__name__)

AGENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_ALLOWED = 24
DEFAULT = "default"


def own_agent(home: Path, configured: str = "") -> str:
    """The profile Speakeasy's runs go to when nothing else is chosen: settings ``hermes_profile``
    (a prefix on the API server) or the profile this plugin was loaded in."""
    return (configured or "").strip().lower() or profile_name(home)


def agent_home(home: Path, agent: str) -> Path:
    """The Hermes home of *agent* on this install (the root home for ``default``)."""
    root = hermes_root(home)
    return root if agent == DEFAULT else root / "profiles" / agent


def _multiplexed(home: Path) -> bool:
    """Whether the gateway serves every profile on one API server (``gateway.multiplex_profiles``)."""
    from .settings import hermes_config
    for cfg in (hermes_config(hermes_root(home)), hermes_config(home)):
        gateway = cfg.get("gateway") if isinstance(cfg, dict) else None
        if isinstance(gateway, dict) and gateway.get("multiplex_profiles") is True:
            return True
    return False


def _meta(home: Path) -> dict[str, str]:
    """``display_name`` / ``description`` from a profile's ``profile.yaml`` (Hermes' own metadata)."""
    try:
        from hermes_cli.profiles import read_profile_meta  # type: ignore
        meta = read_profile_meta(home)
        return {"display_name": str(meta.get("display_name") or ""), "description": str(meta.get("description") or "")}
    except Exception:
        pass
    try:
        import yaml  # type: ignore
        data = yaml.safe_load((home / "profile.yaml").read_text()) or {}
        if isinstance(data, dict):
            return {"display_name": str(data.get("display_name") or "").strip(),
                    "description": str(data.get("description") or "").strip()}
    except Exception:
        pass
    return {"display_name": "", "description": ""}


def label(agent: str, display_name: str = "") -> str:
    """What a person sees: the profile's display name, else its id in words ("venice-research" ->
    "Venice research"); the default profile is "Hermes"."""
    if display_name.strip():
        return display_name.strip()[:40]
    if agent == DEFAULT:
        return "Hermes"
    words = agent.replace("_", " ").replace("-", " ").strip()
    return (words[:1].upper() + words[1:])[:40] or agent


def _served(profile_dir: Path) -> bool:
    """Whether the multiplexing gateway serves this named profile (Hermes' own rules where it has
    them: a live profile with identity, not parked, not standalone)."""
    if not (profile_dir / "config.yaml").exists() or (profile_dir / "gateway.parked").exists():
        return False
    try:
        from hermes_cli import profiles as HP  # type: ignore
        if hasattr(HP, "named_profile_is_deleted") and HP.named_profile_is_deleted(profile_dir):
            return False
    except Exception:
        pass
    from .settings import hermes_config
    gateway = (hermes_config(profile_dir) or {}).get("gateway")
    return not (isinstance(gateway, dict) and gateway.get("standalone") in (True, "true", "yes", "on", 1))


def installed(home: Path) -> list[str]:
    """Profiles this install's multiplexing gateway serves (default first). Reads only *home*'s own
    install (never the process's HERMES_HOME), so tests and other installs see their own."""
    root = hermes_root(home)
    names = [DEFAULT]
    try:
        names += sorted(d.name for d in (root / "profiles").iterdir()
                        if d.is_dir() and AGENT_RE.fullmatch(d.name) and d.name != DEFAULT and _served(d))
    except OSError:
        pass
    return names


def available(home: Path, own: str) -> list[str]:
    """Agents Speakeasy can reach: every served profile when the gateway multiplexes them (one API
    server answers ``/p/<profile>/``), else only its own."""
    if not _multiplexed(home):
        return [own]
    names = installed(home)
    return names if own in names else [own, *names]


def validate_allowed(raw: Any) -> list[str]:
    from .settings import SettingsError
    if not isinstance(raw, list) or len(raw) > MAX_ALLOWED or not all(
            isinstance(a, str) and AGENT_RE.fullmatch(a) for a in raw):
        raise SettingsError(f"agents.allowed must be up to {MAX_ALLOWED} Hermes profile names")
    return list(dict.fromkeys(raw))


def describe(home: Path, agent: str, own: str) -> dict[str, Any]:
    meta = _meta(agent_home(home, agent))
    entry: dict[str, Any] = {"id": agent, "name": label(agent, meta["display_name"])}
    if meta["description"]:
        entry["description"] = meta["description"][:200]
    if agent == own:
        entry["default"] = True
    return entry


def roster(home: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """``GET /voice/agents``: ``agents`` (what the picker offers: Speakeasy's own agent first, then
    the allowed ones this gateway serves), ``default``, ``allowed`` (the saved allowlist) and
    ``available`` (every agent that could be allowed, for the Mac's allowlist editor)."""
    own = own_agent(home, settings.get("hermes_profile", ""))
    reach = available(home, own)
    allowed = [a for a in (settings.get("agents") or {}).get("allowed") or [] if a != own]
    offered = [own] + [a for a in allowed if a in reach]
    return {"agents": [describe(home, a, own) for a in offered], "default": own, "allowed": allowed,
            "available": [describe(home, a, own) for a in reach],
            "multiplexed": len(reach) > 1 or _multiplexed(home)}


def offered(home: Path, settings: dict[str, Any]) -> set[str]:
    own = own_agent(home, settings.get("hermes_profile", ""))
    reach = set(available(home, own))
    return {own} | {a for a in (settings.get("agents") or {}).get("allowed") or [] if a in reach}


def state_db(home: Path, agent: str) -> Path:
    return agent_home(home, agent) / "state.db"
