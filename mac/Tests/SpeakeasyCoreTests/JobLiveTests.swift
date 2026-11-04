import XCTest
@testable import SpeakeasyCore

/// The Mac redesign's live job logic: the raw tool call fields, the pinned call and its duration,
/// a running tile's glance, the job's mood and the board's greeting.
final class JobLiveTests: XCTestCase {
    let now = Date(timeIntervalSince1970: 1_793_820_300)

    private func info(_ extra: [String: Any], status: String = "running") -> WorkInfo {
        var json: [String: Any] = ["run_id": "run_1", "status": status, "title": "Fix the flaky checkout test"]
        json.merge(extra) { $1 }
        return WorkInfo(json: json)!
    }

    private func task(_ id: String, _ status: String, age: TimeInterval = 10, extra: [String: Any] = [:]) -> TaskItem {
        var json: [String: Any] = ["task_id": id, "run_id": "run_\(id)", "status": status, "title": "Job \(id)",
                                   "updated_at": now.timeIntervalSince1970 - age]
        json.merge(extra) { $1 }
        return TaskItem(json: json)!
    }

    // MARK: Raw call fields

    func testDecodesToolArgsResultAndTimes() {
        let i = info(["activity": [["id": "a1", "kind": "run", "text": "Ran the tests", "status": "failed",
                                    "tool": "terminal", "args": "npx playwright test", "result": "10 passed, 2 failed",
                                    "started_at": 1_793_820_009.0, "ended_at": 1_793_820_070.0]]])
        let a = i.activity[0]
        XCTAssertEqual(a.tool, "terminal")
        XCTAssertEqual(a.args, "npx playwright test")
        XCTAssertEqual(a.result, "10 passed, 2 failed")
        XCTAssertEqual(ToolCallLog.duration(a, now: now), 61)
        XCTAssertTrue(ToolCallLog.expandable(a))
    }

    func testRawFieldsAreOptionalAndCapped() {
        let i = info(["activity": [["kind": "read", "text": "Read a file"],
                                   ["kind": "read", "text": "Read b", "args": String(repeating: "x", count: 900),
                                    "result": String(repeating: "y", count: 900), "tool": "  "]]])
        XCTAssertNil(i.activity[0].tool); XCTAssertNil(i.activity[0].args); XCTAssertNil(i.activity[0].startedAt)
        XCTAssertFalse(ToolCallLog.expandable(i.activity[0]), "nothing more to show: the row doesn't open")
        XCTAssertEqual(i.activity[1].args?.count, 300)
        XCTAssertEqual(i.activity[1].result?.count, 500)
        XCTAssertNil(i.activity[1].tool, "a blank tool name is dropped")
    }

