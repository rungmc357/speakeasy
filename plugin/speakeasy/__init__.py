"""Speakeasy: talk to your Hermes agent by voice from a Mac.

Registers the ``voice`` gateway platform and the ``hermes voice`` CLI command.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def _manifest_version() -> str:
    """The version in plugin.yaml: the one number releases bump and the Mac app compares against."""
    try:
        text = (Path(__file__).with_name("plugin.yaml")).read_text(encoding="utf-8")
    except OSError:
        return "0.0.0"
    match = re.search(r"^version:\s*['\"]?([0-9][0-9A-Za-z.+-]*)", text, re.MULTILINE)
    return match.group(1) if match else "0.0.0"


__version__ = _manifest_version()
__all__ = ["register", "__version__"]

_PLATFORM_HINT = (
    "You are being spoken to through Speakeasy, a voice app on the user's Mac. Your reply is read "
    "aloud: answer in plain spoken sentences, no markdown, tables or long lists."
)


def check_requirements() -> bool:
    return True  # stdlib only


def validate_config(config) -> bool:
    return True


def is_connected(config) -> bool:
    """Configured once `hermes voice setup` has written the voice block (it always sets a port).

    Hermes asks this on an ``enabled=True`` probe view too, so it must key on what setup wrote,
    not on ``enabled``; an explicit ``enabled: false`` in config.yaml still turns voice off.
    """
    extra = getattr(config, "extra", {}) or {}
    return bool(extra.get("port") or extra.get("enabled"))


MIN_HERMES = "0.22.0"
UPDATE_HERMES = ("Speakeasy needs Hermes {min} or newer (this is Hermes {current}). Update Hermes first: run "
                 "`hermes update`, then restart it (send /restart in your chat with Hermes), then run "
                 "`hermes voice setup` again.")


def hermes_too_old() -> str | None:
    """The running Hermes version when it predates every API Speakeasy uses, else None.

    Hermes 0.19+ skips the plugin before import when ``requires_hermes`` rejects it (``hermes
    plugins list`` shows why), so reaching this code with that gate present means the version is
    fine. Hermes before the gate imports the plugin anyway; then this answers instead of a crash."""
    try:
        from hermes_cli.plugins_manifest import running_hermes_version  # noqa: F401  (0.19+: gated)
        return None
    except ImportError:
        pass
    try:
        from hermes_cli import __version__ as current  # type: ignore[attr-defined]
    except Exception:
        current = "an older version"
    return str(current or "an older version")


def _register_update_notice(ctx, current: str) -> None:
    """On a too-old Hermes, `hermes voice …` says to update instead of being an unknown command."""
    message = UPDATE_HERMES.format(min=MIN_HERMES, current=current)
    logger.warning("speakeasy: %s", message)

    def _handle(args=None, *rest, **kw) -> int:
        print(message)
        return 1

    try:
        ctx.register_cli_command("voice", help="Speakeasy voice (needs a newer Hermes)",
                                 setup_fn=lambda parser: None, handler_fn=_handle,
                                 description=message)
    except Exception as exc:
        logger.warning("speakeasy: could not register the update notice: %s", exc)


def register(ctx) -> None:
    too_old = hermes_too_old()
    if too_old:
        _register_update_notice(ctx, too_old)
        return
    from .adapter import VoiceAdapter
    from .cli import handle, setup_parser

    ctx.register_platform(
        name="voice", label="Speakeasy", adapter_factory=lambda cfg: VoiceAdapter(cfg),
        check_fn=check_requirements, validate_config=validate_config, is_connected=is_connected,
        required_env=[], install_hint="No extra packages needed (stdlib only)",
        emoji="\U0001f399", allow_update_command=False, platform_hint=_PLATFORM_HINT,
    )
    _register_routing_task(ctx)
    _register_secure_input_hook(ctx)
    _register_context_hook(ctx)
    ctx.register_cli_command(
        "voice", help="Speakeasy voice: setup, pairing and devices",
        setup_fn=setup_parser, handler_fn=handle,
        description="Set up Speakeasy, pair a Mac, list or revoke paired devices.",
    )


def _register_routing_task(ctx) -> None:
    """One auxiliary task for all request routing, so the user picks its model in their own Hermes
    (``hermes model`` → auxiliary tasks, or ``auxiliary.speakeasy_router`` in config.yaml)."""
    from .router import AUX_DESCRIPTION, AUX_DISPLAY_NAME, AUX_TASK
    register = getattr(ctx, "register_auxiliary_task", None)
    if not callable(register):
        return  # older Hermes: routing uses the auxiliary default
    try:
        register(AUX_TASK, display_name=AUX_DISPLAY_NAME, description=AUX_DESCRIPTION, defaults={"timeout": 3})
    except Exception as exc:
        logger.warning("speakeasy: could not register the routing task: %s", exc)


def _register_secure_input_hook(ctx) -> None:
    """Sudo from a Speakeasy secure card is allowed once: after that tool call, Hermes' cached
    password for the run is dropped (secure_input.after_tool_call). A skill's key note rides on
    its skill_view result (secure_input.transform_tool_result)."""
    register = getattr(ctx, "register_hook", None)
    if not callable(register):
        return
    try:
        from .secure_input import after_tool_call
        register("post_tool_call", after_tool_call)
    except Exception as exc:
        logger.warning("speakeasy: could not register the secure input hook: %s", type(exc).__name__)
    try:
        # A skill's key is only asked for once the task needs it; this hook tells the agent so.
        from . import secure_input
        register("transform_tool_result", secure_input.transform_tool_result)
        secure_input._ACTIVE["notes"] = True
    except Exception as exc:
        logger.warning("speakeasy: could not register the skill key note hook: %s", type(exc).__name__)


def _context_hook(**kwargs) -> None:
    """Resolved at call time: a live reload swaps ``context_use`` for new code, and Hermes keeps
    calling the function registered at startup."""
    import sys
    module = sys.modules.get(f"{__name__}.context_use")
    if module is None:
        try:
            from . import context_use as module
        except Exception:
            return
    module.after_api_request(**kwargs)


def _register_context_hook(ctx) -> None:
    """After every model call Hermes says which model answered and how many tokens it read: kept per
    session so task cards and session pages can show the model and how full its context got."""
    register = getattr(ctx, "register_hook", None)
    if not callable(register):
        return
    try:
        register("post_api_request", _context_hook)
    except Exception as exc:
        logger.warning("speakeasy: could not register the context hook: %s", type(exc).__name__)
