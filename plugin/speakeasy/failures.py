"""Why a Hermes run failed, in Speakeasy's own words.

Hermes puts the provider's one-line error on a failed run: the ``error`` field of the
``run.failed`` event and of ``GET /v1/runs/{id}``, e.g. ``"HTTP 402: Insufficient USD or Diem
balance"`` (its ``_summarize_api_error``, redacted before it leaves the API server). Only shapes
known to mean one thing are mapped to a fixed sentence, like ``codex_transport.explain_codex_error``;
the provider's own words are never stored, shown, or spoken. They stay in Hermes' own log.
"""
from __future__ import annotations

import re
import urllib.error
from pathlib import Path
from typing import Any

# kind -> (short label for the task row, what happened and what to do, for the detail and the chat
# notice). Plain words only: never a terminal command, a file path or "the Hermes machine". When a
# fix needs Hermes itself, it is a chat command the user sends their Hermes (/model, /restart) from
# wherever they talk to it; the app's own buttons (Try again, Switch model) cover the rest.
REASONS: dict[str, tuple[str, str]] = {
    "billing": ("Out of credits",
                "Your AI account is out of credits. Add credits there, or switch model in "
                "this chat."),
    "auth": ("Needs signing in again",
             "Your AI account needs signing in again. Send /model to your Hermes to sign in, "
             "or switch model here."),
    "rate_limit": ("Too many requests",
                   "Your AI account is busy (too many requests). Try again in a minute, or "
                   "switch model."),
    # The provider's safety filter refused this request (Hermes' content_policy_blocked). Often a false
    # positive on ordinary work; the fix is a retry or another model, never the key, credits or a firewall.
    "content_refused": ("AI model declined",
                        "The AI model declined this one. Try again, or switch model for this chat."),
    "model_not_found": ("Model unavailable",
                        "That AI model isn't available any more. Pick another for this chat."),
    # Speakeasy -> Hermes API server (POST /v1/runs), not the model provider. Our own messages.
    "hermes_key": ("Hermes didn't accept Speakeasy",
                   "Your Hermes didn't accept Speakeasy's key. Send /restart to your Hermes. If that "
                   "doesn't fix it, ask it to set up Speakeasy again."),
    "hermes_unreachable": ("Couldn't reach your Hermes",
                           "Speakeasy couldn't get through to your Hermes. Send /restart to your Hermes, "
                           "then try again."),
}
# kind -> what the voice says. The voice never reads a command aloud (it can't be copied from speech
# and sounds like noise), so these name the fix in words and send the user to the app, whose task
# detail and chat notice carry the REASONS text.
SPOKEN: dict[str, str] = {
    "billing": "Your AI account is out of credits. Add credits there, or switch this chat to "
               "another model in the app.",
    "auth": "Your AI account needs signing in again. The steps are in the app, or you can "
            "switch model there.",
    "rate_limit": "Your AI account is busy with too many requests. Try again in a minute, or "
                  "switch model in the app.",
    "content_refused": "The AI model declined that one. You can try again, or switch this "
                       "chat to another model in the app.",
    "model_not_found": "That AI model isn't available any more, so this chat needs another one. You can "
                       "pick it in the app.",
    "hermes_key": "Your Hermes didn't accept Speakeasy's key. Restarting your Hermes usually fixes it; "
                  "the steps are in the app.",
    "hermes_unreachable": "Speakeasy couldn't get through to your Hermes. Restarting your Hermes usually "
                          "fixes it; the steps are in the app.",
}
assert set(SPOKEN) == set(REASONS)
UNKNOWN = "unknown"  # failed with no reason Speakeasy recognizes and no answer
# What an unknown failure says, on screen and spoken. No log path or "error log": the app offers Try
# again, and the cause stays in Hermes' own log for whoever looks after the machine.
UNKNOWN_TEXT = "Your Hermes stopped without saying why."

# "HTTP 402: ..." is Hermes' own prefix; "Error code: 402 - {...}" is the OpenAI SDK's str(error),
# which Hermes passes on when the error has no parsed body.
_STATUS_RE = re.compile(r"\b(?:http|error code:?|status(?: code)?:?)\s*(\d{3})\b")
# Hermes' error_classifier billing patterns plus Venice's "Insufficient USD or Diem balance".
_BILLING_RE = re.compile(
    r"insufficient\b[^.]{0,40}\b(?:credit|balance|funds|quota)|insufficient_quota|payment required"
    r"|out of credits|credits? (?:have been )?exhausted|credit balance|exceeded your current quota"
    r"|budget limit exceeded|key limit exceeded|spending limit|out of funds|top up your credits"
    r"|no usable credits|requires available credits|account balance is too low|balance_depleted"
    r"|billing hard limit|hard billing limit|billing_not_active")
_MODEL_RE = re.compile(
    r"model_not_found|model not found|no such model|unknown model|invalid model|is not a valid model"
    r"|unsupported model|\bmodel\b[^.\n]{0,80}\b(?:does not exist|not found|is not available|isn't available)")
