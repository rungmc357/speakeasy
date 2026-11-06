"""The names a user's own Hermes context says they use: their machines, models and projects.

Speech-to-text turns a name it doesn't know into common words ("Mac Pro" -> "Man Pros",
"Qwen" -> "Quinn"), and a task then goes looking for something that doesn't exist. The voice is
told these names (prompt.builder.vocabulary_block), the task's brief lists them, and a spoken
correction is spelled with them (corrections.snap). Everything comes from the user's own files,
read-only: Hermes' config.yaml (provider and model names, server host names), the voice brief and
Hermes memory (machine names written as "on <name>", "<name> -> <name>", "<name>="). Nothing here is ever written anywhere but the voice's instructions and the task brief.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

MAX_TERMS = 40
CACHE_S = 300.0

# Computer names a speech model mishears all the time; always worth knowing.
DEVICES = ("Mac Pro", "Mac mini", "MacBook Pro", "MacBook Air", "iMac", "iPhone", "iPad")
# Model and product names: listed only when the user's own context mentions them.
PRODUCTS = ("Qwen", "DeepSeek", "Gemma", "Llama", "Mistral", "MiniMax", "Kimi", "GLM", "Claude", "Codex",
            "ChatGPT", "Grok", "Gemini", "Hermes", "Speakeasy", "Tailscale", "Cloudflare", "Ollama", "MLX",
            "Venice", "OpenRouter", "Home Assistant", "Discord", "Telegram", "Linear", "TestFlight")
_NAME = r"[a-z][a-z0-9-]{2,15}"
_MACHINE_PATTERNS = (
    re.compile(rf"(?<![\w./-])({_NAME})\s*(?:\u2192|->|=)"),
    re.compile(rf"(?:\u2192|->)\s*({_NAME})(?![\w.-])"),
    re.compile(rf"\b(?:on|ssh|to|from)\s+({_NAME})\b(?=\s*(?:[(),.;:]|\u2192|->|$))", re.M),
)
_COMMON = {
    "the", "and", "all", "any", "one", "two", "use", "uses", "used", "run", "runs", "set", "get", "new", "old", "off",
    "top", "via", "for", "not", "yes", "now", "day", "time", "main", "chain", "fallback", "local", "remote", "home",
    "work", "done", "true", "false", "none", "null", "auto", "default", "model", "models", "server", "lane", "lanes",
    "live", "file", "files", "page", "pages", "wiki", "docs", "memory", "task", "tasks", "it", "this", "that",
    "demand", "hold", "facts", "notes", "page", "only", "here", "there", "back", "next", "last", "first", "voice", "text", "chat", "call", "calls", "data", "user", "users", "agent", "agents",
}
_lock = threading.Lock()
_cache: dict[str, tuple[float, list[str]]] = {}


def _read(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _host_label(url: Any) -> str | None:
    m = re.match(r"^\w+://([^/:?#]+)", str(url or ""))
    if not m:
        return None
    host = m.group(1).lower()
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    label = host.split(".")[0]
    if label in {"localhost", "api", "www", "app", "openrouter", "gateway"} or not re.fullmatch(_NAME, label):
        return None
    return label


def _config_terms(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(names, hosts) from config.yaml: custom provider names before any "(…)", provider server hosts."""
    names: list[str] = []
    hosts: list[str] = []
    for cp in config.get("custom_providers") or []:
        if not isinstance(cp, dict):
            continue
        name = re.split(r"[(\[]", str(cp.get("name") or ""))[0].strip(" -")
        if name and len(name.split()) <= 3:
            names.append(name)
        if (h := _host_label(cp.get("base_url"))):
            hosts.append(h)
    for prov in (config.get("providers") or {}).values():
        if isinstance(prov, dict) and (h := _host_label(prov.get("base_url"))):
            hosts.append(h)
    return names, hosts


def _machine_names(text: str) -> list[str]:
    """Lower-case names written the way people write machine names ("aux lanes -> zeus", "on apollo",
    "zeus=Qwen"), kept when they are written that way at least twice and aren't ordinary words."""
    counts: dict[str, int] = {}
    for pattern in _MACHINE_PATTERNS:
        for m in pattern.finditer(text):
            word = m.group(1)
            if word.lower() != word or word in _COMMON or word.endswith(("ing", "ed", "ly")):
                continue
            counts[word] = counts.get(word, 0) + 1
    return [w for w, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= 2]


def gather(hermes_home: Path | str, extra_text: str = "", config: dict[str, Any] | None = None) -> list[str]:
    """The user's names, most specific first, deduplicated, at most MAX_TERMS."""
    home = Path(hermes_home)
    if config is None:
        try:
            import yaml  # type: ignore
            loaded = yaml.safe_load(_read(home / "config.yaml")) or {}
            config = loaded if isinstance(loaded, dict) else {}
        except Exception:
            config = {}
    context = "\n".join([extra_text, _read(home / "speakeasy" / "voice-brief.md"), _read(home / "memories" / "MEMORY.md"),
                         _read(home / "memories" / "USER.md")])
    cfg_names, cfg_hosts = _config_terms(config)
    lowered = (context + "\n" + repr(config)[:200_000]).lower()
    terms: list[str] = []
    for term in [*cfg_names, *cfg_hosts, *_machine_names(context), *DEVICES,
                 *(p for p in PRODUCTS if p.lower() in lowered)]:
        if term and all(t.lower() != term.lower() for t in terms):
            terms.append(term)
    return terms[:MAX_TERMS]


def terms(hermes_home: Path | str, extra_text: str = "") -> list[str]:
    """``gather``, cached for a few minutes per Hermes home (calls and follow-ups ask often)."""
    key = f"{hermes_home}|{len(extra_text)}"
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_S:
            return list(hit[1])
    found: list[str]
    try:
        found = gather(hermes_home, extra_text)
    except Exception:  # a hint list never breaks a call
        logger.debug("speakeasy: vocabulary unavailable", exc_info=True)
        found = [*DEVICES]
    with _lock:
        _cache[key] = (now, found)
    return list(found)


def listed(names: Iterable[str]) -> str:
    return ", ".join(names)
