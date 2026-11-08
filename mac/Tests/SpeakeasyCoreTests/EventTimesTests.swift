import XCTest
@testable import SpeakeasyCore

final class EventTimesTests: XCTestCase {
    private var cal: Calendar = {
        var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: "America/New_York")!; return c
    }()
    private let us = Locale(identifier: "en_US")

    // Newer systems put a narrow no-break space before AM/PM.
    private func span(_ s: String?, _ e: String?, _ locale: Locale? = nil) -> String? {
        EventTimes.span(start: s, end: e, calendar: cal, locale: locale ?? us)?.replacingOccurrences(of: "\u{202F}", with: " ")
    }
    private func one(_ s: String) -> String? {
        EventTimes.one(s, calendar: cal, locale: us)?.replacingOccurrences(of: "\u{202F}", with: " ")
    }

    func testTimestampsBecomeLocalClockTimes() {
        // 14:00Z is 9:00 AM in New York in March (EST).
        XCTAssertEqual(span("2026-03-02T14:00:00Z", "2026-03-02T15:00:00Z"), "9:00 – 10:00 AM")
        XCTAssertEqual(span("2026-03-02T16:00:00Z", "2026-03-02T18:30:00Z"), "11:00 AM – 1:30 PM")
        XCTAssertEqual(one("2026-03-02T19:15:00.000Z"), "2:15 PM")
    }

    func testNoRawTimestampEverShows() {
        let s = span("2026-11-06T16:00:00Z", "2026-11-06T17:00:00Z") ?? ""
        XCTAssertFalse(s.contains("T16"), s)
        XCTAssertFalse(s.contains("Z"), s)
        XCTAssertNotNil(EventTimes.date("2026-03-02T09:30"))
    }

    func testWordsPassThrough() {
        XCTAssertEqual(span("9:30 AM", "10:00 AM"), "9:30 AM – 10:00 AM")
        XCTAssertEqual(span("Noon", nil), "Noon")
        XCTAssertNil(span(nil, " "))
    }

    func testTwentyFourHourLocaleKeepsBothTimes() {
        XCTAssertEqual(span("2026-03-02T14:00:00Z", "2026-03-02T15:00:00Z", Locale(identifier: "en_GB")), "09:00 – 10:00")
    }

    // SPK-231 card reps: tracker and GitHub data arrive as raw ISO times; a card's corner reads them.
    func testStampReadsLikeACardCorner() {
        let now = EventTimes.date("2026-11-08T15:00:00Z")!   // 10:00 AM Sun Nov 8 in New York
        func stamp(_ s: String) -> String? {
            EventTimes.stamp(s, now: now, calendar: cal, locale: us)?.replacingOccurrences(of: "\u{202F}", with: " ")
        }
        XCTAssertEqual(stamp("2026-11-08T06:53:24Z"), "1:53 AM")
        XCTAssertEqual(stamp("2026-11-07T20:12:00Z"), "Yesterday")
        XCTAssertEqual(stamp("2026-11-04T12:00:00Z"), "Wed")
        XCTAssertEqual(stamp("2026-10-29T00:29:29Z"), "Oct 28")
        XCTAssertEqual(stamp("2025-10-29T12:00:00Z"), "Oct 29, 2025")
        XCTAssertEqual(stamp("2026-11-08"), "Today")
        XCTAssertEqual(stamp("2026-10-28"), "Oct 28")
        XCTAssertEqual(stamp("2h ago"), "2h ago")   // the agent's own words pass through
        XCTAssertNil(EventTimes.stamp("  ", now: now, calendar: cal, locale: us))
    }

    func testCalendarDayHeadingIsWords() {
        let now = EventTimes.date("2026-11-08T15:00:00Z")!
        XCTAssertEqual(EventTimes.dayTitle("2026-11-09", now: now, calendar: cal, locale: us), "Monday, November 9")
        XCTAssertEqual(EventTimes.dayTitle("2027-01-04", now: now, calendar: cal, locale: us), "Monday, January 4, 2027")
        XCTAssertEqual(EventTimes.dayTitle("Tomorrow", now: now, calendar: cal, locale: us), "Tomorrow")
    }
}
