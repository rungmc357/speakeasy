# Speakeasy server API

The Speakeasy server runs inside the Hermes gateway as the `voice` platform plugin. It binds
`127.0.0.1` only (default port `8795`, set by `gateway.platforms.voice.extra.port`). To reach it
from another machine, put a TLS proxy you trust in front of it.

- JSON in and out (`Content-Type: application/json`). Request bodies are at most 128 KB, except
  attachment uploads, which send raw bytes with their own limits (see Look at this).
- **Auth:** every route except `GET /health` and `POST /voice/pair` needs
  `Authorization: Bearer <device token>`. Tokens come from pairing; only their SHA-256 is stored
  (`<HERMES_HOME>/speakeasy/devices.json`, 0600). A missing, wrong or revoked token gets `401 {"error": "unauthorized"}`.
- **Client header (optional):** the apps send `X-Speakeasy-Client: <platform> <os>; app <version> (<build>); <device class>`
  on every request, including `POST /voice/pair`, e.g. `iOS 27.0; app 1.4 (60); iPhone` or
  `macOS 27.0; app 0.3.15 (52); Mac`. Platform is `iOS`, `iPadOS`, `macOS` or `visionOS`; the device class is a
  generic word (`iPhone`, `iPad`, `Mac`, `Apple Vision Pro`), never a hardware identifier. The server keeps the last
  one per device in `devices.json` (`client {platform, os_version, app_version, build, model}`, rewritten only when it
  changes), shows it in `hermes voice devices`, and copies it into each call's `call-log.jsonl` entry (`client`), so a
  call can be matched to the build that made it. A missing or malformed header is ignored (older apps keep working).
- **Errors:** `{"error": "<short reason>"}` with 400 (bad body), 401, 403 (pairing), 404, 409
  (stale/conflicting state), 422 (brief rejected), 502 (Hermes or the voice provider failed).
  Some errors add a machine-readable `reason` (`{"error", "reason"}`); the attachment routes list theirs.
- IDs: `interaction_id` = `vi_<32 hex>`, `run_id` = the Hermes run ID, `task_id` = the voice
  model's handoff/call ID, `draft_id` = `ed_<24 hex>`.
- No route ever returns an API key, the Hermes `API_SERVER_KEY`, a device token (except once, at
  pairing) or a server-side file path.

The examples below are real responses captured from the server running against the fake Hermes
used in the tests (timestamps and IDs will differ).

## Routes

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/health` | none | Liveness |
| POST | `/voice/pair` | none | Trade a one-time code for a device token |
| POST | `/voice/sessions` | device | Start (or resume) a voice call |
| GET | `/voice/interactions/{id}` | device | Call snapshot |
| GET | `/voice/interactions/{id}/events` | device | Live SSE feed for a call |
| POST | `/voice/interactions/{id}/end` | device | End the call |
| POST | `/voice/interactions/{id}/pause` | device | Pause the call (resumable) |
| POST | `/voice/interactions/{id}/approval` | device | Answer a Hermes tool approval |
| POST | `/voice/interactions/{id}/cancel-backend` | device | Stop a task's Hermes run |
| POST | `/voice/work/{run_id}/stop` | device | Stop one task by id, with or without a call (`capabilities.task_stop`) |
| POST | `/voice/interactions/{id}/skip-tour` | device | End the first-call tour |
| POST | `/voice/interactions/{id}/early-request` | device | Words heard while the call connected: the first request (or, in a call from listening mode, "answer from the room") |
| POST | `/voice/interactions/{id}/answer` | device | Answer a task's question card (or a spoken-choice card) |
| POST | `/voice/interactions/{id}/card-action` | device | Press a button on a task's card during a call |
| POST | `/voice/interactions/{id}/input-text` | device | A turn typed on the call screen instead of spoken |
| POST | `/voice/interactions/{id}/mic-check` | device | Log a mic repair (numbers only, no audio or words) |
| POST | `/voice/interactions/{id}/mic` | device | The call's mic went on or off (pauses the progress pulse) |
| POST | `/voice/interactions/{id}/screen` | device | Turn screen sharing on or off for this call |
| POST | `/voice/interactions/{id}/captures/{capture_id}` | device | Report on a capture request |
| POST | `/voice/interactions/{id}/attachments` | device | Upload a capture, picture or file (raw bytes) |
| DELETE | `/voice/interactions/{id}/attachments/{attachment_id}` | device | Remove a pending picture or file |
| POST | `/voice/attachments` | device | Stage a picture or file for a typed message or new job (raw bytes; iPhone/iPad) |
| DELETE | `/voice/attachments/{attachment_id}` | device | Remove a staged picture or file |
| GET | `/voice/work/latest` | device | Latest task + task list + recap |
| GET | `/voice/work/{run_id}` | device | One task |
| POST | `/voice/work/{run_id}/card-action` | device | Press a button on a task's card with no call open |
| POST | `/voice/work/{run_id}/message` | device | Send typed text to one task (reply bar, decision card's "Something else…") |
| POST | `/voice/work` | device | Start a new job from typed text, no call needed (the plugin-update notice's Update) |
| GET | `/voice/history` | device | Every Hermes session and Speakeasy task, newest first (search, source filter, paging) |
| GET | `/voice/history/{session_id}` | device | One session: messages, collapsed tool calls, its Speakeasy jobs and approvals |
| POST | `/voice/history/{session_id}/message` | device | Send typed or voice-message text to that session, by id (never routed) |
| POST | `/voice/history/{session_id}/approval` | device | Answer the approval one of the session's Speakeasy tasks waits on (no call needed) |
| GET | `/voice/models` | device | Hermes' models, the default and the fast/smart presets (`model_choice`, "Models") |
| PUT | `/voice/history/{session_id}/model` | device | Use a model / reasoning level for this chat ("Models") |
| GET | `/voice/schedules` | device | Hermes scheduled (cron) jobs: plain-English schedule, next and last run, result |
| GET | `/voice/schedules/{id}` | device | One schedule: prompt and recent runs (each with its session) |
| POST | `/voice/schedules/preview` | device | Read a sentence or a schedule into a preview to confirm (saves nothing) |
| POST | `/voice/schedules` | device | Save a confirmed preview as a new scheduled job |
| POST | `/voice/schedules/{id}/pause`, `/resume`, `/run` | device | Pause, resume, or run once now |
| PATCH | `/voice/schedules/{id}` | device | Change the schedule, prompt or name |
| DELETE | `/voice/schedules/{id}` | device | Delete a scheduled job |
| POST | `/voice/tasks/dismiss` | device | Hide finished tasks |
| GET | `/voice/titles` | device | Past tasks still under their stand-in title: what the app's Apple Intelligence sweep may rename |
| POST | `/voice/titles` | device | Set one task's title (from the app's on-device titler); the job's name follows |
| GET | `/voice/card-image/{run_id}/{1-8}` | device | Image bytes for an image card |
| GET | `/voice/shared-image/{run_id}/{n}` | device | An image the user shared with a task |
| POST | `/voice/drafts/{draft_id}` | device | Approve / deny / revise an email draft |
| GET, PATCH | `/voice/settings` | device | Server settings |
| GET, PUT | `/voice/brief` | device | The voice brief |
| POST | `/voice/brief/rewrite` | device | Ask Hermes to rewrite the brief |
| GET | `/voice/brief/tune` | device | Brief edits proposed from recent calls (state, edits, product issues) |
| POST | `/voice/brief/tune` | device | Ask Hermes to propose edits from the last week of calls |
| POST | `/voice/brief/tune/apply` | device | Apply the accepted edits: `{"accept": ["e1", ...]}` |
| POST | `/voice/brief/tune/dismiss` | device | Discard the proposal |
| GET | `/voice/status` | device | Provider, Hermes API, brief readiness |
| POST, GET, DELETE | `/voice/codex/login` | device | Sign Codex in to ChatGPT from the app (device code): start, poll, cancel |
| GET | `/voice/destinations` | device | Where finished results can be posted |
| POST | `/voice/destinations/suggest` | device | Ask Hermes to propose delivery channels (never saved) |
| GET, POST | `/voice/onboarding` | device | First-run checklist |
| GET | `/voice/endpoints` | device | Every address of this Hermes (tailnet, Cloudflare) + the address note |
| POST | `/voice/prove` | none | Proves this is the paired Hermes before the app sends its token to a new address |
| POST | `/voice/push` | device | Turn notifications on (APNs token, environment, kinds) or off for this device |
| GET | `/voice/push/events/{id}` | device | The real text of one notification (read by the phone's notification extension) |

## GET /health

No auth. Used by the app to find the server. Off this machine (a tailnet or a Cloudflare tunnel)
it says only `{"ok": true, "platform": "voice"}`; the fields below are for apps on this machine.

`200`
```json
{
  "ok": true,
  "platform": "voice",
  "version": "0.2.0",
  "provider": "openai"
}
```

## POST /voice/pair

No auth. `code` is the 6-digit single-use code from `hermes voice setup` / `hermes voice pair`
(valid 10 minutes; issuing a new code expires the old one). The code is consumed whether or not it
matched. The token is returned once; store it in the Keychain.

```json
{"code": "482913", "device_name": "Work Mac"}
```
`201`
```json
{
  "device_id": "50689888",
  "token": "<device token, shown once>",
  "assistant_name": "Hermes"
}
```

Wrong or expired code: `403 {"error": "pairing code is wrong or expired"}`. From off this machine,
more than 5 attempts a minute from one address (or 20 overall) get `429` (`reason: too_many`), and
that address is locked out for 15 minutes. Codes last 10 minutes while a Cloudflare tunnel is on.

Plugin 0.3.28+ also returns the fields of `GET /voice/endpoints` (`endpoints`, `address_drop`), so
a phone knows every address and can read the address note. Older apps ignore them.

## GET /voice/endpoints

Device token. Every address of this Hermes, for the app to keep and try (tailnet first), and the
encrypted address note for a Cloudflare quick tunnel (`null` otherwise, or when opted out).
Capability `endpoints`.

```json
{
  "endpoints": {"tailnet": "https://mini.tail1.ts.net", "cloudflare": "https://calm-river-sound.trycloudflare.com",
                "cloudflare_permanent": false},
  "address_drop": {"relay": "https://ntfy.sh", "topic": "se-<32 hex>", "key": "<base64url, 32 bytes>"}
}
```

**Address note.** Whenever the quick tunnel's address changes (and every 6 hours), the plugin
POSTs `se1.<base64url(nonce(12) ‖ AES-256-GCM ciphertext ‖ tag)>` to `<relay>/<topic>`, with the
topic as additional authenticated data and plaintext `{"url", "ts", "seq"}`. The app polls
`<relay>/<topic>/json?poll=1&since=24h`, takes the newest note whose `seq` is higher than the last
one it accepted, and switches only after `POST /voice/prove` and `GET /voice/status` succeed there.

## POST /voice/prove

No auth (the token isn't sent until this passes). `{"device_id": "50689888", "nonce": "<22-128
base64url chars>"}` → `200 {"proof": "<hex>"}`, where proof = HMAC-SHA256(key = the lowercase hex
SHA-256 of the device token, message = `speakeasy-prove-v1|<nonce>|<host>`) and host is the `Host`
the app called (no port). An unknown device id gets a random proof.

## Cloudflare mode: host check

While `server.tunnel` is on, a request whose `Host` isn't one the server publishes (loopback,
`advertised_url`, `tunnel_url`, `tailscale_name`, `tunnel_hostname`) gets `421 {"error": "unexpected
host"}`. `PATCH /voice/settings` refuses `server` and `address_drop` (`403`): where the server is
published is set on the computer, never from a paired device.

## POST /voice/sessions

Starts a call. Body: the WebRTC SDP offer, and optionally `resume_from` (the `interaction_id` of a
paused call) to resume it with the conversation reseeded. Send an `Idempotency-Key` header: a
retry with the same key and body returns the same session instead of starting a second call.

```json
{"sdp": "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\n..."}
```
```json
{"sdp": "v=0\r\n...", "resume_from": "vi_2436cf97524f58de64775b5a9ecc3e58"}
```

`screen` (optional): `"ready"` when this app can share the user's screen, `"no_permission"` when it
could but Screen Recording isn't allowed yet. Only calls that send it get capture requests, holds and
screen hints (see Look at this); calls without it (the iPhone app, older Mac apps) work as before.
Send it only when `GET /voice/status` reports `attachments`. A resume without it keeps the paused
call's value; any other value is a 400.

