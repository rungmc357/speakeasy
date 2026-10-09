# Speakeasy architecture (v1, Mac only)

Speakeasy lets someone talk to their own Hermes agent by voice from a Mac. Two parts:

- `plugin/speakeasy/` — a Hermes plugin. It registers a gateway platform named `voice`. When the
  gateway starts the platform, it starts the **Speakeasy server** on a daemon thread inside the
  gateway process. The server is a small HTTP service bound to loopback only.
- `mac/` — the Speakeasy Mac app (Swift). A floating voice panel, menu-bar item, global hotkey,
  and a Settings window. It talks only to the Speakeasy server with a revocable per-device token.

## Voice

- Default: **ChatGPT sign-in (Codex)**. The server runs `codex app-server --stdio --enable
  realtime_conversation` for each call; the voice model is `gpt-live-1-codex`. No API key. Uses the
  user's own `codex login`.
- Optional: an **OpenAI API key** (Platform `gpt-live-1` over WebRTC). Stored only through Hermes'
  own secret storage (the profile `.env`, key `SPEAKEASY_OPENAI_API_KEY`), never in the repo, never
  sent to the app.
- `voice.provider` setting: `codex` (default) | `openai`.

## How work reaches Hermes

When the voice model decides a request needs real work (tools, lookups, actions), the server starts
a Hermes run through the local Hermes **API server** (`/v1/runs`, loopback), streams its events for
progress, relays approvals, and speaks the result. `hermes voice setup` enables the API server on
loopback and generates its key into the profile `.env` if it is not already on. The user's normal
Hermes (tools, memory, skills, approvals, model) does the work.

Features: multiple tasks in parallel, one Hermes session per
task, follow-ups routed to the right task, a task list with live status, approve/deny from the
panel, stop a task, clear finished tasks, pause/resume a call (closes the paid voice session, keeps
the conversation and tasks), 5-minute idle auto-pause, product cards with images, image previews
from Hermes media, "while you were away" recap on the next call, optional heads-up notifications to
one Hermes chat target (`hermes send`) when a task finishes after the call ended, and thread
continuity: a spoken request that clearly refers to a recent Hermes chat on any platform (matched
against `state.db` session titles/chat names) continues that Hermes session instead of starting a
new one, and Speakeasy posts the answer into that chat with `hermes send`.

Hermes `config.yaml`: Speakeasy changes it only during `hermes voice setup` (turns on the voice
platform and adds itself to `plugins.enabled`), through `hermes_config.update()`, which only adds
settings and refuses to write a config it cannot read. Nothing at runtime writes it.

Email approval cards: a task that writes an email ends its answer with a fenced `email-draft` JSON
block instead of sending it. The server strips and validates it, stores it with a `draft_id` and a
SHA-256 of its canonical JSON, and shows it as a card (From/To/Cc/Subject/body; Approve / Deny /
Revise). Nothing is sent until the user presses Approve with that exact hash; approval continues
the task's own Hermes session with the approved draft. A spoken "send it" never approves.

Not in v1: iPhone and Watch clients, platform-specific routing rules (results go to one generic
`delivery.target`), and third-party request classifiers. The voice behavior lives in
`prompt/rules.md` + `prompt/builder.py`, templated with `{assistant_name}`, `{user_name}` and
`{machine_description}`. A request is a new task unless the voice model marks it as a follow-up.

## Listening mode (Mac, macOS 26+, during a call)

Listening mode lets the Mac hear the conversation around a call without the voice answering, then
go back to the call knowing what was said. It exists only during a call: turning it on pauses the
call, and every way of resuming or starting a call while it's on turns it off into that call.

- **Turning it on** (the ear button with the call's controls, the menu bar, or an optional shortcut)
  pauses the live call through the ordinary Pause (the voice session closes, nothing is billed, the
  conversation and tasks are kept), then `RoomListener` (SpeakeasyClient) transcribes the default
  input on-device with Apple's SpeechAnalyzer + SpeechTranscriber (long-form, distant audio, no
  speech-recognition permission). The text (`RoomTranscript`, SpeakeasyCore) stays in memory only,
  the last 30 minutes. Nothing goes to the server or the voice provider while it listens. The panel
  strip and menu bar ring show it's on; it's never shown as listening before real sound arrives,
  and a mic delivering only digital silence for 10 s shows as not hearing. `RoomListeningController`
  (app) holds an idle-sleep assertion and stops after 2 hours, on sleep, or when the call ends;
  Discard drops what it heard and leaves the call paused.
