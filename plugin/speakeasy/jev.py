"""Optional fast routing with Jev, TypeSafe's typed decision model.

Jev answers "where should this spoken request go?" in well under a second, instead of the several
seconds a chat model takes. It is optional: Speakeasy works without it, and any failure falls back
to the normal routing model.

Providers (the user picks one in settings; the key is read through Hermes' secret readers):
  venice      https://api.venice.ai/api/v1/decisions       VENICE_API_KEY      model jev-latest
  openrouter  https://openrouter.ai/api/alpha/decisions    OPENROUTER_API_KEY  model ~typesafe/jev-latest
  typesafe    https://api.typesafe.ai/v1/systemone         TYPESAFE_API_KEY    model jev-latest
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, dict[str, str]] = {
    "venice": {"url": "https://api.venice.ai/api/v1/decisions", "key": "VENICE_API_KEY",
               "model": "jev-latest", "label": "Venice"},
    "openrouter": {"url": "https://openrouter.ai/api/alpha/decisions", "key": "OPENROUTER_API_KEY",
                   "model": "~typesafe/jev-latest", "label": "OpenRouter"},
    "typesafe": {"url": "https://api.typesafe.ai/v1/systemone", "key": "TYPESAFE_API_KEY",
                 "model": "jev-latest", "label": "TypeSafe"},
}
TIMEOUT_S = 1.5
MIN_CONFIDENCE = 0.8
HOME, QUICK, AGENT = "home", "quick", "agent"

ROUTE_QUESTION = {"route": {"type": "choice",
    "instructions": "Where should a voice assistant send this spoken request?",
    "criteria": {
        HOME: "Controls a smart-home device right now: lights, thermostat, fans, scenes, speakers.",
        QUICK: ("A general public-knowledge question answerable with one web search in one or two sentences: "
                "who owns a company, a score, a definition, store hours, a conversion, a height, a date. "
                "Nothing about the user's own data and no action."),
        AGENT: ("Anything else: needs the user's own accounts, files, calendar, email, messages, memory or "
                "projects; does something (book, buy, send, fix, build, deploy); compares many things; or "
                "needs research or several steps."),
    }}}
ABOUT_TASK_QUESTION = {"about_task": {"type": "noul",
    "instructions": "The request is about, answers, or changes one of the tasks already running."}}


def available(hermes_home: Path, provider: str) -> bool:
    cfg = PROVIDERS.get(provider)
    return bool(cfg) and bool(_key(hermes_home, cfg["key"]))


def providers_status(hermes_home: Path) -> list[dict[str, Any]]:
    """Each supported provider and whether this Hermes has its key (never the key itself)."""
    return [{"id": name, "label": cfg["label"], "key_env": cfg["key"], "has_key": bool(_key(hermes_home, cfg["key"]))}
            for name, cfg in PROVIDERS.items()]


def _key(hermes_home: Path, name: str) -> str:
    from .settings import hermes_secret
    try:
        return hermes_secret(Path(hermes_home), name) or ""
    except Exception:
        return ""


def evaluate(hermes_home: Path, provider: str, state: str, questions: dict[str, Any],
             timeout: float = TIMEOUT_S, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, Any] | None:
    """One Jev call; None on any failure (callers fall back)."""
    cfg = PROVIDERS.get(provider)
    if cfg is None:
        return None
    key = _key(hermes_home, cfg["key"])
    if not key:
        return None
    body = json.dumps({"model": cfg["model"], "state": state[:4000], "questions": questions}).encode()
    req = urllib.request.Request(cfg["url"], data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json", "User-Agent": "speakeasy"})
    try:
        with opener(req, timeout=timeout) as response:
            data = json.loads(response.read(200_000) or b"{}")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.info("speakeasy: jev unavailable on %s (%s)", provider, type(exc).__name__)
        return None
    answers = data.get("answers") if isinstance(data, dict) else None
    return answers if isinstance(answers, dict) else None


def route(hermes_home: Path, provider: str, request: str, open_tasks: list[str] | None = None,
          evaluator: Callable[..., dict[str, Any] | None] | None = None) -> tuple[str | None, float, int]:
    """(home | quick | agent | None, confidence, ms). None when Jev is off, down or unsure."""
    started = time.monotonic()
    state = f'Spoken request: "{request.strip()[:600]}"'
    if open_tasks:
        state += "\nTasks already running: " + "; ".join(t[:120] for t in open_tasks[-4:])
    questions = {**ROUTE_QUESTION, **(ABOUT_TASK_QUESTION if open_tasks else {})}
    answers = (evaluator or evaluate)(hermes_home, provider, state, questions)
    ms = int((time.monotonic() - started) * 1000)
    answer = (answers or {}).get("route")
    if not isinstance(answer, dict):
        return None, 0.0, ms
    picked = answer.get("choice")
    try:
        confidence = float(answer.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    if picked not in {HOME, QUICK, AGENT} or confidence < MIN_CONFIDENCE:
        return None, confidence, ms
    about = (answers or {}).get("about_task")
    if isinstance(about, dict) and float(about.get("noul", 0.0) or 0.0) >= 0.3:
        return AGENT, confidence, ms  # meant for a running task: never a quick answer
    return picked, confidence, ms


def placement(hermes_home: Path, provider: str, request: str, channels: list[tuple[str, str]],
              chats: list[tuple[str, str]], evaluator: Callable[..., dict[str, Any] | None] | None = None
              ) -> dict[str, Any] | None:
    """Where a new task goes, in one Jev call: is it several separate asks, which opted-in channel fits,
    whether it continues an existing conversation, and whether they want to see something. None when
    Jev is down or unsure about any of it (the routing model then decides, as before)."""
    questions: dict[str, Any] = {
        "several": {"type": "noul", "instructions": "The request holds two or more separate, independent asks."},
        "show": {"type": "noul", "instructions": "The user wants to SEE something (how a thing looks), not just hear an answer."},
    }
    if channels:
        questions["channel"] = {"type": "choice", "instructions": "Which channel's topic clearly fits this request?",
                                "criteria": {**{label: topic or label for label, topic in channels[:8]},
                                             "none": "None clearly fits; use the default place."}}
    if chats:
        questions["conversation"] = {"type": "choice",
            "instructions": "Does this request continue the same piece of work as one of these conversations?",
            "criteria": {**{ref: summary[:400] for ref, summary in chats[:6]},
                         "none": "No: it is new, general, or only shares a topic or word."}}
    answers = (evaluator or evaluate)(hermes_home, provider, f'Spoken request: "{request.strip()[:600]}"', questions)
    if not answers:
        return None
    several = float((answers.get("several") or {}).get("noul", 1.0) or 0.0)
    if 0.2 < several:  # possibly several asks: the routing model splits them
        return None
    out: dict[str, Any] = {"show": float((answers.get("show") or {}).get("noul", 0.0) or 0.0) >= 0.5,
                           "channel": None, "conversation": None}
    for name in ("channel", "conversation"):
        if name not in questions:
            continue
        answer = answers.get(name) or {}
        picked, confidence = answer.get("choice"), float(answer.get("confidence", 0.0) or 0.0)
        if confidence < MIN_CONFIDENCE or picked not in questions[name]["criteria"]:
            return None
        out[name] = None if picked == "none" else picked
    return out