`tour` (optional, ignored with `resume_from`) starts the call with the one-time first-call tour:
the voice walks the user through a real task, the controls and where results go, in its own
words, and drops it the moment they say skip. Its value names the shortcuts to mention, all
optional: `{"call": "⌃⌥Space", "mute": "⌃⌥M", "pause": "⌃⌥P"}` (`{}` for none).

`room` (optional, plugin 0.2.51+, only when `GET /voice/status` has `room_listening: true`) is what
listening mode heard: `[HH:MM] text` lines, at most 24,000 Unicode code points. The app sends it
with `resume_from` when listening mode (turned on mid-call, which paused the call) is turned off:
it's added to any room text the call already had (newest kept within the cap) and the voice may
respond from the room once more. A new call can carry it too (a resume that fell back to a new
call). A plain resume keeps the server's copy. `""` means no room. With a room, `tour` is ignored.
The room is part of the idempotency fingerprint (a retry with the same key must send the same room).

```json
{"sdp": "v=0\r\n...", "room": "[14:02] Sam says the deadline is Friday the 14th.\n[14:03] Priya will send the deck by Wednesday."}
```

What happens to it: secret-looking lines and tokens are redacted on intake; the voice gets it as a
fenced background block in its instructions (never as instructions); tasks started from that call
get it in their Hermes input (not in posts to a chat thread). It is held in memory on this
interaction only: never in the call log, recent voice, Tune or the task store. It moves to a resumed
call, and is dropped when the call ends or 15 minutes after it was paused.
`201`
```json
{
  "interaction_id": "vi_2436cf97524f58de64775b5a9ecc3e58",
  "session": {
    "id": "sess_x"
  },
  "transport": {
    "type": "webrtc",
    "sdp": "v=0\r\n..."
  },
  "voice_provider": "openai"
}
```

`voice_provider` is `codex` (default: the user's own `codex login`, via `codex app-server`) or
`openai` (`SPEAKEASY_OPENAI_API_KEY` in the profile `.env`). A resumed call also carries
`"resumed_from": "<old interaction_id>"`. Errors: 400 bad body (including a `room` over the cap), 409 the call was already resumed or the Idempotency-Key was reused with a
different SDP or room, 502 the voice provider could not start (see `GET /voice/status` →
`codex_message`).

**Grok voice (beta).** With `voice.provider` `grok` (allowed only while the `beta.grok_voice`
setting is on) the body needs no `sdp` (one sent is ignored) and the response's transport is a
relay instead of WebRTC:

```json
{
  "interaction_id": "vi_2436cf97524f58de64775b5a9ecc3e58",
  "session": {"id": "grok_2436cf97524f58de64775b5a9ecc3e58"},
  "transport": {"type": "relay", "path": "/voice/interactions/vi_2436cf97524f58de64775b5a9ecc3e58/audio",
                "format": {"encoding": "pcm16le", "rate": 24000, "channels": 1}},
  "voice_provider": "grok"
}
```

The plugin holds the one xAI realtime socket (credential: the xAI sign-in Hermes holds, else
`XAI_API_KEY` in the profile `.env`; never returned). 409 when the beta is off or there is no xAI
credential (the message says how to sign in).

## GET /voice/interactions/{id}/audio (WebSocket, Grok calls)

The app's audio relay for a call whose transport is `relay`. A WebSocket upgrade (RFC 6455, no
extensions) on the same port, with the device bearer token like every route. 404 unknown call, 409
a call without a relay or already ended, 426 not an upgrade. One client per call: a second
connection replaces the first (the first gets `closed` with reason `replaced`).

- App → plugin, binary: raw PCM16LE 24 kHz mono mic audio, 20–100 ms chunks.
- App → plugin, text JSON: `{"type":"mute","muted":true|false}` (audio is dropped while muted),
  `{"type":"end"}` (ends the call; same as `POST /end`).
- Plugin → app, binary: PCM16LE 24 kHz mono assistant audio, to play in order.
- Plugin → app, text JSON: `{"type":"speech_started"}` (the user barged in: drop unplayed audio;
  the plugin also cancels the reply), `{"type":"response_done"}` (a reply finished),
  `{"type":"closed","reason":"..."}` (`ended`, `paused`, `replaced`, `relay lost`,
  `max call length`, `voice connection lost`) just before the plugin closes the socket.

The plugin pings every 5 s. The call ends when the app sends `end`, when no relay is connected for
more than 10 s (20 s for the first connection), or at `voice.max_call_minutes`.

## POST /voice/interactions/{id}/early-request

Words the app transcribed on the device while the call was connecting. Body `{"text": "..."}`
(at most 4,000 characters; the first 1,000 are used). Waits up to 8 s for the call to connect
(409 if it doesn't). Returns `{"interaction_id", "task_id"}`.

- Usually the text is the call's first request and is handled like any request (`task_id` is the
  task it started, or `null` when the voice answers it itself).
- In a call started from listening mode (one with a `room`): a question about what was said in the
  room ("what did Sam say the deadline was?") is answered by the voice from the room, with no task.
  An empty text means the user said nothing after turning listening off: the voice responds to the
  conversation once, unless the user has already spoken in the call. Both return `task_id: null`.
- An empty text in any other call does nothing.

## POST /voice/interactions/{id}/answer

A tap on a question card's option (or a typed answer). Body `{"task_id": "...", "text": "..."}`
(text at most 400 characters); it goes back to that task as a follow-up.

## POST /voice/interactions/{id}/input-text

A turn typed on the call screen instead of spoken (plugin with `input_text: true` in
`GET /voice/status`). Body `{"type": "session.input_text", "text": "...", "client_id": "..."}`, where
`client_id` is optional and nothing else is allowed. The text is made one line (control characters and
runs of whitespace become one space) and must then be 1 to 1,000 characters. It goes to the live call
as the user's turn:

- Codex voice: `thread/realtime/appendText` with `role: "user"`. If Codex transcribes it back as a
  user turn, that copy isn't recorded again. Only the next user transcript within 6 s of Codex taking
  the text can be that copy, and each typed turn absorbs at most one. A different user turn ends the
  window, so saying the same words out loud later is always recorded.
- GPT-Live: a session-wide `session.commentary.append` (no delegation id). Its event channel takes no
  user text, so the note quotes the words and asks for a spoken reply. It goes on the commentary channel
  because thinking notes are background the voice may hold without answering.
- Grok voice: a user `message` item, then a reply is asked for.

The turn joins the call's transcript (so the next task the voice hands off carries it) and is
published on the call's event stream as `transcript` `{"role": "user", "text", "source": "typed"}`,
plus `client_id` when the send had one, so the sending app can tell that copy from its own echo.
After Pause and Resume the newest call in the chain takes it.

**`client_id` (optional; same rules as an `Idempotency-Key`: 1 to 128 of `A-Z a-z 0-9 _ -`).** The
app's id for this turn, which makes retries safe. Send the same id again on the same call, before or
after a Pause and Resume (for example after a 504 or a dropped connection), and the voice never gets the
turn twice:

- The first send went through: `200` with the same body plus `"duplicate": true`.
- It's still in flight: the retry waits on it, and may itself get a 504.
- It failed (409, 502): the retry is a new send.
- The same id with different text gets `409 client_id_reused`.

Without a `client_id` the route works as before and every send is a new turn.

`200 {"interaction_id", "type": "session.input_text", "sent": true}` (`interaction_id` is the call
that took it; `client_id` is echoed back when sent). Errors carry `error` and `reason`:

| status | reason | when |
|---|---|---|
| 400 | `invalid_body`, `empty_text`, `text_too_long` | any other body (including a malformed `client_id`), nothing left after cleanup, over 1,000 characters |
| 401 | | missing or wrong device token |
| 404 | `unknown_interaction` | no such call (a plugin without the route also answers 404, with no `reason`) |
| 409 | `call_ended`, `call_paused`, `not_connected` | the call ended or is paused, or its voice connection is gone |
| 409 | `client_id_reused` | this `client_id` was already used for different text on this call |
| 502 | `not_delivered` | the call is still up, but this one send failed. Nothing was recorded, the call is not marked as failed, and sending again is safe |
| 502 | `transport_error` | the voice connection failed unexpectedly |
| 504 | `timeout` | the call didn't confirm within 15 s. **The outcome is unknown: the voice may already have the words.** With a `client_id` the send keeps going, and retrying with the same id gives the outcome without sending twice. Without one the send is abandoned and never recorded, so check the call before sending the same words again |

Nothing is recorded or published unless the voice took the turn.

## POST /voice/interactions/{id}/mic-check

The app found the call's mic wasn't getting through and reopened the connection. Body
`{"note": "..."}` (at most 400 characters of numbers and state, no audio or words). Logged only.

## POST /voice/interactions/{id}/mic

The user muted or unmuted the call's mic. Body exactly `{"muted": true|false}`; `200`
`{"interaction_id": "...", "muted": true}`. The app sends it once when each call (and each resumed
call) starts, muted or not, and on every change. While muted, the call skips its periodic "what the
task is doing" updates (the pulse, every `speech.interval_seconds` while work runs); answers,
questions and approvals are still spoken. With the mic open, a task's first update comes 12-20 s in
and the updates don't back off on a quiet task. A call that never reported keeps the older timing.
Older plugins answer `404`, which the app ignores.

## GET /voice/interactions/{id}

Snapshot of one call. `run_id`/`status` describe the most recent task; `approval` is the pending tool approval, if any.

`200`
```json
{
  "interaction_id": "vi_2436cf97524f58de64775b5a9ecc3e58",
  "status": "completed",
  "run_id": "run_0001",
  "backend_run_id": "run_0001",
  "delegation_id": "call_doc_1",
  "revision": 1,
  "approval": null,
  "finalization": "open",
  "error": null,
  "paused": false,
  "resumed_from": null,
  "screen": {"on": false, "seq": 0, "declared": "ready"},
  "captures": [],
  "attachments": [],
  "hold": null
}
```

`screen`, `captures`, `attachments` and `hold` are the call's Look at this state: sharing on or off
(with the last applied `seq`), open capture requests (`[{"capture_id"}]`, so a client on the
polling fallback still gets them), pictures and files waiting for the next request
(`[{"id", "kind", "name", "app", "state": "pending" | "sending"}]`) and the held "look at my
screen" request (`{"state": "waiting" | "not_sent", "text", "since"}` or `null`). Sharing is off
at the start of every call and every resume.

## GET /voice/interactions/{id}/events (SSE)

`text/event-stream`. Every event has `id:` (monotonic per call), `event:` and one-line JSON `data:`.
The first event is a full `snapshot`; after that, only changes. Reconnect with `Last-Event-ID` to
resume; if that ID is too old you get a fresh `snapshot`. A `: ping` comment is sent every 15 s.
The stream ends after `closed`.

| event | data |
|---|---|
| `snapshot` | `{"interaction", "work", "approval", "tasks", "email_drafts", "away"}` |
| `interaction` | same as `GET /voice/interactions/{id}` |
| `work` | the call's latest task (same as `work` in `/voice/work/latest`) or `null` |
| `tasks` | the call's task list (same items as `tasks` in `/voice/work/latest`) |
| `approval` | `{"run_id", "request_id", "description", "choices"}` or `null`; `choices` is `["once", "deny"]`, plus `"session"` (the rest of that task) when Hermes offered it. Never `"always"` |
| `email_drafts` | every visible draft of the call's tasks, each with `task_id` and `run_id` added; published whenever a draft appears or changes status |
| `closed` | `{"finalization": "complete" \| "incomplete", "error"}` |
| `capture` | `{"capture_id"}`: capture the user's window for one request (see Look at this) |
| `screen.hint` | `{"reason": "screen_off" \| "no_permission" \| "share_request"}`: draw the user's eye to the screen button |
| `screen.state` | `{"on", "seq"}`: sharing changed on the server side ("stop looking at my screen") |
| `transcript` | `{"role": "user", "text", "source": "typed"[, "client_id"]}`: a turn typed on a call screen (`input-text`) joined the call's transcript. Spoken turns don't come here (they arrive on the voice connection). The same event with a `task_id` is a message typed to one task (see Messages to a task), not a turn to the call: apps show it with that task's name and never as a call turn |

```
id: 1
event: snapshot
data: {"interaction": {...}, "work": null, "approval": null, "tasks": [], "email_drafts": [], "away": []}

id: 7
event: email_drafts
data: [{"draft_id": "ed_30b3b5f187309e79598c0daf", "subject": "Dinner on Friday", "status": "pending", "sha256": "fa7d...", "task_id": "call_doc_1", "run_id": "run_0001", ...}]
```

