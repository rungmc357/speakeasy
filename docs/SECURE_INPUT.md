# Secure input: findings and design

Speakeasy shows a **secure box** in the app when a job needs something only the user should type:
an API key a skill needs, the Mac password for one `sudo` command, or a website login / one-time
code the browser needs. The value goes from the box to Hermes; it never enters the model's context,
Speakeasy's logs, `state.sqlite3`, the event feed, a push, a transcript, or a tool log.

## How Hermes asks (evidence, hermes-agent at bfd00f274e)

| need | Hermes lookup | what Hermes does with the answer |
| --- | --- | --- |
| skill key | `tools/skills_tool_setup.py:87-117` `_capture_required_environment_variables` → `_st._turn_secret_capture.get()` (ContextVar, `tools/skills_tool.py:139-148` `bind_turn_secret_capture`), then the process-wide `_secret_capture_callback` | callback `(env_var, prompt, metadata)`; Desktop/Mobile's version (`gateway/session_secret_prompt.py:28-52`) saves with `hermes_cli.config.save_env_value_secure` (`hermes_cli/config.py:2770`) and returns "Secret stored securely. The secret value was not exposed to the model." |
| sudo | `tools/terminal_tool_sudo.py:450-500` `_transform_sudo_command` → `tools.terminal_tool._get_sudo_password_callback()` (thread-local, `tools/terminal_tool.py:81-98`) via `_prompt_for_sudo_password` (`:185`) | password piped to `sudo -S` stdin for that command, then cached per session (`_set_cached_sudo_password`, `:62`) |
| website login | `tools/browser_vault_tool.py:337-372` `browser_vault_save_login` → `agent.vault_backends.unlock.get_save_login_prompt_callback()` gated by `can_prompt_here()` (`agent/vault_backends/unlock.py:248-253`) | stored in the local Fernet vault bound to the page origin (`agent/vault_store.py:547` `add_item`), filled into the page model-blind; the tool result carries only handle/origin/identifier |
| one-time code | `tools/browser_vault_tool.py:400-465` `browser_vault_enter_code` → `get_code_prompt_callback()` | typed into the page once |

Desktop and Hermes Mobile get these as server→client requests (`secret`, `sudo`, `vault.save_login`,
`vault.code`; `tui_gateway/contracts/server_requests.py:212-250`, `gateway/session_server_requests.py`
`ask_viewer`, 120/180 s). Hermes binds them **only for a local viewer's turn**
(`gateway/run_turn_runner.py:1348-1352`; `local_viewer_turn` in `gateway/run_turn_progress.py:1010`
is true only for `Platform.LOCAL` sources).

Speakeasy's jobs are `/v1/runs` turns on the `api_server` platform. There nothing is bound:
a skill gets `GATEWAY_SECRET_CAPTURE_UNSUPPORTED_MESSAGE` (`gateway/platforms/base.py:454`), sudo
runs headless and fails, and `can_prompt_here()` is false because `api_server` is an unattended
platform (`tools/approval_context.py:175-186`), so `browser_vault` answers `prompt_unavailable`.

There is no plugin hook that can bind a prompt callback around a turn (`hermes_cli/plugins.py`
`VALID_HOOKS`: `pre_tool_call` etc. run on a bounded worker and only observe/modify args), and no API
route by which an outside client can attach as a viewer of a `/v1/runs` turn and answer these
requests. So there is **no supported path** for an API-server client today.

## What Speakeasy does

`plugin/speakeasy/secure_input.py` uses the same public callback shapes Desktop/Mobile use, as a
**fallback that only applies to Speakeasy's own live runs**:

- `install()` wraps the four lookups — `skills_tool._turn_secret_capture` (a proxy whose `get()`
  returns the bound callback when there is one), `terminal_tool._get_sudo_password_callback`,
  `vault_backends.unlock.get_save_login_prompt_callback` / `get_code_prompt_callback` — and
  `unlock.can_prompt_here`. A bound callback (Desktop/Mobile/CLI) always wins.
- The fallback answers only when the current turn (ContextVars: approval session key / session id /
  platform) is a Speakeasy job that is running: its run id is the turn's approval session key
  (`api_server_runs.py:950`), or the turn runs in that job's Hermes session. Platform must be
  `api_server` (or unset). Every other turn sees exactly what it saw before.
- The callback opens a pending request and blocks the agent thread (like `ask_viewer`) until the
  paired app answers, skips, the job stops, or Hermes' deadline passes; then it does what
  Desktop's callback does (`save_env_value_secure` for keys; return the password for sudo; return
  `{identifier, password}` for the vault tool to store).
- Allow-once sudo: a `post_tool_call` hook drops Hermes' cached sudo password for that run after
  the command.
- `uninstall()` restores Hermes' originals; reloading the plugin re-wraps the originals (no stacking).

Upstream ask (would retire the wrapper): let an API-server client attach to a run's prompts — e.g.
`secret.request` / `sudo.request` / `vault.*.request` events on `/v1/runs/{id}/events` with
`POST /v1/runs/{id}/respond`, the same way `approval.request` / `POST /v1/runs/{id}/approval` work.

## Security properties (tested in `plugin/tests/test_secure_input.py`)

- Auth: device token; the job's own device only (403 otherwise); other devices don't see it.
- Single use; expiry (120 s keys/sudo, 180 s login/code); 12 answers/min/device; 16 KB body cap;
  ids compared in constant time.
- The request body is read into a `bytearray`, zeroed after the handoff; buffers handed to Hermes are
  zeroed after use. Hermes' `browser_vault_save_login` clears the login dict after saving.
- Never logged (root logger captured at DEBUG), never in `state.sqlite3` or any file under the
  profile except `.env` (keys) / the encrypted vault (logins), never in events, work views, pushes,
  replies. The real-Hermes test runs Hermes' own capture, sudo rewrite and vault store in a temp
  `HERMES_HOME`.
- Defense in depth: entered values are fingerprinted (keyed rolling hash + HMAC, memory only) and any
  Hermes output echoing one (plain or JSON-escaped) is replaced with `[redacted]` at intake
  (`hermes_api.py`) and on the way out (`server.py` replies and SSE).
- Voice: one fixed line; the voice model is told never to ask for a value aloud and not to repeat one.
- Mac: the box is its own window with `sharingType = .none` (hidden from screen sharing/recording).

## Limits

- Wrapping module attributes is not a public Hermes API; a Hermes refactor of these names makes
  `install()` report `wired: false` for that kind (the app then never sees requests of that kind, and
  Hermes behaves as before). Covered by the real-Hermes test.
- Secrets asked for any other way (a skill reading a file, the model asking in chat, MCP servers'
  own auth, password-manager *unlock* for 1Password/Bitwarden) are not covered; `unlock` stays
  Desktop/CLI-only.
- A key saved this way lands in the profile `.env` like Desktop's; logins in the local Fernet vault.
- Thread tasks (Discord/Telegram threads) and cron jobs never get a box.
