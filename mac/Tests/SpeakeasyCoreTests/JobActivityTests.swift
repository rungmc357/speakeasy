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

    func testCapsActivityAtForty() {
        let items = (0..<55).map { ["id": "a\($0)", "kind": "read", "text": "Step \($0)"] }
        let i = info(["activity": items])
        XCTAssertEqual(i.activity.count, 40)
        XCTAssertEqual(i.activity.last?.text, "Step 54", "newest kept")
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
    }
}
