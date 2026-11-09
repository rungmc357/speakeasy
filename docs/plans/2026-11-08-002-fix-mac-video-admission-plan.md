---
title: "fix: Gate Mac Home videos against current plugin capability"
date: 2026-11-08
status: active
---

# Gate Mac Home videos against current plugin capability

## Problem and scope

PR22 predates the Home composer. Upstream main now contains that composer and SPK-220 video staging at `a430f2c0ae6fed1222e82952b1aa0ed5ec88ea34`. Integrate that exact main without rewriting remote history, then connect the existing video gate to the real Mac composer. Apple PR25, call-panel behavior, general attachment redesign and release work are excluded.

## Data shape and decisions

Reuse `VideoAttachmentGate` and `ServerCapabilities`. Read the current paired capabilities through an injected closure, following the existing `api` closure. The real assignment site is `mac/Sources/Speakeasy/App/SpeakeasyApp.swift`. Do not cache a capability across pairing changes. Nil capabilities mean unavailable status, not proof of an old plugin. Refuse a video before creating a chip and use existing connection-error wording when status is unavailable. Loaded status without the video flag uses the issue's approved update line. Keep the picker message truthful when videos are unavailable.

Main actually stages MP4 and MOV but does not advertise `video_attachments`. The integrated plugin must advertise true alongside its real staging support. Preserve all Home and voice-note fields when resolving the verified `ServerSettings.swift` conflict. Do not implement the separate iPhone encoder concern in this Mac-only unit.

## Requirements

- R1. Preserve the current upstream Home implementation and all PR22 capability additions without force pushing or changing main.
- R2. Advertise staged-video support on the current plugin and retain fail-closed missing-key decoding for old plugins.
- R3. Filter the Mac picker and refuse unsupported video drops before chip creation. Refusal must leave text Send usable.
- R4. Read capabilities from the current paired status on each admission. Unknown status must not claim that an update is needed.
- R5. Preserve supported video poster/upload behavior, non-video attachments, mixed batches and call-panel plain-file handling.
- R6. Obtain independent source review and Main Claire exact-byte native/paired acceptance plus Geo OK before merge. No release is authorized.

## Implementation units

### U1. Integrate the landed Home base

Goal is R1. Integrate the frozen main using a merge, keeping both sides of `mac/Sources/SpeakeasyCore/ServerSettings.swift`. Keep all main files and the PR's gate and tests. There is one verified textual conflict in that file. Dependencies are none. Verification requires an integration diff against main containing only the owned capability work and plan, with Home and voice-note decoders intact.

### U2. Wire real Mac admission and capability advertising

Goal is R2 through R5. Modify `mac/Sources/Speakeasy/History/MacHomeComposer.swift`, `mac/Sources/Speakeasy/App/SpeakeasyApp.swift`, `plugin/speakeasy/service.py`, relevant `docs/API.md` capability wording, `mac/Tests/SpeakeasyCoreTests/VideoAttachmentGateTests.swift` and `plugin/tests/test_staged_attachments.py`. Reuse the gate rather than adding another attachment system. Check unknown status before applying the loaded-old-plugin refusal. Apply admission before the item-count guard and Pick creation so rejected videos cannot consume capacity. Leave call-panel behavior untouched.

Test scenarios include loaded old status, loaded new status, nil status, MP4/MOV admission, ordinary documents, a rejected video plus accepted text file, capability changes between two admissions, and preserved text-send eligibility. Verify current server video headers are accepted while missing/false capability decoding remains conservative. Keep existing full-stage invalid-byte rejection tests; do not manufacture invalid files that a correct server must accept.

### U3. Verification and handoff

Goal is R6. Run source and focused pure-domain checks plus contained plugin tests when the established runner is available. The only current SwiftPM test target is `SpeakeasyCoreTests`; there is no app test target to invent. Core checks cover gate results and literal capability payload decoding. Composer ordering, capacity, mixed-file admission and Send eligibility remain explicit native acceptance requirements, not claimed Core coverage. Under nil status the picker hides videos and uses connection wording, never update wording. U1 alone preserves PR22 additions and the plan without altering main-side behavior; U2 then adds the bounded integration. If native composer execution cannot be built, label extracted/source checks as supporting evidence and keep native acceptance pending. Main Claire must build the exact integrated bytes, pair to Claire Work, exercise old/new plugin picker and drag/drop, supported upload/send, unknown-status retry, mixed files and text Send, and the unchanged call-panel path. Use invented attachments and real work queries on Claire's own context only. Record exact app/backend revisions and observations.

## Risks and gates

Original review findings are based on the old main. The app-encoder concern remains an Apple follow-up, not authority to change the phone. No broad gate redesign, new view or approval-policy change. Verify workflows before push; do not publish tags or releases. Independent review of the integrated increment and final native testing cannot be substituted by source tests or an earlier head's approval.

## Planning review

The first tool-enabled planning attempt timed out without a source change. The bounded Opus artifact replacement supplied useful source facts but guessed a nonexistent app test target and left wiring unresolved. Parent inspection resolved both through the actual package manifest and `SpeakeasyApp.swift` assignments. This plan replaces those proposals. No tests or implementation are claimed by this planning artifact.

## Revised after review

Plugin 0.3.47 is released and stages MP4 and MOV without listing `video_attachments`, so a missing flag can't mean "old plugin". The gate now reads the flag as three states and uses `version`: flag `true`, or missing with 0.3.47 or later, takes videos; missing before 0.3.47 gets "Update Speakeasy on your Hermes to send videos."; `false` or an app that can't send videos gets "Videos can't be sent here."; nil status gets the connection line. The app's own limit is checked first.
