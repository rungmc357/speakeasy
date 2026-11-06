"""Setup check: every requirement a first call needs, as rows the apps render as-is.

``GET /voice/setup-check`` answers ``{ready, rows, version}``. Each row is
``{id, ok, blocking, title, detail, fix}`` where ``fix`` is ``None`` or
``{kind, label, payload}``. The apps pick the button from ``kind`` and never match strings, so a
new check (or a better fix) ships with a plugin update and no app build. Kinds:

- ``codex_sign_in``: open the app's Sign in to ChatGPT sheet (Codex's device-code login, which
  also installs or updates Codex on the Hermes computer when needed).
- ``action``: ``POST /voice/setup-check/{id}`` and the plugin fixes it itself (``payload`` unused).
- ``copy``: copy ``payload`` (a ``/restart`` command or a short request for the user's own Hermes
  chat); ``detail`` says where to paste it.
- ``open_settings``: the app's own settings page named by ``payload`` (``voice``).

A client that sees a kind it doesn't know shows the row's detail without a button. Rows never ask
anyone to open a terminal, and no secret ever crosses this route.
"""
from __future__ import annotations

from typing import Any, Callable

RESTART_COMMAND = "/restart"

FINISH_SETUP_REQUEST = ("Please finish setting up Speakeasy on this Hermes: run hermes voice setup --yes, "
                        "then tell me if Hermes needs a restart.")
OPENAI_KEY_REQUEST = ("Please set up Speakeasy's voice with my OpenAI API key: run hermes voice setup --api-key "
                      "and ask me for the key in a private prompt (never in this chat).")
GROK_SIGN_IN_REQUEST = ("Please sign Speakeasy's Grok voice in to my xAI account: run hermes auth add xai-oauth "
                        "and send me the sign-in link and code here.")


def _fix(kind: str, label: str, payload: str = "") -> dict[str, str]:
    return {"kind": kind, "label": label, "payload": payload}


def _row(row_id: str, ok: bool, title: str, detail: str = "", fix: dict[str, str] | None = None,
         blocking: bool = True) -> dict[str, Any]:
    return {"id": row_id, "ok": ok, "blocking": blocking, "title": title, "detail": detail,
            "fix": None if ok else fix}


def _voice_row(status: dict[str, Any]) -> dict[str, Any]:
    provider = status.get("provider") or "codex"
    if provider == "grok":
        if not status.get("grok_beta"):
            return _row("voice", False, "Grok voice is turned off",
                        "Grok voice is a beta on this Hermes. Turn it on to use your xAI sign-in for calls.",
                        _fix("action", "Turn on Grok voice"))
        if status.get("grok_ready"):
            return _row("voice", True, "Voice: Grok is ready")
        return _row("voice", False, "Voice: Grok isn't signed in",
                    "Grok voice needs your xAI sign-in (SuperGrok or X Premium+) on the Hermes computer. "
                    "Paste this request into your chat with Hermes and it will send you a sign-in link.",
                    _fix("copy", "Copy a request for Hermes", GROK_SIGN_IN_REQUEST))
    if provider == "openai":
        if status.get("api_key_set"):
            return _row("voice", True, "Voice: OpenAI API key is set")
        return _row("voice", False, "Voice: no OpenAI API key yet",
                    "Calls on this Hermes use an OpenAI API key, and none is saved. Paste this request into your "
                    "chat with Hermes; it will ask for the key privately. Or pick ChatGPT voice in Settings.",
                    _fix("copy", "Copy a request for Hermes", OPENAI_KEY_REQUEST))
    if status.get("codex_signed_in"):
        return _row("voice", True, "Voice: ChatGPT is signed in")
    if not status.get("codex_found"):
        return _row("voice", False, "Voice: ChatGPT isn't set up yet",
                    "Calls use your ChatGPT sign-in. Speakeasy can install what it needs on the Hermes computer "
                    "and sign you in from here.", _fix("codex_sign_in", "Set up ChatGPT voice"))
    return _row("voice", False, "Voice: ChatGPT isn't signed in",
                "Calls use your ChatGPT sign-in on the Hermes computer. Sign in from here with a short code.",
                _fix("codex_sign_in", "Sign in with ChatGPT"))


def rows(status: dict[str, Any]) -> list[dict[str, Any]]:
    """The rows for one ``Service.status()`` answer, blocking ones first, in fix order."""
    out = [_row("plugin", True, f"Speakeasy {status.get('version') or ''} is on your Hermes".replace("  ", " "))]
    restart = bool(status.get("restart_needed"))
    out.append(_row("restart", not restart, "Hermes is running the current Speakeasy" if not restart
                    else "Hermes needs a restart",
                    "Speakeasy was updated while Hermes was running. Send /restart in your chat with Hermes "
                    "to finish the update; it takes a few seconds.",
                    _fix("copy", "Copy /restart", RESTART_COMMAND)))
    api_ok = bool(status.get("hermes_api_ok"))
    if api_ok:
        out.append(_row("hermes_api", True, "Speakeasy can reach Hermes"))
    elif status.get("hermes_api_key_set"):
        out.append(_row("hermes_api", False, "Hermes' connection for Speakeasy is off",
                        "Setup turned it on, and it starts the next time Hermes starts. Send /restart in your chat "
                        "with Hermes.", _fix("copy", "Copy /restart", RESTART_COMMAND)))
    else:
        out.append(_row("hermes_api", False, "Speakeasy setup isn't finished",
                        "Hermes hasn't been given Speakeasy's connection yet. Paste this request into your chat "
                        "with Hermes and it will finish setup.",
                        _fix("copy", "Copy a request for Hermes", FINISH_SETUP_REQUEST)))
    out.append(_voice_row(status))
    brief = status.get("brief_state") or ""
    brief_ready = brief in {"ready", "edited"}
    out.append(_row("brief", brief_ready, "Voice brief is ready" if brief_ready else (
        "Hermes is writing your voice brief" if brief == "writing" else "No voice brief yet"),
        "" if brief_ready else "Optional: calls work without it. It helps the voice know your work and names.",
        None, blocking=False))
    return out


def check(status: dict[str, Any]) -> dict[str, Any]:
    found = rows(status)
    return {"ready": all(r["ok"] for r in found if r["blocking"]), "rows": found,
            "version": status.get("version") or ""}


def run_fix(row_id: str, status: dict[str, Any], turn_on_grok: Callable[[], Any]) -> bool:
    """Do a row's ``action`` fix on the Hermes computer. False when that row has no action now."""
    row = next((r for r in rows(status) if r["id"] == row_id), None)
    if row is None or row["fix"] is None or row["fix"]["kind"] != "action":
        return False
    if row_id == "voice" and (status.get("provider") == "grok"):
        turn_on_grok()
        return True
    return False
