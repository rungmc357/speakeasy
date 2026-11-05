# Mic-aware narration: narrate while the mic is open, stay quiet when muted

## Problem

Speakeasy feels slow and silent on longer tasks. The pipeline already produces progress, but the
spoken part is gated so coarsely that in practice it barely fires:

- `plugin/speakeasy/calls.py` — `maybe_speak_progress()` only speaks when
  `PROGRESS_AFTER_S = 45.0` has elapsed since the task started, and then at most once per
  `PROGRESS_EVERY_S = 45.0` across the whole call (`Interaction.progress_spoken_at`).
- It also needs new activity beyond `backend.told_count`, so a task that reports one STATUS line and
  then works quietly produces nothing.
- The prompt (`plugin/speakeasy/prompt/builder.py`, `status_rule()`) only asks for a STATUS/DETAIL
  pair "only when the step meaningfully changes" — no pressure to narrate a long run.
- The daemon has no idea whether the user is even in the conversation: the Mac and iOS clients track
  mic mute (`Presentation.micMuted`, `NativeVoiceClient.micMuted`) and send a `{"type":"mute"}`
  frame on the audio relay (`mac/Sources/SpeakeasyCore/RelayAudio.swift:77`), but nothing in
  `plugin/speakeasy/` reads a mute state. `grep -rn mute plugin/speakeasy` finds only `duck.py`
  (system output volume).

So the dead zone is roughly 3s (after the spoken acknowledgement) to 45s, and after that it is one
generic line per 45s regardless of how much is happening.

## Proposal

Make narration follow an attention signal, with mic state as the primary one:

- **Mic open (unmuted)** — the user is in the conversation and can hear: narrate eagerly. First
  spoken update after ~12s of work with news, then roughly every 20s with gentle backoff.
- **Mic muted or unknown** — the user is watching the screen or stepped away: keep today's quieter
  behaviour (45s / 45s). Unknown must default to muted, so older clients behave exactly as they do
  now.
- **Needs-you events** (approval request, blocker, `HEADS_UP_RE` match) — always speak, in either
  state, and skip the spacing. These are the lines that actually matter when he is not looking.
- **Mid-turn politeness** — never speak over the user: if the user started speaking in the last
  ~1.5s, defer the line by a few seconds instead of dropping it; if the voice model is mid-response,
  wait for the turn to finish.

This is the cheap, high-leverage fix: the machinery (activity tracking, wording model, spoken
channel) already exists. What is missing is one flag on the interaction and a smarter gate.

## Design

### 1. Transport: get mic state to the daemon

Add an explicit client → daemon report; do not couple to relay internals.

- `plugin/speakeasy/server.py`: accept `PUT /voice/interactions/{id}/mic` with body
  `{"muted": true|false}` next to the existing action verbs (the regex at line 241 already lists
  `mic-check`; keep the new route separate because it is idempotent state, not a verb).
  Reject anything but a real bool (400), 404 for an unknown interaction.
- `plugin/speakeasy/service.py`: `set_mic(interaction_id, muted)` stores it on the interaction and
  returns the snapshot.
- `plugin/speakeasy/calls.py`: `Interaction.mic_muted: bool | None = None` (None = unknown) next to
  `progress_spoken_at`, plus include `mic` in `Interaction.snapshot()` so clients can render a
  "narrating" hint later.
- Clients (`mac/Sources/SpeakeasyClient/RelayCallEngine.swift` and the iOS call engine): POST on
  every mute toggle and once when the call connects. One-shot, best-effort, no retry loop — a lost
  report only costs a quieter call.

Read the state at decision time inside `maybe_speak_progress()`, never cache it at task start: the
user can unmute 10s into a 3-minute task.

### 2. Policy: adaptive cadence in `maybe_speak_progress()`

Replace the two constants with a policy object so tests can drive a clock:

```
unmuted: first_after = 12s, cadence = 20s, backoff x1.5 up to 60s, max_words = 22
muted/unknown: first_after = 45s, cadence = 45s, no backoff (today's behaviour)
urgent (HEADS_UP_RE, approval, blocker): bypass both first_after and cadence
```

Global guards, kept across all parallel tasks on the call:

- one spoken line per `MIN_SPACING_S = 12s` across the call (unmuted), 45s when muted;
- `MAX_LINES_PER_TASK = 6`, then the card keeps updating but the voice stops;
- never repeat a line already in `backend.told` (verbatim or near-duplicate within 0.8 similarity);
- never speak the same `detail` twice for one task — `record_activity()` already de-dupes, keep it;
- defer, do not drop, when the user is speaking.

Backoff matters: a task that keeps hitting small steps should get quieter over time, not louder.
`backend.spoken_at`, `backend.told`, `backend.told_count` already exist for exactly this.

### 3. Prompt: ask for more granularity when the mic is open

