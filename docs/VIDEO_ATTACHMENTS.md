# Video attachments

Speakeasy accepts playable MP4 and QuickTime MOV clips through its existing attachment uploader. The limit is **10 MB and 2 minutes per clip**, with at most three attachments per message. Trim or compress larger recordings before sending. The original bytes are retained on the Hermes computer; video metadata is not stripped from the original.

## Sending a recording

On iPhone or iPad, use Photos or Files from the attachment menu, or Share a video to Speakeasy. Add the instruction or caption in the ordinary message field. The attachment tray shows a poster and duration. An interrupted upload keeps the clip for Retry or Remove; a failed attachment must be retried or removed before sending. A video shared from another app uses that sheet's existing New chat / recent conversation choice.

On Mac, drop or paste a video file into an active call's attachment entry. The same encoder produces the poster; the call's existing message field supplies the caption. This does not add a separate Mac Home uploader.

Videos use the same attachment IDs and message routes as other files: Home starts work, an existing job's reply remains in that job, and a live-call upload remains owned by that call. Selecting a video does not send it or create a conversation.

## What reaches the agent

The agent gets the original clip's local path and MIME type, not just the poster. The plugin requires installed `ffmpeg` and `ffprobe`, validates a video stream and finite duration, and prepares up to six JPEG frames with approximate seek timestamps. Each frame's long edge is at most 960 pixels. Inputs over nine million pixels per frame are refused. Processing uses file/pipe protocols only and bounded subprocess timeouts.

If audio is present, the plugin extracts a bounded mono audio copy and uses Hermes' existing voice-message transcription implementation, including its configured provider and local fallback. If transcription fails, the audio is kept and the task is told the transcript is unavailable. The task must not invent speech. No new upload service is introduced; the user's existing transcription/vision provider may process the derived media according to their Hermes configuration.

The task brief explicitly says **this is not native video input**. Samples cannot prove events between frames. Originals, frames and extracted audio follow the existing shared-file retention and reference-counting path; Remove and unused-upload expiry include the derived copies.

## Model compatibility

Hermes task backends advertise image support, not universal video support. The sampled-frame path can be used by a task model through its existing `vision_analyze` tool; its configured vision model must support images. Audio uses the configured STT path, independent of the task model.

Hermes also exposes `video_analyze`, whose implementation materializes a local clip and sends a `video_url` part to the configured auxiliary vision client. Native video therefore depends on that client's provider/model, not on a successful upload or a model's ability to read still images. Speakeasy does not automatically switch providers, probe the user's models or claim native compatibility. MP4 and MOV upload/decoder acceptance alone is not an end-to-end model-understanding test.
