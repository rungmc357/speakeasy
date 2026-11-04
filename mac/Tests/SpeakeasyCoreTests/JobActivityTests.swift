import XCTest
@testable import SpeakeasyCore

/// The live job page's optional fields (`capabilities.job_activity`): decoded when sent, absent
/// (and the page as before) when not, and the hero picked only from what the server reported.
final class JobActivityTests: XCTestCase {
    private func info(_ extra: [String: Any]) -> WorkInfo {
        var json: [String: Any] = ["run_id": "run_1", "status": "running", "title": "Fix the flaky checkout test"]
        json.merge(extra) { $1 }
        return WorkInfo(json: json)!
    }

    func testOlderPluginHasNoActivity() {
        let i = info([:])
        XCTAssertTrue(i.activity.isEmpty)
        XCTAssertTrue(i.findings.isEmpty)
        XCTAssertTrue(i.touched.isEmpty)
        XCTAssertNil(i.hero)
        XCTAssertFalse(i.hasLiveActivity)
        XCTAssertNil(JobHero.select(i))
    }

    func testDecodesActivityFindingsTouched() {
        let i = info([
            "activity": [["id": "a1", "at": 1793820000.1, "kind": "read", "text": "Read checkout.spec.ts", "status": "done"],
                         ["id": "a2", "kind": "run", "text": "Ran the test 50 times", "detail": "31 of 50", "status": "running"],
                         ["kind": "nonsense", "text": "Looked something up"],
                         ["kind": "read"]],
            "findings": [["id": "f1", "text": "The test doesn't wait for the payment mock: fails 1 in 6"]],
            "touched": [["kind": "file", "label": "checkout.spec.ts", "detail": "+12 −3"],
                        ["kind": "file", "label": "checkout.spec.ts"],
                        ["kind": "url", "label": "docs.example.com", "url": "https://docs.example.com/waits"],
                        ["kind": "url", "label": "plain", "url": "http://insecure.example.com"],
                        ["kind": "command", "label": "npm test"]],
        ])
        XCTAssertEqual(i.activity.map(\.text), ["Read checkout.spec.ts", "Ran the test 50 times", "Looked something up"])
        XCTAssertEqual(i.activity[1].status, .running)
        XCTAssertEqual(i.activity[1].detail, "31 of 50")
        XCTAssertEqual(i.activity[2].kind, .tool, "an unknown kind still shows, as a generic tool")
        XCTAssertNotNil(i.activity[0].at)
        XCTAssertEqual(i.findings.first?.text, "The test doesn't wait for the payment mock: fails 1 in 6")
        XCTAssertEqual(i.touched.map(\.label), ["checkout.spec.ts", "docs.example.com", "plain", "npm test"], "de-duplicated")
        XCTAssertEqual(i.touched[1].url?.host, "docs.example.com")
        XCTAssertNil(i.touched[2].url, "only https links open")
        XCTAssertTrue(i.hasLiveActivity)
    }

    func testCapsActivityAtAHundred() {
        let items = (0..<130).map { ["id": "a\($0)", "kind": "read", "text": "Step \($0)"] }
        let i = info(["activity": items, "activity_total": 500])
        XCTAssertEqual(i.activity.count, 100)
        XCTAssertEqual(i.activity.last?.text, "Step 129", "newest kept")
        XCTAssertEqual(i.activityTotal, 100, "a total is never more than the server keeps")
        XCTAssertNil(info(["activity": items]).activityTotal)
    }

