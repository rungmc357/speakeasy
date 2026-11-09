import XCTest
@testable import SpeakeasyCore

/// Automated work in the history (SPK-70, SPK-72): a filter menu for board workers, scheduled jobs and helpers,
/// never unread or needs-you, and a board worker's chat opening on its card.
final class HistoryAutomatedTests: XCTestCase {
    private func row(_ id: String, source: String = "speakeasy", automated: Bool = false) -> HistorySession {
        HistorySession(id: id, title: "T", source: source, sourceLabel: source.capitalized, lastActivity: nil, automated: automated)
    }

    private func kinds(kanban: Int = 1, scheduled: Int = 0, helpers: Int = 0, shown: HistoryShown = .standard) -> HistoryAutomated {
        HistoryAutomated(count: kanban + scheduled, hidden: !shown.kanban && !shown.scheduled, kinds: [
            .init(id: "kanban", label: "Board workers", count: kanban, shown: shown.kanban),
            .init(id: "scheduled", label: "Scheduled jobs", count: scheduled, shown: shown.scheduled),
            .init(id: "helpers", label: "Background helpers", count: helpers, shown: shown.helpers)])
    }

    func testEachMenuToggleGoesInTheQueryAndStartsOver() throws {
        var list = HistoryList()
        let q1 = list.reset()
        XCTAssertEqual(q1.path, "/voice/history?hide=kanban,scheduled&limit=40",
                       "standard choice (SPK-263): Kanban and scheduled sessions stay out of the list")
        list.apply(HistoryPage(sessions: [row("a"), row("k", source: "kanban", automated: true)],
                               automated: kinds(kanban: 1, scheduled: 2, helpers: 3)), for: q1)
        XCTAssertTrue(list.offersAutomatedMenu)
        XCTAssertEqual(list.automatedMenu.map(\.id), ["kanban", "scheduled", "helpers"])
        let q2 = try XCTUnwrap(list.setShown("scheduled", true))
        XCTAssertEqual(q2.path, "/voice/history?hide=kanban&limit=40")
        XCTAssertTrue(list.sessions.isEmpty, "starts over")
        XCTAssertNil(list.setShown("scheduled", true), "no change, no request")
        let q3 = try XCTUnwrap(list.setShown("helpers", true))
        XCTAssertEqual(q3.path, "/voice/history?hide=kanban&helpers=1&limit=40")
        let q4 = try XCTUnwrap(list.setShown("scheduled", false))
        XCTAssertTrue(q4.path.contains("hide=kanban,scheduled"))
        // Paging and source filters keep it.
        list.apply(HistoryPage(sessions: [row("a")], nextCursor: "1|a", automated: kinds(shown: list.shown)), for: q4)
        XCTAssertTrue(try XCTUnwrap(list.loadMore()).path.contains("helpers=1"))
        XCTAssertTrue(try XCTUnwrap(list.setSource("discord")).path.contains("hide=kanban,scheduled"))
        XCTAssertEqual(HistoryList(shown: HistoryShown(helpers: true)).shown.helpers, true, "remembered choice starts the list")
    }

    func testTurningOffTheFilteredKindDropsThatSourceFilter() throws {
        var list = HistoryList(shown: HistoryShown(kanban: true))
        _ = list.setSource("kanban")
        let q = try XCTUnwrap(list.setShown("kanban", false))
        XCTAssertNil(q.source)
        XCTAssertTrue(HistoryShown(kanban: false).hides(filter: "kanban"))
        XCTAssertFalse(HistoryShown(kanban: false).hides(filter: "discord"))
        XCTAssertTrue(HistoryShown.standard.hides(filter: "helpers"), "helpers are off by default")
        XCTAssertTrue(HistoryShown.standard.hides(filter: "kanban"), "Kanban workers are off by default (SPK-263)")
        XCTAssertTrue(HistoryShown.standard.hides(filter: "scheduled"), "scheduled jobs are off by default (SPK-263)")
    }

    func testTheIconShowsOnlyWhenTheMenuHasSomething() {
        var list = HistoryList()
        let q = list.reset()
        list.apply(HistoryPage(sessions: [row("a")]), for: q)
        XCTAssertFalse(list.offersAutomatedMenu, "older plugins: no icon")
        XCTAssertFalse(HistoryAutomation.offers(HistoryAutomated(count: 0, hidden: false), shown: .standard), "0.3.37 before kinds: no icon")
        XCTAssertFalse(HistoryAutomation.offers(kinds(kanban: 0), shown: .standard), "nothing automated: no icon")
        XCTAssertEqual(HistoryAutomation.menu(kinds(kanban: 0, helpers: 4), shown: .standard).map(\.id), ["helpers"],
                       "only kinds with sessions")
        XCTAssertEqual(HistoryAutomation.menu(kinds(kanban: 0), shown: HistoryShown(kanban: true)).map(\.id), ["kanban"],
                       "a changed one stays so it can be turned back")
        XCTAssertNotEqual(HistoryAutomation.symbol(.standard), HistoryAutomation.symbol(HistoryShown(kanban: true)),
                          "the icon fills while something is filtered")
    }