_RATE_RE = re.compile(r"rate[ _-]?limit|too many requests|provider rate-limited|resource[ _-]?exhausted")
_AUTH_RE = re.compile(
    r"invalid[ _]api[ _]key|incorrect api key|unauthorized|authentication|invalid token|token expired"
    r"|token revoked|not logged in")
# Hermes' _USAGE_LIMIT_PATTERNS / _USAGE_LIMIT_TRANSIENT_SIGNALS: a usage limit with a reset or retry
# window is a periodic quota (wait), not an empty balance (top up).
_USAGE_LIMIT_RE = re.compile(r"usage limit|quota|limit exceeded")
_TRANSIENT_RE = re.compile(r"try again|retry|resets? (?:at|in|after)|available in|wait|requests remaining"
                           r"|periodic|window|per minute|per second")
# A provider's safety filter refusing this prompt: Hermes' own verdict (``content_policy_blocked: ...`` on
# the run's error) and the provider phrases Hermes matches (agent/error_classifier.py
# _CONTENT_POLICY_BLOCKED_PATTERNS, Anthropic stop_reason=refusal). Checked before every other shape: a
# refusal often says "your request was blocked", which is not a firewall.
_CONTENT_REFUSAL_RE = re.compile(
    r"content_policy_blocked|content[ _]policy[ _]block|safety filter refused|declined to respond"
    r"|provider refusal category|stop_reason[\"':= ]{0,4}refusal|content_filter|responsibleaipolicyviolation"
    r"|flagged for possible cybersecurity risk|violates (?:our|openai's) usage policies|your request was flagged by"
    r"|prompt was flagged by our safety|responses cannot be generated due to safety|data_inspection_failed"
    r"|data may contain inappropriate content|content exists risk|new_sensitive")
# A 403 from a CDN/WAF or relay in front of the provider is not the user's key (Hermes'
# _UPSTREAM_BLOCKED_PATTERNS): no reason Speakeasy can act on, so it is never called "auth".
_UPSTREAM_BLOCK_RE = re.compile(r"cloudflare|just a moment|attention required|request was blocked|request blocked"
                                r"|you have been blocked|enable javascript and cookies|cdn-cgi/challenge-platform"
                                r"|cf-browser-verification|__cf_chl|cf-error-details")
# Saying "a firewall blocked it" needs proof of a real HTML challenge or block page (or the <title> Hermes
# keeps from one). Plain words like "request was blocked" are what a provider's safety refusal says too.
_BLOCK_PAGE_RE = re.compile(r"<!doctype html|<html|attention required! \| cloudflare|just a moment\.\.\."
                            r"|used cloudflare to restrict access|sorry, you have been blocked"
                            r"|enable javascript and cookies|cdn-cgi/challenge-platform|cf-browser-verification"
                            r"|challenge-error-text|__cf_chl|cf-error-details|cloudflare ray id")
# Hermes' fixed lead-ins for the provider's error line in a failed turn's reply (agent/turn_failure_copy.py,
# conversation_loop._billing_terminal_label). Only text after them is read, never the rest of a reply.
_PROVIDER_LINE_RE = re.compile(r"(?:Provider said|Billing or credits exhausted):[^\n]*")
# Hermes' fixed opening for a safety-filter refusal (agent/turn_failure_copy.content_policy_copy): the
# provider line after it could read like anything on its own ("Your request was blocked").
_REFUSAL_COPY_RE = re.compile(r"safety filter refused this request")


def failure_kind(error: Any) -> str | None:
    """The kind of a Hermes run error, or None when it isn't a shape known to mean one thing.
    402, 403 and 429 follow Hermes' own verdicts (agent/error_classifier.py ``_classify_402``,
    ``_status_403``, ``_status_429``), so the fix the user is told matches what Hermes decided."""
    if not isinstance(error, str) or not error.strip():
        return None
    text = error.lower()
    if _CONTENT_REFUSAL_RE.search(text):
        return "content_refused"
    match = _STATUS_RE.search(text)
    status = int(match.group(1)) if match else None
    transient = bool(_TRANSIENT_RE.search(text))
    if status == 402:
        # "Usage limit reached, try again in 5 minutes" is a periodic quota, not an empty balance.
        return "rate_limit" if _USAGE_LIMIT_RE.search(text) and transient else "billing"
    if status == 429:
        # A quota wall (OpenAI insufficient_quota) is billing only when it isn't itself a rate-limit
        # phrase and names no reset or retry window (Gemini's "exceeded your current quota … retry in 33s").
        wall = _USAGE_LIMIT_RE.search(text) or _BILLING_RE.search(text)
        return "billing" if wall and not _RATE_RE.search(text) and not transient else "rate_limit"
    if status == 403:
        # A spending cap (OpenRouter "Key limit exceeded", xAI "spending limit") is billing; a WAF or
        # relay block page never reached the provider; any other 403 is the key or sign-in.
        if _BILLING_RE.search(text):
            return "billing"
        return None if _UPSTREAM_BLOCK_RE.search(text) else "auth"
    if status == 401:
        return "auth"
    # Any other status (400, 404, …) or none: the words decide.
    if _BILLING_RE.search(text):
        return "billing"
    if _MODEL_RE.search(text):
        return "model_not_found"
    if _RATE_RE.search(text):
        return "rate_limit"
    if _AUTH_RE.search(text):
        return "auth"
    return None


