import XCTest
@testable import SpeakeasyCore

/// "Work unavailable: cancelled" banners and cleared tasks coming back on the idle board:
/// a replaced or older poll must never write over newer state or show a banner.
final class StaleWorkTests: XCTestCase {
    private let done = TaskItem(id: "d1", info: WorkInfo(runID: "r1", status: "completed"))
    private let other = TaskItem(id: "d2", info: WorkInfo(runID: "r2", status: "completed"))

    private func board(_ tasks: [TaskItem]) -> VoiceState {
        var s = reduce(VoiceState(), .workOnlyOpened, now: Date())
        s = reduce(s, .tasks(tasks), now: Date())
        return s
    }

    // MARK: Reducer

    func testClearedTasksStayGoneAfterTheBoardReopens() {
        var s = board([done, other])
        s = reduce(s, .tasksDismissed(["r1", "r2"]), now: Date())
        XCTAssertTrue(s.tasks.isEmpty)
        // Back to the foreground / closed and reopened: then a list fetched before the server had the clear.
        s = reduce(s, .reset, now: Date())
        s = reduce(s, .workOnlyOpened, now: Date())
        s = reduce(s, .tasks([done, other]), now: Date())
        XCTAssertTrue(s.tasks.isEmpty, "a stale list never brings back what the user cleared")
    }

    func testClearedTasksStayGoneIntoTheNextCall() {
        var s = board([done])
        s = reduce(s, .tasksDismissed(["r1"]), now: Date())
        s = reduce(s, .startRequested, now: Date())
        s = reduce(s, .tasks([done]), now: Date())
        XCTAssertTrue(s.tasks.isEmpty)
    }

    func testReopeningAnOpenBoardKeepsItsTasks() {
        var s = board([done, other])
        s = reduce(s, .workOnlyOpened, now: Date())
        XCTAssertEqual(s.tasks.map(\.id), ["d1", "d2"], "never blanked while the next list loads")
        XCTAssertTrue(s.workOnly)
    }

    func testAClearedRunThatRunsAgainShows() {
        var s = board([done])
        s = reduce(s, .tasksDismissed(["r1"]), now: Date())
        let again = TaskItem(id: "d1", info: WorkInfo(runID: "r1", status: "working"))
        s = reduce(s, .tasks([again]), now: Date())
        XCTAssertEqual(s.tasks.map(\.id), ["d1"])
    }

    // MARK: Poll gate

