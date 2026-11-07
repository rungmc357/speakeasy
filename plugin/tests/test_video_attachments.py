"""Invented clip, real ffmpeg, real HTTP; no calls to a user's models."""
from pathlib import Path
import subprocess

import pytest

from fakes import http, wait_for
from test_staged_attachments import stage, text_of
from speakeasy import attachments as A, video


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    root = tmp_path_factory.mktemp("invented-clips")
    out = {}
    for ext in ("mp4", "mov"):
        path = root / ("checkout-scroll." + ext)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=180x320:rate=4:duration=2", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=2", "-c:v", "libx264", "-threads", "1",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], check=True, timeout=20)
        out[ext] = path.read_bytes()
    return out


@pytest.mark.parametrize("ext,mime", [("mp4", "video/mp4"), ("mov", "video/quicktime")])
def test_video_reaches_home_with_original_mime_and_timestamped_frames(server, service, hermes, clips, ext, mime):
    service.transcriber = lambda path: {"success": True, "transcript": "The checkout button is hidden."}
    status, staged = stage(server, clips[ext], "file", mime, "bug." + ext)
    assert status == 200, staged
    [attachment] = [a for mine in service.staged._items.values() for a in mine.values()]
    assert Path(attachment.path).read_bytes() == clips[ext]
    assert len(attachment.media["frames"]) == video.FRAME_COUNT
    assert attachment.media["transcript"] == "The checkout button is hidden."
    assert all(Path(f["path"]).is_file() for f in attachment.media["frames"])
    status, response = http(server.base_url, "POST", "/voice/work",
                            {"text": "Review the bug in this invented checkout clip", "attachments": [staged["id"]]}, server.token)
    assert status == 202, response
    wait_for(lambda: hermes.calls)
    prompt = text_of(hermes.calls[-1]["input"])
    assert mime in prompt and attachment.path in prompt
    assert "NOT native video input" in prompt and "between frames" in prompt
    assert "The checkout button is hidden." in prompt
    assert "0.000s:" in prompt
    assert all(f["path"] in prompt for f in attachment.media["frames"])


def test_wrong_signature_and_incomplete_uploads_never_stage(server, service):
    assert stage(server, b"not a movie", "file", "video/mp4", "bad.mp4")[0] == 415
    upload = service.check_stage("device", {"X-Speakeasy-Kind": "file", "Content-Type": "video/mp4",
                                          "X-Speakeasy-Filename": "clip.mp4"}, 100)
    with pytest.raises(Exception, match="upload ended early"):
        service.accept_stage(upload, b"short")
    assert not service.staged._items


def test_corrupt_bmff_is_not_accepted_as_playable(server, service):
    status, _ = stage(server, b"\x00\x00\x00\x18ftypisom" + b"broken", "file", "video/mp4", "bad.mp4")
    assert status == 415
    assert not any(service.staged._items.values())


def test_large_video_is_rejected_before_processing(server):
    status, body = stage(server, b"\x00\x00\x00\x18ftypisom" + b"0" * A.MAX_FILE_BYTES, "file", "video/mp4", "big.mp4")
    assert status == 413 and body["reason"] == "too_large"


def test_real_frames_are_upright_bounded_and_private(tmp_path, clips):
    folder = A.SharedFolder(tmp_path)
    path = folder.save(clips["mp4"], "portrait.mp4")
    prepared = video.prepare(path, folder)
    assert 1.9 <= prepared["duration"] <= 2.2
    assert prepared["audio_status"] == "available_for_transcription"
    assert len(video.paths(prepared)) == 7
    times = [f["seconds"] for f in prepared["frames"]]
    assert times == sorted(times) and times[0] == 0 and times[-1] < prepared["duration"]
    for raw in video.paths(prepared):
        p = Path(raw)
        assert p.is_file() and p.stat().st_mode & 0o777 == 0o600
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0",
                            prepared["frames"][0]["path"]], capture_output=True, check=True, text=True)
    width, height = map(int, probe.stdout.strip().split(","))
    assert height > width and max(width, height) <= 960


def test_long_video_rejected_before_sampling(tmp_path, monkeypatch):
    folder = A.SharedFolder(tmp_path)
    path = folder.save(b"original", "long.mp4")
    monkeypatch.setattr(video, "_run", lambda *a, **kw: b'{"streams":[{"codec_type":"video"}],"format":{"duration":"121"}}')
    with pytest.raises(A.Refused, match="2 minutes"):
        video.prepare(path, folder)


def test_missing_transcript_is_disclosed_not_invented(tmp_path, clips):
    folder = A.SharedFolder(tmp_path)
    path = folder.save(clips["mov"], "sample.mov")
    media = video.prepare(path, folder, transcribe=lambda _: {"success": False})
    prompt = video.prompt({"mime": "video/quicktime", "path": str(path), "media": media})
    assert "transcript unavailable" in prompt and "audio retained" in prompt
    assert "transcript" not in media
