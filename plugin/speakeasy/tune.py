"""Tune the voice brief from the user's own calls.

Hermes reads recent calls (what was said, how each task ended) next to the current brief and proposes
small, specific brief edits, each tied to the moment that prompted it. Nothing changes until the user
accepts edits; only the brief is ever edited, never the shared voice rules. Problems a brief cannot
fix (slow answers, wrong routing, mishearing) come back separately as product issues.

Calls are kept locally (``call-log.jsonl``, owner-only, pruned) so there is something to tune from;
a call's text leaves the machine only when the user asks for a tune, to the model Hermes uses.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import brief as B
from .plain_errors import TUNE_FAILED
from .prompt.builder import PROMPT_DIR

logger = logging.getLogger(__name__)

LOG_FILE = "call-log.jsonl"
PROPOSAL_FILE = "tune-proposal.json"
KEEP_DAYS = 14
KEEP_CALLS = 60
TUNE_DAYS = 7
MAX_DIGEST_CHARS = 60_000
MAX_TURN_CHARS = 600
MAX_EDITS = 8
SECTIONS = B.REQUIRED_SECTIONS
_REDACTED = "[redacted]"


def _private_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def scrub(text: str) -> str:
    """Drop anything that looks like a secret or contact detail before it goes to a model."""
    out = text or ""
    for pattern in B._SECRET_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out


# -- the local call log ------------------------------------------------------------------------

class CallLog:
    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time):
        self.path = state_dir / LOG_FILE
        self.clock = clock
        self._lock = threading.Lock()

    def record(self, interaction_id: str, turns: list[dict[str, str]], tasks: list[dict[str, Any]],
               device: str = "", client: dict[str, str] | None = None) -> None:
        if not turns and not tasks:
            return
        entry: dict[str, Any] = {"id": interaction_id, "ended": self.clock(), "device": device[:40]}
        if client:  # the app build that made the call (devices.parse_client); absent for older apps
            entry["client"] = {k: str(v)[:24] for k, v in client.items()}
        entry["turns"] = [{"role": t.get("role", ""), "kind": t.get("kind", "speech"),
                           "text": str(t.get("text", ""))[:MAX_TURN_CHARS]}
                          for t in turns[-200:]]
        entry["tasks"] = tasks[-40:]
        with self._lock:
            _private_append(self.path, json.dumps(entry, ensure_ascii=False))
            self._prune()

    def _prune(self) -> None:
        entries = self._read()
        cutoff = self.clock() - KEEP_DAYS * 86400
        keep = [e for e in entries if e.get("ended", 0) >= cutoff][-KEEP_CALLS:]
        if len(keep) != len(entries):
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in keep)
            os.replace(tmp, self.path)

    def _read(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def get(self, interaction_id: str) -> dict[str, Any] | None:
        """Exact call lookup within the same retention window as the private call log."""
        cutoff = self.clock() - KEEP_DAYS * 86400
        with self._lock:
            return next((e for e in reversed(self._read())
                         if isinstance(e, dict) and e.get("id") == interaction_id
                         and e.get("ended", 0) >= cutoff), None)

    def recent(self, days: float = TUNE_DAYS) -> list[dict[str, Any]]:
        cutoff = self.clock() - days * 86400
        with self._lock:
            return [e for e in self._read() if e.get("ended", 0) >= cutoff]


def merge_calls(logged: list[dict[str, Any]], from_tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Logged calls (with transcripts) plus calls known only from their tasks (before the log existed)."""
    seen = {c["id"] for c in logged}
    extra = [c for c in from_tasks if c["id"] not in seen]
    return sorted(logged + extra, key=lambda c: c.get("ended", 0))


