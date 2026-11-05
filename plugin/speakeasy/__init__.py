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


def register(ctx) -> None:
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
    password for the run is dropped (secure_input.after_tool_call)."""
    register = getattr(ctx, "register_hook", None)
    if not callable(register):
        return
    try:
        from .secure_input import after_tool_call
        register("post_tool_call", after_tool_call)
    except Exception as exc:
        logger.warning("speakeasy: could not register the secure input hook: %s", type(exc).__name__)
