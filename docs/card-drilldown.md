# Card drill-down (draft)

## UX

Cards stay in the panel column: no navigation stack, question, selection, or server action.
A real HTTPS row URL opens with Link (the existing webURL policy); otherwise a button reveals
available fields in place. A small count/chevron uses the existing leading gutter rather than
adding a collapsed row. Expansion animates, respects Reduce Motion, and is reversible.

- List: full item text/detail; string-only steps: full selectable text.
- Inbox/message: sender, time, subject, full preview; a supplied body has a second disclosure.
- Places/place: supplied name, address, rating, category, price, hours, distance, phone.
- Agenda/calendar day: full title, start/end, place, or the event's HTTPS URL.
- News: full headline/source/time or HTTPS URL.
- Comparison: full column/value readout per row; the winner header reveals its designation.
- Stats/nutrition: header reveals all nonempty label/value rows.
- Package: timeline reveals all supplied steps/times, including rows beyond the compact six.
- Contact: tapping the identity reveals phone/email as selectable text, without calling/sending.
  The existing explicit phone/email links remain separate.
- Media: identity opens its URL or reveals metadata; overview expands without truncation.
- Entity: identity opens its URL or reveals its full summary.
- Chart: tap/hover reads the category's values for every series, with units. Null values retain
  original label positions, rather than silently shifting later points.

Expansion preferences append visible details to the card's accessibility label. Rows retain
native Link/Button semantics; expandable rows include the `shows details` hint. Null/blank
fields and empty rows are filtered. Unknown kinds retain the existing decode-time skip.

## Contract boundaries (no plugin changes)

The current validated server catalog does NOT provide message/inbox body, object-style steps
with detail, places-row address/hours, or comparison row-detail/winner rationale. This change
only displays supplied fields and full existing text; it does not fetch, fabricate, or add fields
to the contract. A supplied message body is supported by the renderer but cannot currently
arrive via the catalog. Richer data would require a separately approved contract proposal.

## Verification on the CLT-only Pro

Toolchain: Swift 6.1.2; xcode-select: /Library/Developer/CommandLineTools.

Tests were added before the expansion implementation. The direct Swift assertion check first
failed with missing CardExpansion/CardDetail; a later chart alignment test first failed with
missing chartPoints. Existing XCTest assertions are unchanged.

Commands from the worktree root:

- `python3 mac/Checks/check-card-drilldown.py`: PASS (exit 0). Runs actual core source assertions
  for independent expansion, descendant collapse, reopening, null/blank filtering, HTTPS
  routing, supplied body, unknown kinds, and chart label/value alignment.
- `python3 mac/Checks/check-card-renderer.py`: PASS (exit 0). Creates an isolated temporary
  Swift package linking the real core and ALL Cards sources; SwiftUI, Charts, and MapKit card
  code actually compiles. This is NOT the complete client/app target.
- `swiftc -frontend -parse mac/Sources/SpeakeasyClient/Cards/*.swift`: PASS.
- `git diff --check`: PASS.
- `swift test --package-path mac`: blocked during binary-artifact downloads (initial tool
  timeout). Retry `swift test --package-path mac --skip-update` timed out after 180 seconds
  downloading Sparkle/WebRTC/LiveKit artifacts (exit 124).
- Isolated package using the real core and unmodified XCTest test directory:
  `swift test --package-path /Users/nk/.claire-work/cache/scratch/card-drilldown-core`:
  blocked, `error: no such module 'XCTest'`. No suite-green claim.
- `swift build --package-path mac --target SpeakeasyClient`: blocked on the same binary
  downloads, timeout after 60 seconds (exit 124). No complete app/client build claim.
- Local scratch `swift run --package-path /Users/nk/.claire-work/cache/scratch/card-drilldown-core Preview`
  built, linked and ran a SwiftUI ImageRenderer fixture at 320/420pt. Collapsed list, inbox and
  comparison screenshots are local smoke evidence, not an automated baseline comparison.

Still required before readiness: full XCTest suite with Xcode, complete app build, live
interaction/keyboard/VoiceOver/Reduce Motion checks, expanded layout checks for all kinds at
320/420pt, and baseline pixel comparison. Do not merge or release on the strength of these
limited checks. Draft PR only; no merge, release, publishing, or TestFlight.