    func testTheChoicesAreRememberedAndOldChoicesDontBringWorkersBack() throws {
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "spk72-\(UUID().uuidString)"))
        XCTAssertEqual(HistoryAutomation.shown(in: defaults), .standard)
        defaults.set(true, forKey: HistoryAutomation.legacyKey)   // SPK-70's "Hide automated" was on
        XCTAssertEqual(HistoryAutomation.shown(in: defaults), HistoryShown(kanban: false, scheduled: false, helpers: false))
        // SPK-263: the old menu wrote every kind on any toggle, so "shown" was saved for everyone who
        // touched it; those choices predate hidden-by-default and must not flood Today again.
        defaults.set(true, forKey: "history.show.kanban"); defaults.set(true, forKey: "history.show.scheduled")
        XCTAssertEqual(HistoryAutomation.shown(in: defaults), .standard)
        HistoryAutomation.set(HistoryShown(kanban: true, scheduled: false, helpers: true), in: defaults)
        XCTAssertNil(defaults.object(forKey: HistoryAutomation.legacyKey), "carried over once")
        XCTAssertEqual(HistoryAutomation.shown(in: defaults), HistoryShown(kanban: true, scheduled: false, helpers: true))
    }

    func testTheKindsFacetDecodesAndDemoListsMatchTheServer() throws {
        let page = HistoryPage(json: ["sessions": [], "automated": ["count": 3, "hidden": false, "kinds": [
            ["id": "kanban", "label": "Board workers", "count": 1, "shown": true],
            ["id": "helpers", "count": 2, "shown": false],
            ["id": "mystery", "label": "?", "count": 9, "shown": true]]]])
        let facet = try XCTUnwrap(page.automated)
        XCTAssertEqual(facet.kinds.map(\.id), ["kanban", "helpers"], "unknown kinds dropped")
        XCTAssertEqual(facet.kinds[1].label, "Background helpers", "a missing label filled in")
        XCTAssertEqual(HistorySources.filterID(for: "subagent"), "helpers")
        let rows = [row("a"), row("k", source: "kanban", automated: true), row("h", source: "subagent", automated: true)]
        XCTAssertEqual(rows.filter { HistoryShown.standard.lists($0) }.map(\.id), ["a"], "SPK-263: workers hidden by default")
        XCTAssertEqual(HistoryShown.standard.facet(for: rows), kinds(kanban: 1, scheduled: 0, helpers: 1))
    }

    func testAutomatedRowsAreNeverUnreadOrNeedsYou() throws {
        let json: [String: Any] = ["sessions": [["id": "k1", "title": "LAN-12: Wick", "source": "kanban", "source_label": "Kanban",
                                                 "state": "needs_you", "unread": true, "automated": true]],
                                   "automated": ["count": 4, "hidden": false]]
        let page = HistoryPage(json: json)
        let s = try XCTUnwrap(page.sessions.first)
        XCTAssertTrue(s.automated)
        XCTAssertFalse(s.unread)
        XCTAssertEqual(s.state, .idle)
        XCTAssertEqual(page.automated, HistoryAutomated(count: 4, hidden: false))
        XCTAssertEqual(HistorySources.filterID(for: "kanban"), "kanban")
        XCTAssertTrue(HistoryList.defaultFilters.contains(HistoryFilter(id: "kanban", label: "Kanban")))
    }

    func testAWorkerChatOpensOnItsCardWithItsLiveLine() throws {
        let json: [String: Any] = ["session": [
            "id": "k1", "title": "LAN-12: Lantern wick burns too fast", "source": "kanban", "source_label": "Kanban",
            "state": "running", "automated": true, "live": ["now": "Running the flame tests", "so_far": "Read the code"],
            "entries": [
                ["kind": "note", "id": "m1", "text": "LAN-12 \u{00B7} Lantern wick burns too fast",
                 "task": ["title": "Lantern wick burns too fast", "issue": "LAN-12", "url": "https://tracker.example/LAN-12",
                          "goal": "Make it last an hour.", "instructions": "The whole brief"]],
                ["kind": "assistant", "id": "m2", "text": "Starting: the wick.", "update": true],
                ["kind": "note", "id": "m3", "text": "Picked up again", "task": ["title": "ignored: no card on a plain note"]],
            ]]]
        let c = try XCTUnwrap(HistoryConversation(json: json))
        XCTAssertTrue(c.automated)
        XCTAssertEqual(c.live, HistoryLive(now: "Running the flame tests", soFar: "Read the code"))
        let card = try XCTUnwrap(c.card)
        XCTAssertEqual(card.heading, "LAN-12 \u{00B7} Lantern wick burns too fast")
        XCTAssertEqual(card.url?.absoluteString, "https://tracker.example/LAN-12")
        XCTAssertEqual(card.goal, "Make it last an hour.")
        XCTAssertTrue(c.entries[1].update)
        let thread = HistoryThread(c)
        XCTAssertEqual(thread.messages.first?.card?.issue, "LAN-12")
        XCTAssertEqual(thread.messages.first?.role, .note)
    }

    func testCardLinksMustBeHTTPS() {
        XCTAssertNil(HistoryWorkerTask(json: ["title": "X", "url": "javascript:alert(1)"])?.url)
        XCTAssertNil(HistoryWorkerTask(json: ["issue": "LAN-1"]), "no title, no card")
        XCTAssertEqual(HistoryWorkerTask(json: ["title": "X"])?.heading, "X")
    }
}
