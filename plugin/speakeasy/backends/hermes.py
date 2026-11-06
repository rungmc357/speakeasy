"""Hermes' implementations of the optional agent features (``base.SessionCatalog``,
``base.SubagentSource``) and the factory that plugs Hermes into Speakeasy's agent slot.

Each class only forwards to the module that did the work before the agent slot existed
(``history.py`` for past conversations, ``subagents.py`` for helpers), bound to this profile's
``state.db``, so Hermes behaves exactly as it did.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class HermesSessionCatalog:
    """Past Hermes conversations, read-only from the profile's ``state.db`` (history.py)."""

    def __init__(self, state_db: Path):
        self.state_db = Path(state_db)

    def list_sessions(self, **kwargs: Any) -> dict[str, Any]:
        from .. import history
        return history.list_sessions(self.state_db, **kwargs)

    def session_meta(self, session_id: str) -> dict[str, Any] | None:
        from .. import history
        return history.session_meta(self.state_db, session_id)

    def session_messages(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        from .. import history
        return history.session_messages(self.state_db, session_id, **kwargs)

    def fingerprint(self, session_ids: list[str] | None = None) -> Any:
        from .. import history
        if session_ids is None:
            return history.fingerprint(self.state_db)
        return history.fingerprint(self.state_db, session_ids)

    def message_time(self, row_id: int | None) -> float | None:
        from .. import history
        return history.message_time(self.state_db, row_id)

    def linked_context(self, session_id: str, title: str, label: str, text: str) -> str:
        from .. import history
        return history.linked_context(self.state_db, session_id, title, label, text)


class HermesSubagentSource:
    """Helpers a Hermes run started with delegate_task, followed through their own child sessions."""

    def __init__(self, state_db: Path):
        self.state_db = Path(state_db)

    def ensure_watch(self, store: Any, idem_key: str, session_of: Callable[[], str | None],
                     **kwargs: Any) -> Any:
        from .. import subagents
        return subagents.ensure_watch(store, idem_key, self.state_db, session_of, **kwargs)


def hermes_agent(host: Any, key_fn: Callable[[], str] | None = None) -> Any:
    """Hermes in the agent slot: its API server on this machine, reached with the profile's key."""
    from ..hermes_api import HermesAPI
    return HermesAPI(host.agent_api_base(), key_fn or host.agent_api_key, host.profile(),
                     state_db=Path(host.home) / "state.db")
