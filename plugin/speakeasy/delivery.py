"""Delivery of finished task results and heads-ups to one Hermes chat target (`hermes send`),
plus the destination list the app offers as a picker.

`delivery.target` is ``none`` (default) or a Hermes send target: ``telegram``, ``discord``,
``discord:<chat_id>``, ``telegram:<chat_id>:<thread_id>``, ``slack:#name`` ...
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .settings import valid_delivery_target

logger = logging.getLogger(__name__)
SEND_TIMEOUT_S = 60
# Platforms that are not chats a person reads (or are Speakeasy itself).
INTERNAL_PLATFORMS = {"api_server", "webhook", "msgraph_webhook", "voice", "cli", "cron", "acp", "relay",
                      "homeassistant"}
MAX_CHATS_PER_PLATFORM = 25
CHAT_TYPE_ORDER = {"dm": 0, "channel": 1, "group": 2, "": 3, "thread": 4}


def hermes_command() -> list[str]:
    """How to invoke the Hermes CLI from inside the gateway (its own interpreter) or a shell."""
    try:
        import hermes_cli  # type: ignore  # noqa: F401
        return [sys.executable, "-m", "hermes_cli.main"]
    except Exception:
        found = shutil.which("hermes")
        return [found] if found else ["hermes"]


class HermesSendNotifier:
    """Posts text through `hermes send`. The body goes on stdin, never on the command line."""

    def __init__(self, hermes_home: Path, profile: str = "",
                 runner: Callable[..., Any] = subprocess.run):
        self.hermes_home, self.profile, self.runner = Path(hermes_home), profile, runner

    def send(self, target: str, text: str) -> bool:
        if not valid_delivery_target(target) or target == "none" or not text.strip():
            return False
        cmd = [*hermes_command()]
        if self.profile:
            cmd += ["--profile", self.profile]
        cmd += ["send", "--to", target, "--file", "-", "--quiet"]
        env = {**os.environ, "HERMES_HOME": str(self.hermes_home)}
        try:
            done = self.runner(cmd, input=text, text=True, capture_output=True, timeout=SEND_TIMEOUT_S, env=env)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("speakeasy: hermes send failed: %s", type(exc).__name__)
            return False
        if getattr(done, "returncode", 1) != 0:
            logger.warning("speakeasy: hermes send exited %s", getattr(done, "returncode", "?"))
            return False
        return True


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _hermes_config(hermes_home: Path) -> dict[str, Any]:
    from .settings import hermes_config
    return hermes_config(Path(hermes_home))


def _config_platform_blocks(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Platform blocks from both places Hermes reads them: `platforms.*` and `gateway.platforms.*`."""
    out: dict[str, dict[str, Any]] = {}
    gateway = config.get("gateway") if isinstance(config.get("gateway"), dict) else {}
    for source in (gateway.get("platforms"), config.get("platforms")):
        if isinstance(source, dict):
            for name, block in source.items():
                if isinstance(name, str) and isinstance(block, dict):
                    out.setdefault(name, {}).update(block)
    return out


def _own_profile(hermes_home: Path) -> str:
    """This Hermes home's profile name: `default` for the root home, else the profiles/<name> dir."""
    home = Path(hermes_home)
    return home.name if home.parent.name == "profiles" else "default"


def other_profile_chats(hermes_home: Path) -> set[str]:
    """`platform:chat_id` for chats the gateway hands to a *different* Hermes profile
    (`gateway.profile_routes`). Answers there land in that profile's history, which this one can't
    (and shouldn't) read, so they must never be voice destinations."""
    config = _hermes_config(hermes_home)
    gateway = config.get("gateway") if isinstance(config.get("gateway"), dict) else {}
    own = _own_profile(hermes_home)
    out: set[str] = set()
    for route in gateway.get("profile_routes") or []:
        if not isinstance(route, dict) or not route.get("platform") or not route.get("chat_id"):
            continue
        if str(route.get("profile") or "default") != own:
            out.add(f"{route['platform']}:{route['chat_id']}")
    return out


