import XCTest
@testable import SpeakeasyCore

/// SPK-254: Hermes' projects decode from the plugin's shapes; invented names only.
final class ProjectsTests: XCTestCase {
    func testListDecodesProjectsCountsAndOffer() {
        let list = ProjectList(json: [
            "projects": [["id": "p_1", "slug": "garden-shed", "name": "Garden shed", "icon": "hammer", "color": "green",
                          "board": NSNull(), "has_folders": false, "chats": 3, "jobs": 1, "notes": "Cedar, not pine."],
                         ["id": "p_2", "name": "Lantern app", "icon": "😀", "color": "neon", "chats": 0, "jobs": 0]],
            "offer": ["subject": "Compost", "chats": 4]])
        XCTAssertEqual(list.projects.count, 2)
        let shed = list.projects[0]
        XCTAssertEqual(shed.symbol, "hammer")
        XCTAssertEqual(shed.color, .green)
        XCTAssertEqual(shed.countLine, "3 chats \u{00B7} 1 job")
        XCTAssertEqual(shed.notes, "Cedar, not pine.")
        // An icon that isn't an SF Symbol name and an unknown colour fall back, never break.
        XCTAssertEqual(list.projects[1].symbol, ProjectSymbols.fallback)
        XCTAssertNil(list.projects[1].color)
        XCTAssertEqual(list.projects[1].countLine, "No chats yet")
        XCTAssertEqual(list.offer, ProjectOffer(subject: "Compost", chats: 4))
        XCTAssertEqual(list.offer?.prompt, "Make a project for Compost?")
        XCTAssertTrue(list.showsSection)
    }

    func testZeroProjectsHidesTheSection() {
        let empty = ProjectList(json: ["projects": []])
        XCTAssertTrue(empty.projects.isEmpty)
        XCTAssertFalse(empty.showsSection)
        XCTAssertTrue(ProjectList(json: ["projects": [], "offer": ["subject": "Compost", "chats": 3]]).showsSection)
    }

    func testPageSplitsRunningAndDoneJobs() {
        let page = ProjectPage(json: [
            "project": ["id": "p_1", "name": "Garden shed", "chats": 1, "jobs": 2],
            "chats": [["id": "s1", "title": "Cedar board prices", "source": "speakeasy", "source_label": "Speakeasy",
                       "last_activity": 1_790_000_000, "project": ["id": "p_1", "name": "Garden shed"]]],
            "jobs": [["run_id": "r1", "title": "Compare door hinges", "status": "running", "short_status": "Checking hinge loads"],
                     ["run_id": "r2", "title": "Floor frame cut list", "status": "completed", "summary": "Nine joists"]],
            "board": ["slug": "shed", "name": "Shed", "counts": ["running": 1]]])
        XCTAssertNotNil(page)
        XCTAssertEqual(page?.chats.first?.project, ProjectRef(id: "p_1", name: "Garden shed"))
        XCTAssertEqual(page?.running.map(\.runID), ["r1"])
        XCTAssertEqual(page?.done.map(\.runID), ["r2"])
        XCTAssertEqual(page?.running.first?.line, "Checking hinge loads")
        XCTAssertEqual(page?.board?.slug, "shed")
        XCTAssertNil(ProjectPage(json: ["chats": []]))
    }

    func testNewProjectBodyTrimsAndValidates() {
        XCTAssertNil(NewProject(name: "   ").body())
        XCTAssertNil(NewProject(name: String(repeating: "x", count: 61)).body())
        let body = NewProject(name: "  Garden   shed ", icon: "hammer", color: .green, fromOffer: "Garden").body()
        XCTAssertEqual(body?["name"] as? String, "Garden shed")
        XCTAssertEqual(body?["icon"] as? String, "hammer")
        XCTAssertEqual(body?["color"] as? String, "green")
        XCTAssertEqual(body?["from_offer"] as? String, "Garden")
        XCTAssertNil(NewProject(name: "Shed", icon: "Not A Symbol").body()?["icon"])
    }

    func testHomeCardCarriesProjectOrSuggestionNeverBoth() {
        let filed = NowItem(json: ["id": "s:r1", "kind": "working", "run_id": "r1", "title": "Fix the lantern build",
                                   "project": ["id": "p_2", "name": "Lantern app"],
                                   "project_suggestion": ["id": "p_2", "name": "Lantern app"]])
        XCTAssertEqual(filed?.project?.name, "Lantern app")
        XCTAssertNil(filed?.projectSuggestion)
        let offered = NowItem(json: ["id": "s:r2", "kind": "working", "run_id": "r2", "title": "Fix the lantern build",
                                     "project_suggestion": ["id": "p_2", "name": "Lantern app"]])
        XCTAssertNil(offered?.project)
        XCTAssertEqual(offered?.projectSuggestion?.id, "p_2")
        let plain = NowItem(json: ["id": "s:r3", "kind": "working", "run_id": "r3", "title": "Find a cafe"])
        XCTAssertNil(plain?.project)
        XCTAssertNil(plain?.projectSuggestion)
    }

    func testCapabilityDecodes() throws {
        let on = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"projects": true}"#.utf8))
        XCTAssertTrue(on.projects)
        let off = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{}"#.utf8))
        XCTAssertFalse(off.projects)
    }

    @MainActor
    func testDemoBackendCreatesAndFiles() async throws {
        let demo = DemoProjectsBackend(empty: true, offer: true)
        var list = try await demo.projects()
        XCTAssertTrue(list.projects.isEmpty)
        XCTAssertNotNil(list.offer)
        _ = try await demo.create(NewProject(name: "Compost", fromOffer: "Compost"))
        list = try await demo.projects()
        XCTAssertEqual(list.projects.map(\.name), ["Compost"])
        XCTAssertNil(list.offer)
        try await demo.file(session: HistorySession(id: "s1", title: "Bin size", source: "speakeasy", sourceLabel: "Speakeasy",
                                                    lastActivity: nil), into: list.projects[0].id)
        XCTAssertEqual(demo.filed.first?.0, "s1")
        let page = try await demo.page(list.projects[0].id)
        XCTAssertEqual(page.chats.map(\.id), ["s1"])
        XCTAssertEqual(page.chats.first?.project?.name, "Compost")
    }
}
