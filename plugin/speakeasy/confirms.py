"""Exact-preview confirmation cards. This confines the card route, not arbitrary agent tools.

Only an authenticated app tap or a call worker handling live speech may claim a held card.
The claim is durable before dispatch: an uncertain dispatch is never automatically repeated.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

KINDS = {"checkout", "send", "book", "invite", "pay", "subscription", "post", "form", "deploy", "home", "delete_share"}
REQUIRED = {
    "checkout": {"merchant", "address", "delivery", "payment_last4", "subtotal", "tax", "shipping", "total", "currency", "checkout_url", "total_source"},
    "send": {"recipient", "body", "channel"},
    "book": {"when", "party", "price", "cancellation"},
    "invite": {"when", "attendees", "conflicts"},
    "pay": {"payee", "total", "currency", "payment_last4", "arrival"},
    "subscription": {"operation", "consequences", "final_charge", "effective"},
    "post": {"account", "body"},
    "form": {"destination"},
    "deploy": {"target", "tests", "changes"},
    "home": {"device", "operation"},
    "delete_share": {"operation", "items_description", "recipient"},
}
FIELDS = set().union(*REQUIRED.values()) | {"subject", "photo_url", "description", "channel", "count"}
LABELS = {"send": "Send", "book": "Book", "invite": "Send invite", "post": "Post", "form": "Submit",
          "deploy": "Deploy", "home": "Confirm", "subscription": "Confirm", "delete_share": "Share"}


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(view: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in view.items() if k not in {
        "sha256", "actions", "state", "receipt", "error"}}).encode()).hexdigest()


def _text(value: Any, limit: int = 2000) -> str | None:
    # Reject rather than clip: approving a truncated message is approving something else.
    return value.strip() if isinstance(value, str) and value.strip() and len(value) <= limit else None


def _money(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,9}(?:\.\d{1,2})?", value):
        return None
    try:
        return format(Decimal(value).quantize(Decimal("0.01")), ".2f")
    except InvalidOperation:
        return None


def clean(raw: Any) -> dict | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str) or raw["type"] not in KINDS or not _text(raw.get("title"), 160):
        return None
    kind = raw["type"]
    details = raw.get("details")
    if not isinstance(details, dict):
        return None
    out = {}
    for key, value in details.items():
        if key not in FIELDS:
            continue
        text = _text(value, 20000 if key == "body" else 2000)
        if text is None:
            return None
        if key in {"total", "subtotal", "tax", "shipping"}:
            text = _money(text)
            if text is None:
                return None
        if key == "payment_last4" and not re.fullmatch(r"\d{4}", text):
            return None
        if key in {"checkout_url", "photo_url"} and not re.fullmatch(r"https://[^\s]+", text):
            return None
        out[key] = text
    if not REQUIRED[kind] <= out.keys():
        return None
    if "currency" in out and not re.fullmatch(r"[A-Z]{3}", out["currency"]):
        return None
    items = []
    if kind == "checkout":
        if out["total_source"] != "merchant_final":
            return None
        # Line totals come from the merchant, not arithmetic invented by the agent.
        if Decimal(out["subtotal"]) + Decimal(out["tax"]) + Decimal(out["shipping"]) != Decimal(out["total"]):
            return None
        source = raw.get("items")
        if not isinstance(source, list) or not 1 <= len(source) <= 30:
            return None
        for item in source:
            if not isinstance(item, dict) or not _text(item.get("name"), 240):
                return None
            qty = item.get("quantity")
            if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 999:
                return None
            row = {"name": item["name"].strip(), "quantity": qty}
            for key in ("description", "size", "colour", "photo_url"):
                if key in item:
                    text = _text(item[key], 600)
                    if not text or (key == "photo_url" and not re.fullmatch(r"https://[^\s]+", text)):
                        return None
                    row[key] = text
            items.append(row)
    fields = []
    if kind == "form":
        source = raw.get("fields")
        if not isinstance(source, list) or not 1 <= len(source) <= 40:
            return None
        for field in source:
            if not isinstance(field, dict) or not _text(field.get("label")) or not _text(field.get("value")) or not isinstance(field.get("guessed"), bool):
                return None
            fields.append({"label": field["label"], "value": field["value"], "guessed": field["guessed"]})
    if kind == "subscription" and out["operation"] not in {"cancel", "sign_up"}:
        return None
    if kind == "home" and out["operation"] not in {"unlock", "open_garage", "disarm"}:
        return None
    if kind == "delete_share" and out["operation"] not in {"delete", "share"}:
        return None
    view = {"kind": "confirm", "type": kind, "title": raw["title"].strip(), "details": out}
    if items:
        view["items"] = items
    if fields:
        view["fields"] = fields
    view["sha256"] = digest(view)
    view["state"] = "pending"
    label = LABELS.get(kind, "Confirm")
    label_amount = ("$" if out.get("currency") == "USD" else out.get("currency", "") + " ") + out.get("total", "")
    if kind in {"checkout", "pay"}:
        label = ("Buy for " if kind == "checkout" else "Pay ") + label_amount
    elif kind == "subscription":
        label = "Cancel subscription" if out["operation"] == "cancel" else "Sign up"
    elif kind == "home":
        label = {"unlock": "Unlock", "open_garage": "Open garage", "disarm": "Disarm"}[out["operation"]]
    elif kind == "delete_share" and out["operation"] == "delete":
        label = "Delete " + out.get("count", "items")
    view["actions"] = [{"id": "approve", "label": label, "style": "primary", "value": view["sha256"]},
                       {"id": "cancel", "label": "Cancel", "style": "secondary", "value": view["sha256"]}]
    return view


class Conflict(ValueError):
    pass


def claim(store: Any, run_id: str, index: int, expected: str, action: str, *, source: str, live_call: bool = False) -> tuple[str, dict, bool]:
    """Atomically claim the saved exact preview. Never trust a caller-supplied card or state.

    Returned bool says whether dispatch is newly authorized. A retry only reads the state.
    Updating the saved result in the same transaction keeps all client surfaces in agreement.
    """
    if source not in {"tap", "voice"} or (source == "voice" and not live_call):
        raise Conflict("Outside a live call, tap the card to approve.")
    if action not in {"approve", "cancel"}:
        raise Conflict("Unknown confirmation action.")
    with store._lock, store._db:
        store._db.execute("""CREATE TABLE IF NOT EXISTS confirmation_claims (
            run_id TEXT NOT NULL, card_index INTEGER NOT NULL, sha256 TEXT NOT NULL,
            state TEXT NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY(run_id, card_index, sha256))""")
        row = store._db.execute("SELECT idem_key,result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise Conflict("That confirmation is no longer available.")
        key, encoded = row
        result = json.loads(encoded or "{}")
        views = result.get("views", [])
        if not 0 <= index < len(views) or views[index].get("kind") != "confirm":
            raise Conflict("That confirmation is no longer available.")
        view = views[index]
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or not hmac.compare_digest(expected, digest(view)) or expected != view.get("sha256"):
            raise Conflict("The preview changed. Review it and approve again.")
        consumed = store._db.execute(
            "SELECT state FROM confirmation_claims WHERE run_id=? AND card_index=? AND sha256=?",
            (run_id, index, expected)).fetchone()
        if consumed:
            view["state"], view["actions"] = consumed[0], []
            store._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?", (canonical(result), key))
            return key, view, False
        state = view.get("state")
        if state != "pending":
            return key, view, False
        view["state"] = "approved" if action == "approve" else "cancelled"
        view["actions"] = []
        store._db.execute("INSERT INTO confirmation_claims VALUES (?,?,?,?,?)",
                          (run_id, index, expected, view["state"], source))
        store._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?", (canonical(result), key))
        return key, view, action == "approve"


def edit(store: Any, run_id: str, index: int, payload: Any) -> str:
    """Edit only the draft's body/subject; a stale edit cannot overwrite a newer approval."""
    if not isinstance(payload, dict) or not _text(payload.get("body"), 20000):
        raise Conflict("The draft is empty or too long.")
    with store._lock, store._db:
        row = store._db.execute("SELECT idem_key,result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise Conflict("That draft is no longer available.")
        key, encoded = row
        result = json.loads(encoded or "{}")
        views = result.get("views", [])
        if not 0 <= index < len(views):
            raise Conflict("That draft is no longer available.")
        view = views[index]
        if view.get("type") != "send" or view.get("state") != "pending" or view.get("sha256") != payload.get("sha256"):
            raise Conflict("The draft changed. Review the current version.")
        raw = {**view, "details": {**view["details"], "body": payload["body"]}}
        if "subject" in payload:
            if not _text(payload["subject"], 2000):
                raise Conflict("The subject is empty or too long.")
            raw["details"]["subject"] = payload["subject"]
        replacement = clean(raw)
        if replacement is None:
            raise Conflict("The edited draft is incomplete.")
        views[index] = replacement
        store._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?", (canonical(result), key))
        return key


def recover(store: Any) -> int:
    """After process loss, an accepted action is uncertain, never retryable automatically."""
    changed = 0
    with store._lock, store._db:
        if not store._db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='confirmation_claims'").fetchone():
            return 0
        rows = store._db.execute("SELECT run_id,card_index,sha256 FROM confirmation_claims WHERE state='approved'").fetchall()
        for run_id, index, expected in rows:
            row = store._db.execute("SELECT idem_key,result_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row:
                result = json.loads(row[1] or "{}")
                views = result.get("views", [])
                if 0 <= index < len(views) and views[index].get("sha256") == expected:
                    views[index].update(state="unverified", actions=[], error="Connection was interrupted after approval. Check the service before trying again.")
                    store._db.execute("UPDATE runs SET result_json=? WHERE idem_key=?", (canonical(result), row[0]))
                    changed += 1
            store._db.execute("UPDATE confirmation_claims SET state='unverified' WHERE run_id=? AND card_index=? AND sha256=?",
                              (run_id, index, expected))
    return changed


def execution_message(view: dict) -> str:
    return ("The user approved exactly this preview (approval " + view["sha256"] + "). "
            "Do only this action. For checkout, read the merchant's final total again immediately before buying; "
            "if ANY price, recipient, item or other term changed, DO NOT ACT: return a fresh confirm card and ask again. "
            "Never report a receipt until the service confirms success.\n" + canonical(view))
