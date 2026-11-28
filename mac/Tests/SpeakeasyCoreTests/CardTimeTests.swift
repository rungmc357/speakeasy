import XCTest
@testable import SpeakeasyCore

/// A card's `as_of` (SPK-456): the plugin stamps stored results with epoch seconds, so every card
/// that shows an "As of" line must read a number as the time it is, never print its digits.
/// Invented times throughout.
final class CardTimeTests: XCTestCase {
    private let nyc: Calendar = { var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: "America/New_York")!; return c }()
    // Nov 12, 2026, 4:03 PM in New York.
    private let stamp: Double = 1_794_517_380.123
    private var now: Date { Date(timeIntervalSince1970: 1_794_520_000) }

    func testEpochSecondsAndMillisecondsBecomeATime() {
        XCTAssertEqual(CardTime.asOf(.number(stamp)), "2026-11-12T21:03:00Z")
        XCTAssertEqual(CardTime.asOf(.number(stamp * 1000)), "2026-11-12T21:03:00Z")
        // The digits an older reader made of the number read the same way.
        XCTAssertEqual(CardTime.text("1794517380.123"), "2026-11-12T21:03:00Z")
        XCTAssertEqual(CardTime.text("1794517380123"), "2026-11-12T21:03:00Z")
    }

    func testWordsAndTimesPassThroughAndJunkNumbersSayNothing() {
        XCTAssertEqual(CardTime.asOf(.string("Thursday's close")), "Thursday's close")
        XCTAssertEqual(CardTime.asOf(.string("2026-11-06T15:42:00-05:00")), "2026-11-06T15:42:00-05:00")
        XCTAssertEqual(CardTime.asOf(.number(2025)), "2025")
        XCTAssertNil(CardTime.asOf(.number(42.5)))
        XCTAssertNil(CardTime.asOf(.null))
        XCTAssertNil(CardTime.text("   "))
        XCTAssertNil(CardTime.text("9999999999999999"), "a count far outside any date never prints")
    }

    func testAChartStampedWithEpochSecondsSaysAReadableTime() {
        let card = ViewCard(kind: "chart", data: ["title": "Signups", "style": "bar", "labels": ["Mon", "Tue"],
                                                  "series": [["name": "Signups", "values": [3, 4]]], "as_of": stamp])
        let f = ChartFacts(card, now: now, calendar: nyc)
        XCTAssertEqual(f.freshness, "As of 4:03 PM")
        XCTAssertFalse(f.stale)
        // Three days later it carries its date and reads as older data.
        let later = ChartFacts(card, now: now + 3 * 86_400, calendar: nyc)
        XCTAssertEqual(later.freshness, "As of Nov 12, 4:03 PM")
        XCTAssertTrue(later.stale)
        XCTAssertFalse((later.freshness ?? "").contains("1794517380"))
    }

    func testTheQuoteLineFormatsATimeAndKeepsTheSourcesWords() {
        XCTAssertEqual(CardTime.quoteLine(.number(stamp), now: now, calendar: nyc), "As of 4:03 PM")
        XCTAssertEqual(CardTime.quoteLine(.string("last 24 hours"), now: now, calendar: nyc), "last 24 hours")
        XCTAssertEqual(CardTime.quoteLine(.string("2026-11-12T16:03:00-05:00"), now: now, calendar: nyc), "As of 4:03 PM")
        XCTAssertNil(CardTime.quoteLine(.string(""), now: now, calendar: nyc))
    }

    func testTheMetricFootReadsAnEpochAsAClockTime() {
        let iso = CardTime.asOf(.number(stamp))
        let line = EventTimes.stamp(iso, now: now, calendar: nyc, locale: Locale(identifier: "en_US"))
        XCTAssertEqual(line?.replacingOccurrences(of: "\u{202F}", with: " "), "4:03 PM")
    }
}