## POST /voice/interactions/{id}/end

Body `{}`. Ends the call. Tasks keep running; their results show in the task list, the next call's
"while you were away" recap, and (if set) the delivery target.
`200`
```json
{
  "finalization": "open"
}
```

`finalization` is `open` while the transport is still closing, then `confirmed`.

## POST /voice/interactions/{id}/pause

Body `{}`. Pauses the call (the voice transport is closed, tasks keep running). Resume with `POST /voice/sessions` + `resume_from`. A call left idle for `idle_pause_minutes` is paused the same way.

`200`
```json
{
  "interaction_id": "vi_2436cf97524f58de64775b5a9ecc3e58",
  "paused": true
}
```

## POST /voice/interactions/{id}/approval

Answers a Hermes tool approval for one run. `once` and `deny` are accepted, and `session` only when
the approval's `choices` include it. `always` (permanent) is refused with 400 from every surface;
the call's voice can answer the same approval ("approve", "no") under the same rules.
```json
{"run_id": "run_0001", "request_id": "req_7f2c", "choice": "once"}
```
`200`
```json
{"interaction_id": "vi_...", "choice": "once", "run_id": "run_0001", "resolved": 1}
```
409 when that approval is no longer pending (stale). 502 if Hermes rejected it.

## POST /voice/interactions/{id}/skip-tour

Body `{}`. The Skip tour button: tells the live call to drop the first-call tour and carry on
normally. Returns `{"interaction_id", "tour": "skipped"}`.

## POST /voice/work/{run_id}/stop

Stops one task by its run id whether or not a call is open (a panel row's Stop): the plugin finds the
call or the voiceless worker holding it and stops it the way `cancel-backend` does. Body `{}`.
`202` with the same reply as `cancel-backend`; `409` when that task isn't running; `404` for a bad id.
Servers with `capabilities.task_stop`.

## POST /voice/interactions/{id}/cancel-backend

Stops one task's Hermes run.
```json
{"run_id": "run_0001"}
```
`202`
```json
{"interaction_id": "vi_...", "backend_cancel": "stopping", "run_id": "run_0001", "voice_session": "open"}
```
409 when that run is not active.

## GET /voice/titles

Past tasks whose shown title is still the stand-in label built from the request's own words —
the cloud titler never landed (out of credits, reload mid-name) or the task never got one.
Finished tasks only (a running one may still get the cloud titler's name). Newest first, at most 40. The Mac app's on-device Apple Intelligence sweep (off by default; Settings › General) renames these;
tasks the titler (or a chat thread) already named properly are never offered.

`200`
```json
{"tasks": [{"task_id": "call_doc_1", "title": "Also separately, I want to use",
            "request": "Also separately, I want to use Apple Intelligence to rename the title of previous chat threads",
            "updated": 1792474018.0}]}
```

## POST /voice/titles

Set one task's title, generated on device in the app (Apple Intelligence; the app's sweep is off
until turned on in Settings). The title also renames the job the task first named, but only while
that job still has its stand-in name.
`{"task_id": "call_doc_1", "title": "rename voice threads"}` →
`200 {"task_id": "call_doc_1", "title": "rename voice threads", "renamed": true}`.
Errors: 400 (bad body, or a title outside 2-90 characters), 404 (unknown task), 409 (the task
already has a real title: the cloud titler or a chat thread named it after the app asked).

## GET /voice/work/latest

The newest task (`work`), the task list (`tasks`, newest last, finished ones hidden after
dismissal) and the recap of results that finished while no call was open (`away`). Each task has
`task_id`, `run_id`, `status` (`admitting`, `working`, `waiting_for_approval`, `waiting_on_task`,
`completed`, `failed`, `cancelled`, `interrupted`, `ambiguous`), live `short_status`/`detail`, `events`,
`result` (`spoken`, `full`, optional `label`, optional `cards`, optional `summary_points`) and `email_drafts`.

**Job progress** (capability `job_progress`; all optional, null when unknown):
`started_at` (unix seconds the task was admitted), `finished_at` (unix seconds it ended, null while
running), `step_index` / `step_count` (the step it is on, 1-based, out of its `plan`; null without a
plan; `step_index == step_count` once completed), `eta_seconds` (only once two steps are done, from
their measured pace; never a guess), and `failure_reason`: one short plain sentence for a `failed`
or `interrupted` task (e.g. "Hermes's model provider is out of credits.", "It timed out waiting on
Hermes's model provider.", "Hermes stopped with an error and didn't say why."), never "unknown";
null otherwise. The richer `failure` object (below) is unchanged.

**Summary points** (capability `summary_points`): when an answer is laid out in sections (two or
more headings, or a list of three or more), `result.summary_points` holds up to four key points of
at most ten words each. `result.full` is unchanged; apps show the points and fold `full` behind
Details.

**Spoken choices** (capability `spoken_choices`): when the voice offers the user a pick out loud
("the quick summary or the full report?"), a lightweight task appears (`task_id` starting
`choice_`, status `completed`, no Hermes run) whose `result.question` / `result.views[0]` is a
`question` view with the options. A tap goes through `POST /voice/interactions/{id}/answer` like any
question card; the voice hears it as the user's answer and carries on (nothing is sent to Hermes).
Answering out loud, a newer choice, or the call ending sets the card aside (`set_aside: true`).
`follow_ups` (only when present) lists what the user added while it ran, oldest first:
`{"text", "milestone", "state", "at"}`, where `milestone` is the matching `You added: …` event and
`state` is `queued` (Hermes accepted it; the task has not read it yet), `landed` (it is in the task's
transcript, or the run finished without handing it back) or `not_read` (the run ended first).
A task with status `waiting_on_task` was held so two tasks don't edit one project at once; it has
`waiting: {"name", "project"}` (the task it waits for) and starts when that task ends. An earlier
call's task shown only for its pending draft or review card has `carried: true`.
Product and image cards appear as `result.cards`; image cards are fetched through
`/voice/card-image/...`, never by path.

Each task also has `job`: `{"id": "...", "name": "Night shift"}`. The id is the Hermes session
the task runs in (else the session it continued, else the task's own key); tasks with the same id
are one job, across calls and days. The name is 2-4 words from the job's first task (never a chat
thread's title), set once and possibly refined by the titler; never empty.

A question card (`result.question`, also the first of `result.views`) may carry `context`, `why`
and `affects` strings, and each of its `options` is either a string or an object
`{text, detail, tags[{text, tone}], risk (low|medium|high), reversible}` (tag tone: good, warn,
bad, neutral). `approvable: true` is set by the plugin only, never taken from the model: the
recommended option is low risk and reversible, and nothing in the question or that option spends
money, speaks as the user, publishes or deletes. Answer with the option's `text`.

View kinds for explaining a job (tone: good, warn, bad, info, neutral; an unknown tone is dropped):
`lanes {title, start_label, end_label, ticks[≤6], lanes[≤6]{label, segments[≤8]{from, to, tone,
label}}, note}` (from/to clamped to 0..1, a segment with to ≤ from is dropped);
`before_after {title, before{label, text, image}, after{label, text, image}}` (image: an https URL
or a card number for `/voice/card-image`); `flow {title, nodes[≤5]{label, detail, tone}}`;
`devices {title, items[≤8]{name, value, detail, tone, history[≤6]}}`.

A `failed` task also has `failure`: why it failed, in Speakeasy's own words (the provider's error
text is never passed through). `kind` is `billing`, `auth`, `rate_limit`, `model_not_found`,
`hermes_key`, `hermes_unreachable` or `unknown`; `label` is a short tag for the status line
("Out of credits"), present only for a known reason; `text` says what happened and what to do.
`unknown` means Hermes gave neither a recognized reason nor an answer, and its `text` points at
Hermes' errors.log. A failed item in `away` carries the same `failure` object.

```json
"failure": {"kind": "billing", "label": "Out of credits",
            "text": "Hermes's model provider is out of credits. Top up that account, or run hermes model on the Hermes machine to switch providers."}
```

`200`
```json
{
  "work": {
    "run_id": "run_0001",
    "status": "completed",
    "stale": false,
    "updated": 1790527324.037596,
    "events": [
      {
        "kind": "request",
        "text": "Email Pat about dinner Friday",
        "at": 1790527324.0339231
      },
      {
        "kind": "result",
        "text": "Drafted.",
        "at": 1790527324.037605
      }
    ],
    "short_status": "Searching weather",
    "detail": "Searching for: weather",
    "source_run_id": "run_0001",
    "updated_at": 1790527324.0364969,
    "status_source": "tool",
    "result": {
      "spoken": "I drafted it — take a look and approve when ready.",
      "full": "Drafted.",
      "label": "Email drafted"
    },
    "title": "Email Pat about dinner Friday",
    "summary": null,
    "dismissed": false,
    "email_drafts": [
      {
        "draft_id": "ed_30b3b5f187309e79598c0daf",
        "bcc": [],
        "body": "Hi Pat,\n\nStill on for Friday at 7?",
        "cc": [],
        "from": "me@example.com",
        "subject": "Dinner on Friday",
        "to": [
          "Pat Doe <pat@example.org>"
        ],
        "sha256": "fa7dc978803802821de7277bd9ab00226c63b4c2a3b4113c829e8bcf7baf2dfa",
        "status": "pending",
        "created_at": 1790527324.037801,
        "updated_at": 1790527324.037801
      }
    ]
  },
  "away": [],
  "tasks": [
    {
      "run_id": "run_0001",
      "status": "completed",
      "stale": false,
      "updated": 1790527324.037596,
      "events": [
        {
          "kind": "request",
          "text": "Email Pat about dinner Friday",
          "at": 1790527324.0339231
        },
        {
          "kind": "result",
          "text": "Drafted.",
          "at": 1790527324.037605
        }
      ],
      "short_status": "Searching weather",
      "detail": "Searching for: weather",
      "source_run_id": "run_0001",
      "updated_at": 1790527324.0364969,
      "status_source": "tool",
      "result": {
        "spoken": "I drafted it — take a look and approve when ready.",
        "full": "Drafted.",
        "label": "Email drafted"
      },
      "title": "Email Pat about dinner Friday",
      "summary": null,
      "dismissed": false,
      "email_drafts": [
        {
          "draft_id": "ed_30b3b5f187309e79598c0daf",
          "bcc": [],
          "body": "Hi Pat,\n\nStill on for Friday at 7?",
          "cc": [],
          "from": "me@example.com",
          "subject": "Dinner on Friday",
          "to": [
            "Pat Doe <pat@example.org>"
          ],
          "sha256": "fa7dc978803802821de7277bd9ab00226c63b4c2a3b4113c829e8bcf7baf2dfa",
          "status": "pending",
          "created_at": 1790527324.037801,
          "updated_at": 1790527324.037801
        }
      ],
      "task_id": "call_doc_1"
    }
  ]
}
```

## GET /voice/work/{run_id}

One task by Hermes run ID (including tasks from past calls). 404 if unknown.

`200`
```json
{
  "work": {
    "run_id": "run_0001",
    "status": "completed",
    "stale": false,
    "updated": 1790527324.037596,
    "events": [
      {
        "kind": "request",
        "text": "Email Pat about dinner Friday",
        "at": 1790527324.0339231
      },
      {
        "kind": "result",
        "text": "Drafted.",
        "at": 1790527324.037605
      }
    ],
    "short_status": "Searching weather",
    "detail": "Searching for: weather",
    "source_run_id": "run_0001",
    "updated_at": 1790527324.0364969,
    "status_source": "tool",
    "result": {
      "spoken": "I drafted it — take a look and approve when ready.",
      "full": "Drafted.",
      "label": "Email drafted"
    },
    "title": "Email Pat about dinner Friday",
    "summary": null,
    "dismissed": false,
    "email_drafts": [
      {
        "draft_id": "ed_30b3b5f187309e79598c0daf",
        "bcc": [],
        "body": "Hi Pat,\n\nStill on for Friday at 7?",
        "cc": [],
        "from": "me@example.com",
        "subject": "Dinner on Friday",
        "to": [
          "Pat Doe <pat@example.org>"
        ],
        "sha256": "fa7dc978803802821de7277bd9ab00226c63b4c2a3b4113c829e8bcf7baf2dfa",
        "status": "pending",
        "created_at": 1790527324.037801,
        "updated_at": 1790527324.037801
      }
    ]
  }
}
```

## Live job activity

Capability `job_activity`. A task's work object (`GET /voice/work/{run_id}`, `GET /voice/work/latest`
`tasks[]`, and the `work` / `tasks` events on the call's event stream) may carry four more fields,
each left out when there is nothing real to show. Clients that don't know them ignore them.

```json
"activity": [{"id": "a7", "at": 1793820082.0, "kind": "search",
              "text": "Searched the web for “playwright wait for mock server ready”", "status": "done",
              "tool": "web_search", "started_at": 1793820082.0, "ended_at": 1793820085.0,
              "args": "playwright wait for mock server ready",
              "result": "5 results: playwright.dev/docs/test-fixtures …"},
             {"id": "a13", "at": 1793820113.0, "kind": "run", "text": "Ran the tests",
              "detail": "npx playwright test checkout.spec.ts --repeat-each=50", "status": "running",
              "tool": "terminal", "started_at": 1793820113.0,
              "args": "npx playwright test checkout.spec.ts --repeat-each=50"}],
"activity_total": 13,
"findings": [{"id": "f1", "at": 1793820080.0,
              "text": "The test doesn't wait for the payment mock to start, so it fails about 1 in 6 runs"}],
"touched":  [{"kind": "file", "label": "checkout.spec.ts", "detail": "+3 −2"},
             {"kind": "url", "label": "playwright.dev/docs/test-fixtures", "url": "https://playwright.dev/docs/test-fixtures"},
             {"kind": "command", "label": "npx tsc --noEmit"}],
"hero": {"type": "trials", "total": 50, "passed": 31, "failed": 0}
```

- **`activity`** — newest last. The task keeps its last 100; the event stream, the task feed and
  `GET /voice/work/latest` carry the newest 40 (without `args`/`result`), and
  `GET /voice/work/{run_id}` carries all of them with their previews. `activity_total` says how many
  the task has kept, so a client knows when to fetch the rest. One item per tool call the Hermes run made (from its
  `tool.started` / `tool.completed` events; for a task Hermes runs in a chat thread, which has no
  event stream, from the tool calls in that session's transcript, read at most once a second while
  it runs, with the transcript's own start and end times), plus `STATUS:` lines it wrote (`kind: "message"`), a
  "Thinking it over" item when it reasons (never the reasoning itself), helpers it started and
  approvals it asked for. `kind` is one of `read`, `edit`, `run`, `search`, `browse`, `think`,
  `message`, `tool`; `status` is `running`, `done` or `failed` (items still running when the run ends
  take its outcome). `text` ≤ 80 characters, optional `detail` ≤ 100. Ids are stable for the task.
- **Tool calls** (capability `tool_log`). An item that is a tool call also carries `tool` (the raw
  tool name, e.g. `terminal`, `mcp__tracker__create_issue`), `started_at` and, once it settles,
  `ended_at` (epoch seconds). On `GET /voice/work/{run_id}` it may carry `args` (Hermes' own
  argument preview, ≤ 300 characters) and `result` (its result preview, ≤ 500 characters, up to 16
  lines). Both go through the same redactor as `text` (tokens, keys — quoted JSON ones too — env
  values, emails and query strings removed, paths cut to their file name) before they are cut, and
  are absent when nothing safe is left. They are previews: never the whole argument or output.
  Items that aren't tool calls (`think`, `STATUS:` messages) have none of these fields.
- **`findings`** — at most 8, ≤ 120 characters: statements from the agent's own commentary while it
  works (`FINDING:` lines when it writes them), never its plans ("Let me…") or questions. Absent
  when it hasn't said anything learned.
- **`touched`** — de-duplicated, most recent last, at most 20. `kind` is `file`, `url` or
  `command`. A file's `detail` is `+added −removed` when the edit's line counts are known
  (a whole `patch` result, or `files.changed` from a coding backend), `edited` when it was written
  but the counts aren't known, else absent (read only). A `url` may carry an `https` link
  (no query string).
- **`hero`** — at most one, only when the data clearly supports it:
  `trials` (`total`, `passed` and/or `failed`) from a repeated-run count the run itself reported
  ("31/50 passed"); else `diff` (`files`, `added`, `removed`, optional `preview` ≤ 6 lines) from
  counted edits; else `sources` (`items[]` of `{title, site}`) when it read two or more pages and
  edited nothing; else `steps` once there are three or more activity items. `series` is reserved:
  this server doesn't derive it yet.

Everything is plain language built from the run's events on the server: secrets, tokens, keys,
environment values, emails and query strings are removed and paths are shortened to their file
name. Updates are saved at most once a second per task (immediately when the run ends) and kept with
the task, so reopening the app shows the same feed.

## Helpers a task started

Capability `job_subagents`. When a task's Hermes run hands part of its work to helpers (Hermes
`delegate_task` subagents), its work object carries `subagents`, oldest first. The field is missing when
the task started none; hide the section then. A helper never changes a task's status or where it ran.

```json
"subagents": [
  {"id": "sa-0-1a2b3c4d", "title": "Compare the shops' return policies", "status": "done",
   "goal": "Compare the return policies of the three shops…", "started_at": 1793890304.7,
   "ended_at": 1793890391.2, "summary": "Shop B has the longest return window (60 days).",
   "tool_count": 9, "steps_total": 9},
  {"id": "sa-1-5e6f7a8b", "title": "Find the cheapest parcel shipping", "status": "running",
   "started_at": 1793890304.8, "now": "Searched the web for “parcel rates”", "steps_total": 4},
  {"id": "s3", "title": "Check island delivery", "status": "failed", "summary": "The page did not load.",
   "note": "…", "depth": 2, "parent_id": "sa-0-1a2b3c4d"}
]
```

- `id` is stable for the task's life. `title` is a 4–8 word name (a word-cut of the goal at once, then the
  task titler's name, same rules as a task's). `status` is `running`, `done`, `failed` or `stopped`.
- `now` (running only) is its latest step in plain words. `summary` is its answer (≤ 500 characters,
  redacted) once it ends; `note` says why it stopped without one ("Stopped with the task",
  "It ended without an answer", "No word back from this helper").
- `depth`/`parent_id` mark a helper started by another helper. `goal`, `model`, `tool_count`,
  `started_at`/`ended_at` (epoch seconds) are each present only when known.
- `GET /voice/work/{run_id}` adds each helper's own `activity` (its steps, the same item shape as the
  task's `activity`, with `tool`/`args`/`result` previews) and `touched`. The live stream (`work.list`,
  `state` events) carries the rows without steps, so it stays small: open the task to get them.

