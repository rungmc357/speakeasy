import XCTest
@testable import SpeakeasyCore

/// Session pages: finished tasks sit where they were asked; only live ones stay at the bottom.
final class SessionTaskLayoutTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    private func job(_ run: String, _ title: String?, status: String, started: TimeInterval?, finished: TimeInterval? = nil,
                     ask: String? = nil) -> WorkInfo {
        var o: [String: Any] = ["run_id": run, "status": status]
        if let title { o["title"] = title }
        if let started { o["started_at"] = t0.timeIntervalSince1970 + started }
        if let finished { o["finished_at"] = t0.timeIntervalSince1970 + finished }
        if let ask { o["events"] = [["kind": "request", "text": ask]] }
        return WorkInfo(json: o)!
    }

    private func user(_ id: String, _ text: String, _ at: TimeInterval?) -> SessionTaskLayout.Anchor {
        .init(id: id, isUser: true, at: at.map { t0.addingTimeInterval($0) }, text: text)
    }
    private func reply(_ id: String, _ at: TimeInterval) -> SessionTaskLayout.Anchor {
        .init(id: id, isUser: false, at: t0.addingTimeInterval(at), text: "Done.")
    }

    private var thread: [SessionTaskLayout.Anchor] {
        [user("u1", "Fix the login redirect", 0), reply("a1", 60),
         user("u2", "Make the export button bigger", 120), reply("a2", 200),
         user("u3", "Now add a dark mode toggle", 300), reply("a3", 320)]
    }

    func testFinishedTasksSitAfterTheirAskNotAtTheBottom() {
        // Newest first, as the server sends them.
        let jobs = [job("r2", "Bigger export button", status: "completed", started: 121, finished: 190),
                    job("r1", "Fixed the login redirect", status: "completed", started: 1, finished: 55)]
        let layout = SessionTaskLayout(jobs: jobs, messages: thread, hasMore: false)
        XCTAssertEqual(layout.after["a1"]?.map(\.runID), ["r1"], "closes its exchange, after the reply")
        XCTAssertEqual(layout.after["a2"]?.map(\.runID), ["r2"])
        XCTAssertTrue(layout.pinned.isEmpty, "nothing finished stays at the bottom")
        XCTAssertFalse(layout.showsFinishedRow)
    }

    func testOnlyRunningAndNeedsYouArePinnedAndFinishingMovesItBack() {
        let running = job("r3", "Dark mode toggle", status: "running", started: 301)
        let approval = job("r4", "Delete the old exports", status: "waiting_for_approval", started: 305)
        let done = job("r1", "Fixed the login redirect", status: "completed", started: 1, finished: 55)
        var layout = SessionTaskLayout(jobs: [approval, running, done], messages: thread, hasMore: false)
        XCTAssertEqual(layout.pinned.map(\.runID), ["r3", "r4"], "oldest first above the composer")
        XCTAssertNil(layout.after["a3"])

        var finished = running
        finished.status = "completed"
        layout = SessionTaskLayout(jobs: [approval, finished, done], messages: thread, hasMore: false)
        XCTAssertEqual(layout.pinned.map(\.runID), ["r4"])
        XCTAssertEqual(layout.after["a3"]?.map(\.runID), ["r3"], "back in its place in the thread")
    }

    func testAnApprovalCardStandsInForItsTask() {
        let waiting = job("r4", "Delete the old exports", status: "waiting_for_approval", started: 305)
        let layout = SessionTaskLayout(jobs: [waiting], messages: thread, hasMore: false, asking: ["r4"])
        XCTAssertTrue(layout.pinned.isEmpty)
        XCTAssertTrue(layout.inThread.isEmpty)
    }

    func testDuplicatesFoldIntoOne() {
        let jobs = [job("r9", "Fix asterisk bold in chat bubbles", status: "completed", started: 301, finished: 330),
                    job("r8", "Fix asterisk bold in chat bubbles.", status: "completed", started: 122, finished: 150),
                    job("r7", "fix Asterisk bold in chat bubbles", status: "failed", started: 2, finished: 20)]
        let layout = SessionTaskLayout(jobs: jobs, messages: thread, hasMore: false)
        XCTAssertEqual(layout.finished.map(\.runID), ["r9"])
        XCTAssertEqual(layout.inThread.map(\.runID), ["r9"])
    }

    func testARunningTwinHidesTheFinishedCopy() {
        let jobs = [job("r9", "Make stopped task dismissible", status: "running", started: 301),
                    job("r8", "Make stopped task dismissible", status: "completed", started: 122, finished: 150)]
        let layout = SessionTaskLayout(jobs: jobs, messages: thread, hasMore: false)
        XCTAssertEqual(layout.pinned.map(\.runID), ["r9"])
        XCTAssertTrue(layout.inThread.isEmpty)
    }

    func testUntitledTasksFoldByTheirAsk() {
        let jobs = [job("r2", nil, status: "completed", started: 301, ask: "Check the weather"),
                    job("r1", nil, status: "completed", started: 1, ask: "check the weather!")]
        XCTAssertEqual(SessionTaskLayout(jobs: jobs, messages: thread, hasMore: false).finished.map(\.runID), ["r2"])
        let distinct = [job("r2", nil, status: "completed", started: 301), job("r1", nil, status: "completed", started: 1)]
        XCTAssertEqual(SessionTaskLayout(jobs: distinct, messages: thread, hasMore: false).finished.count, 2)
    }

    func testTheAsksWordsWinOverTime() {
        // Admitted late (a slow handoff) but the request names the second message.
        let late = job("r2", "Bigger export button", status: "completed", started: 310, ask: "Make the export button bigger")
        let layout = SessionTaskLayout(jobs: [late], messages: thread, hasMore: false)
        XCTAssertEqual(layout.after["a2"]?.map(\.runID), ["r2"])
    }

    func testAskStoredJustAfterAdmissionStillCounts() {
        let messages = [user("u1", "Hello", 0), reply("a1", 5), user("u2", "Rename the report", 108), reply("a2", 140)]
        let j = job("r1", "Renamed the report", status: "completed", started: 100)
        XCTAssertEqual(SessionTaskLayout(jobs: [j], messages: messages, hasMore: false).after["a2"]?.map(\.runID), ["r1"])
    }

    func testTasksAskedBeforeTheLoadedPageGoToTheCollapsedRow() {
        let old = job("r0", "Older work", status: "completed", started: -5_000, finished: -4_000)
        let recent = job("r1", "Fixed the login redirect", status: "completed", started: 1)
        let layout = SessionTaskLayout(jobs: [recent, old], messages: thread, hasMore: true)
        XCTAssertEqual(layout.earlier.map(\.runID), ["r0"])
        XCTAssertTrue(layout.showsFinishedRow)
        XCTAssertEqual(layout.finished.count, 2, "the row lists the chat's whole history")
        XCTAssertEqual(TaskLineCopy.finishedRow(layout.finished.count), "2 tasks finished in this chat")

        // The whole chat is loaded: it goes at the top instead, no extra row.
        let whole = SessionTaskLayout(jobs: [recent, old], messages: thread, hasMore: false)
        XCTAssertEqual(whole.top.map(\.runID), ["r0"])
        XCTAssertFalse(whole.showsFinishedRow)
    }

    func testAnAskWithNoReplyYetKeepsTheLineUnderIt() {
        let messages = [user("u1", "Rename the report", 0), user("u2", "Also the folder", 30)]
        let j = job("r1", "Renamed the report", status: "failed", started: 1)
        XCTAssertEqual(SessionTaskLayout(jobs: [j], messages: messages, hasMore: false).after["u1"]?.map(\.runID), ["r1"])
    }

    func testNoStartTimeSitsAfterTheNewestMessage() {
        let j = job("r1", "Something", status: "completed", started: nil)
        XCTAssertEqual(SessionTaskLayout(jobs: [j], messages: thread, hasMore: false).after["a3"]?.map(\.runID), ["r1"])
    }

    func testQuietLineCopy() {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "America/New_York")!
        let us = Locale(identifier: "en_US")
        let now = t0.addingTimeInterval(3_600)
        let done = job("r1", "Fixed asterisk bold in chat bubbles", status: "completed", started: 0, finished: 60)
        let line = TaskLineCopy.line(done, now: now, calendar: cal, locale: us)
        XCTAssertTrue(line.hasPrefix("Fixed asterisk bold in chat bubbles · "), line)
        XCTAssertTrue(line.hasSuffix("PM") || line.hasSuffix("AM"), line)
        let failed = job("r2", "Rename the report", status: "failed", started: 0, finished: 60)
        XCTAssertTrue(TaskLineCopy.line(failed, now: now, calendar: cal, locale: us).contains("Rename the report · Didn't finish · "))
        let stopped = job("r3", "Rename the report", status: "cancelled", started: 0, finished: 60)
        XCTAssertTrue(TaskLineCopy.line(stopped, now: now, calendar: cal, locale: us).contains("· Stopped · "))
        XCTAssertEqual(TaskLineCopy.finishedRow(1), "1 task finished in this chat")
        let lastWeek = TaskLineCopy.time(t0, now: t0.addingTimeInterval(10 * 86_400), calendar: cal, locale: us)
        XCTAssertFalse(lastWeek.contains(":"), lastWeek)
    }
}
