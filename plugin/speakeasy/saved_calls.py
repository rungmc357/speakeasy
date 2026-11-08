"""Read-only saved call conversation, separate from Hermes chat session IDs."""
from __future__ import annotations

import re
from typing import Any


def conversation(log: dict[str, Any], answers: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep call order; replace the voice reading an answer with its full written version once.

    Older call logs contain only role/text, so match the stored spoken result, not a guessed
    timestamp. Unmatched written answers follow the transcript, with their task's name.
    """
    turns = [{"role": t["role"], "text": t["text"],
              "kind": "typed" if t.get("kind") in {"text", "typed"} and t["role"] == "user" else "speech"}
             for t in log.get("turns", []) if isinstance(t, dict)
             and t.get("role") in {"user", "assistant"} and isinstance(t.get("text"), str) and t["text"].strip()]
    used: set[str] = set()
    for answer in answers:
        key = answer["task_id"]
        full = answer.get("full") or ""
        if key in used or not full.strip():
            continue
        used.add(key)
        written = {"role": "assistant", "kind": "text", "text": full,
                   "task_id": key, "task_name": answer.get("title") or ""}
        spoken = re.findall(r"\w+", (answer.get("spoken") or "").lower())
        matches = []
        for i, turn in enumerate(turns):
            if turn["role"] != "assistant" or turn["kind"] != "speech" or not spoken:
                continue
            said = re.findall(r"\w+", turn["text"].lower())
            # Both directions: short stock acknowledgements must not replace a long answer.
            if said and min(len(set(said) & set(spoken)) / len(set(said)),
                            len(set(said) & set(spoken)) / len(set(spoken))) >= 0.6:
                matches.append(i)
        if matches:
            turns[matches[0]] = written
            # Only fold repeats in this reply stretch; an identical answer after a new user turn
            # may belong to another ask and must not disappear.
            end = next((i for i in range(matches[0] + 1, len(turns)) if turns[i]["role"] == "user"), len(turns))
            for i in reversed([i for i in matches[1:] if i < end]):
                del turns[i]
        else:
            turns.append(written)
    return {"interaction_id": log["id"], "ended_at": log.get("ended"), "turns": turns}
