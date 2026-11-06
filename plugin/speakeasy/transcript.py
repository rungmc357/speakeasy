"""A call's transcript as whole speaker turns, for the whole call.

Voice providers deliver the transcript as streaming pieces: a user turn in one or a few pieces, the
voice's reply a word or two at a time. The call kept only the newest 512 pieces, so a task started
ten minutes into a call saw the last three or four exchanges, cut mid-reply ("Assistant: Sure,
I'll"). ``Fragments`` still keeps the newest pieces for the live checks that look at what was just
said, and joins every piece into whole turns as it arrives; tasks and the router get those turns,
capped by characters with the oldest turns dropped whole, so the context never starts mid-sentence.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Iterable

from .text import clean_transcript

RECENT_FRAGMENTS = 512        # pieces kept for the live checks (what was just said)
CALL_LOG_CHARS = 128 * 1024   # whole turns kept for the call; the oldest turn goes whole past this
TASK_CONTEXT_CHARS = 16_000   # the call handed to a new task
ROUTER_CONTEXT_CHARS = 6_000  # the call the routing model reads (what "that", "the first one" mean)
LABELS = ("User: ", "Assistant: ")


def piece_ms(fragment: dict[str, Any]) -> int:
    """Where a piece sits on the call's clock (0 for typed turns and taps: always before a handoff)."""
    return int(fragment.get("end_ms") or fragment.get("start_ms") or 0)


class Fragments(deque):
    """The newest transcript pieces (a bounded deque, as before) plus ``turns``: every piece of the
    call joined into speaker turns, ``{"speaker", "at", "pieces": [(ms, text), ...]}``. The pieces
    stay apart inside a turn so a handoff can still take exactly the words said before it."""

    def __init__(self, items: Iterable[dict[str, Any]] = (), maxlen: int | None = RECENT_FRAGMENTS,
                 log_chars: int = CALL_LOG_CHARS):
        super().__init__(maxlen=maxlen)
        self.turns: list[dict[str, Any]] = []
        self.chars = 0
        self.log_chars = log_chars
        for item in items:
            self.append(item)

    def append(self, fragment: dict[str, Any]) -> None:  # type: ignore[override]
        super().append(fragment)
        speaker, text = fragment.get("speaker"), fragment.get("text") or ""
        if self.turns and self.turns[-1]["speaker"] == speaker:
            self.turns[-1]["pieces"].append((piece_ms(fragment), text))
        else:
            self.turns.append({"speaker": speaker, "at": fragment.get("at", 0.0),
                               "pieces": [(piece_ms(fragment), text)]})
        self.chars += len(text)
        while self.chars > self.log_chars and len(self.turns) > 1:
            old = self.turns.pop(0)
            self.chars -= sum(len(t) for _, t in old["pieces"])


def turn_log(fragments: Any) -> list[dict[str, Any]]:
    """The whole-call turns of ``fragments`` (a plain deque, as some callers keep, is joined here)."""
    turns = getattr(fragments, "turns", None)
    if turns is not None:
        return list(turns)
    return Fragments(list(fragments or []), maxlen=None).turns


def spoken_turns(fragments: Any, before_ms: int | None = None) -> list[dict[str, Any]]:
    """Cleaned turns ``{"role", "text", "at"}``, oldest first. ``before_ms``: only the words said up
    to that point on the call's clock (a handoff's boundary). Turns left empty are skipped, and
    turns of one speaker that end up next to each other are joined."""
    out: list[dict[str, Any]] = []
    for turn in turn_log(fragments):
        raw = "".join(t for ms, t in turn["pieces"] if before_ms is None or ms <= before_ms)
        if not raw:
            continue
        if out and out[-1]["role"] == turn["speaker"]:
            out[-1]["raw"] += raw
        else:
            out.append({"role": turn["speaker"], "raw": raw, "at": turn["at"]})
    turns = []
    for t in out:
        text = clean_transcript(t["raw"]).strip()
        if text:
            turns.append({"role": t["role"], "text": text, "at": t["at"]})
    return turns


def label(turn: dict[str, Any]) -> str:
    return f"{str(turn['role']).title()}: {turn['text']}"


def whole_turns(lines: list[str], budget: int) -> tuple[list[str], int]:
    """The newest lines that fit in ``budget`` characters, oldest dropped whole; how many were dropped.
    The newest line always stays (cut from its start at a word only if it alone is over budget)."""
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        cost = len(line) + 1
        if kept and used + cost > budget:
            break
        if not kept and cost > budget:
            name, _, body = line.partition(": ")
            tail = body[-(budget - len(name) - 4):]
            tail = tail.split(" ", 1)[1] if " " in tail else tail
            line = f"{name}: …{tail}"
            cost = len(line) + 1
        kept.append(line)
        used += cost
    return list(reversed(kept)), len(lines) - len(kept)


def dropped_note(count: int) -> str:
    return (f"(Earlier in the call: {count} older turn{'s' if count != 1 else ''} not shown.)"
            if count else "")


def tail_text(text: str, budget: int) -> str:
    """A labeled transcript (``User: ``/``Assistant: `` lines) cut to ``budget`` at turn boundaries:
    never mid-turn, so it never starts mid-sentence. A line without a label joins the turn above."""
    lines: list[str] = []
    for line in (text or "").splitlines():
        if line.startswith(LABELS) or not lines:
            lines.append(line)
        else:
            lines[-1] += "\n" + line
    kept, _ = whole_turns([l for l in lines if l.strip()], budget)
    return "\n".join(kept)
