# Atomic task outcomes

A task's terminal event saves its status, result (including question and pictures), failure reason, email drafts and `settled_at` in one SQLite transaction through `StateStore.settle`. `settle` reuses the checked transition contract; it does not introduce a second state machine. A rejected duplicate outcome changes neither the answer nor its drafts. Draft insertion failure rolls the entire outcome back, including superseding an older draft.

Completion listeners run only after the transaction commits. The queued-message drain requires both a send-eligible terminal status and the committed `settled_at` marker. Its short scheduling delay is not a correctness guarantee. An independent SQLite reader sees either the pre-finish row or the complete outcome, never the intermediate SQL writes.

## Reconciliation with earlier fixes

- SPK-75 corrected production answer-before-status ordering and test waits. SPK-77 replaced those separate writes with a checked transaction, including quick answers, home-control answers and atomically created spoken-choice cards. These merged paths are retained.
- SPK-121's ordering patch covers those same paths on its older base. It adds no distinct outcome guarantee beyond the merged SPK-77 implementation. Keep its parallel test runner and test-wait improvements; do not restore the older split writes over `transition(result=...)` or `settle`.
- SPK-79 already gives questions an independent server-owned asking lifecycle (`question_state`: open, answered, set_aside, dismissed, moved_on, expired, superseded). Spoken-choice cards retain their compatible completed *computation* status while their question is open; this is not evidence that the user answered. Do not add a competing run-status enum for asking or use computation status to decide whether the question needs a reply.
- SPK-78 closes the residual gap: failure reasons and email drafts were still stored after completion listeners fired. It also makes queued messages check the committed finish marker.
- Durable delivery receipts and notification outboxes remain outside this change (SPK-80).

## Regression coverage

`tests/test_atomic_outcome.py` checks the first completion acknowledgement, first failure acknowledgement, independent-connection visibility during draft insertion, transactional rollback, duplicate terminal-event protection, and queue blocking without a committed marker. The first-ack draft and failure tests and the missing-marker queue test fail on the post-SPK-75/SPK-77 build-67 main. The spoken-choice blank-card regression remains in `tests/test_spoken_choices.py`; the shared question lifecycle tests remain in `tests/test_question_state.py`.

## Verification ledger (2026-11-06)

- Six new regression tests: 6 passed locally. The first-ack draft/failure checks and missing-marker queue check: 3 failed on base `865e7e8`, proving the residual bugs rather than only the new API.
- Apollo concurrent stress: 10/10 runs passed, 240 tests per run (2,400 passing executions), with two stress copies and the full suite running concurrently. No retries. Covered the eight SPK-75 test files plus atomic outcomes, run state and spoken choices.
- Apollo full suite, first pass: **2,370 passed, 2 failed** in 1,356.56 seconds. Failures: `test_profiles_port.py::test_its_own_server_is_recognised_and_named` and `::test_default_profile_keeps_its_port`.
- Solo retry of the whole profile-port file: **6 passed, 2 failed**. These are persistent host-provider isolation failures already owned by SPK-170, not retry-passes. No production or test change for SPK-170 was absorbed into this branch.
- Secret scan, real-call scan and diff whitespace check pass.
- Resumed with independently reviewed SPK-170 isolation tests (Claire's patch, equivalent to `ddf1f64`, incorporated as `24c0962`). This changes only the profile setup tests; release integration must keep the collaborator's separate merge gate.
- Full suite after that patch: **2,376 passed, 2 failed** in 1,410.04 seconds on apollo. The original profile setup failures are cleared. New first-pass failures: `test_history_sync.py::test_history_events_watcher_sees_state_db_changes` and `test_screen_hold.py::test_look_at_my_screen_waits_for_sharing_then_runs_with_it`.
- One apollo solo retry per failure: screen hold passed; history watcher failed. An initial retry was auto-routed to zeus by a changed remote-run default and is excluded from acceptance evidence; subsequent commands explicitly pin `--host apollo`.
- Unchanged build-67 base plus SPK-170 tests (`ddf1f64`): watcher passed the initial single run, then **4 passed / 1 failed** across five sequential ordinary test invocations. `HistoryEvents.opened()` returns before `_watch()` records its initial stamp; a message inserted in that gap becomes the baseline rather than a change. Evidence handed to the existing SPK-179 reliability owner. No watcher/test-wait change was made here.
- **Not ready to ship.** Remaining gate: SPK-179's deterministic watcher readiness/wait repair, followed by a full green plugin suite. Keep both full first-pass failure ledgers and the solo retry counts in release reporting. Atomic outcomes and ten concurrent stress runs remain verified; this does not waive the full-suite gate. No pushes, installs, version bumps or app changes were made.
