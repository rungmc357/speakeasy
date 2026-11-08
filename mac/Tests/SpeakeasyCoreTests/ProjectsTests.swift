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

    // MARK: Where Home works (the chip, the call dock, "Working in", the side panel)

    func testListReadsSelectedOnlyWhenThatProjectExists() {
        let list = ProjectList(json: ["projects": [["id": "p1", "name": "Garden shed"]], "selected": "p1"])
        XCTAssertEqual(list.selected, "p1")
        let stale = ProjectList(json: ["projects": [["id": "p1", "name": "Garden shed"]], "selected": "gone"])
        XCTAssertNil(stale.selected)
    }

    @MainActor
    func testChipSaysHomeUntilAProjectIsChosen() async {
        let model = ProjectsModel(backend: DemoProjectsBackend())
        model.supported = true
        await model.reload()
        XCTAssertTrue(model.showsChip)
        XCTAssertNil(model.selected)
        XCTAssertEqual(model.chipSymbol, "house")
        XCTAssertEqual(model.chipName, "Home")
        XCTAssertNotEqual(model.chipName.lowercased(), "no project")
        XCTAssertEqual(HomeSpace.homeRow, "Home \u{2014} not in a project")
    }

    @MainActor
    func testSelectingAProjectSetsTheChipAndTheServerAndBackToHomeClearsIt() async {
        let demo = DemoProjectsBackend()
        let model = ProjectsModel(backend: demo)
        model.supported = true
        await model.reload()
        await model.select("p_shed")
        XCTAssertEqual(model.chipName, "Garden shed")
        XCTAssertEqual(model.chipSymbol, "hammer")
        XCTAssertEqual(demo.list.selected, "p_shed")
        // Switching mid-session: the next jobs follow the new choice.
        await model.select("p_trip")
        XCTAssertEqual(demo.list.selected, "p_trip")
        XCTAssertEqual(model.chipName, "Lakeside trip")
        await model.select(nil)
        XCTAssertNil(demo.list.selected)
        XCTAssertEqual(model.chipName, "Home")
    }

    @MainActor
    func testSidebarSelectionAndChipShareOneValue() async {
        // Phone and Mac read the same stored value: a second model on the same server agrees.
        let demo = DemoProjectsBackend()
        let phone = ProjectsModel(backend: demo), mac = ProjectsModel(backend: demo)
        phone.supported = true; mac.supported = true
        await phone.reload(); await mac.reload()
        await phone.select("p_lantern")
        await mac.reload()
        XCTAssertEqual(mac.selected?.id, "p_lantern")
        XCTAssertEqual(mac.chipName, "Lantern app")
    }

    @MainActor
    func testRefusedSelectionPutsTheOldChoiceBack() async {
        @MainActor final class Refuses: ProjectsBackend {
            func projects() async throws -> ProjectList { ProjectList(projects: [HermesProject(id: "p1", name: "Garden shed")]) }
            func page(_ id: String) async throws -> ProjectPage { ProjectPage(project: HermesProject(id: id, name: "x")) }
            func create(_ p: NewProject) async throws -> HermesProject { HermesProject(id: "n", name: p.name) }
            func file(session: HistorySession, into projectID: String?) async throws {}
            func file(runID: String, into projectID: String?) async throws {}
            func setNotes(_ notes: String, for projectID: String) async throws {}
            func dismissOffer(_ subject: String) async throws {}
            func select(_ projectID: String?) async throws { throw URLError(.timedOut) }
        }
        let model = ProjectsModel(backend: Refuses())
        model.supported = true
        await model.reload()
        await model.select("p1")
        XCTAssertNil(model.selected)
        XCTAssertEqual(model.chipName, "Home")
        XCTAssertNotNil(model.error)
    }

    @MainActor
    func testNewProjectFromTheChipSheetBecomesWhereHomeWorks() async {
        let demo = DemoProjectsBackend()
        let model = ProjectsModel(backend: demo)
        model.supported = true
        await model.reload()
        let made = await model.create(NewProject(name: "Compost", icon: "leaf", select: true))
        XCTAssertEqual(model.selected?.id, made?.id)
        XCTAssertEqual(demo.list.selected, made?.id)
    }

    func testOlderServerHidesTheChip() async {
        let model = await ProjectsModel(backend: DemoProjectsBackend())
        let shows = await model.showsChip
        XCTAssertFalse(shows)
    }

    func testActivityLineSaysRunningElseAge() {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(identifier: "America/New_York")!
        let now = Date(timeIntervalSince1970: 1_800_000_000)
        XCTAssertEqual(HermesProject(id: "a", name: "A", running: 2, activeAt: now).activityLine(now: now, calendar: cal), "2 running")
        XCTAssertEqual(HermesProject(id: "a", name: "A", activeAt: now.addingTimeInterval(-600)).activityLine(now: now, calendar: cal), "10m ago")
        XCTAssertEqual(HermesProject(id: "a", name: "A", activeAt: now.addingTimeInterval(-86400 - 60)).activityLine(now: now, calendar: cal), "Yesterday")
        XCTAssertEqual(HermesProject(id: "a", name: "A", chats: 2).activityLine(now: now, calendar: cal), "2 chats")
    }

    func testPageTabsShowBoardOnlyWhenABoardIsBound() {
        XCTAssertEqual(ProjectPage(project: HermesProject(id: "a", name: "A")).tabs, [.chats, .work])
        XCTAssertEqual(ProjectPage(project: HermesProject(id: "a", name: "A", board: "lantern")).tabs, [.chats, .work, .board])
        XCTAssertEqual(ProjectTab.allCases.map(\.label), ["Chats", "Work", "Board"])
    }

    @MainActor
    func testExpandingAProjectLoadsItsThreeNewestChats() async {
        let model = ProjectsModel(backend: DemoProjectsBackend())
        model.supported = true
        await model.reload()
        model.toggleExpanded("p_shed")
        XCTAssertTrue(model.expanded.contains("p_shed"))
        await model.loadPage("p_shed")
        let recent = model.pages["p_shed"]?.recentChats() ?? []
        XCTAssertLessThanOrEqual(recent.count, 3)
        XCTAssertFalse(recent.isEmpty)
        model.toggleExpanded("p_shed")
        XCTAssertFalse(model.expanded.contains("p_shed"))
    }
}
