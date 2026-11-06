import XCTest
@testable import SpeakeasyCore

/// The header's work line follows the board: a finished task the user cleared (or a failure he
/// already opened) no longer drives it, and never turns into "didn't get that".
final class ClearedWorkHeaderTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_000_000)

    private func failed(_ run: String = "run_f1", dismissed: Bool = false, seen: Bool = false) -> WorkInfo {
        var info = WorkInfo(runID: run, status: "failed", updated: t0)
        info.failure = WorkFailure(kind: "unknown", label: "Refused by the model's safety filter",
                                   text: "The model declined to work on this request.")
        info.dismissed = dismissed
        info.seen = seen
        return info
    }

    private func header(_ s: VoiceState) -> String { present(s).secondary }

    /// The board (no call) showing one failed task, its run in the header.
    private func boardWithFailure(at now: Date) -> VoiceState {
        var s = reduce(VoiceState(), .workOnlyOpened, now: now)
        s = reduce(s, .tasks([TaskItem(id: "t1", info: failed())]), now: now)
        s = reduce(s, .work(failed()), now: now)
        return s
    }

    func testFailedTaskShowsInTheHeaderUntilDismissed() {
        var s = boardWithFailure(at: t0)
        XCTAssertEqual(header(s), "Work failed · Refused by the model's safety filter")
        s = reduce(s, .tasksDismissed(["run_f1"]), now: t0 + 1)
        XCTAssertTrue(s.tasks.isEmpty)
        XCTAssertNil(s.workInfo)
        XCTAssertEqual(s.work, WorkPhase.none)
        XCTAssertFalse(header(s).contains("Work failed"))
    }

    func testTheBoardsNextPollOfTheSameRunDoesNotBringItBack() {
        var s = boardWithFailure(at: t0)
        s = reduce(s, .tasksDismissed(["run_f1"]), now: t0 + 1)
        // The board keeps fetching the latest run; the server marks it dismissed (or an old copy doesn't).
        s = reduce(s, .work(failed(dismissed: true)), now: t0 + 3)
        s = reduce(s, .work(failed()), now: t0 + 5)
        s = reduce(s, .tasks([TaskItem(id: "t1", info: failed())]), now: t0 + 5)
        XCTAssertNil(s.workInfo)
        XCTAssertFalse(header(s).contains("Work failed"))
    }

    func testClearingTheBoardEmptiesTheHeader() {
        var s = reduce(VoiceState(), .workOnlyOpened, now: t0)
        let done = TaskItem(id: "t2", info: WorkInfo(runID: "run_d2", status: "completed", updated: t0))
        s = reduce(s, .tasks([TaskItem(id: "t1", info: failed()), done]), now: t0)
        s = reduce(s, .work(failed()), now: t0)
        s = reduce(s, .tasksDismissed(["run_f1", "run_d2"]), now: t0 + 1)
        XCTAssertTrue(s.tasks.isEmpty)
        XCTAssertEqual(s.work, WorkPhase.none)
        XCTAssertEqual(header(s), present(reduce(VoiceState(), .workOnlyOpened, now: t0 + 1)).secondary)
    }

    func testClearedOnAnotherDeviceClearsTheHeader() {
        var s = boardWithFailure(at: t0)
        // Another device cleared it: the next row from the server says so.
        s = reduce(s, .work(failed(dismissed: true)), now: t0 + 2)
        XCTAssertNil(s.workInfo)
        XCTAssertFalse(header(s).contains("Work failed"))
    }

    func testAFailureTheUserOpenedNoLongerDrivesTheHeader() {
        var s = boardWithFailure(at: t0)
        s = reduce(s, .tasks([TaskItem(id: "t1", info: failed(seen: true))]), now: t0 + 2)
        XCTAssertNil(s.workInfo)
        XCTAssertFalse(header(s).contains("Work failed"))
        // The task itself stays on the board until cleared.
        XCTAssertEqual(s.tasks.map(\.id), ["t1"])
    }

    func testADismissedFailureNeverSaysDidntGetThat() {
        // In a call: handed off, the run failed, the user cleared it, and time passes.
        var s = reduce(VoiceState(), .startRequested, now: t0)
        s = reduce(s, .sessionAdmitted(interactionID: "vi_1"), now: t0)
        s = reduce(s, .sessionStarted, now: t0)
        s = reduce(s, .delegationCreated, now: t0 + 1)
        s = reduce(s, .interaction(InteractionSnapshot(status: "failed", runID: "run_f1")), now: t0 + 2)
        s = reduce(s, .tasks([TaskItem(id: "t1", info: failed())]), now: t0 + 3)
        s = reduce(s, .work(failed()), now: t0 + 3)
        XCTAssertTrue(header(s).hasPrefix("Work failed"))
        s = reduce(s, .tasksDismissed(["run_f1"]), now: t0 + 4)
        s = reduce(s, .interaction(InteractionSnapshot(status: "failed", runID: "run_f1")), now: t0 + 5)
        s = reduce(s, .tick, now: t0 + 4 + VoiceState.notReceivedAfter + 30)
        XCTAssertNil(s.delegationAt)
        XCTAssertEqual(s.work, WorkPhase.none)
        XCTAssertFalse(header(s).contains("didn't get that"))
        XCTAssertFalse(header(s).contains("Waiting for"))
        XCTAssertFalse(header(s).contains("Work failed"))
    }

    func testANewRunAfterAClearedFailureShowsNormally() {
        var s = boardWithFailure(at: t0)
        s = reduce(s, .tasksDismissed(["run_f1"]), now: t0 + 1)
        let next = WorkInfo(runID: "run_n2", status: "working", updated: t0 + 2,
                            shortStatus: "Checking the forecast", updatedAt: t0 + 2)
        s = reduce(s, .work(next), now: t0 + 2)
        XCTAssertEqual(s.workInfo?.runID, "run_n2")
        XCTAssertEqual(header(s), "Checking the forecast")
    }

    func testANewCallAfterAFailureShowsOnlyTheNewCall() {
        var s = boardWithFailure(at: t0)
        s = reduce(s, .startRequested, now: t0 + 10)
        s = reduce(s, .sessionAdmitted(interactionID: "vi_2"), now: t0 + 10)
        s = reduce(s, .sessionStarted, now: t0 + 11)
        XCTAssertNil(s.workInfo)
        XCTAssertFalse(header(s).contains("Work failed"))
        let next = WorkInfo(runID: "run_n3", status: "working", updated: t0 + 12,
                            shortStatus: "Looking up flights", updatedAt: t0 + 12)
        s = reduce(s, .delegationCreated, now: t0 + 12)
        s = reduce(s, .interaction(InteractionSnapshot(status: "working", runID: "run_n3")), now: t0 + 12)
        s = reduce(s, .work(next), now: t0 + 12)
        XCTAssertEqual(header(s), "Looking up flights")
    }

    func testAnUnclearedFailureStillShows() {
        let s = boardWithFailure(at: t0)
        XCTAssertEqual(s.workInfo?.runID, "run_f1")
        XCTAssertEqual(header(s), "Work failed · Refused by the model's safety filter")
    }

    func testDecodesDismissed() {
        let info = WorkInfo(json: ["run_id": "run_x", "status": "failed", "dismissed": true])
        XCTAssertEqual(info?.dismissed, true)
        XCTAssertEqual(WorkInfo(json: ["run_id": "run_x", "status": "failed"])?.dismissed, false)
    }
}