Where it comes from: the run's `subagent.start`/`subagent.complete` events, and each helper's own
session transcript in Hermes (read-only, about once a second) for its steps, and for finding helpers of a
task that has no event stream (a chat-thread task). A helper that never reports is settled from its
transcript once its session closes, or marked `stopped` when the task ended and it has been quiet 15 minutes.
A running helper stops with a task that was stopped or failed.

## Card actions

Capability `card_actions`. Cards (entries of `result.views`) may carry buttons:
`"actions": [{"id": "toggle", "label": "Turn off", "style": "primary|secondary|destructive", "confirm": false}]`
(at most four; an action with `"value": "brightness"` also takes a 0–100 value, shown as a slider).
Apps draw every action they get; the server decides what an id does and refuses ids it doesn't know.

- `POST /voice/interactions/{id}/card-action` during a call: `{"run_id": "...", "view": 0, "action": "toggle", "value": 40}`
- `POST /voice/work/{run_id}/card-action` with no call open (board browsing): the same body without `run_id`.

`view` is the card's index in `result.views`; `value` is optional. `200 {"ok": true, "message":
"Office Lamp is off."}` (a short line for a toast); the task's card is updated in place (new state
and label) and the change is published to open calls. Errors: `400` bad body or an action the card
doesn't have, `404` unknown run or card, `409 {"error": "..."}` when the action couldn't be done
(Home Assistant unreachable, home control off, a bad value).

Implemented actions: `toggle` on `home` cards. An instant home command's task shows one `home` card
per light, switch or fan it moved (up to four), each with a `toggle` button that flips it (with a
`value`, sets a light's brightness; 0 turns it off). Client-only actions (open a place in Maps, open
a link, copy, chart scrubbing) need no server call.

## Messages to a task

Capability `task_messages` (plugin 0.3.19+). The app sends typed words to **one task, by its id**:
from the task's reply bar or a decision card's "Something else…" field. These never go through task
routing, and live speech routing is unchanged. (Typing to the call itself, `input-text` below the
`input_text` flag, is different: it counts exactly like saying it and goes through the same routing as
speech. One box per screen, labelled with who gets it: "Message <assistant>" on the call screen,
"Message this job" on a job page, "Something else…" on a decision.)

### POST /voice/work/{run_id}/message

Body `{"text": "...", "source": "typed", "client_id": "<uuid>", "interaction_id":
"<optional, the open call>", "choice_task_id": "<optional, the decision card this answers>"}`.
`text` is trimmed, 1–4,000 characters; `source` defaults to `typed`. Works with or without a call.

- A running task gets it as an added instruction (`delivered_as: "steer"`).
- A task waiting on the user gets it as the answer (`"answer"`): its question card, or the approval
  it is waiting on (a plain yes/no answers it; anything else declines it and becomes the instruction).
  With `choice_task_id` naming a spoken-choice card, it is the user's answer to the voice's question.
- A finished or failed task gets a follow-up task in the same job, continuing its context
  (`"follow_up"`, `run_id` is the new task's). That `run_id` always opens it with
  `GET /voice/work/{run_id}`, while it runs and after: when Hermes refuses the run and the task goes
  into a chat thread instead, or it has no run id yet, the id handed out keeps resolving to the task
  (which then settles under its own `lr_` id, as in the task feed). A message to a task a follow-up
  already replaced goes to the newest task of that job.
- Idempotent on `client_id`: the same id again within 10 minutes gets the same response and nothing
  is sent twice (a send that failed may be retried with the same id).
- While a call is open, the message is added to the call as a `transcript` event `{"kind": "text",
  "role": "user", "source", "text", "task_id", "run_id", "at"}` (it is in the call's transcript as a
  typed turn, never spoken), and the voice gets a silent note. It never reads the message back: at
  most a few words ("Sent."), nothing while the user is muted.

`200 {"ok": true, "run_id": "run_0002", "delivered_as": "steer" | "answer" | "follow_up", "task": {...}}`
(`task` is the updated task, as in `/voice/work/{run_id}`). Errors carry one plain sentence in
`error`, fit to show: `400` empty or too long, `404 "I couldn't find that task."`, `409` the task
was cancelled or deleted ("That task was cancelled, so it can't take messages. Start a new one
instead."), `502` Hermes didn't take it ("I couldn't get that to the task. Try again.").

### POST /voice/work

Capability `start_jobs`. A new job from typed text with **no call**: the plugin-update notice's
**Update** button sends the update request this way. Body `{"text": "...", "source": "typed",
"client_id": "<uuid>"}` (`text` 1–4,000 characters). It runs like a job started on a call (its own
Hermes session, on the board, openable by `run_id`), never through routing. Idempotent on
`client_id` as above. `202 {"ok": true, "run_id": "run_0002", "task_id": "job_…", "task": {...}}`;
`400` for an empty, too long or malformed body, `502` when Hermes didn't take it.

While such a job runs, the server counts as busy: a newly installed plugin version loads only after
it (and any call) has finished, exactly like a job started on a call.

### Attachments on typed messages and new jobs

Capability `attachments` (plugin 0.3.33+). Both bodies above also take `"attachments": ["att_…", …]`:
up to 3 ids this device staged with `POST /voice/attachments` (below). With attachments, `text` may be
empty (it becomes "Take a look at what I attached."). They take the same path as the Mac's panel
attachments: pictures go to Hermes as image parts of the run (or, when its model doesn't read images
or the task is running and gets a steer, by their saved copy's path), files by their saved copy's
path; the task lists them under `shared` and its images come from `/voice/shared-image/{run_id}/{n}`.
Never a screen capture. All or nothing: an unknown id (sent, removed, expired, or another device's) is
`404 {"reason": "unknown"}` and nothing is sent; too many images for one request together is
`409 {"reason": "too_large"}`; `400` for a malformed list. A task waiting on an approval, or a spoken
choice, takes words only (`409 {"reason": "not_now"}`). A send that fails gives the ids back, so a retry
with the same ids (and `client_id`) works. The `200`/`202` reply adds
`"attachments": [{"id", "kind", "name"}]`.

### POST /voice/attachments

Raw bytes, the same headers as the call upload: `Content-Type`, `X-Speakeasy-Kind: picture | file`,
`X-Speakeasy-Filename` (percent-encoded, required for files), `Content-Length`. Authenticated and
size-checked before any of the body is read.

- **Pictures**: `image/jpeg` or `image/png`, at most 2.5 MB (the app downscales to 2048 px and
  re-encodes HEIC as JPEG first). The bytes must match the type. The server strips JPEG APP1 (Exif,
  XMP), APP13 and comment segments and PNG `eXIf`/`tEXt`/`zTXt`/`iTXt` chunks, so no location
  survives even if the app missed it; held in memory.
- **Files**: `application/pdf` (must start `%PDF-`), `text/plain`, `text/markdown`, `text/csv`,
  `application/json` (UTF-8, no NUL bytes), at most 10 MB; saved at once under
  `<HERMES_HOME>/cache/speakeasy/shared/<sha256[:32]>/<safe name>` (0600), like the Mac's files.
- Up to 6 waiting per device; one device never sees, sends or removes another's. What isn't sent
  within 30 minutes is dropped (a saved file's copy too). Sent copies are kept with their task and
  deleted with it, or after a week.

`200 {"id": "att_…", "kind": "picture" | "file", "name", "size", "expires_in": 1800}`. Errors: `400`
bad headers or empty, `401`, `409 {"reason": "too_many"}`, `413 {"reason": "too_large"}`,
`415 {"reason": "unsupported"}` (HEIC, other types, bytes that aren't what they say). Logs carry kind
and size only, never bytes or names. `DELETE /voice/attachments/{id}` → `200 {"id", "removed": true}`,
`404` once sent, removed or expired.

### Where a spoken follow-up went

When routing adds something the user said to an existing task (not a new one), the voice starts its
next line with "Adding that to *task name*." Right after, "no, the dashboard one", "wrong task" or
"not that one" moves what was added to the task they name (or the voice asks "Which one?"): the
wrong task is told to ignore it (or the follow-up it started is stopped), and nothing is started twice.

## History

Capability `history` (added after plugin 0.3.32). Speakeasy as a full Hermes client: the apps list every Hermes
session (Speakeasy's own tasks, Discord and Telegram threads, the terminal, Desktop, scheduled runs on
request), open any of them and write to it, typed or as a voice message, with or without a call.
Read from `<HERMES_HOME>/state.db`, read-only. Everything sent from a session page goes to **that
session, by id**; it never goes through the call router, and live call speech routing is unchanged.
Replies always show in Speakeasy. **Nothing is ever posted into Discord, Telegram or any chat
platform** from these routes.

### GET /voice/history

Query: `cursor` (from `next_cursor`), `q` (search), `source` (one of the `filters` ids), `limit`
(1–100, default 40). Newest activity first. Internal sessions are never listed (helpers/subagents,
tool and one-shot sessions, hidden or archived ones); a conversation Hermes compressed into several
segments is one row (its newest segment's id). Scheduled (`cron`) sessions only with
`source=scheduled`. `q` searches message text through Hermes' full-text index (`messages_fts`) and
titles and chat names; `search` says which ran (`fts`, or `like` when the index is missing).

```json
{"sessions": [
  {"id": "20261104_093012_ab12cd", "title": "Shelf brackets", "source": "discord",
   "source_label": "Discord · #build", "last_activity": 1793891284.09,
   "preview": "Three brackets at 12 in spacing will hold it.", "state": "idle",
   "match": "size the walnut shelf brackets"},
  {"id": "speakeasy_task_2a85e2b31d574f068443206e", "title": "Bake sale flyer", "source": "speakeasy",
   "source_label": "Speakeasy", "last_activity": 1793890001.2, "preview": "Flyer drafted.",
   "state": "running", "run_ids": ["run_0002"]}],
 "next_cursor": "1793890001.2|speakeasy_task_2a85e2b31d574f068443206e",
 "filters": [{"id": "speakeasy", "label": "Speakeasy"}, {"id": "discord", "label": "Discord"}, "..."],
 "sources": [{"source": "speakeasy", "count": 12}, {"source": "discord", "count": 3}],
 "search": null}
