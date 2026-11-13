"""Email draft cards: parse Hermes' `email-draft` block, canonicalize, hash.

A task that writes an email for the user ends its answer with a fenced `email-draft` JSON block
instead of sending it. The server strips the block from spoken/visible text, validates it, and
shows it as a card. Nothing is sent until the user presses Approve with the exact hash shown.
"""
from __future__ import annotations

import difflib
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
    # {"Pat Doe": "Lark & Finch"}: a firm the ask named beside a person (shown by the name only when
    # that person's address domain supports it; never a recipient of its own).
    orgs = raw.get("orgs")
    if isinstance(orgs, dict):
        kept = {n: o for n, o in ((_line(k, 120), _line(v, 120)) for k, v in list(orgs.items())[:10]) if n and o}
        if kept:
            out["orgs"] = kept
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


# An organisation named in the ask is never a missing recipient (b73: "an email to Ben and Wren
# Mallory", where Wren Mallory is Ben's firm). Only a named PERSON without an address is warned about.
ORG_WORD_RE = re.compile(
    r"(&|\+|\b(?:llp|llc|lp|inc|incorporated|corp|corporation|co|company|ltd|limited|plc|gmbh|ag|bv|pllc|"
    r"group|partners|partnership|associates|capital|ventures|holdings|advisors|advisers|consulting|"
    r"legal|foundation|labs|agency|realty|properties|"
    r"cpas?|accountants|solicitors|attorneys)\b\.?)", re.IGNORECASE)
# Mail hosts say nothing about where someone works.
PERSONAL_DOMAINS = frozenset({
    "gmail", "googlemail", "outlook", "hotmail", "live", "msn", "yahoo", "ymail", "icloud", "me", "mac", "aol",
    "proton", "protonmail", "pm", "fastmail", "gmx", "zoho", "hey", "mail", "example"})


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _domain_label(address: str) -> str:
    """"ben@mail.lakeshore.co.uk" -> "lakeshore" (the registrable name, best effort)."""
    host = (address or "").rpartition("@")[2].lower()
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return ""
    second_level = len(parts) >= 3 and parts[-2] in {"co", "com", "org", "net", "ac", "gov"} and len(parts[-1]) == 2
    label = parts[-3] if second_level else parts[-2]
    return "" if label in PERSONAL_DOMAINS else label


def domain_supports(org: str, address: str) -> bool:
    """Whether an address's domain is plausibly this organisation's: "Lark & Finch" ~ larkfinch.example,
    "Wren Mallory" ~ wrenmalory.com, "Harbor Lane Partners" ~ harborlane.example. Letters only, never a
    guess beyond them; a personal mail host never supports one."""
    label = _domain_label(address)
    words = [w for w in re.findall(r"[a-z0-9]+", (org or "").lower()) if not ORG_WORD_RE.fullmatch(w)]
    name = "".join(words) or _squash(org)
    if len(label) < 3 or len(name) < 3:
        return False
    short, long = sorted((label, name), key=len)
    if short in long and len(short) / len(long) >= 0.6:
        return True   # most of the letters, not a fragment: "Pat" never matches patterson.example
    if len(words) >= 2 and "".join(w[0] for w in words) == label:
        return True   # initials: "Ostrow Fenn Kalb" ~ ofk.example
    return difflib.SequenceMatcher(None, label, name).ratio() >= 0.85


def looks_like_org(name: str) -> bool:
    return bool(ORG_WORD_RE.search(name or ""))