def in_other_profile(target: str, foreign: set[str]) -> bool:
    parts = str(target).split(":")
    return len(parts) >= 2 and f"{parts[0]}:{parts[1]}" in foreign


def _gateway_home_channels(hermes_home: Path) -> dict[str, dict[str, str]]:
    """Home channels as the running gateway resolved them (config.yaml *and* env such as
    DISCORD_HOME_CHANNEL). Only used when this process belongs to the same Hermes home."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        if Path(get_hermes_home()).resolve() != Path(hermes_home).resolve():
            return {}
        from gateway.config import load_gateway_config  # type: ignore
        cfg = load_gateway_config()
        out: dict[str, dict[str, str]] = {}
        for platform, pconf in cfg.platforms.items():
            home = getattr(pconf, "home_channel", None)
            if home is not None and getattr(home, "chat_id", None):
                out[platform.value] = {"chat_id": str(home.chat_id), "name": str(getattr(home, "name", "") or "Home"),
                                       "thread_id": str(getattr(home, "thread_id", "") or "")}
        return out
    except Exception:
        return {}


def home_label(home: dict[str, str], chats: list[dict[str, Any]]) -> str:
    """'Home · The Wetlands / general': the home channel is left out of the chat list, so its entry must
    say which chat it actually is, or a user can't tell whether their #general is offered at all."""
    label = str(home.get("name") or "Home").strip() or "Home"
    for chat in chats:
        if str(chat.get("id")) == str(home.get("chat_id")) and chat.get("type") != "group" and chat.get("name"):
            real = str(chat["name"])
            real = f"{chat['guild']} / {real}" if chat.get("guild") else real
            return label if real.lower() == label.lower() else f"{label} · {real}"
    return label


def _chat_target(platform: str, chat: dict[str, Any]) -> str:
    target = f"{platform}:{chat['id']}"
    if chat.get("thread_id"):
        target += f":{chat['thread_id']}"
    return target


def destinations(hermes_home: Path) -> dict[str, Any]:
    """Connected Hermes chat platforms, their home channel and known chats (no secrets).

    Sources: gateway_state.json (which platforms are up), the gateway's resolved home channels
    (falling back to config.yaml), and channel_directory.json (chats the gateway has seen). Every
    entry carries a ready-to-use `target` for `delivery.target`; `suggested` is the best default.
    """
    hermes_home = Path(hermes_home)
    state = _read_json(hermes_home / "gateway_state.json") or {}
    platform_states = state.get("platforms") if isinstance(state, dict) else None
    platform_states = platform_states if isinstance(platform_states, dict) else {}
    configured = _config_platform_blocks(_hermes_config(hermes_home))
    homes = _gateway_home_channels(hermes_home)
    directory = _read_json(hermes_home / "channel_directory.json") or {}
    chats_by_platform = directory.get("platforms") if isinstance(directory, dict) else None
    chats_by_platform = chats_by_platform if isinstance(chats_by_platform, dict) else {}

    foreign = other_profile_chats(hermes_home)
    names = {n for n in (set(platform_states) | set(configured) | set(chats_by_platform))
             if isinstance(n, str) and ":" not in n} - INTERNAL_PLATFORMS
    out = []
    for name in sorted(names):
        pstate = platform_states.get(name) if isinstance(platform_states.get(name), dict) else {}
        pconf = configured.get(name, {})
        chats = [c for c in (chats_by_platform.get(name) or []) if isinstance(c, dict) and c.get("id")]
        # A platform the gateway knows nothing about beyond an empty block isn't a destination.
        if not pstate and not chats and not pconf.get("enabled"):
            continue
        entry: dict[str, Any] = {
            "platform": name,
            "connected": pstate.get("state") == "connected",
            "state": pstate.get("state") or ("configured" if pconf else "unknown"),
            "target": name,
            "home_channel": None,
            "chats": [],
        }
        home = homes.get(name)
        if home is None:
            raw = pconf.get("home_channel") if isinstance(pconf.get("home_channel"), dict) else None
            if raw and raw.get("chat_id"):
                home = {"chat_id": str(raw["chat_id"]), "name": str(raw.get("name") or "Home"),
                        "thread_id": str(raw.get("thread_id") or "")}
        if home:
            target = f"{name}:{home['chat_id']}" + (f":{home['thread_id']}" if home.get("thread_id") else "")
            if valid_delivery_target(target):
                entry["home_channel"] = {"name": home_label(home, chats)[:120], "target": target}
        chats.sort(key=lambda c: CHAT_TYPE_ORDER.get(str(c.get("type") or ""), 3))
        seen = {entry["home_channel"]["target"]} if entry["home_channel"] else set()
        for chat in chats:
            if len(entry["chats"]) >= MAX_CHATS_PER_PLATFORM:
                break
            target = _chat_target(name, chat)
            if target in seen or not valid_delivery_target(target) or in_other_profile(target, foreign):
                continue
            seen.add(target)
            label = str(chat.get("name") or chat["id"])
            if chat.get("guild"):
                label = f"{chat['guild']} / {label}"
            entry["chats"].append({"name": label[:120], "type": str(chat.get("type") or ""), "target": target})
        if entry["home_channel"] or entry["chats"]:
            out.append(entry)
    return {"destinations": out, "none": {"target": "none", "name": "Don't post results anywhere"},
            "suggested": suggested_target(out)}