def digest(calls: list[dict[str, Any]], assistant: str = "Assistant", user: str = "User") -> str:
    """The calls as plain text for the tuning prompt, newest kept when it's long, secrets scrubbed."""
    blocks = []
    for call in calls:
        when = time.strftime("%a %b %d, %H:%M", time.localtime(call.get("ended", 0)))
        lines = [f"### Call ending {when}" + (f" (on {call['device']})" if call.get("device") else "")]
        if call.get("turns"):
            for t in call["turns"]:
                who = user if t.get("role") == "user" else assistant
                lines.append(f"{who}: {t.get('text', '')}")
        else:
            lines.append("(No transcript kept for this call; only its tasks.)")
        for task in call.get("tasks") or []:
            lines.append(f"- Task \"{task.get('title') or task.get('request', '')[:80]}\": asked \"{task.get('request', '')[:300]}\""
                         f" -> {task.get('status', '?')}" + (f": {task['result'][:300]}" if task.get("result") else ""))
        blocks.append("\n".join(lines))
    text = scrub("\n\n".join(blocks))
    return text[-MAX_DIGEST_CHARS:]


# -- proposals ------------------------------------------------------------------------------

def _norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"^[\s>*-]+", "", line or "")).strip().lower()


def find_line(brief_text: str, old: str) -> int | None:
    """Index of the brief line an edit refers to: exact after trimming bullets and spacing."""
    target = _norm_line(old)
    if not target:
        return None
    for i, line in enumerate(brief_text.splitlines()):
        if _norm_line(line) == target:
            return i
    return None


def section_of(heading: str) -> str | None:
    h = " ".join(re.sub(r"[^a-z ]+", " ", heading.lower()).split())
    for section, words in B._SECTION_KEYWORDS.items():
        if any(w in h for w in words):
            return section
    return None


def parse_proposal(output: str, brief_text: str) -> dict[str, Any]:
    """Strict JSON from Hermes, then every edit checked against the brief; unusable ones dropped."""
    match = re.search(r"\{.*\}", B.strip_status_lines(output or ""), re.S)
    if not match:
        raise B.BriefInvalid("the tuning answer wasn't in the expected format")
    try:
        data = json.loads(match.group(0))
    except ValueError:
        raise B.BriefInvalid("the tuning answer wasn't valid JSON") from None
    edits = []
    for raw in data.get("edits") or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        new = " ".join(str(raw.get("new") or "").split())
        old = str(raw.get("old") or "")
        why = " ".join(str(raw.get("why") or "").split())[:400]
        evidence = " ".join(str(raw.get("evidence") or "").split())[:300]
        section = raw.get("section")
        if kind not in {"add", "change", "remove"} or not why:
            continue
        if kind in {"add", "change"} and (not new or len(new) > 400 or scrub(new) != new):
            continue  # empty, too long, or looks like it carries a secret
        if kind in {"change", "remove"} and find_line(brief_text, old) is None:
            continue  # refers to a line that isn't in the brief
        if kind == "add" and section not in SECTIONS:
            continue
        edits.append({"id": f"e{len(edits) + 1}", "kind": kind, "section": section if kind == "add" else None,
                      "old": old.strip() if kind != "add" else None, "new": new if kind != "remove" else None,
                      "why": why, "evidence": evidence})
        if len(edits) >= MAX_EDITS:
            break
    issues = []
    for raw in data.get("product_issues") or []:
        if isinstance(raw, dict) and raw.get("what"):
            issues.append({"what": " ".join(str(raw["what"]).split())[:300],
                           "evidence": " ".join(str(raw.get("evidence") or "").split())[:300]})
    return {"edits": edits, "product_issues": issues[:6],
            "summary": " ".join(str(data.get("summary") or "").split())[:400]}


def apply_edits(brief_text: str, edits: list[dict[str, Any]]) -> str:
    """The brief with the accepted edits; changes and removals by line, additions at the end of a section."""
    lines = brief_text.rstrip("\n").splitlines()
    for e in [e for e in edits if e["kind"] in {"change", "remove"}]:
        i = find_line("\n".join(lines), e["old"])
        if i is None:
            continue
        if e["kind"] == "remove":
            del lines[i]
        else:
            bullet = re.match(r"^(\s*[-*]\s+)", lines[i])
            lines[i] = (bullet.group(1) if bullet else "") + re.sub(r"^[-*]\s+", "", e["new"])
    for e in [e for e in edits if e["kind"] == "add"]:
        start = next((i for i, l in enumerate(lines)
                      if re.match(r"^#{1,4}\s", l) and section_of(l) == e["section"]), None)
        text = "- " + re.sub(r"^[-*]\s+", "", e["new"])
        if start is None:
            lines += ["", f"## {e['section']}", text]
            continue
        end = next((i for i in range(start + 1, len(lines)) if re.match(r"^#{1,4}\s", lines[i])), len(lines))
        last = end - 1
        while last > start and not lines[last].strip():
            last -= 1
        lines.insert(last + 1, text)
    return "\n".join(lines) + "\n"


