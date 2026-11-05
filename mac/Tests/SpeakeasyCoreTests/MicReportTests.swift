import XCTest
@testable import SpeakeasyCore

/// Every call tells the plugin its mic state once at the start (open or muted), then each change.
/// The plugin's open-mic call pulse only runs when it was told the mic is open.
final class MicReportTests: XCTestCase {
    func testAFreshUnmutedCallPostsMutedFalse() {
        var report = MicReport()
        XCTAssertEqual(report.next(interaction: "vi_a", muted: false), false, "a call that starts unmuted says so")
        XCTAssertNil(report.next(interaction: "vi_a", muted: false), "once per call")
    }

    func testAFreshMutedCallPostsMutedTrue() {
        var report = MicReport()
        XCTAssertEqual(report.next(interaction: "vi_a", muted: true), true)
        XCTAssertNil(report.next(interaction: "vi_a", muted: true))
    }

    func testTogglingMidCallPostsEachChange() {
        var report = MicReport()
        XCTAssertEqual(report.next(interaction: "vi_a", muted: false), false)
        XCTAssertEqual(report.next(interaction: "vi_a", muted: true), true)
        XCTAssertNil(report.next(interaction: "vi_a", muted: true))
        XCTAssertEqual(report.next(interaction: "vi_a", muted: false), false)
    }

    func testAResumedCallReportsAgainUnderItsNewID() {
        var report = MicReport()
        XCTAssertEqual(report.next(interaction: "vi_a", muted: false), false)
        XCTAssertEqual(report.next(interaction: "vi_b", muted: false), false, "the resumed (head) call is told too")
        XCTAssertEqual(report.interaction, "vi_b")
    }

    func testSpokenUpdateIntervalRoundTripsAndLabels() throws {
        let raw = #"{"speech": {"progress": true, "interval_seconds": 90}}"#
        let s = try JSONDecoder().decode(ServerSettings.self, from: Data(raw.utf8))
        XCTAssertEqual(s.speech?.intervalSeconds, 90)
        let old = try JSONDecoder().decode(ServerSettings.self, from: Data(#"{"speech": {"progress": true}}"#.utf8))
        XCTAssertNil(old.speech?.intervalSeconds, "an older plugin has no interval")
        XCTAssertEqual(ServerSettings.Speech.intervalChoices.map(ServerSettings.Speech.intervalLabel),
                       ["30 s", "45 s", "1 min", "1.5 min", "2 min"])
    }
}
