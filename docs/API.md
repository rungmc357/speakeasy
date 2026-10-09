# Speakeasy server API

The Speakeasy server runs inside the Hermes gateway as the `voice` platform plugin. It binds
`127.0.0.1` only (default port `8795`, set by `gateway.platforms.voice.extra.port`). To reach it
from another machine, put a TLS proxy you trust in front of it.

- JSON in and out (`Content-Type: application/json`). Request bodies are at most 128 KB.
- **Auth:** every route except `GET /health` and `POST /voice/pair` needs
  `Authorization: Bearer <device token>`. Tokens come from pairing; only their SHA-256 is stored
  (`<HERMES_HOME>/speakeasy/devices.json`, 0600). A missing, wrong or revoked token gets `401 {"error": "unauthorized"}`.
- **Errors:** `{"error": "<short reason>"}` with 400 (bad body), 401, 403 (pairing), 404, 409
  (stale/conflicting state), 422 (brief rejected), 502 (Hermes or the voice provider failed).
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
| POST | `/voice/interactions/{id}/skip-tour` | device | End the first-call tour |
| POST | `/voice/interactions/{id}/early-request` | device | Words heard while the call connected: the first request (or, in a call from listening mode, "answer from the room") |
| POST | `/voice/interactions/{id}/answer` | device | Answer a task's question card |
| POST | `/voice/interactions/{id}/mic-check` | device | Log a mic repair (numbers only, no audio or words) |
| GET | `/voice/work/latest` | device | Latest task + task list + recap |
| GET | `/voice/work/{run_id}` | device | One task |
| POST | `/voice/tasks/dismiss` | device | Hide finished tasks |
| GET | `/voice/card-image/{run_id}/{1-8}` | device | Image bytes for an image card |
| POST | `/voice/drafts/{draft_id}` | device | Approve / deny / revise an email draft |
| GET, PATCH | `/voice/settings` | device | Server settings |
| GET, PUT | `/voice/brief` | device | The voice brief |
| POST | `/voice/brief/rewrite` | device | Ask Hermes to rewrite the brief |
| GET | `/voice/brief/tune` | device | Brief edits proposed from recent calls (state, edits, product issues) |
| POST | `/voice/brief/tune` | device | Ask Hermes to propose edits from the last week of calls |
| POST | `/voice/brief/tune/apply` | device | Apply the accepted edits: `{"accept": ["e1", ...]}` |
| POST | `/voice/brief/tune/dismiss` | device | Discard the proposal |
| GET | `/voice/status` | device | Provider, Hermes API, brief readiness |
| GET | `/voice/destinations` | device | Where finished results can be posted |
| POST | `/voice/destinations/suggest` | device | Ask Hermes to propose delivery channels (never saved) |
| GET, POST | `/voice/onboarding` | device | First-run checklist |

## GET /health

No auth. Used by the app to find the server.

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

Wrong or expired code: `403 {"error": "pairing code is wrong or expired"}`.

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

## POST /voice/interactions/{id}/mic-check

The app found the call's mic wasn't getting through and reopened the connection. Body
`{"note": "..."}` (at most 400 characters of numbers and state, no audio or words). Logged only.

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
  "resumed_from": null
}
```

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
| `approval` | `{"run_id", "request_id", "description", "choices": ["once", "deny"]}` or `null` |
| `email_drafts` | every visible draft of the call's tasks, each with `task_id` and `run_id` added; published whenever a draft appears or changes status |
| `closed` | `{"finalization": "complete" \| "incomplete", "error"}` |

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

Answers a Hermes tool approval for one run. Only `once` and `deny` are accepted from the app.
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

## GET /voice/work/latest

The newest task (`work`), the task list (`tasks`, newest last, finished ones hidden after
dismissal) and the recap of results that finished while no call was open (`away`). Each task has
`task_id`, `run_id`, `status` (`admitting`, `working`, `waiting_for_approval`, `completed`,
`failed`, `cancelled`, `interrupted`, `ambiguous`), live `short_status`/`detail`, `events`,
`result` (`spoken`, `full`, optional `label`, optional `cards`) and `email_drafts`.
Product and image cards appear as `result.cards`; image cards are fetched through
`/voice/card-image/...`, never by path.

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
| `voice.max_call_minutes` | `30` | hard cap per call |
| `speech.progress` | `true` | one short spoken update on tasks running over 20 s, at most every 30 s |
| `idle_pause_minutes` | `5` | pause a silent call after this long |
| `instructions_extra` | `""` | extra voice instructions (up to 1,000 characters) |
| `hermes_profile` | `""` | Hermes profile for runs; empty = this gateway's |
| `delivery.target` | `"none"` | post finished results to a Hermes chat: `none`, `telegram`, `discord`, `discord:<chat_id>`, `telegram:<chat_id>[:<thread_id>]`, ... |
| `delivery.new_thread` | `false` | run each task in a new thread of the default target when `threads_supported` (older `new_thread_per_task` is accepted and migrated) |
| `delivery.channels` | `[]` | up to 8 opted-in channels: `{target, label, topic, new_thread}`. A new task goes to the channel named in the request ("put this in #work"), else the one whose `topic` fits (routing model), else `delivery.target`. `new_thread` runs the task in a new thread there (Discord, Telegram, Slack, Matrix; needs `threads_supported`), otherwise the answer is posted there. Follow-ups stay where their task runs |
| `continuity.enabled` | `true` | when a request is about something already being discussed in a Hermes chat or thread, continue inside that conversation (with its history) and post the reply there |
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
    "idle_pause_minutes": 5,
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
14 days / 60 calls); their text goes to the model Hermes uses only when a tune is requested.

## GET /voice/status

Readiness. `voice_ready` is true when the chosen provider can start a call. When Codex is missing or signed out, `codex_message` says what to do. `threads_supported` reports whether this Hermes can open a new thread per task (its webhook platform must be on; `threads_reason` says why not). `routing_model` names the model task routing uses (`auxiliary.speakeasy_router`), `routing_hint` how to change it. `advertised_url` / `tailscale_name` are the address setup advertised (empty = local only). `room_listening` (plugin 0.2.51+) is true when the server takes listening mode's `room` with `POST /voice/sessions`; missing on older plugins, which reject unknown session fields. `room_on_resume` is true when it also takes `room` with `resume_from`, which listening mode during a call needs (the app checks it before pausing the call for listening).

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
  "room_listening": true,
  "room_on_resume": true,
  "version": "0.2.0"
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
