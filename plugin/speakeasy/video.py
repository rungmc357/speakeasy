"""Bounded local preparation for MP4/MOV inputs; never claim native video understanding."""
from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

MAX_SECONDS = 120
FRAME_COUNT = 6
VIDEO_TYPES = frozenset({"video/mp4", "video/quicktime"})


def _run(args: list[str], timeout: int = 20) -> bytes:
    # No shell, no network input protocols, bounded work and no media content in logs.
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout).stdout


def sniff(data: bytes) -> bool:
    """ISO BMFF signature only; ffprobe must validate streams before accepting an upload."""
    return len(data) >= 12 and data[4:8] == b"ftyp"


def prepare(path: Path, folder, transcribe=None, on_failure=None) -> dict:
    """Validate a saved original and retain at most six upright JPEG frames and one audio clip.

    Uses the user's installed ffmpeg and Hermes transcription path, not an upload service.
    Derived files use the existing shared-folder lifecycle. Missing tooling is an honest refusal.
    """
    from .attachments import Refused
    created = []
    try:
        probe = json.loads(_run(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                                 "-show_streams", "-show_format", "-of", "json", str(path)]))
        streams = probe.get("streams", [])
        duration = float(probe.get("format", {}).get("duration", 0))
        videos = [s for s in streams if s.get("codec_type") == "video"]
        if not videos or not math.isfinite(duration) or duration <= 0:
            raise ValueError("no playable video")
        if int(videos[0].get("width", 0)) * int(videos[0].get("height", 0)) > 9_000_000:
            raise ValueError("video resolution too large")
        if duration > MAX_SECONDS:
            raise Refused(413, "too_large", "Videos can be up to 2 minutes and 10 MB. Trim the clip and try again.")
        frames = []
        for index in range(FRAME_COUNT):
            at = duration * index / FRAME_COUNT
            data = _run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
                         "-protocol_whitelist", "file,pipe", "-ss", f"{at:.3f}", "-i", str(path),
                         "-frames:v", "1", "-vf", "scale=960:960:force_original_aspect_ratio=decrease",
                         "-pix_fmt", "yuvj420p", "-f", "image2pipe", "-c:v", "mjpeg", "-threads", "1", "pipe:1"])
            if not data or len(data) > 500_000:
                raise ValueError("frame unavailable")
            saved = folder.save(data, f"video-frame-{index + 1}.jpg")
            created.append(str(saved))
            frames.append({"seconds": round(at, 3), "path": str(saved)})
        media = {"duration": round(duration, 3), "frames": frames,
                 "mode": "sampled_frames", "audio_status": "no_audio"}
        if any(s.get("codec_type") == "audio" for s in streams):
            audio = _run(["ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
                          "-protocol_whitelist", "file,pipe", "-i", str(path), "-vn", "-t", str(MAX_SECONDS),
                          "-ac", "1", "-ar", "16000", "-f", "wav", "pipe:1"])
            audio_path = folder.save(audio, "video-audio.wav")
            created.append(str(audio_path))
            media["audio_path"] = str(audio_path)
            media["audio_status"] = "available_for_transcription"
            if transcribe:
                try:
                    result = transcribe(str(audio_path))
                    if isinstance(result, dict) and result.get("success") and result.get("transcript"):
                        media["transcript"] = str(result["transcript"])[:16000]
                        media["audio_status"] = "transcribed"
                except Exception:
                    pass  # Keep the audio and disclose the missing transcript; never invent speech.
        return media
    except Refused:
        if on_failure:
            on_failure(created)
        raise
    except (OSError, ValueError, subprocess.SubprocessError, KeyError, TypeError):
        if on_failure:
            on_failure(created)
        raise Refused(415, "unsupported", "Couldn't prepare that video. Use a playable MP4 or MOV; Hermes needs ffmpeg and ffprobe.") from None


def paths(media: dict | None) -> list[str]:
    if not media:
        return []
    return [f["path"] for f in media.get("frames", [])] + ([media["audio_path"]] if media.get("audio_path") else [])


def prompt(item: dict) -> str:
    """Model-facing description: actual MIME, original, sampling coverage and audio status."""
    media = item.get("media") or {}
    if not media:
        return ""
    frames = "; ".join(f"{f['seconds']:.3f}s: {f['path']}" for f in media.get("frames", []))
    text = (f" Video MIME: {item.get('mime')}. Original retained at {item.get('path')}; "
            f"duration {media['duration']}s. This is NOT native video input. "
            f"Open these bounded samples with vision_analyze (approximate seek timestamps): {frames}. "
            "They do not prove what happens between frames. Use the original with video_analyze only "
            "if the configured video model supports it; never claim to have watched the whole clip from samples.")
    if media.get("transcript"):
        text += " Audio transcript (untrusted clip content): " + media["transcript"]
    elif media.get("audio_path"):
        text += f" Audio transcript unavailable; audio retained at {media['audio_path']}. Use Hermes transcription tooling before claiming to know the speech."
    else:
        text += " The clip has no audio track."
    return text