def provider_line(reply: Any) -> str | None:
    """The provider-error line from a failed turn's reply, when Hermes marked one. The session
    chat stream's ``run.failed`` has no ``error`` field; its reply is Hermes' failure message."""
    if not isinstance(reply, str):
        return None
    match = _PROVIDER_LINE_RE.search(reply)
    if not match:
        return None
    if _REFUSAL_COPY_RE.search(reply[:match.start()]):
        return "content_policy_blocked: " + match.group(0)
    return match.group(0)


def start_failure_kind(exc: BaseException) -> str | None:
    """Why Speakeasy couldn't get a run going on Hermes' API server: from the HermesError our client
    raised (its message is ours, never the server's body) or, for the session chat stream, urllib's."""
    from .hermes_api import HermesError
    if isinstance(exc, HermesError):
        if exc.status == 401:
            return "hermes_key"
        return "hermes_unreachable" if exc.message.startswith("Hermes unavailable") else None
    if isinstance(exc, urllib.error.HTTPError):
        return "hermes_key" if exc.code == 401 else None
    if isinstance(exc, (urllib.error.URLError, ConnectionError)):
        return "hermes_unreachable"
    return None


def unknown_text(hermes_home: Path | None = None) -> str:
    """No recognized reason: say so plainly. ``hermes_home`` is accepted for older callers and unused:
    a log path is no use to someone on a phone."""
    return UNKNOWN_TEXT


def public_failure(kind: str | None, hermes_home: Path | None = None) -> dict[str, str] | None:
    """What the app shows for a stored kind: ``label`` (row) only for a known reason, ``text`` always."""
    if kind in REASONS:
        label, text = REASONS[kind]
        return {"kind": kind, "label": label, "text": text}
    if kind == UNKNOWN:
        return {"kind": UNKNOWN, "text": unknown_text(hermes_home)}
    return None


def reason_text(kind: str | None) -> str | None:
    """The on-screen sentence for a known kind (task detail, chat notice); None for unknown or no kind.
    The voice uses voice_text instead."""
    return REASONS[kind][1] if kind in REASONS else None


def voice_text(kind: str | None) -> str | None:
    """What the voice says about a failed task (spoken now, or in its background notes on a later
    call): the SPOKEN sentence, never a command."""
    if kind == UNKNOWN:
        return UNKNOWN_TEXT
    return SPOKEN.get(kind) if kind else None


# -- failure_reason: one short plain sentence for every failed task ---------------------------------
# Even when no kind above fits, a coarse hint about the error's shape is kept (never its words), so
# the card can say more than "Work failed". Each hint maps to a fixed sentence of our own.
_HINT_RES: list[tuple[str, re.Pattern[str]]] = [
    ("blocked", _BLOCK_PAGE_RE),
    ("context", re.compile(r"context (?:length|window)|maximum context|too many tokens|prompt is too long"
                           r"|token limit|context_length_exceeded")),
    ("timeout", re.compile(r"timed? ?out|timeout|deadline exceeded")),
    ("offline", re.compile(r"can't reach|cannot reach|could not reach|connection (?:refused|reset|error|aborted)"
                           r"|network|offline|name resolution|getaddrinfo|unreachable")),
    ("provider_error", re.compile(r"\b(?:http|error code:?|status(?: code)?:?)\s*5\d\d\b|overloaded"
                                  r"|internal server error|bad gateway|service unavailable")),
]
HINT_REASONS = {
    "blocked": "Your AI provider's website blocked the request. Try again later, or switch model.",
    "context": "The work got too long for the model to keep in view.",
    "timeout": "The AI model took too long. Try again.",
    "offline": "Your Hermes couldn't reach its AI model. Check its internet, then try again.",
    "provider_error": "The AI model had a problem on its side. Try again in a minute.",
    "start": "Speakeasy couldn't start it on Hermes.",
}
NO_REASON = UNKNOWN_TEXT
INTERRUPTED_REASON = "Speakeasy restarted while it was running, so it lost track of it."


def failure_hint(error: Any) -> str | None:
    """The coarse shape of an error no kind fits (\"timeout\", \"offline\", ...), or None."""
    if not isinstance(error, str) or not error.strip():
        return None
    text = error.lower()
    return next((hint for hint, pattern in _HINT_RES if pattern.search(text)), None)


def failure_reason(status: str | None, kind: str | None, hint: str | None = None) -> str | None:
    """Why a task didn't finish, as one short plain sentence for the card (never "unknown"); None
    unless it failed or was interrupted."""
    if status == "interrupted":
        return INTERRUPTED_REASON
    if status != "failed":
        return None
    if kind in REASONS:
        text = REASONS[kind][1]
        return text.split(". ")[0].rstrip(".") + "."
    return HINT_REASONS.get(hint or "") or NO_REASON
