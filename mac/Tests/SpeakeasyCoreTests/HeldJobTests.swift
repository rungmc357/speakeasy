import XCTest
@testable import SpeakeasyCore

/// A job held behind another job on the same project: its card says "Waiting · after ..." and offers
/// "Start now anyway" (invented data).
final class HeldJobTests: XCTestCase {
    func testAWaitingItemCarriesWhatItWaitsForAndStartNow() throws {
        let item = try XCTUnwrap(NowItem(json: [
            "id": "s:s_held", "kind": "waiting", "run_id": "lr_0123abcd", "title": "Settings page cleanup",
            "line": "Waiting \u{00B7} after Paint search", "waiting_on": "Paint search", "can_start_now": true,
        ] as [String: Any]))
        XCTAssertEqual(item.kind, .waiting)
        XCTAssertEqual(item.line, "Waiting \u{00B7} after Paint search")
        XCTAssertEqual(item.waitingOn, "Paint search")
        XCTAssertTrue(item.canStartNow)
    }

    func testOnlyAWaitingJobWithARunCanStartNow() throws {
        let queued = try XCTUnwrap(NowItem(json: ["id": "s:a", "kind": "waiting", "title": "A", "line": "Waiting",
                                                  "can_start_now": true] as [String: Any]))
        XCTAssertFalse(queued.canStartNow)   // no run id to start
        let running = try XCTUnwrap(NowItem(json: ["id": "s:b", "kind": "working", "run_id": "run_b", "title": "B",
                                                   "line": "Reading", "can_start_now": true] as [String: Any]))
        XCTAssertFalse(running.canStartNow)
        let older = try XCTUnwrap(NowItem(json: ["id": "s:c", "kind": "waiting", "run_id": "run_c", "title": "C",
                                                 "line": "Starts when \u{201C}D\u{201D} finishes."] as [String: Any]))
        XCTAssertFalse(older.canStartNow)    // an older plugin: no button
    }
}
