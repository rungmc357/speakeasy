"""Email draft cards: parse Hermes' `email-draft` block, canonicalize, hash.

A task that writes an email for the user ends its answer with a fenced `email-draft` JSON block
instead of sending it. The server strips the block from spoken/visible text, validates it, and
shows it as a card. Nothing is sent until the user presses Approve with the exact hash shown.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

DRAFT_BLOCK_RE = re.compile(r"(?s)\n?```email-draft\s*\n(.*?)\n```")
ADDRESS_RE = re.compile(r"^(?:[^<>\x00-\x1f\"]{0,80}<)?[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+>?$")
MAX_RECIPIENTS = 50
MAX_SUBJECT = 300
MAX_BODY = 20_000
MAX_DRAFTS_PER_ANSWER = 3
DRAFT_STATES = {"pending", "approved", "denied", "revising", "sent", "failed", "superseded"}
# What the card shows around the email, never part of what is sent (so never in its hash).
CONTEXT_KEY = "context"
NAMED_RE = re.compile(r"^\s*\"?([^<>\"]*?)\"?\s*<([^<>]+)>\s*$")


class DraftInvalid(ValueError):
    pass


def _address(value: Any) -> str:
    if not isinstance(value, str):
        raise DraftInvalid("address must be a string")
    text = value.strip()
    if not text or len(text) > 254 or not ADDRESS_RE.fullmatch(text):
        raise DraftInvalid("invalid email address")
    return text


def _addresses(value: Any, field: str, required: bool = False) -> list[str]:
    if value is None:
        value = []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > MAX_RECIPIENTS:
        raise DraftInvalid(f"{field} must be a list of addresses")
    out = [_address(v) for v in value]
    if required and not out:
        raise DraftInvalid(f"{field} needs at least one address")
    return out


def normalize_draft(raw: Any) -> dict[str, Any]:
    """Validated canonical draft: from, to, cc, bcc, subject, body (+ optional reply id / account)."""
    if not isinstance(raw, dict):
        raise DraftInvalid("draft must be an object")
    subject = raw.get("subject")
    body = raw.get("body")
    if not isinstance(subject, str) or len(subject) > MAX_SUBJECT or re.search(r"[\r\n\x00]", subject):
        raise DraftInvalid("subject must be one line")
    if not isinstance(body, str) or not body.strip() or len(body) > MAX_BODY or "\x00" in body:
        raise DraftInvalid("body must be plain text")
    if re.search(r"(?is)<\s*(?:html|body|div|p|br|table|script)\b", body):
        raise DraftInvalid("body must be plain text")
    sender = raw.get("from")
    draft: dict[str, Any] = {
        "from": _address(sender) if sender not in (None, "") else "",
        "to": _addresses(raw.get("to"), "to", required=True),
        "cc": _addresses(raw.get("cc"), "cc"),
        "bcc": _addresses(raw.get("bcc"), "bcc"),
        "subject": subject.strip(),
        "body": body.replace("\r\n", "\n").replace("\r", "\n").strip(),
    }
    for key, limit in (("reply_to_message_id", 500), ("account", 200)):
        value = raw.get(key)
        if value in (None, ""):
            continue
        if not isinstance(value, str) or len(value) > limit or re.search(r"[\r\n\x00]", value):
            raise DraftInvalid(f"{key} must be one short line")
        draft[key] = value.strip()
    return draft


def _line(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    return text[:limit].rstrip() if text else None


def _lines(value: Any, limit: int, count: int) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = [t for t in (_line(v, limit) for v in value) if t]
    return list(dict.fromkeys(out))[:count]


def draft_context(raw: Any) -> dict[str, Any]:
    """What the card shows around a draft (bounded; anything malformed is left out, never fatal):
    the thread it replies to, recipients the task named but couldn't find an address for, and
    notes on anything it changed from what was asked."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    subject = _line(raw.get("thread_subject"), MAX_SUBJECT)
    if subject:
        out["thread_subject"] = subject
    count = raw.get("thread_count")
    if isinstance(count, int) and not isinstance(count, bool) and 1 <= count <= 999:
        out["thread_count"] = count
    last = raw.get("thread_last")
    if isinstance(last, dict) and isinstance(last.get("text"), str) and last["text"].strip():
        quoted = {"text": last["text"].replace("\r\n", "\n").strip()[:4000]}
        for key, limit in (("from", 200), ("when", 60)):
            if (v := _line(last.get(key), limit)):
                quoted[key] = v
        out["thread_last"] = quoted
    for key, limit, count_max in (("unresolved", 120, 10), ("notes", 240, 5), ("attachments", 200, 10)):
        values = _lines(raw.get(key), limit, count_max)
        if values:
            out[key] = values
    if raw.get("edited") is True:
        out["edited"] = True
    return out


def with_context(draft: dict[str, Any], raw: Any) -> dict[str, Any]:
    context = draft_context(raw)
    return {**draft, CONTEXT_KEY: context} if context else draft


