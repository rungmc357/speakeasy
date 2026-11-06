"""The host Speakeasy runs inside: where its settings, secrets, side model, chat delivery and
liveness come from.

Two seams keep Speakeasy agent-neutral:

- the **agent** (``backends.TaskBackend``): how tasks run (Hermes' ``/v1/runs`` today);
- the **host** (this module): everything Speakeasy borrows from the program it lives in.

Today the only host is Hermes' gateway (``HermesHost``): the profile home, its secret and config
readers (``settings.hermes_secret`` / ``hermes_config``), its API server address, its auxiliary
model client (routing, titles, tidy-ups), ``hermes send`` for chat delivery, its webhook threads and
its running-agent snapshot for liveness. ``HermesHost`` only forwards to the code that did each of
these before, so Hermes users see exactly the same behaviour; a standalone host (``speakeasy
serve`` with its own home and secret store) can implement the same surface later.

Hermes-only features stay gated on the agent's ``Capabilities`` (chat delivery, threads, email
drafts, daily brief), never on which host is running.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class Host(Protocol):
    """What Speakeasy needs from the program it runs inside."""

    kind: str          # "hermes" | "standalone"
    home: Path         # where Speakeasy keeps its own files (``<home>/speakeasy``)

    def secret(self, name: str) -> str: ...

    def save_secret(self, name: str, value: str) -> None: ...

    def config(self) -> dict[str, Any]: ...

    def agent_api_base(self) -> str: ...

    def agent_api_key(self) -> str: ...

    def profile(self) -> str: ...

    def aux_client(self) -> tuple[Callable[..., Any], Callable[[Any], str | None] | None]: ...

    def notifier(self, profile: str = "") -> Any: ...

    def threads_supported(self) -> bool: ...

    def session_activity(self, session_id: str | None) -> dict[str, Any] | None: ...


class HermesHost:
    """Speakeasy as a Hermes gateway plugin: every call forwards to the Hermes-side code that served
    it before this layer existed (no behaviour change)."""

    kind = "hermes"

    def __init__(self, home: Path):
        self.home = Path(home)

    # -- settings and secrets (the profile's own, through Hermes' readers) ---------------------
    def secret(self, name: str) -> str:
        from .settings import hermes_secret
        return hermes_secret(self.home, name)

    def save_secret(self, name: str, value: str) -> None:
        from .settings import save_hermes_secret
        save_hermes_secret(self.home, name, value)

    def config(self) -> dict[str, Any]:
        from .settings import hermes_config
        return hermes_config(self.home)

    def profile(self) -> str:
        from .settings import Settings
        return Settings(self.home).get()["hermes_profile"]

    # -- the agent's own API ---------------------------------------------------------------------
    def agent_api_base(self) -> str:
        from .settings import hermes_api_base
        return hermes_api_base(self.home)

    def agent_api_key(self) -> str:
        return self.secret("API_SERVER_KEY")

    # -- the side model (routing, titles, tidy-ups) -------------------------------------------------
    def aux_client(self) -> tuple[Callable[..., Any], Callable[[Any], str | None] | None]:
        """Hermes' auxiliary client: ``(call_llm, extract_content_or_reasoning)`` (the second is None
        on a Hermes without it). Raises ImportError outside Hermes, which every caller already treats
        as "no side model"."""
        import importlib
        aux = importlib.import_module("agent.auxiliary_client")
        return aux.call_llm, getattr(aux, "extract_content_or_reasoning", None)

    # -- chat delivery and threads -------------------------------------------------------------
    def notifier(self, profile: str = "") -> Any:
        from .delivery import HermesSendNotifier
        return HermesSendNotifier(self.home, profile)

    def threads_supported(self) -> bool:
        from .delivery import threads_supported
        return threads_supported()

    # -- liveness: is a turn really still running in this session? --------------------------------
    def session_activity(self, session_id: str | None) -> dict[str, Any] | None:
        from . import liveness
        return liveness.session_activity(session_id)


# The host this process runs in. Module-level helpers (router.py's side-model calls) read it; the
# gateway binds it once per profile home (``bind``), and HermesHost is the default.
_ACTIVE: Host | None = None


def bind(host: Host) -> Host:
    global _ACTIVE
    _ACTIVE = host
    return host


def active() -> Host:
    """The bound host, else a HermesHost on the default Hermes home (the plugin runs in Hermes)."""
    if _ACTIVE is not None:
        return _ACTIVE
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        home = Path(get_hermes_home())
    except Exception:
        home = Path.home() / ".hermes"
    return HermesHost(home)


def aux_client() -> tuple[Callable[..., Any], Callable[[Any], str | None] | None]:
    """The active host's side-model client (raises ImportError when it has none)."""
    return active().aux_client()
