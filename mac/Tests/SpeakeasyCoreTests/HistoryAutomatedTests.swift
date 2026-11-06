import XCTest
@testable import SpeakeasyCore

/// Automated work in the history (SPK-70): one show/hide switch for board workers and scheduled jobs,
/// never unread or needs-you, and a board worker's chat opening on its card.
final class HistoryAutomatedTests: XCTestCase {
    private func row(_ id: String, source: String = "speakeasy", automated: Bool = false) -> HistorySession {
        HistorySession(id: id, title: "T", source: source, sourceLabel: source.capitalized, lastActivity: nil, automated: automated)
    }

    func testHideAutomatedGoesInTheQueryAndStartsOver() throws {
        var list = HistoryList()
        let q1 = list.reset()
        XCTAssertFalse(q1.path.contains("automated"))
        list.apply(HistoryPage(sessions: [row("a"), row("k", source: "kanban", automated: true)],
                               automated: HistoryAutomated(count: 1, hidden: false)), for: q1)
        XCTAssertTrue(list.offersAutomatedSwitch)
        let q2 = try XCTUnwrap(list.setHideAutomated(true))
        XCTAssertEqual(q2.path, "/voice/history?automated=0&limit=40")
        XCTAssertTrue(list.sessions.isEmpty, "starts over")
        XCTAssertNil(list.setHideAutomated(true), "no change, no request")
        // Paging and filters keep it.
        list.apply(HistoryPage(sessions: [row("a")], nextCursor: "1|a", automated: HistoryAutomated(count: 1, hidden: true)), for: q2)
        XCTAssertTrue(try XCTUnwrap(list.loadMore()).path.contains("automated=0"))
        XCTAssertTrue(try XCTUnwrap(list.setSource("kanban")).path.contains("automated=0"))
        XCTAssertTrue(HistoryList(hideAutomated: true).hideAutomated, "remembered choice starts the list")
    }

    func testTheSwitchShowsOnlyWhenThereIsSomethingToHide() {
        var list = HistoryList()
        let q = list.reset()
        list.apply(HistoryPage(sessions: [row("a")]), for: q)
        XCTAssertFalse(list.offersAutomatedSwitch, "older plugins: no switch")
        XCTAssertFalse(HistoryAutomation.offers(HistoryAutomated(count: 0, hidden: false), hiding: false))
        XCTAssertTrue(HistoryAutomation.offers(HistoryAutomated(count: 0, hidden: true), hiding: true), "can always turn it back")
        XCTAssertEqual(HistoryAutomation.label(hiding: true), "Show automated")
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
