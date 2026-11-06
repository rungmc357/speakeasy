"""What one conversation used: tokens and an estimated cost, from what Hermes already records.

Hermes writes token counts and a cost verdict on every session row (``sessions.input_tokens``,
``output_tokens``, ``cache_read_tokens``, ``cache_write_tokens``, ``reasoning_tokens``,
``api_call_count``, ``estimated_cost_usd``, ``actual_cost_usd``, ``cost_status``,
``billing_provider``). ``cost_status`` is one of:

* ``actual``    - the provider reported a price (rare);
* ``estimated`` - Hermes priced the tokens from its own price list;
* ``included``  - a subscription sign-in (a ChatGPT plan, ...): no per-use bill;
* ``unknown``   - no price on file (a custom or local model, ...).

A conversation is more than its newest row: earlier compression segments, the helpers it started
(``subagent`` child sessions, nested), and for a Discord/Telegram chat continued in Speakeasy, the
linked Speakeasy task sessions. ``session_usage`` adds them up and phrases the result in plain
words. Every figure is an estimate and is labelled as one; nothing here is a bill.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

from . import history as H

MAX_SESSIONS = 400   # sessions one page's usage may add up (helpers of helpers included)
HELPER_DEPTH = 4
CHAIN_STEPS = 20

_NUM = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
        "api_call_count")
_PLANS = {"openai-codex": "ChatGPT", "openai": "ChatGPT", "anthropic": "Claude", "claude": "Claude",
          "venice": "Venice", "xai": "Grok", "xai-oauth": "Grok", "nous": "Nous", "copilot": "Copilot",
          "github-copilot": "Copilot"}
NOTE = ("Estimated from the token counts and price list Hermes keeps. Your provider's bill may differ, "
        "and plan sign-ins don't bill per use.")


def _segments(db: sqlite3.Connection, roots: Iterable[str]) -> list[str]:
    """Every session id that belongs to these conversations: each one's compression chain (root to
    newest segment) plus the helper sessions they started, helpers of helpers included."""
    ids: list[str] = []
    seen: set[str] = set()

    def add(i: str) -> None:
        if i and i not in seen and len(ids) < MAX_SESSIONS:
            seen.add(i)
            ids.append(i)

    roots = [r for r in roots if r]
    tips = H._tips(db, set(roots)) if roots else {}
    for root in roots:
        for i in H._chain(db, tips.get(root, root))[-CHAIN_STEPS:]:
            add(i)
    frontier = list(ids)
    for _ in range(HELPER_DEPTH):
        if not frontier or len(ids) >= MAX_SESSIONS:
            break
        marks = ",".join("?" * len(frontier))
        try:
            rows = db.execute(f"SELECT id FROM sessions WHERE parent_session_id IN ({marks}) AND source='subagent'",
                              tuple(frontier)).fetchall()
        except sqlite3.Error:
            break
        frontier = []
        for (i,) in rows:
            if i not in seen:
                add(i)
                frontier.append(i)
    return ids


def _rows(db: sqlite3.Connection, ids: list[str]) -> tuple[list[dict[str, Any]], int]:
    cols = H._columns(db, "sessions")
    if "input_tokens" not in cols:
        return [], 0
    pick = ["id", "source"] + [c for c in (*_NUM, "estimated_cost_usd", "actual_cost_usd", "cost_status",
                                           "billing_provider", "model") if c in cols]
    out: list[dict[str, Any]] = []
    for start in range(0, len(ids), 200):
        part = ids[start:start + 200]
        marks = ",".join("?" * len(part))
        out += [dict(r) for r in db.execute(f"SELECT {', '.join(pick)} FROM sessions WHERE id IN ({marks})",
                                             tuple(part)).fetchall()]
    return out, sum(1 for r in out if r.get("source") == "subagent")


def tokens_text(n: int) -> str:
    """1234 -> '1.2K', 2_500_000 -> '2.5M' (plain, no 'tokens')."""
    n = int(n or 0)
    if n < 1000:
        return str(n)
    for size, unit in ((1_000_000_000, "B"), (1_000_000, "M"), (1000, "K")):
        if n >= size:
            v = n / size
            s = f"{v:.1f}" if v < 100 else f"{v:.0f}"
            return s.removesuffix(".0") + unit
    return str(n)


def money_text(usd: float) -> str:
    if usd < 0.01:
        return "under 1¢"
    if usd < 1:
        return f"{round(usd * 100):.0f}¢"
    if usd < 100:
        return f"${usd:,.2f}"
    return f"${usd:,.0f}"


def about(usd: float) -> str:
    """'About $2.50' / 'Under 1¢' (capitalized: it starts a line)."""
    return "Under 1¢" if usd < 0.01 else f"About {money_text(usd)}"


def plan_name(providers: Iterable[str]) -> str:
    names = sorted({_PLANS.get((p or "").lower(), "") for p in providers})
    names = [n for n in names if n]
    return f"your {names[0]} plan" if len(names) == 1 else "your plan"


def summarize(rows: list[dict[str, Any]], helpers: int = 0, linked: int = 0,
              name_for: Callable[[str], str] | None = None) -> dict[str, Any] | None:
    """The page's ``usage`` block, or None when nothing was recorded (no tokens)."""
    tot = {k: 0 for k in _NUM}
    cost = 0.0
    priced = included = unknown = 0      # tokens in each billing kind
    plan_providers: list[str] = []
    models: dict[str, dict[str, Any]] = {}
    for r in rows:
        nums = {k: int(r.get(k) or 0) for k in _NUM}
        for k in _NUM:
            tot[k] += nums[k]
        tokens = nums["input_tokens"] + nums["cache_read_tokens"] + nums["cache_write_tokens"] + nums["output_tokens"]
        if tokens <= 0:
            continue
        status = (r.get("cost_status") or "unknown").lower()
        actual, est = r.get("actual_cost_usd"), r.get("estimated_cost_usd")
        amount = float(actual) if actual not in (None, "") and float(actual) > 0 else float(est or 0)
        if status in ("actual", "estimated") and amount > 0:
            priced += tokens
            cost += amount
            kind = "priced"
        elif status == "included":
            included += tokens
            plan_providers.append(r.get("billing_provider") or "")
            kind = "included"
        else:
            unknown += tokens
            kind = "unknown"
        mid = (r.get("model") or "").strip() or "Unknown model"
        m = models.setdefault(mid, {"tokens": 0, "cost_usd": 0.0, "kinds": set()})
        m["tokens"] += tokens
        m["cost_usd"] += amount if kind == "priced" else 0.0
        m["kinds"].add(kind)
    tin = tot["input_tokens"] + tot["cache_read_tokens"] + tot["cache_write_tokens"]
    total = tin + tot["output_tokens"]
    if total <= 0:
        return None
    plan = plan_name(plan_providers)
    if priced and not (included or unknown):
        basis, summary = "estimated", f"{about(cost)} · estimate"
    elif included and not (priced or unknown):
        basis, summary = "included", f"Included in {plan} · {tokens_text(total)} tokens"
    elif unknown and not (priced or included):
        basis, summary = "unknown", f"{tokens_text(total)} tokens · no price known"
    elif priced:
        basis = "mixed"
        summary = f"{about(cost)} · estimate, plus " + ("plan use" if included else "unpriced use")
    else:
        basis, summary = "mixed", f"Included in {plan}, plus unpriced use"
    lines = [{"label": "Cost", "value": (f"{about(cost)} (estimate)" if priced else
                                         "Nothing billed per use" if included and not unknown else "Not known")},
             {"label": "Tokens", "value": f"{tokens_text(total)} total"},
             {"label": "Sent to the model", "value": tokens_text(tin) + (
                 f" ({tokens_text(tot['cache_read_tokens'])} reused from cache)" if tot["cache_read_tokens"] else "")},
             {"label": "Written by the model", "value": tokens_text(tot["output_tokens"]) + (
                 f" (incl. {tokens_text(tot['reasoning_tokens'])} thinking)" if tot["reasoning_tokens"] else "")}]
    if tot["api_call_count"]:
        lines.append({"label": "Model calls", "value": f"{tot['api_call_count']:,}"})
    if included and basis == "mixed":
        lines.append({"label": "Covered by " + plan.removeprefix("your "), "value": f"{tokens_text(included)} tokens"})
    if unknown and basis == "mixed":
        lines.append({"label": "No price on file", "value": f"{tokens_text(unknown)} tokens"})
    if helpers:
        lines.append({"label": "Includes", "value": f"{helpers} helper{'s' if helpers != 1 else ''}"
                      + (f" and {linked} Speakeasy follow-up{'s' if linked != 1 else ''}" if linked else "")})
    elif linked:
        lines.append({"label": "Includes", "value": f"{linked} Speakeasy follow-up{'s' if linked != 1 else ''}"})
    by_model = []
    for mid, m in sorted(models.items(), key=lambda kv: -kv[1]["tokens"]):
        name = (name_for(mid) if name_for and mid != "Unknown model" else mid) or mid
        kinds = m["kinds"]
        value = (about(m["cost_usd"]).lower() if "priced" in kinds and m["cost_usd"] > 0 else
                 "plan" if kinds == {"included"} else "no price")
        by_model.append({"name": name, "tokens": m["tokens"], "value": f"{tokens_text(m['tokens'])} tokens · {value}"})
    return {"estimate": True, "basis": basis, "summary": summary,
            "cost_usd": round(cost, 4) if priced else None,
            "tokens": total, "input_tokens": tin, "output_tokens": tot["output_tokens"],
            "cache_read_tokens": tot["cache_read_tokens"], "reasoning_tokens": tot["reasoning_tokens"],
            "model_calls": tot["api_call_count"], "included_tokens": included, "unpriced_tokens": unknown,
            "helpers": helpers, "linked_tasks": linked,
            "lines": lines, "models": by_model[:6], "note": NOTE}


def session_usage(state_db: Path, session_id: str, linked: Iterable[str] = (),
                  name_for: Callable[[str], str] | None = None) -> dict[str, Any] | None:
    """``usage`` for one session page: the conversation (``session_id``'s chain and helpers) plus the
    linked Speakeasy task sessions (``linked``: a continued chat's follow-ups). None when Hermes
    recorded no tokens (or state.db can't be read: usage is never worth failing a page for)."""
    db = H._open(state_db)
    if db is None:
        return None
    try:
        others = [i for i in dict.fromkeys(linked) if i and i != session_id]
        ids = _segments(db, [session_id, *others])
        rows, helpers = _rows(db, ids)
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return summarize(rows, helpers=helpers, linked=len(others), name_for=name_for)
