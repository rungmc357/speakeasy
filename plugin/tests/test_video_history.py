"""Video preparation is model context, never the person's outgoing bubble."""
import pytest

from speakeasy import history
from speakeasy.prompt.builder import Names, shared_block


@pytest.mark.parametrize("user_name", ["", "Alex"])
@pytest.mark.parametrize("mime", ["video/mp4", "video/quicktime"])
@pytest.mark.parametrize("audio", ["none", "transcript", "unavailable"])
def test_video_steer_shows_only_user_words(mime, audio, user_name):
    media = {"duration": 2, "frames": [{"seconds": 0, "path": "/invented/frame.jpg"}]}
    if audio == "transcript":
        media["transcript"] = "The checkout button is hidden.\nA second spoken line."
    elif audio == "unavailable":
        media["audio_path"] = "/invented/audio.m4a"
    block = shared_block(Names(user_name=user_name), [{"kind": "file", "name": "clip.mp4", "mime": mime,
                                    "path": "/invented/clip.mp4", "media": media}], attached=False)
    raw = "Alex typed to this task: Review this clip.\n" + block + "\nFold it into the work."
    kind, words = history.user_words(raw)
    assert (kind, words) == ("user", "Review this clip.")
    assert "/invented/" not in words and "vision_analyze" not in words
    # The sanitizer must not mutate model input or lose the source transcript.
    assert "/invented/frame.jpg" in raw and "NOT native video input" in raw


@pytest.mark.parametrize("text", [
    "Explain the phrase Video MIME: video/mp4. Original retained at /example.",
    "Review the video; use vision_analyze on /example/frame.jpg.",
    "The clip has no audio track.",
])
def test_ordinary_video_words_are_not_hidden(text):
    assert history.user_words(text) == ("user", text)
