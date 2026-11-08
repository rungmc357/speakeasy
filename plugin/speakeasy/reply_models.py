"""Reply attribution from persisted evidence, never the current model picker.

Older Hermes versions have no per-message model. A task's served runtime is the
next best evidence; session records are used only when not known to be mixed.
No reasoning *content*, credentials, URLs or arbitrary metadata leave this adapter.
"""
from __future__ import annotations

import json
from typing import Any

from . import models as MD


def object_value(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def clean(value: Any, limit: int = 200) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() in {"unknown", "default", "none", "null"}:
        return None
    # This field is a model identifier, not an endpoint or credential.
    if "://" in value or any(c in value for c in "\n\r"):
        return None
    return value[:limit]


def badge(record: dict, source: str) -> dict | None:
    served = object_value(record.get("served"))
    model = clean(served.get("model")) or clean(record.get("model"))
    if not model:
        return None
    provider = clean(served.get("provider"), 80) or clean(record.get("provider"), 80)
    # Requested ids in runs.model are provider-qualified. Raw message models may
    # contain a slash (a model repository), so split only the requested run id.
    requested = clean(record.get("model"))
    if source == "run" and requested and "/" in requested:
        requested_provider, requested_model = MD.split_id(requested)
        if not served:
            provider, model = requested_provider, requested_model
    if not clean(model.rsplit('/', 1)[-1]):
        return None
    mid = MD.make_id(provider, model) if provider else model
    requested_id = requested
    if requested and provider and "/" not in requested:
        requested_id = MD.make_id(clean(record.get("provider"), 80) or provider, requested)
    fallback = record.get("fallback") is True or (bool(served) and bool(requested_id) and mid != requested_id)
    # A requested reasoning level doesn't prove the fallback used that level.
    level = served.get("reasoning") if served else record.get("reasoning_effort", record.get("reasoning"))
    if fallback and not served.get("reasoning"):
        level = None
    return {"id": mid, "name": MD.friendly_name(model), "provider": provider or "",
            "provider_name": provider or "", "reasoning": level if level in MD.LEVELS else None,
            "source": source, "served": bool(served) or source == "message", "fallback": fallback}


def message_badge(row: Any) -> dict | None:
    r = dict(row)
    # Only dedicated provenance fields; `reasoning` on Hermes messages is text,
    # not a reasoning effort and must never be inspected or returned here.
    raw = {k: r.get(k) for k in ("model", "provider", "reasoning_effort", "fallback")}
    meta = object_value(r.get("display_metadata"))
    explicit = object_value(meta.get("model_attribution"))
    return badge(explicit or raw, "message")


def attribute(rows: list[Any], entries: list[dict], sessions: dict[str, dict],
              runs: dict[str, list[dict]] | None = None) -> list[dict]:
    by_id = {f"m{r['id']}": dict(r) for r in rows}
    for entry in entries:
        if entry.get("kind") != "assistant":
            continue
        row = by_id.get(entry["id"], {})
        sid = row.get("session_id") or ""
        model = message_badge(row)
        if model is None and isinstance(entry.get("at"), (float, int)):
            at = entry["at"]
            candidates = (runs or {}).get(sid, [])
            # Each persisted run owns only its own time interval. Overlap is
            # ambiguous: never assign another parallel task's model to a reply.
            matches = [r for r in candidates if r["start"] <= at and (r.get("end") is None or at <= r["end"])]
            if len(matches) > 1:
                continue
            if len(matches) == 1:
                model = badge(matches[0]["record"], "run")
        if model is None:
            known = [badge(r['record'], 'run') for r in (runs or {}).get(sid, [])]
            identities = {b['id'] for b in known if b}
            if len(identities) <= 1:
                model = badge(sessions.get(sid, {}), "session")
        if model:
            entry["model"] = model
    return entries
