"""Display-only automatic events. Never infer automation from human message text."""
import hashlib
import json
import re

_KINDS = {"internal_notification", "async_delegation_complete", "process_complete"}
_HEADER = re.compile(r"\[kanban\] Task (t_[A-Za-z0-9_-]{1,80}) (completed|failed|blocked(?:; needs attention)?|ready for review|gave up \(retries exhausted\)(?:, crashed \(worker exited\); dispatcher will retry)?)\.[ \t]*(?:\r?\n|$)")
_UNSAFE = re.compile(r"(?:\b(?:Assignee|Board|Task ID|Provider|Instructions):|t_[a-z0-9]{6,}|(?:/Users/|~/)|```|\b(?:tool_call|continuation|kanban_create|remote-run)\b)", re.I)


def _field(value, limit):
    if not isinstance(value, str):
        return ""
    # Keep recorded prose, not executable scaffolding. Never paraphrase a release claim.
    value = re.sub(r"\s*\((?:Codex|local|agent)[^)]*continuation\)\s*", "", value, flags=re.I)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    safe = []
    for line in lines:
        if _UNSAFE.search(line) or line.startswith(("This is an automatic", "Check the result", "Inspect the current")):
            break
        safe.append(line)
    return " ".join(safe)[:limit].strip()


def event_for(row):
    """Only persisted producer display kinds are authoritative. Legacy fallback parses their
    bounded known envelope; an identical human paste, even with a footer, remains human."""
    def get(key):
        try:
            return row[key]
        except (KeyError, IndexError):
            return None
    kind = get("display_kind")
    if kind not in _KINDS or get("role") not in {"user", "system"}:
        return None
    raw = get("display_metadata")
    try:
        meta = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        meta = None
    meta = meta if isinstance(meta, dict) else {}
    text = (get("content") or "")[:16000]
    header = _HEADER.search(text[:3000]) if kind == "internal_notification" else None
    structured = meta.get("task_event")
    structured = structured if isinstance(structured, dict) else {}
    task_id = structured.get("task_id") or (header[1] if header else None)
    state = structured.get("state") or (header[2] if header else None)
    task_id = task_id if isinstance(task_id, str) else None
    state = state if isinstance(state, str) else None
    if state and state.startswith("blocked;"):
        state = "blocked"
    if state and state.startswith("gave up"):
        state = "failed"
    labels = {"completed": "Task completed", "failed": "Task failed", "blocked": "Task needs attention", "ready for review": "Ready for review"}
    title = _field(structured.get("title"), 160)
    summary = _field(structured.get("summary"), 640)
    if header:
        body = text[header.end():]
        title_match = re.search(r"^Title: ([^\n]+)", body, re.M)
        result = re.search(r"^Result: ([\s\S]*?)(?=\n\s*\n|\nThis is an automatic|\Z)", body, re.M)
        title = title or _field(title_match[1] if title_match else "", 160)
        summary = summary or _field(result[1] if result else "", 640)
    if kind == "async_delegation_complete":
        state = "failed" if meta.get("failed_count") else "completed"
        title = title or "Background work"
    elif kind == "process_complete":
        title = title or "Background process"
    title = re.sub(r"\s*\((?:Codex|local|agent)[^)]*continuation\)\s*", "", title, flags=re.I).strip()
    title = title or "Automatic task update"
    label = labels.get(state or "", "Task update")
    if len(summary) >= 640:
        summary = summary[:639].rstrip() + "…"
    elif summary and summary[-1] not in ".!?…":
        summary = summary.rstrip() + "…"
    summary = summary or "A recorded update is available."
    identity = f"{kind}|{task_id}|{state}" if task_id else f"{kind}|{get('id')}"
    event = {"id": hashlib.sha256(identity.encode()).hexdigest()[:24], "title": title,
             "state": state if state in labels else "unknown", "label": label, "summary": summary}
    # Only explicit safe links from producer metadata, never URLs scraped from instructions.
    url = structured.get("url")
    if isinstance(url, str) and re.fullmatch(r"https://[^\s<>]{1,1000}", url):
        event["url"] = url
    return event


def dedupe_events(entries):
    seen = set()
    out = []
    for entry in entries:
        event = entry.get("event")
        if event:
            if event["id"] in seen:
                continue
            seen.add(event["id"])
        # Exact mirrored results only; never suppress a later substantive answer.
        if entry.get("kind") == "assistant" and out and out[-1].get("event"):
            if entry.get("text", "").strip() == out[-1]["event"]["summary"]:
                continue
        out.append(entry)
    return out