```

- `sources`: the `filters` ids the person actually has listable sessions under, with how many, in
  `filters` order; ids with none are left out. Counted over every visible session, not just this
  page, and regardless of `q` and `source`, so it's the same on every page. The apps offer only
  these as source filters and hide the filter row when there's one source (added after plugin 0.3.33;
  older plugins don't send it and the apps fall back to the sources of the rows they've loaded).

- `source`: `speakeasy` for a Hermes API session one of Speakeasy's tasks ran in, else Hermes'
  source (`discord`, `telegram`, `cli`, `gui`, `cron`, …). `source_label` is ready to show
  (`Discord · #channel` when the chat name has one).
- `state`: `running` (a Speakeasy task of it, or one linked to it, is under way, or another surface's
  turn is in progress), `needs_you` (a Speakeasy task waits on an approval or its question), else `idle`.
- `run_ids`: the session's Speakeasy tasks, newest first (open with `GET /voice/work/{run_id}`).
- `match`: a short snippet of the matching message (searches only).
- `400` for an unknown `source`, a bad `cursor` or `limit`.

### GET /voice/history/{session_id}

The conversation, oldest first, in pages of 300 rows (`has_more`; pass `before` back to page up).

```json
{"session": {"id": "...", "title": "Shelf brackets", "source": "discord", "source_label": "Discord · #build",
  "state": "idle", "send_via": "linked",
  "entries": [
    {"kind": "user", "id": "m101", "at": 1793891100.0, "text": "Can you size the walnut shelf brackets?"},
    {"kind": "tools", "id": "t102", "items": [{"n": 1, "text": "Ran a command", "tool": "terminal",
       "status": "done", "args": "…", "result": "…", "started_at": 1793891101.0, "ended_at": 1793891103.2}]},
    {"kind": "note", "id": "m104", "at": 1793891150.0, "text": "A helper reported back"},
    {"kind": "assistant", "id": "m105", "at": 1793891284.1, "text": "Three brackets at 12 in spacing will hold it."}],
  "has_more": false, "before": null,
  "jobs": [], "approvals": []}}
```

- `entries[].kind`: `user` (the person's words; for a Speakeasy task, its request, not the task
  brief), `assistant`, `note` (a system row shown as a short label), `tools` (the calls made between
  two messages; `items` are exactly the job tool log's items, `GET /voice/work/{run_id}` with
  `activity_detail`, redacted the same way).
- A task (`GET /voice/work/{run_id}`, the event stream's tasks) carries `session_id`, the Hermes
  session its conversation is in, so a job page can open "Show full session".
- `jobs`: the session's Speakeasy tasks (up to 8, newest first), each as `GET /voice/work/{run_id}`'s
  `work`, with its approval and question cards (`result.question`, `status:
  "waiting_for_approval"`), helpers and tool log. Answer them with the existing routes
  (`POST /voice/work/{run_id}/message`, `/card-action`, `/voice/interactions/{id}/approval`).
- `approvals`: approvals the session's Speakeasy tasks wait on right now
  (`[{run_id, task_id, description, choices}]`, `choices` from `once`, `session`, `deny`; never
  `always`). Answer with `POST /voice/history/{session_id}/approval`.
- A chat continued from Speakeasy (see below) shows, after its own messages, a note
  `"Continued in Speakeasy · not posted to Discord"` and the linked tasks' messages; their jobs lead
  `jobs`.
- `send_via`: which door `POST …/message` will use: `task`, `session_job` or `linked`.
- `404 "I couldn't find that conversation."`

### POST /voice/history/{session_id}/message

Body `{"text": "...", "source": "typed" | "voice_message", "client_id": "<uuid>", "attachments"?: [...]}` (`text` trimmed,
1–4,000 characters; `voice_message` is text the app transcribed on the device from a recorded voice
message, no audio is sent). Idempotent on `client_id` (10 minutes), like task messages. The session
named gets it, by id:

- **A Speakeasy task's session** (`via: "task"`): exactly `POST /voice/work/{run_id}/message` to its
  newest task: a steer while it runs, the answer while it waits, else a follow-up run in the same
  session. `run_id` opens it like any job.
- **Another Hermes API session** (`via: "session_job"`): a new Speakeasy job that runs in that same
  session (`/v1/runs` with its `session_id`). `run_id` opens it.
- **Any other session** — a Discord or Telegram thread, the terminal, Desktop (`via: "linked"`):
  Hermes has no door that writes a turn into such a session without delivering the reply to its
  chat: `/v1/runs` refuses a session another surface owns, `/api/sessions/{id}/chat/stream` fans
  the reply out to the platform, and the gateway socket's viewer submit (`session.resume` +
  `prompt.submit`, what Hermes Desktop uses) runs the turn on the chat's own route, which delivers
  it there too (`gateway/session_ingress.py`). Speakeasy never posts into a chat, so the first
  message starts a Speakeasy task **linked** to the conversation, seeded with its recent messages
  and the session id (the task can look up more). Later messages from that page go to the same
  linked task (steer, answer or follow-up in its own session). `run_id` opens it like any job.

`200 {"ok": true, "session_id": "...", "via": "task" | "session_job" | "linked", "run_id": "run_0002",
"delivered_as": "steer" | "answer" | "follow_up" | "linked", "task": {...} | null}`.
`400` empty, too long or malformed; `404` unknown session; `409`/`502` as for task messages.

With capability `attachments`, the body also takes `"attachments": ["att_…", …]` exactly as task
messages and new jobs do ("Attachments on typed messages and new jobs"): up to 3 ids this device
staged, claimed all or nothing, given back if the send fails, `text` may then be empty, and the reply
adds `"attachments": [{"id", "kind", "name"}]`. They go with whichever path the session takes (the
task, the job in that API session, or the linked task); nothing is ever posted to a chat.