def suggested_target(entries: list[dict[str, Any]]) -> str:
    """The home channel of the first connected platform that has one (Telegram/Discord first),
    else ``none``. Used as the preselected choice in onboarding."""
    preferred = {"telegram": 0, "discord": 1, "slack": 2, "signal": 3, "whatsapp": 4}
    for entry in sorted(entries, key=lambda e: preferred.get(e["platform"], 9)):
        if entry.get("connected") and entry.get("home_channel"):
            return entry["home_channel"]["target"]
    return "none"


def threads_supported() -> bool:
    """True when the installed Hermes webhook platform can open a new thread per delivery
    (`source_new_thread`). Detected from the installed source, never assumed."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("gateway.platforms.webhook")
        if spec is None or not spec.origin:
            return False
        return "source_new_thread" in Path(spec.origin).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False


def target_label(target: str, hermes_home: Path | None = None) -> str:
    """How the voice names where results go: the chat's own name when Hermes knows it
    ("#voice on Discord", "your Home chat on Telegram"), else the platform ("your Telegram")."""
    if not target or target == "none":
        return ""
    platform, _, rest = target.partition(":")
    chat_id = rest.split(":", 1)[0]
    name = _chat_name(Path(hermes_home), platform, chat_id) if hermes_home and chat_id else ""
    if name:
        return f"{name} on {platform.title()}"
    return "your " + platform.title()


def _chat_name(hermes_home: Path, platform: str, chat_id: str) -> str:
    directory = _read_json(hermes_home / "channel_directory.json") or {}
    chats = (directory.get("platforms") or {}).get(platform) if isinstance(directory, dict) else None
    for chat in chats or []:
        if isinstance(chat, dict) and str(chat.get("id")) == chat_id and chat.get("type") != "group":
            name = " ".join(str(chat.get("name") or "").split())
            if name:
                return name if name.startswith("#") or platform != "discord" else "#" + name
    for chat in chats or []:
        if isinstance(chat, dict) and str(chat.get("id")) == chat_id:
            name = " ".join(str(chat.get("name") or "").split())
            if name:
                return name.rsplit(" / ", 1)[-1]
    return ""


def flat_chats(dest: dict[str, Any]) -> list[dict[str, str]]:
    """Every concrete chat in a destinations() result as {target, label, platform}, home first."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in dest.get("destinations") or []:
        platform = str(entry.get("platform") or "")
        chats = ([dict(entry["home_channel"], name=f"{entry['home_channel']['name']} (home)")]
                 if entry.get("home_channel") else []) + list(entry.get("chats") or [])
        for chat in chats:
            target = chat.get("target")
            if not target or target in seen:
                continue
            seen.add(target)
            out.append({"target": target, "label": f"{platform.title()}: {chat.get('name') or target}"[:140],
                        "platform": platform})
    return out
