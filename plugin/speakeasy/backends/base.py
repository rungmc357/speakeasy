"""The contract every task backend implements: Hermes, Codex, Claude Code.

Speakeasy's call logic (``calls.py``) drives a backend through exactly the surface ``HermesAPI``
already exposes, so Hermes stays a zero-change adapter and a coding-agent adapter only has to
translate its own protocol into these calls and these events.

Methods are synchronous and thread-safe; the call engine runs them via ``asyncio.to_thread``.

Event vocabulary (dicts passed to the ``events`` callback). ``event`` is required; every other
field is optional unless noted. Adapters MUST NOT invent events outside this list; the call
engine ignores unknown ones.

- ``run.started``            {run_id}
- ``tool.started``           {tool, preview}          one tool call began. ``tool`` is a short
                                                      stable name (``read_file``, ``terminal``,
                                                      ``edit_file``, ``web_search``...); ``preview``
                                                      a redacted one-line argument summary.
- ``message.interim``        {text}                   user-facing commentary. Lines of the form
                                                      ``STATUS: …`` / ``DETAIL: …`` become the
                                                      task's progress (see ``text.interim_progress``).
- ``approval.request``       {request_id, description, kind}
                                                      the agent is blocked on the user. ``kind`` is
                                                      one of ``command`` | ``file_change`` |
                                                      ``permission`` | ``other``; ``description`` is
                                                      one plain sentence ("Run `npm test` in
                                                      web/"). Answered via ``approve``.
- ``files.changed``          {files: [{path, added, removed}], diff}
                                                      the run's cumulative working-tree change so
                                                      far (coding backends only). ``diff`` is a
                                                      unified diff capped at 64 KB.
- ``media.seen``             {path | url, name, source}
                                                      an image the agent produced or is looking at
                                                      right now (a browser screenshot, an image it
                                                      opened, a rendered design, a generated image).
                                                      ``source`` is ``screenshot`` | ``viewed`` |
                                                      ``generated``. Local ``path`` must be absolute;
                                                      the server only serves it from allowed roots.
                                                      Drives the live "what it's looking at" view.
- ``run.completed`` | ``run.failed`` | ``run.cancelled`` | ``run.stopped``
                             {output, error}          terminal. ``output`` is the final answer text.

Session ids: ``start_run(session_id=…)`` continues an existing conversation. For Hermes that is a
Hermes session id; for Codex a thread id; for Claude Code a session UUID. Speakeasy stores them
opaquely per task, so follow-ups land in the same agent conversation.

Images: ``start_run(images=[…])`` sends pictures with the prompt (a screen capture, a dropped
photo), each a base64 ``data:image/…`` URL. Only a backend whose capabilities say ``images`` gets
them; one without it ignores the argument.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

EventCallback = Callable[[dict[str, Any]], None]

APPROVAL_KINDS = frozenset({"command", "file_change", "permission", "other"})
APPROVAL_CHOICES = frozenset({"once", "deny"})


class BackendError(Exception):
    """Raised for any backend failure. ``status`` follows HTTP semantics (400 caller error,
    404 unknown run, 409 not accepting, 502 backend unavailable) so the server can map it."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Capabilities:
    """What a backend can do, so the call engine and the Mac app can hide what it can't.

    Hermes-only features (chat delivery, threads, continuity across chat conversations, email
    drafts, daily briefs) key off these flags instead of checking ``kind == "hermes"``."""

    kind: str                                   # "hermes" | "codex" | "claude_code"
    display_name: str                           # spoken/shown name of the worker ("Hermes", "Codex", "Claude Code")
    steer: bool = True                          # mid-run guidance
    approvals: bool = True                      # approval.request events
    file_changes: bool = False                  # files.changed events
    chat_delivery: bool = False                 # results can be posted to a chat
    threads: bool = False                       # tasks can open chat threads
    conversation_continuity: bool = False       # "continue my X conversation" via session history
    email_drafts: bool = False
    daily_brief: bool = False
    needs_workspace: bool = False               # tasks run in a project folder the user picks
    images: bool = False                        # start_run takes images and the agent can read them
    # Optional features (see the mixins below). An agent without one simply doesn't offer it: the
    # server drops the matching capability flag so the apps hide it.
    history: bool = False                       # past conversations readable by id (SessionCatalog)
    subagents: bool = False                     # helpers a run starts are followable (SubagentSource)
    questions: bool = False                     # the agent can ask the user mid-run and take the answer
    version: str = ""                           # the agent's own version, when it reports one
    extras: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class TaskBackend(Protocol):
    """The surface ``calls.py`` / ``service.py`` use. ``HermesAPI`` already satisfies it."""

    last_error: str

    def capabilities(self) -> Capabilities: ...

    def health(self, timeout: float = 2) -> bool: ...

    def start_run(self, prompt: str, idem_key: str, session_id: str | None = None,
                  session_key: str | None = None, images: Sequence[str] | None = None) -> str:
        """Start a task and return its run id immediately (the work continues in the background).
        Same ``idem_key`` → same run id, never a second run. ``session_key`` is Hermes-only
        routing and other backends ignore it. ``images`` are base64 ``data:image/…`` URLs sent
        with the prompt (see Images above); without them the prompt goes as plain text."""
        ...

    def events(self, run_id: str, callback: EventCallback) -> bool:
        """Block, delivering this run's events in order (replaying any already emitted), until a
        terminal event or the stream ends. Returns True iff a terminal event was delivered."""
        ...

    def get_run(self, run_id: str) -> dict[str, Any]:
        """``{run_id, status, output?, error?, session_id?}``. ``status`` is ``running``,
        ``waiting_for_approval`` or a terminal status."""
        ...

    def stop(self, run_id: str) -> dict[str, Any]: ...

    def steer(self, run_id: str, text: str) -> bool:
        """Queue guidance into a running task; False when it no longer accepts it."""
        ...

    def approve(self, run_id: str, request_id: str, choice: str) -> dict[str, Any]:
        """Answer an ``approval.request``; ``choice`` is ``once`` or ``deny``."""
        ...

    def session_messages(self, session_id: str, limit: int = 60, timeout: float = 1.5) -> list[dict[str, Any]]:
        """Recent ``{role, content}`` messages of a conversation, newest last; [] if unknown."""
        ...

    def run_to_completion(self, prompt: str, idem_key: str, session_id: str | None = None) -> tuple[str, str]:
        """Start and wait: ``(status, output)``."""
        ...


# -- optional features ---------------------------------------------------------------------------
# A backend offers each by setting its Capabilities flag and returning an object from the matching
# accessor; Speakeasy never reads an agent's own storage except through these.

@runtime_checkable
class SessionCatalog(Protocol):
    """Past conversations (``history``): list, read one, and tell when anything changed."""

    def list_sessions(self, **kwargs: Any) -> dict[str, Any]: ...

    def session_meta(self, session_id: str) -> dict[str, Any] | None: ...

    def session_messages(self, session_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def fingerprint(self, session_ids: list[str] | None = None) -> Any: ...


@runtime_checkable
class SubagentSource(Protocol):
    """Helpers a run started (``subagents``): follow each one's steps until it settles."""

    def ensure_watch(self, store: Any, idem_key: str, session_of: Callable[[], str | None],
                     **kwargs: Any) -> Any: ...


@runtime_checkable
class Questions(Protocol):
    """The agent asked the user something mid-run (``questions``); the answer goes back here."""

    def answer(self, run_id: str, text: str) -> bool: ...