A **new chat** (the panel's New chat button) is `POST /voice/work`: the first message starts a new
job in its own session, which then appears in the history like any other.

### POST /voice/history/{session_id}/approval

Body `{"run_id": "run_0002", "choice": "once" | "session" | "deny"}`: answers the approval that task (of
this session) waits on, through the worker holding it (the open call's, or the voiceless one), the same
way the voice does. `200 {"ok": true, "choice": "once"}`; `400` bad choice or not offered, `404` not a
task of this session, `409` nothing waiting, `502` Hermes refused.

## Models

With capability `model_choice`, each chat can run on its own model and reasoning level. The list is
Hermes' own (`GET /api/model/options`, the inventory its pickers show: signed-in providers only), and a
run's choice goes to Hermes as its per-request overrides on `POST /v1/runs`: `provider` + `model`, and
`model_options.reasoning` `{"enabled": true, "effort": "high"}` (`{"enabled": false}` for `none`).
Nothing is chosen = nothing is sent: Hermes' configuration runs it. Speakeasy never edits Hermes'
config and the model choice never changes how requests are routed.

What a run uses, model and level resolved separately (first that applies):

1. a spoken or typed "use the smart/fast model for this" in the request: that task (and its follow-ups);
2. the job's own pick (`model`/`reasoning` on `POST /voice/work`);
3. its chat's pick (`PUT /voice/history/{id}/model`, or a message that carries `model`/`reasoning`). A
   linked task (a chat Speakeasy can't write into) and a follow-up use their chat's pick too;
4. Speakeasy's default (settings `jobs.model` / `jobs.reasoning`);
5. Hermes' own configuration.

A level the model doesn't take is clamped to the nearest weaker one it does (never more cost).

### GET /voice/models[?refresh=1]

Cached for ten minutes (`refresh=1` re-reads Hermes). Shape:

```json
{"available": true,
 "providers": [{"id": "openai-codex", "name": "OpenAI Codex", "current": true, "local": false, "more": 12,
                "models": [{"id": "openai-codex/gpt-5.5", "model": "gpt-5.5", "provider": "openai-codex",
                            "provider_name": "OpenAI Codex", "name": "GPT-5.5", "default": true,
                            "hermes_default": true, "reasoning_levels": ["low", "medium", "high", "xhigh"]}]}],
 "default": {"model": "openai-codex/gpt-5.5", "reasoning": "medium", "source": "hermes" | "speakeasy"},
 "hermes_default": {"model": "openai-codex/gpt-5.5", "reasoning": "medium"},
 "fast": {"model": "openai-codex/gpt-5.5-mini", "reasoning": "low"},
 "smart": {"model": "openai-codex/gpt-5.5", "reasoning": "high"},
 "levels": [{"id": "none", "label": "Off", "short": "Off"}, ...]}
```

`id` ("provider/model") is what a choice names. `reasoning_levels` are the levels that model takes
(from Hermes' per-provider tables; `[]` = it doesn't reason: hide the control). Each provider lists its
featured (else first) six models plus any in use; `more` counts the rest. `available: false` (Hermes'
list couldn't be read): hide the chip.

### PUT /voice/history/{session_id}/model

Body `{"model"?: "<id>" | "default", "reasoning"?: "<level>" | "default"}`: Use for this chat. `default`
(or null) clears that half; a half left out stays as it was; switching model keeps the chat's level
only as far as the new model takes it. `200 {"ok": true, "session_id", "model", "next_model",
"model_choice"}`; `400` unknown model, a level the model doesn't take, bad body; `404` unknown session.

`POST /voice/work` and `POST /voice/history/{id}/message` take the same optional `model` / `reasoning`.
On `/voice/work` (New chat) the pick runs the job and becomes the new chat's own; on a history message
it is saved for the chat first, as with `PUT`.

### What ran

Tasks (`/voice/work/latest`, `/voice/work/{id}`, a session page's `jobs`) carry `"model": {"id", "name",
"provider", "provider_name", "reasoning", "reasoning_label", "source": "voice" | "task" | "chat" |
"default" | "hermes", "served": bool, "preset"?: "fast" | "smart"}`. `served` = Hermes reported the
run's runtime (`runtime` on its `run.*` events): the model that actually answered, which differs from
the request after a provider fallback. `GET /voice/history/{id}` adds `"model"` (what did the chat's
latest work; for a chat Speakeasy never ran, the model Hermes recorded for it), `"next_model"` (what
the next message runs on) and `"model_choice"` (the chat's own pick, or null).

### Settings

`jobs`: `{"model": "", "reasoning": "", "fast_model": "", "fast_reasoning": "", "smart_model": "",
"smart_reasoning": ""}` ("" = default). `model`/`reasoning` are Speakeasy's default; `fast_*` / `smart_*`
are what "use the fast/smart model" picks (by default: a quick sibling of the default model at Low, and
the default model at High).
## Skills

`capabilities.slash_skills` (plugin 0.3.37+). Typing `/` at the start of a message box offers the
user's Hermes skills; a message that starts with `/<skill>` (or several stacked, `/a /b do this`) runs
that skill, the same way `/skill` works in Telegram or Discord.

### GET /voice/skills

`{"skills": [{"command": "soup-scaler", "name": "soup-scaler", "description": "Use when scaling a soup
recipe.", "category": "cooking"}]}` — every skill enabled for the API server, sorted by `command`
(what goes after the slash). Descriptions are one line, at most 160 characters. An older Hermes
without skill commands returns an empty list.

### Sending a skill

No new route: send `"/soup-scaler for twelve"` as the `text` of `POST /voice/work`,
`/voice/work/{run_id}/message` or `/voice/history/{session_id}/message`. The plugin builds the skill
block with Hermes' own `agent.skill_commands` (its full instructions plus the words after the command)
and adds it to the task's prompt (or to the steer, for a running task). The session page shows only
what was typed. A `/word` that isn't an enabled skill is sent as plain text.

## Schedules

Capability `schedules` (added after plugin 0.3.33). The Hermes scheduled jobs (cron) of this profile,
seen and managed from the apps. Everything goes through Hermes' own cron API in the profile's
`HERMES_HOME`: the job store (`cron.jobs`), the run ledger (`cron.executions`), each run's session
(`state.db`) and its output document; writes go through the same `cronjob` entry point as
`hermes cron` and the agent's cron tool (prompt safety scan, schedule parsing). On a call, "every
morning at 8 remind me…" still goes to Hermes as a request; call routing is unchanged.

### GET /voice/schedules

Running and active first (soonest next run), then paused, then finished one-time jobs.

{"schedules": [
  {"id": "a1b2c3d4e5f6", "name": "Morning garden check", "schedule_label": "Weekdays at 8:00 AM",
   "kind": "cron", "next_run_at": "2026-11-06T08:00:00-05:00", "last_run_at": "2026-11-05T08:00:41-05:00",
   "last_status": "ok", "last_result": "Light frost tonight. Cover the basil.", "delivery_problem": null,
   "enabled": true, "state": "scheduled", "running": false, "delivery_label": "Telegram"}],
 "timezone": "America/New_York"}

- `schedule_label`: plain English ("Every day at 7:30 AM", "Every 2 hours", "Mondays and Thursdays at
  9:00 AM", "Monthly on the 1st at 9:00 AM", "Once, tomorrow at 9:00 AM"); an expression too unusual
  to say simply is `Custom schedule (<expr>)`.
- `last_status`: `ok`, `failed`, `cancelled`, `running`, or null (never ran). `last_result` is a short
  plain-text line (the run's answer; "Nothing to report." for a silent run; the error for a failure).
- `state`: `scheduled`, `paused`, `running`, `completed` (a one-time job that ran) or `error`.
  `next_run_at` is null while paused or completed. `enabled` false: show it dimmed.
- `delivery_label`: where results go ("Telegram", "Discord · #garden", "Saved only").

### GET /voice/schedules/{id}

The row above plus `prompt`, `skills`, `schedule_text` (the schedule as Hermes stores it, for the
edit field), `script_only`, `created_at` and `runs` (newest first, up to 15):

{"runs": [{"id": "9f8e…", "started_at": "2026-11-05T08:00:02-05:00", "finished_at": "2026-11-05T08:00:41-05:00",
           "status": "ok", "result": "Light frost tonight. Cover the basil.", "error": null,
           "session_id": "cron_a1b2c3d4e5f6_20261105_080002"}]}

`session_id` opens the run in the session page (`GET /voice/history/{session_id}`); null for script
jobs, which have no session. `404` for an unknown id.

### POST /voice/schedules/preview

Saves nothing. Either `{"text": "every weekday at 8 summarize my inbox"}` (one sentence: when and
what) or `{"schedule": "weekdays at 9", "prompt": "…"}`. `schedule` takes words ("weekdays at 9",
"every 2 hours", "tomorrow at 9") or Hermes' own forms (`0 9 * * 1-5`, `every 45m`, an ISO time).

{"schedule": "0 8 * * 1-5", "schedule_label": "Weekdays at 8:00 AM", "kind": "cron",
 "prompt": "Summarize my inbox", "name": "Summarize my inbox",
 "next_run_at": "2026-11-06T08:00:00-05:00", "next_runs": ["…", "…", "…"], "assumed": null,
 "timezone": "America/New_York", "deliver": "local", "delivery_label": "Saved only",
 "delivery_targets": [{"id": "local", "label": "Saved only"}, {"id": "telegram", "label": "Telegram"}]}

`assumed` names a part that was filled in (`"9:00 AM"` for "every weekday summarize my inbox"), so
the preview can say so. A bare hour reads the way people say it (7–11 morning, 12 noon, 1–6
afternoon, "evening at 7" 7 PM); the preview shows the result before anything is saved. `400` with
a plain sentence when the when or the what is missing, or a one-time is in the past.

### POST /voice/schedules

`{"schedule", "prompt", "name"?, "deliver"?, "confirmed": true}`, from a confirmed preview →
`201` with the new schedule's detail. `confirmed` is required (`400` without it). `deliver` must be
one of the preview's `delivery_targets` (default `local`). Hermes' prompt scan refuses prompts that
try to change its instructions or reach secrets (`400`).

### POST /voice/schedules/{id}/pause, /resume, /run

`200` with the detail. **Run now** queues one run: the gateway's scheduler fires it on its next tick
(within a minute) with its normal delivery; the reply carries `queued: true`, and `resumed: true` when
the job was paused (Hermes' rule: Run now resumes a paused job). `409` while it's running or for a
one-time job that already ran.

### PATCH /voice/schedules/{id}

Any of `{"schedule", "prompt", "name"}` → `200` with the detail. `schedule` takes the same words or
forms as the preview. `400` for an unreadable schedule or nothing to change.

### DELETE /voice/schedules/{id}

`200 {"ok": true, "id"}`. Apps confirm first.

## POST /voice/tasks/dismiss

Hides finished tasks from the list. Only terminal runs are dismissed; the rest are ignored.
```json
{"run_ids": ["run_0001"]}
```
`200`
```json
{
  "dismissed": [
    "run_0001"
  ]
}
```

## GET /voice/card-image/{run_id}/{n}

`n` is 1-8, the position among that task's image cards. Returns the image bytes with its
`Content-Type` (`image/png`, `image/jpeg`, `image/gif`, `image/webp`). Local images come from
Hermes `MEDIA:` output and are served only when they live under `image_roots` (default: the
Hermes home), are regular files (no symlinks) and are at most 8 MB. Remote product images are
fetched over HTTPS only, with no redirects to private addresses. 404 otherwise.

## GET /voice/shared-image/{run_id}/{n}

`n` is 1-12, the position in that task's `shared` list (a task keeps its first 12, so positions never shift) (screens and pictures; a file there is 404).
Returns the bytes Speakeasy kept of an image the user shared with the task, from the speakeasy
`shared` folder, with its `Content-Type`. Tasks list what they carried as
`"shared": [{"kind": "screen" | "picture" | "file", "app"?, "name"?}]` (only when non-empty;
never a path).

## Look at this: screen, pictures and files

During a call the user can let Hermes see their screen, or drop or paste pictures and files onto the
panel. Everything goes from the app to this server to the user's own Hermes, never to the voice
model, the routing and naming models, quick-answer search, chat notices or logs.

- **Screen sharing** is per call and off at the start of every call and resume. While it's on, each
  new Hermes run started from speech (a new task, a follow-up that starts a new run, a continued DM
  chat) carries one capture of the frontmost window, taken for that request.
- **The server asks for each capture.** When a request heads for Hermes it publishes a `capture`
  event (and lists the request under the snapshot's `captures`). The app first posts `capturing`;
  only on `200` does it capture and upload. A closed request answers `410`, so a late or replayed
  event never captures anything. The server waits up to 2.5 s for the upload (6 s once the app
  reports `uploading`), then the work goes ahead with words only and the voice says why.
- **Pictures and files** wait on the panel (up to 3) and go with the next Hermes request, whether or
  not sharing is on. Pictures go to Hermes as images; other files are saved on the Hermes machine and
  named in the task so Hermes opens them with its own tools.
- **Where they may go:** never into a group chat's Hermes session, a chat thread, or a channel the
  user didn't name. A request that carries them is answered home (or in a channel the user named).
  A capture that only rides along because sharing is on is dropped at such destinations instead.

Limits: images are `image/jpeg` or `image/png`, at most 2.5 MB each, at most 4 per request and
6.5 MB in total (a capture that won't fit is left out, with a spoken reason); files at most 10 MB.

### POST /voice/interactions/{id}/screen

Body exactly `{"on": true | false, "seq": <int ≥ 0>}`. `seq` only goes up within a call; an older
one is ignored and the reply reports the state in force. Turning sharing off closes every open
capture request. `200 {"on", "seq"}`; `409` with `reason` `ended`, `permission` (the call declared
`no_permission`) or `not_declared` (the call never declared `screen`).

### POST /voice/interactions/{id}/captures/{capture_id}

Body `{"status": "capturing" | "uploading" | "failed", "reason"?}`. `capturing` asks whether the
request is still wanted (`200`, else `410 {"reason": "closed"}`); `uploading` extends the wait;
`failed` closes the request and needs one of `permission`, `no_window`, `speakeasy_window`,
`password_manager`, `secure_input`, `blank`, `too_large`, `timeout`, `unsupported`.

### POST /voice/interactions/{id}/attachments

Raw bytes, metadata in headers. `Content-Length` is required (no chunked bodies) and checked before
any of the body is read.

| Header | |
|---|---|
| `X-Speakeasy-Kind` | `screen`, `picture` or `file` |
| `Content-Type` | `image/jpeg` or `image/png` for a screen or picture; anything for a file |
| `X-Speakeasy-Capture-Id` | required for `screen`, not allowed otherwise |
| `X-Speakeasy-Filename` | required for `file`; percent-encoded UTF-8 |
| `X-Speakeasy-App` | optional: the captured app's name |

`200 {"id", "kind"}`; uploading the same capture again returns the same id. Errors: `400` bad
headers, `404` unknown call, `408 {"reason": "timeout"}` the body stalled for 15 s or took longer than
its size allows (a minute up to 1 MB, two minutes above, 15 s per MB for the largest, at most three),
`409` with `reason` `ended`, `too_many` or
`too_large` (no room left in the request), `410 {"reason": "closed"}` the capture request is no
longer open, `413 {"reason": "too_large"}`, `415 {"reason": "unsupported"}` (image bytes that
aren't the declared type).

### DELETE /voice/interactions/{id}/attachments/{attachment_id}

Removes a pending picture or file. `200 {"id", "removed": true}`; `404` unknown; `409 {"reason":
"sent"}` once it went with a request.

## Email drafts

When a task writes an email for the user, Hermes is told **not** to send it and to end its answer
with a fenced `email-draft` block:

````
```email-draft
{
  "from": "me@example.com",
  "to": [
    "Pat Doe <pat@example.org>"
  ],
  "cc": [],
  "bcc": [],
  "subject": "Dinner on Friday",
  "body": "Hi Pat,\n\nStill on for Friday at 7?"
}
```
````

Optional fields: `reply_to_message_id`, `account`. The server strips the block from the spoken
and displayed text and validates it: `to` needs at least one address; every address must be a
plain `name@domain` or `Name <name@domain>`; at most 50 recipients per field; `subject` is one line of up to
300 characters; `body` is plain text (no HTML) of up to 20,000 characters; at most 3 drafts per
answer. Invalid blocks are dropped, never shown half-parsed.

Each valid draft gets a `draft_id` and a `sha256` of its canonical JSON (keys sorted, compact
separators, UTF-8). Drafts show on the task as `email_drafts` and in the SSE `email_drafts` event.

| status | meaning |
|---|---|
| `pending` | waiting for the user |
| `approved` | approved; the task's session was told to send it |
| `sent` | the session reported it sent |
| `failed` | the session could not send it (`error` says why) |
| `denied` | denied; the session was told to discard it |
| `revising` | changes requested; a new draft will replace it |
| `superseded` | replaced by a newer draft (hidden from lists; cannot be approved) |

The voice model is told a draft is waiting ("I drafted it — take a look and approve when ready").
A spoken "approve" or "send it" does **not** approve: only the card's Approve button does. Voice can
request changes ("revise it to ..."), which reach the same Hermes session as a follow-up and
produce a new draft.

### POST /voice/drafts/{draft_id}

```json
{"action": "approve", "sha256": "fa7dc978803802821de7277bd9ab00226c63b4c2a3b4113c829e8bcf7baf2dfa"}
```
```json
{"action": "deny", "sha256": "fa7dc978..."}
```
```json
{"action": "revise", "sha256": "fa7dc978...", "instructions": "Make it 8pm and add Alex on cc."}
```

- `sha256` must equal the hash of the draft the user saw, else **409**.
- `approve` continues the task's own Hermes session with a message that the user approved exactly
  this draft (the full draft JSON is included) and asks it to send it now with its email tool and
  report the result. `deny` tells the session not to send and to discard it. `revise` sends the
  instructions and asks for a new `email-draft` block; the new draft gets a new `draft_id` and the
  old one becomes `superseded`.
- Idempotent: repeating the decision already taken returns the current state and never sends twice.
  A different decision on a draft that is no longer `pending` is **409**.

`200`
```json
{
  "draft": {
    "draft_id": "ed_30b3b5f187309e79598c0daf",
    "bcc": [],
    "body": "Hi Pat,\n\nStill on for Friday at 7?",
    "cc": [],
    "from": "me@example.com",
    "subject": "Dinner on Friday",
    "to": [
      "Pat Doe <pat@example.org>"
    ],
    "sha256": "fa7dc978803802821de7277bd9ab00226c63b4c2a3b4113c829e8bcf7baf2dfa",
    "status": "approved",
    "created_at": 1790527324.037801,
    "updated_at": 1790527324.2317472
  }
}
```

Repeating the same approve after it was sent:

`200`
```json
{
  "draft": {
    "draft_id": "ed_30b3b5f187309e79598c0daf",
    "bcc": [],
    "body": "Hi Pat,\n\nStill on for Friday at 7?",
    "cc": [],
    "from": "me@example.com",
    "subject": "Dinner on Friday",
    "to": [
      "Pat Doe <pat@example.org>"
    ],
    "sha256": "fa7dc978803802821de7277bd9ab00226c63b4c2a3b4113c829e8bcf7baf2dfa",
    "status": "sent",
    "created_at": 1790527324.037801,
    "updated_at": 1790527324.2333078
  }
}
```

Wrong hash:

`409`
```json
{
  "error": "draft changed; review the current draft"
}
```

## Sign in to ChatGPT: /voice/codex/login

Capability `codex_login`. Voice through Codex needs the Codex program on the Hermes computer signed
in to ChatGPT, and that computer often has no screen. These routes run Codex's own device-code
login (`codex app-server` → `account/login/start {"type": "chatgptDeviceCode"}`, Codex 0.150.0+):
the user opens the link on any device, enters the code, and Codex saves the sign-in in its own
store. Speakeasy never sees, copies or logs a token and never touches Hermes' own sign-in.

Before any code is shown, the plugin looks for a working sign-in in **every Codex copy on the
machine** (`voice.codex_path`, PATH, Homebrew, npm, the ChatGPT app's bundled Codex, the older
Codex app, Speakeasy's own copy) and silently uses the first one signed in with ChatGPT (saved as
`voice.codex_path`). `GET /voice/status` reports the result as `codex_sign_in`: `chatgpt`,
`api_key` (Codex works but bills an OpenAI API key) or `none`. Apps show **Sign in to ChatGPT**
only for `none` (and offer a switch for `api_key`), never while a working sign-in exists.

**POST** `{}` or `{"switch_from_api_key": true}` (the body may be empty):

- already signed in with ChatGPT (any copy): `{"state": "signed_in"}`; no code.
- signed in with an API key, no `switch_from_api_key`: `409` with `reason: "api_key_signed_in"`.
- no Codex (or only copies older than 0.150.0): Codex is installed/updated the way
  `hermes voice setup --yes` does it, `{"state": "installing"}`; the flow then starts on its own.
- otherwise `{"state": "pending", "verification_url": "https://auth.openai.com/codex/device",
  "user_code": "ABCD-12345", "expires_at": 1767225600, "expires_in": 885}`. A second POST while a
  code is waiting returns the same code (one flow at a time). The URL is always https on an
  openai.com / chatgpt.com host; anything else fails the flow (`502`, `reason: "codex_login_failed"`).

**GET** reports `state`: `idle`, `installing`, `pending` (with the fields above), `signed_in`,
`expired`, `failed` or `cancelled`, plus `message`, `sign_in` and `codex_found`. Poll every ~2 s
while the sheet is open. **DELETE** cancels a waiting code (Codex's child process is stopped).
The code expires with Codex's own 15-minute limit; the child is stopped on expiry.

A call that can't start through Codex returns `502` with a `reason`: `codex_not_signed_in` (the
apps show **Sign in to ChatGPT** instead of Try again, when the server has `codex_login`) or
`codex_plan_no_voice` (the ChatGPT plan or workspace has no realtime voice: the apps offer Grok or
an API key in Settings › Voice instead of a redial).

## GET /voice/settings, PATCH /voice/settings

Stored in `<HERMES_HOME>/speakeasy/settings.json` (0600). Never holds secrets. PATCH takes a
partial object (nested objects merge); unknown keys or invalid values are 400.

| key | default | meaning |
|---|---|---|
| `assistant_name` | `"Hermes"` | what the user calls the assistant; used by the voice rules |
| `user_name` | `""` | how to address the user (optional) |
| `machine_description` | `"this Mac"` | how the rules describe where Hermes runs |
| `voice.provider` | `"codex"` | `codex` (ChatGPT sign-in) or `openai` (API key) |
| `voice.voice` | `""` | voice name; empty = provider default |
| `voice.codex_path` | `""` | path to `codex`; empty = find on PATH |
| `voice.max_call_minutes` | `30` | hard cap per call; `0` = no limit (a forgotten call keeps using the voice provider's minutes) |
| `speech.progress` | `true` | short spoken "still working" updates on long tasks while the call is quiet (finished work, questions and approvals are always spoken, even while muted) |
| `speech.interval_seconds` | `45` | how often those updates come: `30`, `45`, `60`, `90` or `120`. With the mic open (the app posted `/mic` `{"muted": false}`) a task's first line comes 12-20 s in, then one every interval; never-reported calls start at the interval and back off on a quiet task; muted calls get none |
| `idle_pause_minutes` | `15` | pause a silent call after this long; `0` = never. Tasks keep running and stay on the board; a new call starts with them |
| `instructions_extra` | `""` | extra voice instructions (up to 1,000 characters) |
| `hermes_profile` | `""` | Hermes profile for runs; empty = this gateway's |
| `delivery.target` | `"none"` | post finished results to a Hermes chat: `none`, `telegram`, `discord`, `discord:<chat_id>`, `telegram:<chat_id>[:<thread_id>]`, ... |
| `delivery.new_thread` | `false` | run each task in a new thread of the default target when `threads_supported` (older `new_thread_per_task` is accepted and migrated) |
| `delivery.channels` | `[]` | up to 8 opted-in channels: `{target, label, topic, new_thread}`. A new task goes to the channel named in the request ("put this in #work"), else the one whose `topic` fits (routing model), else `delivery.target`. `new_thread` runs the task in a new thread there (Discord, Telegram, Slack, Matrix; needs `threads_supported`), otherwise the answer is posted there. Follow-ups stay where their task runs |
| `continuity.enabled` | `true` | when a request is about something already being discussed in a Hermes chat or thread, continue inside that conversation (with its history) and post the reply there |
| `audio_duck.enabled` | `false` | lower the Mac's output volume while the assistant speaks, on calls whose app runs on the Mac running Hermes (never for an iPhone or another computer). Settings › Behavior in the Mac app, or `hermes voice config set audio_duck.enabled true`. macOS only; `SPEAKEASY_VOICE_DUCK=0` in the environment also turns it off |
| `audio_duck.level` | `30` | output volume (0-100) while ducked; a volume already at or below it, or muted, is left alone |
| `audio_duck.hold_s` | `4.0` | seconds without the assistant's speech (0.5-60) before the old volume comes back; it also comes back when the call ends. A volume the user changed while ducked is kept; if the output device changed while ducked, the old volume goes back on the device that was ducked (or the duck just ends if it's gone) |
| `server.advertised_url`, `server.tailscale_name` | `""` | set by `hermes voice setup`: the address other devices use (e.g. the tailnet URL) |
| `brief.auto_refresh` | `true` | daily background brief refresh when its inputs change |
| `brief.include_recent_voice` | `true` | include recent voice turns in a new call's context |
| `image_roots` | `[]` | extra directories image cards may come from (the Hermes home is always allowed) |
| `onboarding.names_set`, `onboarding.delivery_set` | `false` | first-run checklist flags |

`200`
```json
{
  "settings": {
    "assistant_name": "Hermes",
    "user_name": "",
    "machine_description": "this Mac",
    "voice": {
      "provider": "openai",
      "voice": "",
      "codex_path": "",
      "max_call_minutes": 30
    },
    "idle_pause_minutes": 15,
    "instructions_extra": "",
    "hermes_profile": "",
    "delivery": {
      "target": "none",
      "new_thread": false,
      "channels": []
    },
    "continuity": {
      "enabled": true
    },
    "brief": {
      "auto_refresh": true,
      "include_recent_voice": true
    },
    "image_roots": [],
    "onboarding": {
      "names_set": false,
      "delivery_set": false
    }
  }
}
```

```json
{"assistant_name": "Nova", "delivery": {"target": "telegram"}}
```
`200` returns the full settings, as for GET.

## GET /voice/brief, PUT /voice/brief, POST /voice/brief/rewrite

The voice brief is written by the user's own Hermes (see VOICE_PROMPT.md) and stored in
`<HERMES_HOME>/speakeasy/voice-brief.md`. `state` is `none`, `writing`, `ready`, `edited` or `failed`.

`200`
```json
{
  "brief": "",
  "state": "none",
  "updated_at": null,
  "edited": false,
  "auto_refresh": true,
  "error": null,
  "words": 0
}
```

PUT sets it by hand (`{"brief": "# User\n..."}`). It must be 200-12,000 characters, contain the
five sections (User, Assistant persona, Capability map, Answer preferences, Current context) and
no secrets or card-like numbers, else **422**. A manual edit turns auto-refresh off for it
(`state: "edited"`). `{"brief": ""}` deletes it.

POST `/voice/brief/rewrite` (body `{}`) asks Hermes to write a new brief in the background and
returns `202` with the status (`state: "writing"`) and the current `brief`. 409 if the Hermes API
is not configured. Poll GET or `/voice/status` → `brief_state`.


### Tune from my calls

`POST /voice/brief/tune` (body `{}`, 202) asks Hermes to read the last week of calls next to the brief
and propose at most 8 edits. `GET /voice/brief/tune` returns
`{"state": "none|working|ready|failed", "calls": N, "summary", "edits": [...], "product_issues": [...], "error"}`.
Each edit is `{"id", "kind": "add|change|remove", "section" (add), "old" (change/remove: an exact brief line),
"new" (add/change), "why", "evidence"}`; edits that don't match the brief or look like secrets are dropped
server-side. `POST /voice/brief/tune/apply` with `{"accept": [ids]}` applies only those edits (the brief is
then marked edited, so auto-refresh leaves it alone). Calls are kept locally in `call-log.jsonl` (owner-only,
14 days / 60 calls); their text goes to the model Hermes uses only when a tune is requested. Each entry
carries the paired device's id (`device`) and the app build it last reported (`client`, see
the client header above; absent for older apps).

## Push notifications

Notifications reach a paired iPhone or iPad while the app is closed, and **no words about the work
leave this Hermes**: the relay that wakes the phone gets only opaque ids. `capabilities.push` in
`GET /voice/status` says the plugin has this (0.3.33+).

```
Hermes plugin ──signed {device_token, environment, kind, nonce, collapse_id, event_id}──▶ relay (speakeasy-push.todd-562.workers.dev)
relay ──generic alert "Your Hermes has an update" + mutable-content + event id──▶ APNs ──▶ phone
phone's Notification Service Extension ──GET /voice/push/events/{id} (device token)──▶ this Hermes
                                        ◀── {"title": "Done: Speed up the dashboard", "body": …}
```

### POST /voice/push

```json
{"apns_token": "<hex>", "environment": "sandbox" | "production", "kinds": ["finished", "needs_you", "failed", "reply"]}
```

→ `200 {"ok": true, "enabled": true, "kinds": [...], "relay": "https://speakeasy-push.todd-562.workers.dev", "available": true}`.
`{"enabled": false}` turns notifications off and forgets the token. The token is kept in
`devices.json` next to the device (`push: {apns_token, environment, kinds, updated_at}`); revoking the
device drops it. `400` for a token that isn't hex, an unknown environment or kind.

### When Hermes notifies

Only when the app is **not** open on that device: no live `/events` stream from it and no request
from it in the last `push.presence_s` seconds (default 45). The extension's own fetch doesn't count.
Each event notifies a device once.

| kind | when | title |
|---|---|---|
| `finished` | a task completed | `Done: <task>` |
| `needs_you` | an approval is waiting, a result asks a question, or an email draft is ready | `Needs you: <task>` |
| `failed` | a task failed | `Failed: <task>` |
| `reply` | an answer landed in a Hermes conversation the task was sent to | `New reply: <task>` |

A user stopping a task, and a conversation turn still going elsewhere, notify nothing.

### GET /voice/push/events/{id}

`200 {"kind", "title", "body", "run_id", "thread_id"}` for the device the notification went to;
`404` for any other device, an unknown id, or after 6 hours (kept in memory only). Not a sign the app is open.

### The relay

`relay/push/` (Cloudflare Worker). The plugin registers once (`POST /v1/register` →
`{install_id, secret}`, saved to `speakeasy/push_install.json`, 0600) and signs every
`POST /v1/notify`: `X-Speakeasy-Install`, `X-Speakeasy-Timestamp`, and
`X-Speakeasy-Signature = hex HMAC-SHA256(secret, "v1\n<install>\n<timestamp>\n<sha256 hex of body>")`.
The relay refuses any body field beyond the six above, replays (nonce), timestamps off by more than
5 minutes, and rate-limits per install and per device token. Its reply `{ok, status, reason, invalid_token}`:
`invalid_token: true` (APNs said the token is dead) makes the plugin forget that token.

Settings: `push: {enabled: true, relay_url: "https://speakeasy-push.todd-562.workers.dev", presence_s: 45}`
(`relay_url` must be https, or http on 127.0.0.1/localhost for local testing).

## Secure input (keys, admin passwords, website logins)

Servers with `capabilities.secret_input`. When a Speakeasy job (call job, typed job, history
message, linked task) needs something only the user should type, the agent's turn waits on a
**secure box** instead of asking in the conversation:

| kind | Hermes asks through | the answer goes to |
| --- | --- | --- |
| `secret` | a skill's missing `required_environment_variables` (secret capture) | `save_env_value_secure` → the profile `.env` as `env_var` |
| `sudo` | a terminal command that needs `sudo` (sudo password prompt) | that one command's stdin; Speakeasy drops Hermes' cached copy after the tool call (allow once) |
| `login` | `browser_vault action=save_login` | Hermes' encrypted vault, bound to `origin`; filled model-blind |
| `code` | `browser_vault action=enter_code` | typed into the page once (a code entered on the login card answers the next ask for that site) |

Requests expire with Hermes' own deadlines (120 s key/sudo, 180 s login/code). Skip, expiry, or the
job stopping return Hermes' "skipped / declined" result. The job shows a "Waiting for you" milestone,
a `needs_you` push names the need (never a value), and a live call says one line ("I need a key to
continue — I've put a secure box on your screen") and is told never to ask for the value aloud.

### GET /voice/secrets

`{"requests": [...], "wired": {"secret": true, "sudo": true, "login": true}}`. Only requests this
device may answer (a call's job: the device that placed the call; a job with no call: any paired
device). Each request: `id` (`sec_…`), `kind`, `job`, `run_id`, `session_id`, `expires_at`,
`seconds_left`, `label`, `stored_where`, and per kind `env_var`/`prompt`/`help`/`skill` (secret),
`command` (sudo, credentials masked as Hermes' approval prompts mask them), `origin`/`site` (login),
`site`/`help` (code). A call's event feed also carries them as a `secure_inputs` event.
Never contains a value.

### POST /voice/secrets/{id}

Body: `{"value": "…"}`, or for `login` `{"identifier": "…", "password": "…", "code": "…"?}`, or
`{"action": "skip"}`. Reply: `{"ok": true, "outcome": "sent" | "skipped", "stored_as"?, "site"?}`.
Device-token auth; single use (a second answer is 404); 403 `wrong_device`; 404 `closed` once
answered, skipped or expired; 429 after 12 answers a minute from one device; 413 over 16 KB.
The body is read into a buffer that is zeroed after the handoff and is never parsed into a log
line, stored in `state.sqlite3`, published or echoed. Values entered are fingerprinted (keyed
hash, in memory) so any later Hermes output that echoes one is replaced with `[redacted]` before
Speakeasy stores, speaks, pushes or returns it.

## GET /voice/status

Readiness. `voice_ready` is true when the chosen provider can start a call. When Codex is missing or signed out, `codex_message` says what to do. Grok voice (beta): `grok_beta` (the setting), `grok_signed_in` (Hermes holds an xAI sign-in), `grok_api_key_set` (`XAI_API_KEY` fallback), `grok_ready` and `grok_message` (what to do, e.g. run `hermes auth add xai-oauth`); with provider `grok`, onboarding's `codex_message` carries the Grok message. `threads_supported` reports whether this Hermes can open a new thread per task (its webhook platform must be on; `threads_reason` says why not). `routing_model` names the model task routing uses (`auxiliary.speakeasy_router`), `routing_hint` how to change it. `advertised_url` / `tailscale_name` are the address setup advertised (empty = local only). `room_listening` (plugin 0.2.51+) is true when the server takes listening mode's `room` with `POST /voice/sessions`; missing on older plugins, which reject unknown session fields. `room_on_resume` is true when it also takes `room` with `resume_from`, which listening mode during a call needs (the app checks it before pausing the call for listening). `input_text` is true when the server takes a typed turn on a live call (`POST /voice/interactions/{id}/input-text`); missing on older plugins, which answer that route with 404, so apps show their composer only when it's true.
`attachments` says whether runs can carry images (`images`) and how Hermes reads them (`vision`:
`native` the main model sees them, `described` an auxiliary vision model describes them, `none`, or
`unknown` until the first check), plus the Look at this limits. Apps hide screen sharing and refuse
drops when it's missing, `images` is false or `vision` is `none`.
`capabilities` lists newer features this server supports: `card_actions`, `job_progress`,
`spoken_choices`, `summary_points`, `task_messages`, `input_text`, `job_activity`, `tool_log`, `job_subagents`, `history`, `secret_input`, `model_choice`, `schedules`, `task_stop` (each `true`). A missing object or flag means an older server:
hide the matching controls and keep the old behaviour.

`200`
```json
{
  "assistant_name": "Hermes",
  "user_name": "",
  "provider": "openai",
  "voice": "marin",
  "voice_ready": true,
  "codex_found": true,
  "codex_signed_in": true,
  "codex_message": "Signed in to Codex.",
  "api_key_set": true,
  "brief_state": "none",
  "hermes_api_ok": true,
  "hermes_api_key_set": true,
  "delivery_target": "none",
  "threads_supported": true,
  "threads_reason": "",
  "thread_platforms": ["discord", "matrix", "slack", "telegram"],
  "continuity_enabled": true,
  "devices": 1,
  "routing_model": "Hermes auxiliary default (auto)",
  "routing_hint": "Change it with `hermes model` → auxiliary tasks, or in your Hermes config under auxiliary → speakeasy_router.",
  "advertised_url": "",
  "tailscale_name": "",
  "attachments": {"images": true, "vision": "native", "max_attachments": 3, "max_image_bytes": 2500000,
                  "max_request_image_bytes": 6500000, "max_file_bytes": 10000000},
  "room_listening": true,
  "room_on_resume": true,
  "input_text": true,
  "version": "0.2.0",
  "capabilities": {"card_actions": true, "job_progress": true, "spoken_choices": true, "summary_points": true,
                   "task_messages": true, "input_text": true, "job_activity": true, "tool_log": true,
                   "job_subagents": true}
}
```

## GET /voice/destinations

Where results can be posted, read from the Hermes gateway state and config with no secrets:
connected platforms, their home channels and chats the gateway has seen. Each has a `target`
string ready for `delivery.target`.

`200`
```json
{
  "destinations": [],
  "none": {
    "target": "none",
    "name": "Don't post results anywhere"
  },
  "current": "none",
  "threads_supported": true,
  "threads_reason": "",
  "thread_platforms": ["discord", "matrix", "slack", "telegram"]
}
```

With platforms connected, each entry looks like:
```json
{"platform": "telegram", "connected": true, "state": "connected", "target": "telegram",
 "home_channel": {"name": "Home", "target": "telegram:123456"},
 "chats": [{"name": "Trip planning", "type": "group", "target": "telegram:-100222"}]}
```

## POST /voice/destinations/suggest

Body `{}`. Runs one read-only Hermes turn (up to 90 s) that sees only the chat labels and targets
from `/voice/destinations` and proposes up to 5 channels from what it knows about the user. Every
item is checked against those targets and the `delivery.channels` rules; anything else is dropped.
Nothing is saved: the app shows the list and the user picks.

`200`
```json
{"suggestions": [{"target": "discord:9000000000001", "label": "#general", "topic": "everyday questions", "new_thread": false}]}
```
`502` with a plain `error` when Hermes can't answer or suggests nothing usable.

## GET /voice/onboarding, POST /voice/onboarding

The first-run checklist. `complete` is true when the device is paired, voice is ready, and names and delivery are confirmed (the brief is optional).

`200`
```json
{
  "steps": {
    "paired": true,
    "codex_signed_in": true,
    "voice_ready": true,
    "names_set": false,
    "delivery_set": false,
    "brief_ready": false
  },
  "complete": false,
  "brief_state": "none",
  "codex_message": "Signed in to Codex.",
  "assistant_name": "Hermes",
  "user_name": "",
  "delivery_target": "none",
  "continuity_enabled": true,
  "advertised_url": "",
  "tailscale_name": ""
}
```

POST confirms any of `assistant_name`, `user_name`, `delivery_target`, `continuity_enabled`, and can start the first
brief write with `"write_brief": true`. It returns the checklist plus the updated `settings`.
```json
{"assistant_name": "Nova", "user_name": "Sam", "delivery_target": "none"}
```
`200`
```json
{
  "steps": {
    "paired": true,
    "codex_signed_in": true,
    "voice_ready": true,
    "names_set": true,
    "delivery_set": true,
    "brief_ready": false
  },
  "complete": true,
  "brief_state": "none",
  "codex_message": "Signed in to Codex.",
  "assistant_name": "Nova",
  "user_name": "Sam",
  "delivery_target": "none"
}
```

(`settings` omitted above.)