def sendable(draft: dict[str, Any]) -> dict[str, Any]:
    """The fields that go out (and are hashed): no card context, no server bookkeeping."""
    return {k: v for k, v in draft.items() if k != CONTEXT_KEY and not k.startswith("_")
            and k not in {"draft_id", "sha256", "status", "created_at", "updated_at", "error"}}


def canonical_json(draft: dict[str, Any]) -> str:
    return json.dumps(sendable(draft), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def draft_sha256(draft: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(draft).encode("utf-8")).hexdigest()


def stored_json(draft: dict[str, Any]) -> str:
    """What the store keeps: the sendable fields plus the card context."""
    return json.dumps(draft, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def recipient(value: str) -> dict[str, str]:
    """"Ben Rivera <ben@example.com>" -> {"name": "Ben Rivera", "address": "ben@example.com"};
    a bare address keeps an empty name (the card shows the address alone)."""
    match = NAMED_RE.match(value or "")
    if match:
        return {"name": match.group(1).strip(), "address": match.group(2).strip()}
    return {"name": "", "address": (value or "").strip()}


def apply_edit(draft: dict[str, Any], edit: Any) -> dict[str, Any]:
    """The user's own edit on the card (recipients, subject, body), validated like a task's draft.
    Unchanged fields are kept; the context says it was edited so the task updates a saved copy."""
    if not isinstance(edit, dict) or not edit or not set(edit) <= {"to", "cc", "bcc", "subject", "body"}:
        raise DraftInvalid("edit takes to, cc, bcc, subject and body")
    base = sendable(draft)
    merged = normalize_draft({**base, **edit})
    context = dict(draft.get(CONTEXT_KEY) or {})
    context["edited"] = True
    # A recipient the user just added is no longer one the task couldn't find.
    if context.get("unresolved"):
        named = " ".join(recipient(a)["name"].lower() for a in merged["to"] + merged["cc"] + merged["bcc"])
        left = [u for u in context["unresolved"] if u.lower() not in named]
        if left:
            context["unresolved"] = left
        else:
            context.pop("unresolved")
    return {**merged, CONTEXT_KEY: context}


def _display(people: list[dict[str, str]]) -> str:
    names = [p["name"] or p["address"] for p in people]
    if not names:
        return ""
    return names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more" if len(names) > 2 \
        else f"{names[0]} and {names[1]}"


def email_view(draft: dict[str, Any]) -> dict[str, Any]:
    """The email card for a stored draft: every recipient with name and address, the thread it
    answers, what the task couldn't resolve, and the draft id + hash the card's buttons act on."""
    context = draft.get(CONTEXT_KEY) or {}
    people = {k: [recipient(a) for a in draft.get(k) or []] for k in ("to", "cc", "bcc")}
    view: dict[str, Any] = {
        "kind": "draft", "channel": "email",
        "to": _display(people["to"]) or (context.get("unresolved") or [""])[0],
        "recipients": {k: v for k, v in people.items() if v or k == "to"},
        "subject": draft.get("subject") or "", "body": draft.get("body") or "",
        "draft_id": draft.get("draft_id"), "sha256": draft.get("sha256"),
        "status": draft.get("status") or "pending",
        # Older apps draw Send under the card; the server turns it into this draft's approval.
        "actions": [{"id": "send", "label": "Send", "style": "primary", "confirm": True}]
        if (draft.get("status") or "pending") == "pending" else [],
    }
    if draft.get("from"):
        view["send_as"] = draft["from"]
    if context.get("thread_subject") or draft.get("reply_to_message_id"):
        view["thread"] = context.get("thread_subject") or draft.get("subject") or ""
    for key in ("thread_count", "thread_last", "unresolved", "notes", "attachments", "edited"):
        if key in context:
            view[key] = context[key]
    if draft.get("error"):
        view["error"] = draft["error"]
    return view


def with_email_views(views: Any, drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A task's cards with its newest email draft as the email card. It takes the place of a draft
    card the task drew by hand for the same email (one card, not two), else goes last, so no other
    card's index (which card buttons address) ever moves. Home shows it in place of the done card."""
    views = list(views) if isinstance(views, list) else []
    live = [d for d in drafts if d.get("status") != "superseded"]
    if not live:
        return views
    card = email_view(live[-1])
    hand = [i for i, v in enumerate(views)
            if isinstance(v, dict) and v.get("kind") == "draft" and str(v.get("channel", "")).lower() == "email"]
    if hand:
        views[hand[0]] = card
        return views
    return [*views, card]


def extract_email_drafts(output: str) -> tuple[str, list[dict[str, Any]]]:
    """Strip every `email-draft` block; return (text, valid canonical drafts). Invalid blocks are
    dropped (and still stripped), never shown half-parsed."""
    drafts: list[dict[str, Any]] = []
    for match in DRAFT_BLOCK_RE.finditer(output):
        try:
            raw = json.loads(match.group(1))
            draft = with_context(normalize_draft(raw), raw)
        except (ValueError, TypeError):
            continue
        if len(drafts) < MAX_DRAFTS_PER_ANSWER:
            drafts.append(draft)
    return DRAFT_BLOCK_RE.sub("", output), drafts