def _with_orgs(people: dict[str, list[dict[str, str]]], context: dict[str, Any]) -> tuple[dict, list[str]]:
    """Recipients with the firm the ask named beside them (only where the address domain supports
    it), and the names still missing an address: people only, never an organisation."""
    named = {k.lower(): v for k, v in (context.get("orgs") or {}).items()}
    everyone = [p for k in ("to", "cc", "bcc") for p in people[k]]
    orgs = set(named.values())
    missing: list[str] = []
    for entry in context.get("unresolved") or []:
        if entry in orgs or looks_like_org(entry) or any(domain_supports(entry, p["address"]) for p in everyone):
            orgs.add(entry)
        else:
            missing.append(entry)
    out = {k: [dict(p) for p in v] for k, v in people.items()}
    shown: set[str] = set()
    for p in (q for key in ("to", "cc", "bcc") for q in out[key]):
        mapped = named.get(p["name"].lower()) if p["name"] else None
        if mapped and domain_supports(mapped, p["address"]):
            p["org"] = mapped
            shown.add(mapped)
    # A firm named without a mapping goes beside the first named person whose address is that firm's.
    for org in sorted(orgs - shown):
        for p in (q for key in ("to", "cc", "bcc") for q in out[key]):
            if "org" not in p and p["name"] and domain_supports(org, p["address"]):
                p["org"] = org
                break
    return out, missing


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
    people, missing = _with_orgs({k: [recipient(a) for a in draft.get(k) or []] for k in ("to", "cc", "bcc")},
                                 context)
    view: dict[str, Any] = {
        "kind": "draft", "channel": "email",
        "to": _display(people["to"]) or (missing or [""])[0],
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
    for key in ("thread_count", "thread_last", "notes", "attachments", "edited"):
        if key in context:
            view[key] = context[key]
    if missing:
        view["unresolved"] = missing
    if draft.get("error"):
        view["error"] = draft["error"]
    return view


_REPLY_PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|aw|sv)\s*:\s*)+", re.I)
_WORD = re.compile(r"[^\W\d_]{2,}", re.U)


def _subject_key(value: Any) -> str:
    text = _REPLY_PREFIX.sub("", value if isinstance(value, str) else "")
    return " ".join(text.split()).casefold()


def _people_words(text: str) -> set[str]:
    return {w.casefold() for w in _WORD.findall(text or "")}


def _same_email(hand: dict[str, Any], draft: dict[str, Any]) -> bool:
    """Whether a card the task drew by hand shows this saved draft: its subject (ignoring Re:/Fwd:)
    and its recipient line must agree with the draft's. A hand card naming neither can't be told
    apart from any other email, so it is kept rather than silently replaced."""
    subject, to = hand.get("subject"), hand.get("to")
    has_subject = isinstance(subject, str) and subject.strip() != ""
    has_to = isinstance(to, str) and to.strip() != ""
    if not (has_subject or has_to):
        return False
    if has_subject and _subject_key(subject) != _subject_key(draft.get("subject")):
        return False
    if has_to:
        line = str(to).casefold()
        addresses: list[str] = []
        words: set[str] = set()
        for raw in draft.get("to") or []:
            item = str(raw)
            match = re.search(r"<([^>]+)>", item)
            address = (match.group(1) if match else item).strip().casefold()
            addresses.append(address)
            words |= _people_words(item[:match.start()] if match else "")
            words |= _people_words(re.split(r"[@]", address)[0].replace(".", " ").replace("_", " "))
        if not (any(a and a in line for a in addresses) or (_people_words(str(to)) & words)):
            return False
    return True


def with_email_views(views: Any, drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A task's cards with its newest email draft as the email card. It takes the place of a draft
    card the task drew by hand for the same email (one card, not two), else goes last, so no other
    card's index (which card buttons address) ever moves. A hand-drawn card for a different email
    stays, so a job's second email is never swapped for its first. Home shows it in place of the
    done card."""
    views = list(views) if isinstance(views, list) else []
    live = [d for d in drafts if d.get("status") != "superseded"]
    if not live:
        return views
    draft = live[-1]
    card = email_view(draft)
    hand = [i for i, v in enumerate(views)
            if isinstance(v, dict) and v.get("kind") == "draft" and str(v.get("channel", "")).lower() == "email"
            and _same_email(v, draft)]
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