- **Turning it off** (Turn off and ask, Resume, the call or pause shortcut) releases the mic and
  resumes the call with the rendered transcript as `room` on `POST /voice/sessions` + `resume_from`
  (sent only when `GET /voice/status` says `room_listening`). The call comes back unmuted; the
  existing listen-while-connecting capture (`EarlyCapture`) catches what's said next as the request.
  If nothing is said, ~2 s after turning it off (once the call is live, with no speech on its mic)
  the app sends an empty early request and the voice responds from the room (`RoomCall` /
  `RoomTakeoffPolicy`), once per take-off. If the call can't come back, what was heard is kept (not
  listening) to ask about in a new call or discard; if the server can't take room text, the call
  carries on without it.
- **On the server:** the room text is redacted for secrets, kept only on the in-memory
  `Interaction` (never in the transcript fragments, call log, recent voice, Tune or the store), put
  in the voice's instructions as a fenced background block, and added to Hermes run inputs for tasks
  from that call (new tasks, follow-ups, splits and continued chats; never posts to a chat thread).
  Each take-off adds to the room text the call already has (newest kept within the cap). It moves to
  a resumed call and is cleared when the call ends or 15 minutes after a pause.

## Server API (all JSON; Bearer device token unless noted)

Full request and response shapes are in `docs/API.md`; the Mac app's `ServerClient` is the
reference client.

- `GET /health` (no auth)
- `POST /voice/sessions` (SDP offer → answer; response includes `voice_provider`)
- `GET /voice/interactions/{id}`, `GET /voice/interactions/{id}/events` (SSE)
- `POST /voice/interactions/{id}/end` | `/pause` | `/approval` | `/cancel-backend`
- `GET /voice/work/latest`, `GET /voice/work/{run_id}`, `POST /voice/tasks/dismiss`
- `GET /voice/card-image/{run_id}/{1-8}`
- New in Speakeasy:
  - `POST /voice/pair` (no auth) `{code, device_name}` → `{device_id, token}`; single use, 10 min
  - `GET /voice/settings`, `PATCH /voice/settings` → server-side settings (below)
  - `GET /voice/brief`, `PUT /voice/brief` (edit), `POST /voice/brief/rewrite` (see VOICE_PROMPT.md)
  - `GET /voice/status` → `{assistant_name, provider, voice_ready, codex_signed_in, codex_message,
    api_key_set, brief_state, hermes_api_ok, threads_supported, version, ...}`
  - `GET /voice/destinations` → connected Hermes platforms/chats as `delivery.target` strings (no secrets)
  - `GET|POST /voice/onboarding` → first-run checklist (`paired`, `codex_signed_in`, `names_set`,
    `delivery_set`, `brief_ready`)
  - `POST /voice/drafts/{draft_id}` `{action: approve|deny|revise, sha256, instructions?}`

`docs/API.md` (written by the plugin port) is the full contract with request/response examples.

## Settings

Server-side (`<HERMES_HOME>/speakeasy/settings.json`, editable from the app and `hermes voice config`):
- `assistant_name` (default "Hermes"), `user_name` (optional), `machine_description`,
  `voice.provider` (`codex`|`openai`), `voice.voice`, `voice.codex_path`, `idle_pause_minutes`
  (default 5, 0 = off), `delivery.target` (default `none`; a Hermes send target such as `telegram`
  or `discord:<chat_id>`), `delivery.new_thread_per_task` (default off; used only when
  `threads_supported`), `continuity.enabled` (default on), `brief.auto_refresh`,
  `instructions_extra` (optional extra persona text), `hermes_profile`, `image_roots`.
  Full table in API.md.

Client-side (Mac app, UserDefaults; token in Keychain):
- server address, global hotkey, start muted, launch at login, show panel on call start,
  output/input device follows system (default on).

## Networking

- Same Mac: `http://127.0.0.1:<port>` (default 8795). Zero config.
- Tailscale (optional, manual for now): run `tailscale serve` yourself in front of the loopback port
  and pass the HTTPS URL to `hermes voice setup --server <url>`; the app accepts `https://*.ts.net`.
  (A built-in `--tailscale` flag is an open question; see OPEN_QUESTIONS.md.)
- Never binds a non-loopback address. No public exposure.

## Pairing (Mac)

- Same Mac: `hermes voice setup` ends by opening `speakeasy://pair?server=<url>&code=<code>`.
- Other machine: `hermes voice pair --send <target>` sends that link through a Hermes chat; or the
  user types the server address and 6-digit code into the app.
- `hermes voice devices`, `hermes voice revoke <id>`.

## Secrets rule

No keys, tokens, personal names, hostnames, tailnet names, chat IDs or 1Password references in the
repo. `scripts/scan-secrets.sh` must pass before any push.