Injected into `build_task_prompt()` and the continuation message, alongside `status_rule()`:

- unmuted: "The user has the mic open and is listening: write a STATUS/DETAIL pair at each
  meaningful step — each new tool, each new finding, each change of direction — one short sentence
  each."
- muted/unknown: keep today's "only when the step meaningfully changes".

The agent supplies the substance (what it is doing); the daemon keeps the pacing. Do not let the
prompt set cadence — the prompt cannot see the clock or the other tasks.

### 4. Anti-echo and anti-interruption guards

Speaking into an open mic puts TTS into the microphone path. The relay has echo cancellation, but do
not rely on it:

- skip if an assistant response is in flight (queue the line for after the turn, drop it if stale);
- skip if `user_spoke_since(now - 1.5)` is true, retry once after 4s;
- cap line length at 22 words — long lines are both chatty and more likely to loop.

### 5. Rollout

- `plugin/speakeasy/settings.py`: keep `speech.progress` as the master switch; add
  `speech.narration: "adaptive" | "classic"` so the change can be reverted per user without a
  build. Default `"adaptive"`.
- Log one line per decision at info level:
  `narration: state=unmuted first=11.8s gap=20.4s task=se_9f3… reason=ok|spacing|cap|user-speaking`.
  That single line is how this gets tuned after launch.

## Files touched

| File | Change |
| --- | --- |
| `plugin/speakeasy/calls.py` | `Interaction.mic_muted`, `maybe_speak_progress()` policy, guards, log line |
| `plugin/speakeasy/server.py` | `PUT /voice/interactions/{id}/mic` route |
| `plugin/speakeasy/service.py` | `set_mic()` |
| `plugin/speakeasy/prompt/builder.py` | narration hint in `status_rule()` / `build_task_prompt()` |
| `plugin/speakeasy/settings.py` | `speech.narration` knob |
| `mac/Sources/SpeakeasyClient/RelayCallEngine.swift` | report mute on toggle and on connect |
| iOS call engine (`iOS/Todd/…`) | same report; watch inherits via `PhoneWatchBridge` |
| `plugin/tests/test_narration.py` | new suite |
| `plugin/tests/test_server.py` | route validation |
| `mac/Tests/SpeakeasyCoreTests/` | mute report payload |

## Phases

1. **Transport** (small): route, `set_mic`, `Interaction.mic_muted`, client reporting, tests.
   No behaviour change — unknown still means muted.
2. **Policy** (core): adaptive cadence + guards behind `speech.narration`, unit tests on a fake
   clock using the existing `_run_with_clock` harness in `plugin/tests/test_routing.py`.
3. **Prompt**: narration hint per state, prompt-content tests.
4. **Guards**: anti-echo / deferral, line cap.
5. **Tune**: ship default adaptive, read the `narration:` log lines for a week, adjust constants.

## Acceptance criteria

- On an unmuted call, a task that runs 90s with regular activity produces at least 3 spoken lines;
  the first lands between 12s and 20s; none exceed 22 words; none repeat.
- The same task with the mic muted produces at most 2 lines, spaced 45s.
- `mic_muted = None` (older client) behaves exactly like muted — verified against a recorded
  session, no regression in `plugin/tests/test_routing.py`.
- Zero lines spoken within 1.5s of the user starting to speak; deferred lines are spoken after the
  pause, not dropped, unless the task has finished.
- No more than one line per 12s across all tasks on the call; hard cap of 6 per task.
- `speech.progress = false` yields zero lines in both states.
- p95 delay from "activity recorded" to "line spoken" ≤ 4s when unmuted.
- Toggling mute mid-task changes cadence within one decision (no restart needed).

## Risks

- **Chatter fatigue.** Backoff plus the 6-line cap plus the 12s global spacing is the mitigation;
  if it still feels busy, raise `cadence` before touching `first_after`, because the first line is
  the one that actually fixes "I don't know what it's doing".
- **Echo loops.** Phase 4 is not optional; ship 1–3 behind the knob and test on a real call before
  turning adaptive on by default in a build.
- **Cost.** Each spoken line is one small wording call (`router.progress_update`). 3× volume is
  still cents per call, but reuse the last line for near-identical activity instead of re-wording.
- **Privacy.** Only vetted STATUS/DETAIL text keeps flowing; `safe_user_text` and the existing
  sensitive-text filters stay in the path. No tool arguments get read aloud.
- **Parallel tasks.** Two tasks narrating at once is the worst failure mode; the global spacing is
  what prevents it, so it must be enforced on `Interaction`, not per backend.

## Out of scope

- Narrating before the task exists (the router/dispatch window). The spoken acknowledgement already
  covers 0–3s; if it still feels dead, that is a separate change to `dispatch()`.
- Continuous ambient narration in listening mode (`Interaction.room`) — different risk profile.
