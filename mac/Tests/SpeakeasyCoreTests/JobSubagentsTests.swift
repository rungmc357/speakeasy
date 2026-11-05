import XCTest
@testable import SpeakeasyCore

/// Helpers a job started (`capabilities.job_subagents`): decoded when sent, nothing when not.
/// Invented data only.
final class JobSubagentsTests: XCTestCase {
    private func info(_ extra: [String: Any]) -> WorkInfo {
        var json: [String: Any] = ["run_id": "run_1", "status": "running", "title": "Compare the invented shops"]
        json.merge(extra) { $1 }
        return WorkInfo(json: json)!
    }

    private let three: [[String: Any]] = [
        ["id": "sa-0-aaaa1111", "title": "Compare the shops' return policies", "status": "done",
         "started_at": 1793890000.0, "ended_at": 1793890087.0, "summary": "Shop B has the longest window.\nMore detail.",
         "goal": "Compare the return policies of the three invented shops", "steps_total": 6, "tool_count": 6],
        ["id": "sa-1-bbbb2222", "title": "Find the cheapest parcel shipping", "status": "running",
         "started_at": 1793890000.0, "now": "Searched the web for “parcel rates”", "steps_total": 3,
         "activity": [["id": "a1", "kind": "search", "text": "Searched the web for “parcel rates”", "status": "running",
                       "tool": "web_search", "args": "parcel rates"]]],
        ["id": "sa-2-cccc3333", "title": "Check island delivery", "status": "failed", "summary": "The page did not load.",
         "depth": 2, "parent_id": "sa-0-aaaa1111"],
    ]

    func testOlderPluginHasNoHelpers() {
        let i = info([:])
        XCTAssertTrue(i.subagents.isEmpty)
        XCTAssertNil(i.helpersSummary)
        XCTAssertFalse(i.hasLiveActivity)
    }

    func testDecodesHelpers() {
        let i = info(["subagents": three])
        XCTAssertEqual(i.subagents.map(\.status), [.done, .running, .failed])
        XCTAssertEqual(i.subagents[0].line, "Shop B has the longest window.")
        XCTAssertEqual(i.subagents[1].line, "Searched the web for “parcel rates”")
        XCTAssertEqual(i.subagents[2].line, "The page did not load.")
        XCTAssertEqual(i.subagents[2].depth, 2)
        XCTAssertEqual(i.subagents[2].parentID, "sa-0-aaaa1111")
        XCTAssertEqual(i.subagents[1].activity.first?.tool, "web_search")
        XCTAssertEqual(i.subagents[0].elapsed(now: Date()), 87)
        XCTAssertEqual(i.helpersSummary, "1 of 3 working")
        XCTAssertTrue(i.hasLiveActivity)
    }

    func testBadRowsAreSkippedAndUnknownStatusIsRunning() {
        let i = info(["subagents": [["title": "no id"], ["id": "s1", "status": "weird"], ["id": "s1", "title": "dup"],
                                    "nonsense"]])
        XCTAssertEqual(i.subagents.count, 1)
        XCTAssertEqual(i.subagents[0].status, .running)
        XCTAssertEqual(i.subagents[0].title, "Helper")
        XCTAssertEqual(i.subagents[0].line, "Getting started")
    }

    func testNowIsOnlyForARunningHelperAndStoppedSaysWhy() {
        let i = info(["subagents": [["id": "s1", "status": "stopped", "now": "Read notes.md", "note": "Stopped with the task"]]])
        XCTAssertNil(i.subagents[0].now)
        XCTAssertEqual(i.subagents[0].line, "Stopped with the task")
        XCTAssertNil(i.subagents[0].elapsed(now: Date()))
        XCTAssertEqual(i.helpersSummary, "1 helper")
    }

    func testTheDetailsStepsFillInTheStreamsRow() {
        let stream = info(["subagents": three.map { var h = $0; h.removeValue(forKey: "activity"); return h }])
        let detail = info(["subagents": three])
        let merged = stream.subagents[1].merged(with: detail.subagents[1])
        XCTAssertEqual(merged.activity.count, 1)
        XCTAssertEqual(stream.subagents[0].merged(with: detail.subagents[1]), stream.subagents[0], "a different helper is ignored")
    }

    func testCapabilityFlag() throws {
        let caps = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"job_subagents": true}"#.utf8))
        XCTAssertTrue(caps.jobSubagents)
        let old = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"job_activity": true}"#.utf8))
        XCTAssertFalse(old.jobSubagents)
    }
}
