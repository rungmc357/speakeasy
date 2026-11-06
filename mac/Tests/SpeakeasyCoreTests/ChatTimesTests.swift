import XCTest
@testable import SpeakeasyCore

/// Timestamps on session pages: day dividers, times after a pause, a task's started/done/took line,
/// when a group of steps ran, and "Done 9:05\u{202F}PM" on finished task lines.
final class ChatTimesTests: XCTestCase {
    private var cal: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "America/New_York")!
        return c
    }()
    private let us = Locale(identifier: "en_US")
    /// Thu Nov 5 2026, 9:00\u{202F}PM Eastern.
    private var evening: Date { cal.date(from: DateComponents(year: 2026, month: 11, day: 5, hour: 21))! }

    private func at(_ minutes: Double, days: Int = 0) -> Date {
        evening.addingTimeInterval(minutes * 60 + Double(days) * 86_400)
    }

    func testDayDividerOnTheFirstMessageOfEachDayAndTimesOnlyAfterAPause() {
        let items: [(id: String, at: Date?)] = [
            ("y1", at(0, days: -1)), ("y2", at(2, days: -1)),          // yesterday, one stretch
            ("t1", at(0)), ("t2", at(3)), ("t3", at(10)),               // today: no pause under 15 min
            ("note", nil),                                              // untimed: never marked
            ("t4", at(40)), ("t5", at(41)),                             // a 30-min pause: a time
        ]
        let marks = ChatTimeline.marks(items, now: at(45), calendar: cal, locale: us)
        XCTAssertEqual(marks["y1"], .init(day: "Yesterday", time: "9:00\u{202F}PM"))
        XCTAssertNil(marks["y2"])
        XCTAssertEqual(marks["t1"], .init(day: "Today", time: "9:00\u{202F}PM"))
        XCTAssertNil(marks["t2"]); XCTAssertNil(marks["t3"]); XCTAssertNil(marks["note"])
        XCTAssertEqual(marks["t4"], .init(day: nil, time: "9:40\u{202F}PM"))
        XCTAssertNil(marks["t5"])
        XCTAssertEqual(Set(marks.keys), ["y1", "t1", "t4"])
    }

    func testDayLabels() {
        let now = at(0)
        XCTAssertEqual(ChatTimeline.day(at(-60), now: now, calendar: cal, locale: us), "Today")
        XCTAssertEqual(ChatTimeline.day(at(0, days: -1), now: now, calendar: cal, locale: us), "Yesterday")
        XCTAssertEqual(ChatTimeline.day(at(0, days: -3), now: now, calendar: cal, locale: us), "Monday")
        XCTAssertEqual(ChatTimeline.day(at(0, days: -30), now: now, calendar: cal, locale: us), "Tue, Oct 6")
        XCTAssertEqual(ChatTimeline.day(at(0, days: -400), now: now, calendar: cal, locale: us), "Oct 1, 2025")
    }

    func testExactTimeOnTap() {
        XCTAssertEqual(ChatTimeline.exact(at(5), now: at(10), calendar: cal, locale: us), "9:05\u{202F}PM")
        XCTAssertEqual(ChatTimeline.exact(at(5, days: -1), now: at(10), calendar: cal, locale: us), "Yesterday, 9:05\u{202F}PM")
    }

    private func job(_ status: String, started: Double?, finished: Double? = nil) -> WorkInfo {
        var o: [String: Any] = ["run_id": UUID().uuidString, "status": status, "title": "Rename the report"]
        if let started { o["started_at"] = at(started).timeIntervalSince1970 }
        if let finished { o["finished_at"] = at(finished).timeIntervalSince1970 }
        return WorkInfo(json: o)!
    }

    func testTaskViewStartedDoneTook() {
        XCTAssertEqual(TaskTimes.line([job("completed", started: -8, finished: 5)], now: at(20), calendar: cal, locale: us),
                       "Started 8:52\u{202F}PM · Done 9:05\u{202F}PM · Took 13 min")
        XCTAssertEqual(TaskTimes.line([job("running", started: -8)], now: at(5), calendar: cal, locale: us),
                       "Started 8:52\u{202F}PM · 13 min so far")
        // A follow-up in the same view: one stretch, earliest start to latest finish.
        XCTAssertEqual(TaskTimes.line([job("completed", started: 30, finished: 95), job("completed", started: 0, finished: 10)],
                                      now: at(100), calendar: cal, locale: us),
                       "Started 9:00\u{202F}PM · Done 10:35\u{202F}PM · Took 1 h 35 min")
        XCTAssertEqual(TaskTimes.line([job("failed", started: 0, finished: 1)], now: at(5), calendar: cal, locale: us),
                       "Started 9:00\u{202F}PM · Ended 9:01\u{202F}PM · Took 1 min")
        XCTAssertNil(TaskTimes.line([job("completed", started: nil)], now: at(5), calendar: cal, locale: us))
        XCTAssertNil(TaskTimes.line([], now: at(5), calendar: cal, locale: us))
    }

    func testTaskPageHeaderIsTheNewestJobAndItsFollowUps() {
        func j(_ run: String, _ job: String?, _ status: String, _ s: Double, _ f: Double?) -> WorkInfo {
            var o: [String: Any] = ["run_id": run, "status": status, "started_at": at(s).timeIntervalSince1970]
            if let f { o["finished_at"] = at(f).timeIntervalSince1970 }
            if let job { o["job"] = ["id": job, "name": "Polish the app"] }
            return WorkInfo(json: o)!
        }
        let jobs = [j("r3", "jB", "completed", 60, 70), j("r2", "jB", "completed", 40, 50), j("r1", "jA", "completed", 0, 20)]
        XCTAssertEqual(TaskTimes.header(jobs, now: at(80), calendar: cal, locale: us),
                       "Started 9:40\u{202F}PM · Done 10:10\u{202F}PM · Took 30 min")
        XCTAssertEqual(TaskTimes.header([j("r1", nil, "running", 0, nil), j("r0", nil, "completed", -60, -50)],
                                        now: at(5), calendar: cal, locale: us),
                       "Started 9:00\u{202F}PM · 5 min so far")
    }

    func testAReplyWithItsStepsBeginsAtTheFirstStep() {
        let step = JobActivityItem(id: "a1", at: at(-5), kind: .run, text: "Ran the tests", tool: "terminal",
                                   startedAt: at(-5), endedAt: at(-4))
        XCTAssertEqual(ChatTimeline.began(at(3), tools: [step]), at(-5))
        XCTAssertEqual(ChatTimeline.began(at(3), tools: []), at(3))
        XCTAssertNil(ChatTimeline.began(nil, tools: []))
        // Steps before midnight, reply after: the divider goes above the steps, not between.
        let marks = ChatTimeline.marks([("u1", at(-10)), ("a1", ChatTimeline.began(at(3), tools: [step]))],
                                       now: at(5), calendar: cal, locale: us)
        XCTAssertNil(marks["a1"])
    }

    func testDurations() {
        XCTAssertEqual(TaskTimes.duration(0.2), "1 s")
        XCTAssertEqual(TaskTimes.duration(40), "40 s")
        XCTAssertEqual(TaskTimes.duration(13 * 60 + 10), "13 min")
        XCTAssertEqual(TaskTimes.duration(2 * 3600), "2 h")
        XCTAssertEqual(TaskTimes.duration(64 * 60), "1 h 4 min")
    }

    func testFinishedTaskLineSaysDoneAndWhen() {
        let line = TaskLineCopy.line(job("completed", started: 0, finished: 5), now: at(30), calendar: cal, locale: us)
        XCTAssertEqual(line, "Rename the report · Done 9:05\u{202F}PM")
        XCTAssertEqual(TaskLineCopy.outcome(job("cancelled", started: 0, finished: 5), now: at(30, days: 2), calendar: cal, locale: us),
                       "Stopped Thu 9:05\u{202F}PM")
        XCTAssertEqual(TaskLineCopy.outcome(job("completed", started: nil), now: at(30), calendar: cal, locale: us), "Done")
    }

    func testWhenAGroupOfStepsRan() {
        let items = [
            JobActivityItem(id: "a1", at: at(2), kind: .search, text: "Searched the web", tool: "web_search",
                            startedAt: at(2), endedAt: at(2).addingTimeInterval(4)),
            JobActivityItem(id: "a2", at: at(2.1), kind: .read, text: "Read a page", tool: "web_extract",
                            startedAt: at(2.1), endedAt: at(2.1).addingTimeInterval(2)),
        ]
        XCTAssertEqual(ToolCallLog.startedAt(items), at(2))
        XCTAssertEqual(ToolCallLog.whenLine(items, now: at(10), calendar: cal, locale: us), "9:02\u{202F}PM · 6 s")
        var running = items
        running[1].status = .running; running[1].endedAt = nil
        XCTAssertEqual(ToolCallLog.whenLine(running, now: at(10), calendar: cal, locale: us), "9:02\u{202F}PM", "no total while it runs")
        XCTAssertNil(ToolCallLog.whenLine([JobActivityItem(id: "x", kind: .think, text: "Thinking it over")], now: at(10)))
    }
}