    func testCancellationIsNeverABanner() {
        var gate = WorkPollGate()
        let loop = gate.startLoop()
        let ticket = gate.ticket(generation: loop)
        for _ in 0..<5 {
            XCTAssertNil(gate.failed(URLError(.cancelled), ticket: ticket))
            XCTAssertNil(gate.failed(CancellationError(), ticket: ticket))
            XCTAssertNil(gate.failed(NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled), ticket: ticket))
        }
        XCTAssertEqual(gate.consecutiveFailures, 0)
        XCTAssertNil(gate.failed(URLError(.timedOut), ticket: ticket, cancelled: true), "the task was cancelled")
    }

    func testAReplacedLoopsFailureIsQuietAndItsAnswerDropped() {
        var gate = WorkPollGate()
        let old = gate.ticket(generation: gate.startLoop())
        gate.startLoop()   // foreground again: a new loop replaces it
        for _ in 0..<5 { XCTAssertNil(gate.failed(URLError(.notConnectedToInternet), ticket: old)) }
        XCTAssertFalse(gate.accepts(old))
        XCTAssertTrue(gate.accepts(gate.ticket(generation: gate.generation)))
    }

    func testOnlyAPersistingFailureShowsOnce() {
        var gate = WorkPollGate()
        let ticket = gate.ticket(generation: gate.startLoop())
        XCTAssertNil(gate.failed(URLError(.timedOut), ticket: ticket))
        XCTAssertNil(gate.failed(URLError(.timedOut), ticket: ticket))
        gate.succeeded(ticket)
        XCTAssertNil(gate.failed(URLError(.timedOut), ticket: ticket))
        XCTAssertNil(gate.failed(URLError(.timedOut), ticket: ticket))
        let banner = gate.failed(URLError(.timedOut), ticket: ticket)
        XCTAssertNotNil(banner)
        XCTAssertEqual(banner, WorkPollGate.notice)
        XCTAssertNil(gate.failed(URLError(.timedOut), ticket: ticket), "shown once, not every 2 s")
    }

    // MARK: The notice (SPK-152)

    func testThreeFailuresShowAPlainNoticeWithoutTheSystemsText() {
        var gate = WorkPollGate()
        let ticket = gate.ticket(generation: gate.startLoop())
        var notice: String?
        for _ in 0..<WorkPollGate.failuresBeforeNotice { notice = gate.failed(URLError(.timedOut), ticket: ticket) }
        XCTAssertEqual(notice, "Can't load your work right now. Speakeasy keeps trying.")
        XCTAssertFalse(notice!.contains(URLError(.timedOut).localizedDescription), "never the system's error text")
        XCTAssertTrue(WorkPollGate.isNotice(notice))
    }

    func testAServerCodeRidesAlongForTheRewordingButNoOtherText() {
        struct Coded: LocalizedError { var errorDescription: String? { "Server HTTP 502" } }
        XCTAssertEqual(WorkPollGate.notice(for: Coded()), WorkPollGate.notice + " (HTTP 502)")
        let other = NSError(domain: "test", code: 1, userInfo: [NSLocalizedDescriptionKey: "The data couldn't be read."])
        XCTAssertEqual(WorkPollGate.notice(for: other), WorkPollGate.notice)
    }

    func testTheNoticeGoesWhenTheBoardLoadsAgain() {
        var gate = WorkPollGate()
        let ticket = gate.ticket(generation: gate.startLoop())
        var s = board([done])
        for _ in 0..<WorkPollGate.failuresBeforeNotice {
            if let notice = gate.failed(URLError(.notConnectedToInternet), ticket: ticket) { s = reduce(s, .error(notice), now: Date()) }
        }
        XCTAssertTrue(WorkPollGate.isNotice(s.lastError), "3 failures in a row: the notice shows")
        XCTAssertEqual(CallProblem.of(s)?.fix, .reloadBoard, "the board's Try again reloads the board")
        XCTAssertEqual(CallProblem.of(s)?.title, "Can't load your work right now")
        gate.succeeded(ticket)
        s = reduce(s, .workNoticeCleared, now: Date())
        XCTAssertNil(s.lastError, "one answer: the notice is gone")
        XCTAssertNil(CallProblem.of(s))
        XCTAssertEqual(s.tasks.map(\.id), ["d1"])
    }

    func testAPollAnswerNeverClearsACallError() {
        var s = reduce(VoiceState(), .error("Couldn't resume the paused call"), now: Date())
        s = reduce(s, .workNoticeCleared, now: Date())
        XCTAssertEqual(s.lastError, "Couldn't resume the paused call")
        var failed = reduce(VoiceState(), .startRequested, now: Date())
        failed = reduce(failed, .failed("The voice connection couldn't open"), now: Date())
        failed = reduce(failed, .workNoticeCleared, now: Date())
        XCTAssertEqual(failed.lastError, "The voice connection couldn't open")
        XCTAssertEqual(CallProblem.of(failed)?.fix, .retry, "a call failure keeps its own fix")
    }

    func testAListFetchedBeforeAClearIsDropped() {
        var gate = WorkPollGate()
        let loop = gate.startLoop()
        let before = gate.ticket(generation: loop)
        gate.listChanged()   // Clear tapped
        let during = gate.ticket(generation: loop)
        gate.listChanged()   // the server has it
        XCTAssertFalse(gate.accepts(before))
        XCTAssertFalse(gate.accepts(during), "sent while the clear was on its way")
        XCTAssertTrue(gate.accepts(gate.ticket(generation: loop)))
        XCTAssertFalse(gate.accepts(WorkPollGate.Ticket(generation: nil, epoch: before.epoch)))
        XCTAssertTrue(gate.accepts(gate.ticket()))
    }
}