    func testToolCallFields() {
        let i = info(["activity": [
            ["id": "a1", "kind": "run", "text": "Ran the tests", "status": "done", "tool": "terminal",
             "started_at": 1793820000.0, "ended_at": 1793820007.5, "args": "npm test", "result": String(repeating: "x", count: 900)],
            ["id": "a2", "kind": "think", "text": "Thinking it over"],
            ["id": "a3", "kind": "read", "text": "Read a.ts", "tool": "bad name; rm", "status": "running", "started_at": 1793820010.0],
        ], "activity_total": 23])
        let a = i.activity[0]
        XCTAssertEqual(a.tool, "terminal")
        XCTAssertEqual(a.args, "npm test")
        XCTAssertEqual(a.result?.count, 500, "result capped")
        XCTAssertEqual(a.duration(now: Date()) ?? 0, 7.5, accuracy: 0.01)
        XCTAssertNil(i.activity[2].tool, "an odd tool name isn't shown")
        XCTAssertEqual(i.activity[2].duration(now: Date(timeIntervalSince1970: 1793820013)) ?? 0, 3, accuracy: 0.01, "running: up to now")
        XCTAssertEqual(i.toolCalls.map(\.id), ["a1", "a3"], "thinking isn't a tool call")
        XCTAssertEqual(i.activityTotal, 23)
        XCTAssertEqual(JobActivityItem.durationText(0.42), "0.4 s")
        XCTAssertEqual(JobActivityItem.durationText(127), "2 min 7 s")
        XCTAssertEqual(JobActivityItem.durationText(3840), "1 h 4 min")
        let slim = JobActivityItem(id: "a1", kind: .run, text: "Ran the tests", tool: "terminal")
        XCTAssertEqual(slim.merged(with: a).args, "npm test", "the detail fills in the stream's summary")
        let caps = try? JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"tool_log": true}"#.utf8))
        XCTAssertEqual(caps?.toolLog, true)
    }

    func testHeroKinds() {
        XCTAssertEqual(info(["hero": ["type": "trials", "total": 50, "passed": 31, "failed": 0]]).hero,
                       .trials(total: 50, passed: 31, failed: 0))
        XCTAssertEqual(info(["hero": ["type": "trials", "total": 10, "passed": 12, "failed": 4]]).hero,
                       .trials(total: 10, passed: 10, failed: 0), "counts clamp to the total")
        XCTAssertNil(info(["hero": ["type": "trials", "total": 0]]).hero)
        XCTAssertEqual(info(["hero": ["type": "sources", "items": [["title": "Pricing", "site": "tasklane.example"]]]]).hero,
                       .sources([JobHero.Source(title: "Pricing", site: "tasklane.example")]))
        XCTAssertNil(info(["hero": ["type": "sources", "items": []]]).hero)
        XCTAssertEqual(info(["hero": ["type": "diff", "files": 3, "added": 48, "removed": 12, "preview": "+ a\n- b\n\n+ c"]]).hero,
                       .diff(files: 3, added: 48, removed: 12, preview: ["+ a", "- b", "+ c"]))
        let series = info(["hero": ["type": "series", "label": "Signups", "unit": "/h",
                                    "points": [["x": "1", "y": 40], ["x": "2", "y": 6]]]]).hero
        XCTAssertEqual(series, .series(label: "Signups", unit: "/h", points: [.init(x: "1", y: 40), .init(x: "2", y: 6)]))
        XCTAssertNil(info(["hero": ["type": "series", "points": [["x": "1", "y": 1]]]]).hero, "one point is not a chart")
        XCTAssertNil(info(["hero": ["type": "confetti"]]).hero)
    }

    func testHeroSelection() {
        let plan = ["Reproduce it", "Find the cause", "Run it 50 times", "Open a pull request"]
        // The server's hero wins.
        XCTAssertEqual(JobHero.select(info(["plan": plan, "hero": ["type": "trials", "total": 50, "passed": 3]]))?.kind, "trials")
        // No hero, a plan: the step path while it runs; nothing once finished.
        XCTAssertEqual(JobHero.select(info(["plan": plan])), .steps)
        XCTAssertNil(JobHero.select(info(["plan": plan, "status": "completed"])))
        // "steps" asked for but no plan to draw: nothing.
        XCTAssertNil(JobHero.select(info(["hero": ["type": "steps"]])))
        XCTAssertNil(JobHero.select(info(["plan": ["Only one"]])))
    }

    func testCapabilityFlag() throws {
        let on = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"job_activity": true}"#.utf8))
        XCTAssertTrue(on.jobActivity)
        let off = try JSONDecoder().decode(ServerCapabilities.self, from: Data("{}".utf8))
        XCTAssertFalse(off.jobActivity)
    }

    /// The plugin's own sample (activity-sample.json: GET /voice/work/{run_id} with job_activity).
    func testPluginSampleDecodes() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "work_activity", withExtension: "json", subdirectory: "Contract"))
        let raw = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let caps = try JSONDecoder().decode(ServerCapabilities.self, from: JSONSerialization.data(withJSONObject: raw["capabilities"] ?? [:]))
        XCTAssertTrue(caps.jobActivity)
        let info = try XCTUnwrap(WorkInfo(json: raw["work"]))
        XCTAssertEqual(info.activity.count, 15)
        XCTAssertEqual(info.activity.last?.status, .running)
        XCTAssertEqual(info.activity.first { $0.status == .failed }?.text, "Ran the tests")
        XCTAssertEqual(info.findings.count, 2)
        XCTAssertEqual(info.touched.count, 8)
        XCTAssertEqual(info.touched.first { $0.kind == .url }?.url?.host, "playwright.dev")
        XCTAssertEqual(info.hero, .trials(total: 50, passed: 31, failed: 0))
        XCTAssertEqual(JobHero.select(info), .trials(total: 50, passed: 31, failed: 0))
        // The tool-call log: names, times and redacted previews from the plugin itself.
        XCTAssertEqual(info.activityTotal, 15)
        XCTAssertEqual(info.activity.first?.tool, "search_files")
        XCTAssertEqual(info.activity.last?.args, "npx playwright test checkout.spec.ts --repeat-each=50")
        XCTAssertNil(info.activity.last?.endedAt, "still running")
        XCTAssertTrue(info.activity.first { $0.status == .failed }?.result?.contains("2 failed") ?? false)
        XCTAssertEqual(info.toolCalls.count, 13)
    }
}
