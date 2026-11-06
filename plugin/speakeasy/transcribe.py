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


def hermes_transcriber() -> Transcriber | None:
    """Hermes' transcription, as a gateway voice note uses it: configured provider, then the local
    fallback. None when this Hermes has no transcription module."""
    try:
        from tools.transcription_tools import transcribe_audio  # type: ignore
    except Exception:  # noqa: BLE001 - an older Hermes or a broken install: say unavailable
        return None
    try:
        from tools.transcription_tools import transcribe_audio_local_fallback  # type: ignore
    except Exception:  # noqa: BLE001
        transcribe_audio_local_fallback = None

    def run(path: str) -> dict[str, Any]:
        result = transcribe_audio(path, None, "gateway")
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


# -- voice notes: the recording itself is the message ---------------------------------------------
# A voice message sent from a session page goes to Hermes the way a Telegram or Discord voice note
# does (gateway/run_inbound_transcription.py): the clip is kept in Hermes' own audio cache, Hermes'
# transcription runs on it, and the agent gets exactly the text the gateway would have given it — the
# transcript as one quoted line, or the gateway's own note when it came through empty or couldn't be
# transcribed (then with the clip's path, so the agent can still listen to it).

EMPTY_NOTE = ("[The user sent a voice message but it came through empty or inaudible — speech-to-text "
              "returned no words. Do not guess at the content; ask the user to resend or type it out.]")
_UNTRANSCRIBED = "[voice message could not be transcribed automatically; the audio is available at: "


def untranscribed_note(path: str) -> str:
    """The gateway's one neutral marker for a clip its transcription couldn't read."""
    agent_path = os.path.abspath(path)
    try:
        from tools.credential_files import to_agent_visible_cache_path  # type: ignore
        agent_path = to_agent_visible_cache_path(agent_path)
    except Exception:  # noqa: BLE001 - an older Hermes: the plain path
        pass
    return f"{_UNTRANSCRIBED}{agent_path}]"


def is_voice_note_marker(text: str) -> bool:
    """True for the gateway's notes that stand for a voice message whose words aren't known."""
    t = (text or "").strip()
    return t == EMPTY_NOTE or (t.startswith(_UNTRANSCRIBED) and t.endswith("]"))


def note_words(text: str) -> str:
    """The words of a voice note as the agent got it (``"words"``) → ``words``; a marker → ``""``."""
    t = (text or "").strip()
    if is_voice_note_marker(t):
        return ""
    if len(t) >= 2 and t[0] == '"' and t[-1] == '"':
        return t[1:-1].strip()
    return t


def keep_clip(data: bytes, suffix: str, fallback_dir: Path) -> str:
    """Save the clip where Hermes keeps a Telegram voice note (its audio cache, which it prunes
    itself); ``fallback_dir`` (private) on a Hermes without that cache. Returns the path."""
    try:
        from gateway.platforms.base import cache_audio_from_bytes  # type: ignore
        return cache_audio_from_bytes(data, suffix)
    except Exception:  # noqa: BLE001 - an older or trimmed Hermes: our own private folder
        pass
    fallback_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(fallback_dir, 0o700)
    fd, path = tempfile.mkstemp(prefix="voice-", suffix=suffix, dir=str(fallback_dir))
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return path


def voice_note(path: str, transcriber: Transcriber | None) -> tuple[str, str | None]:
    """``(text for the agent, transcript or None)`` for a kept clip — the gateway's
    ``_transcribe_one_clip``: configured STT with the local fallback, the transcript as one quoted
    line, the gateway's own note when it came back empty or failed."""
    if transcriber is None:
        return untranscribed_note(path), None
    if not _SLOTS.acquire(timeout=60):
        return untranscribed_note(path), None
    try:
        try:
            result = transcriber(path) or {}
        except Exception:  # noqa: BLE001 - a provider crash is a clip that couldn't be transcribed
            result = {"success": False}
    finally:
        _SLOTS.release()
    if not result.get("success"):
        return untranscribed_note(path), None
    transcript = str(result.get("transcript") or "").strip()
    if not transcript:
        return EMPTY_NOTE, ""
    return f'"{transcript}"', transcript