    func testContractFixtureWithoutRawCallsStillDecodes() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "work_activity", withExtension: "json", subdirectory: "Contract"))
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let work = try XCTUnwrap(WorkInfo(json: root["work"]))
        XCTAssertFalse(work.activity.isEmpty)
        XCTAssertTrue(work.activity.allSatisfy { $0.tool == nil || !$0.tool!.isEmpty }, "older payloads: no raw call, no problem")
    }

    // MARK: Pinned call and durations

    func testPinnedIsNewestRunningElseNewest() {
        let items = [JobActivityItem(id: "1", kind: .read, text: "a"),
                     JobActivityItem(id: "2", kind: .run, text: "b", status: .running),
                     JobActivityItem(id: "3", kind: .read, text: "c")]
        XCTAssertEqual(ToolCallLog.pinned(items)?.id, "2")
        XCTAssertEqual(ToolCallLog.pinned(Array(items.prefix(1)) + [items[2]])?.id, "3")
        XCTAssertNil(ToolCallLog.pinned([]))
    }

    func testDurationOnlyFromWhatWasSent() {
        let done = JobActivityItem(id: "1", kind: .read, text: "a", status: .done)
        XCTAssertNil(ToolCallLog.duration(done, now: now), "no times sent, no duration made up")
        let running = JobActivityItem(id: "2", kind: .run, text: "b", status: .running, startedAt: now - 75)
        XCTAssertEqual(ToolCallLog.duration(running, now: now), 75)
        let fromAt = JobActivityItem(id: "3", at: now - 5, kind: .run, text: "c", status: .running)
        XCTAssertEqual(ToolCallLog.duration(fromAt, now: now), 5)
        let backwards = JobActivityItem(id: "4", kind: .run, text: "d", startedAt: now, endedAt: now - 3)
        XCTAssertNil(ToolCallLog.duration(backwards, now: now))
    }

    func testDurationText() {
        XCTAssertEqual(ToolCallLog.durationText(0.42), "0.4 s")
        XCTAssertEqual(ToolCallLog.durationText(12), "12 s")
        XCTAssertEqual(ToolCallLog.durationText(125), "2 m 05 s")
        XCTAssertEqual(ToolCallLog.durationText(3720), "1 h 02 m")
        XCTAssertEqual(ToolCallLog.allLabel(1), "1 tool call")
        XCTAssertEqual(ToolCallLog.allLabel(15), "All 15 tool calls")
        XCTAssertNotEqual(ToolCallLog.listKey(job: "j"), ToolCallLog.rowKey(job: "j", item: "toolcalls"))
    }

    // MARK: Glance

    func testGlanceFollowsHeroThenStepsThenNow() {
        XCTAssertEqual(JobGlance.of(info(["hero": ["type": "trials", "total": 50, "passed": 31, "failed": 1]])),
                       .trials(passed: 31, failed: 1, total: 50))
        XCTAssertEqual(JobGlance.of(info(["hero": ["type": "diff", "files": 2, "added": 10, "removed": 9]])),
                       .diff(files: 2, added: 10, removed: 9))
        let sources = JobGlance.of(info(["hero": ["type": "sources", "items": (1...6).map { ["site": "s\($0).example"] }]]))
        XCTAssertEqual(sources, .sources(count: 6, sites: ["s3.example", "s4.example", "s5.example", "s6.example"]))
        XCTAssertEqual(JobGlance.of(info(["plan": ["One", "Two", "Three"], "step_index": 2, "step_count": 3])),
                       .steps(index: 2, count: 3))
        XCTAssertEqual(JobGlance.of(info(["activity": [["kind": "read", "text": "Read the config"]]])), .now("Read the config"))
        XCTAssertNil(JobGlance.of(info([:])), "nothing sent, nothing drawn")
    }

    // MARK: Mood and greeting

    func testMood() {
        let jobs = JobBoard.jobs([task("r", "running"), task("f", "failed"), task("d", "completed"), task("c", "cancelled")], now: now)
        func mood(_ id: String) -> LiveMood { LiveMood.of(jobs.first { $0.tasks.contains { $0.id == id } }!) }
        XCTAssertEqual(mood("r"), .working)
        XCTAssertEqual(mood("f"), .failed)
        XCTAssertEqual(mood("d"), .done)
        XCTAssertEqual(mood("c"), .quiet)
        XCTAssertTrue(LiveMood.working.isLive); XCTAssertFalse(LiveMood.done.isLive)
    }

    func testGreeting() {
        XCTAssertEqual(BoardGreeting.greeting(hour: 8), "Good morning")
        XCTAssertEqual(BoardGreeting.greeting(hour: 13), "Good afternoon")
        XCTAssertEqual(BoardGreeting.greeting(hour: 19), "Good evening")
        XCTAssertEqual(BoardGreeting.greeting(hour: 2), "Hello")
        let jobs = JobBoard.jobs([task("r", "running"), task("s", "running")], now: now)
        let sections = JobBoard.sections(jobs, seen: [], now: now)
        XCTAssertEqual(BoardGreeting.summary(sections), "2 running")
        XCTAssertEqual(BoardGreeting.summary(JobBoard.sections([], seen: [], now: now)), "")
        XCTAssertFalse(BoardGreeting.suggestions.isEmpty)
    }
}
