"""The one close path for a task's question (docs/state-machines.md, item 3).

A question is ``open`` until exactly one of: the user answers it (a tapped option, a reply), sets it
aside, dismisses it (✕, undoable), the conversation moves on without it, a newer task replaces the
one that asked (``superseded``), or it is a day old (``expired``). Every one of those goes through
``Store.close_question`` (``set_aside``/``mark_answered`` included), which records the state and then
calls the hook ``wire`` installs here, for every task whose question closed (twins included):

- publishes ``decision_resolved`` on the history feed (other devices, session pages, the panel);
- withdraws the Needs you notification (``PushNotifier.withdraw``), so a closed phone's extension
  and its Live Activity stop asking.

The caller republishes the call boards it holds (they read the same store).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def wire(store: Any, rt: Any) -> None:
    """Every close the store records (any path, any thread) tells every surface."""
    store.on_question_closed = lambda keys: [resolved(rt, store, k) for k in keys]


def resolved(rt: Any, store: Any, key: str) -> None:
    """Tell every surface *key*'s question is no longer open. Never raises into the caller."""
    try:
        row = store.run_row(key) or {}
        run_id = row.get("run_id")
        events = getattr(rt, "history_events", None)
        if events is not None:
            sessions = {row.get("session_id"), store.linked_source(key)} - {None}
            for sid in sessions or {None}:
                events.publish("decision_resolved", {"run_id": run_id, "session_id": sid})
        notifier = getattr(rt, "push", None)
        if notifier is not None and hasattr(notifier, "withdraw"):
            for name in {run_id, key} - {None}:
                notifier.withdraw(f"question:{name}")
    except Exception:
        logger.warning("speakeasy: telling the apps a question closed failed", exc_info=True)