# -- the manager ----------------------------------------------------------------------------

class TuneManager:
    def __init__(self, state_dir: Path, run_fn: Callable[[str, str], tuple[str, str]] | None,
                 brief: B.BriefManager, calls_fn: Callable[[], list[dict[str, Any]]],
                 names: Callable[[], tuple[str, str]] = lambda: ("Assistant", "User"),
                 clock: Callable[[], float] = time.time):
        self.path = state_dir / PROPOSAL_FILE
        self.run_fn, self.brief, self.calls_fn, self.names, self.clock = run_fn, brief, calls_fn, names, clock
        self._lock = threading.Lock()
        self._working = False

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        B._write_private(self.path, json.dumps(data, ensure_ascii=False, indent=1))

    def get(self) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            working = self._working
        calls = len(self.calls_fn())
        state = "working" if working else data.get("state") or "none"
        return {"state": state, "calls": calls, "error": data.get("error"), "created": data.get("created"),
                "summary": data.get("summary") or "", "edits": data.get("edits") or [],
                "product_issues": data.get("product_issues") or []}

    def request_prompt(self, brief_text: str, calls_text: str) -> str:
        template = (PROMPT_DIR / "tune_request.md").read_text(encoding="utf-8")
        return template.replace("{brief}", brief_text.strip()).replace("{calls}", calls_text)

    def start(self, background: bool = True) -> dict[str, Any]:
        if self.run_fn is None:
            raise B.BriefInvalid("Hermes API is not configured")
        if not self.brief.text():
            raise B.BriefInvalid("there's no voice brief to tune yet")
        if not self.calls_fn():
            raise B.BriefInvalid("no calls in the last week to learn from yet")
        with self._lock:
            if self._working:
                return self.get()
            self._working = True
        if background:
            threading.Thread(target=self._run, daemon=True, name="speakeasy-tune").start()
            return self.get()
        self._run()
        return self.get()

    def _run(self) -> None:
        now = self.clock()
        try:
            brief_text = self.brief.text() or ""
            assistant, user = self.names()
            prompt = self.request_prompt(brief_text, digest(self.calls_fn(), assistant, user))
            idem = "speakeasy_tune_" + hashlib.sha256(f"{now}".encode()).hexdigest()[:24]
            status, output = self.run_fn(prompt, idem)  # type: ignore[misc]
            if status != "completed":
                raise B.BriefInvalid(f"Hermes didn't finish the tune ({status})")
            proposal = parse_proposal(output, brief_text)
            proposal.update(state="ready", created=now, error=None, brief_sha=hashlib.sha256(brief_text.encode()).hexdigest())
            with self._lock:
                self._save(proposal)
            logger.info("speakeasy: tune proposed %d edits, %d product issues",
                        len(proposal["edits"]), len(proposal["product_issues"]))
        except Exception as exc:
            message = str(exc) if isinstance(exc, B.BriefInvalid) else TUNE_FAILED
            logger.warning("speakeasy: %s (%s)", message, type(exc).__name__)
            with self._lock:
                self._save({"state": "failed", "created": now, "error": message[:200]})
        finally:
            with self._lock:
                self._working = False

    def apply(self, accept: list[str]) -> dict[str, Any]:
        """Apply the accepted edits to the brief (kept as an edit by the user), then clear the proposal."""
        with self._lock:
            data = self._load()
        if data.get("state") != "ready":
            raise B.BriefInvalid("there's no tune waiting to apply")
        chosen = [e for e in data.get("edits") or [] if e.get("id") in set(accept)]
        brief_text = self.brief.text() or ""
        if chosen:
            result = self.brief.put(apply_edits(brief_text, chosen))
        else:
            result = self.brief.get()
        self.dismiss()
        return {**result, "applied": len(chosen)}

    def dismiss(self) -> dict[str, Any]:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        return self.get()
