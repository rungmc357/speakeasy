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
}
