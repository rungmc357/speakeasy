"""Voice messages transcribed by Hermes: ``POST /voice/transcribe`` takes the recorded audio and
returns its text, through the same transcription Hermes uses for a voice note sent in Telegram or
Discord (``tools.transcription_tools.transcribe_audio``, whatever ``stt`` provider is configured,
with Hermes' own local fallback). The app records the whole clip first, so nothing at the start is
lost the way a live on-device recognizer can lose it, and the user's own setup decides the quality.

The audio is written to a private temp file only for the length of the call and deleted after;
neither the bytes nor the words are logged.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from .calls import ServiceError

MAX_AUDIO_BYTES = 25 * 1024 * 1024  # Hermes' own cap for a hosted provider
# Content-Type → the suffix Hermes' transcription reads the format from.
AUDIO_TYPES = {
    "audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/webm": ".webm",
    "audio/ogg": ".ogg", "audio/opus": ".opus", "audio/x-caf": ".caf", "audio/flac": ".flac",
    "audio/mpeg": ".mp3",
}
# Transcriptions at once (each may hold a model on the Studio for a while).
_SLOTS = threading.BoundedSemaphore(2)

Transcriber = Callable[[str], dict[str, Any]]


def check(content_type: str | None, length: int) -> str:
    """The file suffix for an upload, or a ServiceError before any of the body is read."""
    mime = (content_type or "").split(";")[0].strip().lower()
    suffix = AUDIO_TYPES.get(mime)
    if not suffix:
        raise ServiceError(415, "voice messages must be audio (m4a, wav, ogg, webm, caf, flac, mp3)", reason="unsupported")
    if length <= 0:
        raise ServiceError(400, "empty upload")
    if length > MAX_AUDIO_BYTES:
        raise ServiceError(413, "voice message too long to send", reason="too_large")
    return suffix


# The user's own names (vocabulary.py) for the clip being transcribed right now, on this thread:
# Hermes' ``pre_transcription`` hook (``pre_transcription`` below) adds them to the provider's prompt
# only while Speakeasy's own transcription runs, never for the gateway's other voice notes.
_HINTS = threading.local()


def hint_prompt(terms: list[str]) -> str:
    return ("Names that may come up: " + ", ".join(t for t in terms if t) + ".")[:600] if terms else ""


def pre_transcription(**kwargs: Any) -> dict[str, str] | None:
    """Hermes hook: while Speakeasy transcribes a voice message, its names list joins the prompt
    (after any ``stt.prompt`` the user set). Any other transcription is left alone."""
    hint = getattr(_HINTS, "prompt", "")
    if not hint:
        return None
    base = kwargs.get("prompt")
    return {"prompt": f"{base.strip()} {hint}" if isinstance(base, str) and base.strip() else hint}


def hermes_transcriber(hints: Callable[[], list[str]] | None = None) -> Transcriber | None:
    """Hermes' transcription, as a gateway voice note uses it: configured provider, then the local
    fallback. None when this Hermes has no transcription module. ``hints``: the user's names, given
    to the provider through the ``pre_transcription`` hook."""
    try:
        from tools.transcription_tools import transcribe_audio  # type: ignore
    except Exception:  # noqa: BLE001 - an older Hermes or a broken install: say unavailable
        return None
    try:
        from tools.transcription_tools import transcribe_audio_local_fallback  # type: ignore
    except Exception:  # noqa: BLE001
        transcribe_audio_local_fallback = None

    def run(path: str) -> dict[str, Any]:
        try:
            _HINTS.prompt = hint_prompt(hints()) if hints is not None else ""
        except Exception:  # noqa: BLE001 - a names list never stops a transcription
            _HINTS.prompt = ""
        try:
            result = transcribe_audio(path, None, "gateway")
        finally:
            _HINTS.prompt = ""
        if not result.get("success") and transcribe_audio_local_fallback is not None:
            fallback = transcribe_audio_local_fallback(path)
            if fallback.get("success"):
                return fallback
        return result

    return run


def transcribe(data: bytes, suffix: str, transcriber: Transcriber | None, root: Path | None = None) -> dict[str, Any]:
    """``{"text": ..., "provider": ...}`` for the clip. 503 ``unavailable`` when Hermes can't
    transcribe, 503 ``busy`` when two are already running, 502 ``failed`` when the provider failed."""
    if transcriber is None:
        raise ServiceError(503, "this Hermes can't transcribe audio", reason="unavailable")
    if not _SLOTS.acquire(timeout=30):
        raise ServiceError(503, "busy transcribing, try again", reason="busy")
    folder = None
    try:
        if root is not None:
            root.mkdir(parents=True, exist_ok=True)
        folder = tempfile.mkdtemp(prefix="memo-", dir=str(root) if root is not None else None)
        os.chmod(folder, 0o700)
        path = os.path.join(folder, "voice" + suffix)
        with open(path, "wb") as handle:
            handle.write(data)
        try:
            result = transcriber(path) or {}
        except Exception:  # noqa: BLE001 - a provider crash is a failed transcription, not a 500
            result = {"success": False}
        if not result.get("success"):
            raise ServiceError(502, "couldn't transcribe that", reason="failed")
        return {"text": str(result.get("transcript") or "").strip(), "provider": str(result.get("provider") or "")}
    finally:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        _SLOTS.release()
