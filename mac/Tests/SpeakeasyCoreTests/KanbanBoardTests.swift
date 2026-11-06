import XCTest
@testable import SpeakeasyCore

@MainActor
final class KanbanBoardTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_800_000_000)

    private var ny: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "America/New_York")!
        return c
    }

    func testDecodesBoardPageInColumnOrder() throws {
        let json: [String: Any] = [
            "slug": "garden", "name": "Garden club site", "change": "42",
            "columns": [
                ["key": "done", "title": "Done", "count": 1, "cards": [
                    ["id": "t3", "title": "Move the calendar", "column": "done", "since": 1_799_999_880,
                     "summary": "Moved it to the top."]]],
                ["key": "blocked", "title": "Blocked", "count": 1, "cards": [
                    ["id": "t2", "title": "Pick a header color", "status": "blocked", "column": "blocked",
                     "since": 1_799_999_640, "needs_you": true, "reason": "Green or blue?", "session_id": "local-a"]]],
                ["key": "running", "title": "Running", "count": 1, "cards": [
                    ["id": "t1", "title": "Fix the RSVP form", "column": "running", "since": 1_799_998_500,
                     "session_id": "local-b"]]],
            ],
        ]
        let page = try XCTUnwrap(KanbanBoardPage(json: json))
        XCTAssertEqual(page.columns.map(\.key), [.ready, .running, .blocked, .done])
        XCTAssertTrue(page.column(.ready).cards.isEmpty)
        XCTAssertEqual(page.column(.running).cards.first?.sessionID, "local-b")
        let blocked = try XCTUnwrap(page.column(.blocked).cards.first)
        XCTAssertTrue(blocked.needsYou)
        XCTAssertEqual(blocked.reason, "Green or blue?")
        XCTAssertEqual(page.needsYouCount, 1)
        XCTAssertEqual(page.column(.done).cards.first?.summary, "Moved it to the top.")
        XCTAssertNil(page.column(.done).cards.first?.sessionID)
    }

    func testTimeLinesInPlainWordsAndUSTime() {
        let running = KanbanCard(id: "a", title: "x", column: .running, since: now.addingTimeInterval(-25 * 60))
        XCTAssertEqual(running.timeLine(now: now, calendar: ny), "Running 25 min")
        let long = KanbanCard(id: "a", title: "x", column: .running, since: now.addingTimeInterval(-65 * 60))
        XCTAssertEqual(long.timeLine(now: now, calendar: ny), "Running 1 hr 5 min")
        let blocked = KanbanCard(id: "b", title: "x", status: "blocked", column: .blocked,
                                 since: now.addingTimeInterval(-6 * 60), needsYou: true)
        XCTAssertEqual(blocked.timeLine(now: now, calendar: ny), "Waiting on you · 6 min")
        let review = KanbanCard(id: "c", title: "x", status: "review", column: .blocked, since: now.addingTimeInterval(-120))
        XCTAssertEqual(review.timeLine(now: now, calendar: ny), "Waiting for review · 2 min")
        // 1_800_000_000 is Jan 15 2027, 3:00 AM in New York.
        let doneToday = KanbanCard(id: "d", title: "x", column: .done, since: now.addingTimeInterval(-2 * 3600 - 12 * 60))
        XCTAssertEqual(doneToday.timeLine(now: now, calendar: ny), "Finished 12:48 AM")
        let doneYesterday = KanbanCard(id: "e", title: "x", column: .done, since: now.addingTimeInterval(-10 * 3600))
        XCTAssertEqual(doneYesterday.timeLine(now: now, calendar: ny), "Finished yesterday 5:00 PM")
        let queued = KanbanCard(id: "f", title: "x", column: .ready, since: now.addingTimeInterval(-12 * 60))
        XCTAssertEqual(queued.timeLine(now: now, calendar: ny), "Queued 12 min ago")
        XCTAssertEqual(BoardFormat.span(from: now.addingTimeInterval(-3 * 86400), to: now), "3 days")
    }

    func testBoardListSubtitle() {
        let list = KanbanBoardList(json: ["boards": [
            ["slug": "garden", "name": "Garden", "counts": ["ready": 2, "running": 1, "blocked": 1, "done": 3]],
            ["slug": "quiet", "name": "Quiet", "counts": ["done": 0]],
        ], "suggested": "garden"])
        XCTAssertEqual(list.suggested, "garden")
        XCTAssertEqual(list.boards[0].subtitle, "1 running · 1 needs you · 2 ready")
        XCTAssertEqual(list.boards[1].subtitle, "Nothing going on")
    }

    func testRemembersPickAndFallsBackToSuggested() async throws {
        let defaults = try XCTUnwrap(UserDefaults(suiteName: "KanbanBoardTests-\(UUID().uuidString)"))
        let model = BoardModel(backend: DemoBoardBackend(now: now), defaults: defaults)
        await model.reload()
        XCTAssertEqual(model.selected, "garden-site")  // the server's suggestion
        XCTAssertEqual(model.page?.name, "Garden club website")
        XCTAssertEqual(model.needsYouCount, 1)
        model.select("bake-sale")
        await model.loadPage()
        XCTAssertEqual(model.page?.slug, "bake-sale")
        XCTAssertEqual(defaults.string(forKey: BoardModel.rememberKey), "bake-sale")

        let again = BoardModel(backend: DemoBoardBackend(now: now), defaults: defaults)
        XCTAssertEqual(again.selected, "bake-sale")
        await again.reload()
        XCTAssertEqual(again.page?.slug, "bake-sale")

        defaults.set("gone-board", forKey: BoardModel.rememberKey)
        let stale = BoardModel(backend: DemoBoardBackend(now: now), defaults: defaults)
        await stale.reload()
        XCTAssertEqual(stale.selected, "garden-site")  // a removed board falls back to the suggestion
    }

    func testDemoBoardHasEveryColumnAndAWaitingCard() async throws {
        let page = try await DemoBoardBackend(now: now).board("garden-site")
        for key in KanbanCard.Column.allCases { XCTAssertFalse(page.column(key).cards.isEmpty, key.rawValue) }
        XCTAssertTrue(page.column(.blocked).cards.allSatisfy(\.needsYou))
    }

    // MARK: Narrow panes (SPK-82)

    private func page(blocked needsYou: Bool) -> KanbanBoardPage {
        KanbanBoardPage(slug: "b", name: "B", columns: [
            KanbanColumn(key: .ready, cards: [KanbanCard(id: "r", title: "Queue it", column: .ready)]),
            KanbanColumn(key: .blocked, cards: [KanbanCard(id: "x", title: "Pick a color", column: .blocked, needsYou: needsYou)]),
        ])
    }

    func testFourColumnsShareAWidePane() {
        let l = KanbanBoardLayout(width: 1040, minColumn: 150, spacing: 12, inset: 28, peek: 44)
        XCTAssertFalse(l.scrolls)
        XCTAssertEqual(l.visible, 4)
        XCTAssertEqual(l.columnWidth, (1040 - 56 - 36) / 4, accuracy: 0.01)
        XCTAssertEqual(l.openingLeading(for: page(blocked: true)), 0)
        XCTAssertNil(l.offscreenAttention(leading: 0, page: page(blocked: true)))
    }

    func testNarrowPaneShowsWholeColumnsAndAPeekOfTheNext() {
        // The Mac History pane at a 720-pt window, and iPad Air 11-inch upright.
        for (width, minColumn, inset, want) in [(CGFloat(470), CGFloat(150), CGFloat(28), 2), (820, 200, 28, 3)] {
            let l = KanbanBoardLayout(width: width, minColumn: minColumn, spacing: 12, inset: inset, peek: 44)
            XCTAssertTrue(l.scrolls)
            XCTAssertEqual(l.visible, want, "width \(width)")
            XCTAssertGreaterThanOrEqual(l.columnWidth, minColumn)
            // The full columns end exactly where the 44-pt peek of the next one begins.
            let used = inset + CGFloat(l.visible) * (l.columnWidth + 12)
            XCTAssertEqual(width - used, 44, accuracy: 0.01)
        }
        let tiny = KanbanBoardLayout(width: 200, minColumn: 150, spacing: 12, inset: 28, peek: 44)
        XCTAssertEqual(tiny.visible, 1)
        XCTAssertGreaterThan(tiny.columnWidth, 100)
    }

    func testOpensWithTheCardThatNeedsYouInView() {
        let l = KanbanBoardLayout(width: 470, minColumn: 150, spacing: 12, inset: 28, peek: 44)   // 2 in view
        let waiting = page(blocked: true)
        XCTAssertEqual(waiting.attentionIndex, 2)
        // Opening at the start would hide Blocked behind the edge: open one column along instead.
        XCTAssertEqual(l.openingLeading(for: waiting), 1)
        XCTAssertTrue(l.inView(leading: l.openingLeading(for: waiting)).contains(2))
        XCTAssertNil(l.offscreenAttention(leading: 1, page: waiting))
        // Scrolled back to the start or on to the end, the edge on its side points at it.
        XCTAssertEqual(l.offscreenAttention(leading: 0, page: waiting), .trailing)
        let one = KanbanBoardLayout(width: 240, minColumn: 150, spacing: 12, inset: 28, peek: 44)
        XCTAssertEqual(one.offscreenAttention(leading: 3, page: waiting), .leading)
        XCTAssertEqual(one.openingLeading(for: waiting), 2)
        // Nothing waiting on you: the board opens at Ready, no red pointer.
        XCTAssertEqual(l.openingLeading(for: page(blocked: false)), 0)
        XCTAssertNil(l.offscreenAttention(leading: 0, page: page(blocked: false)))
        XCTAssertEqual(l.openingLeading(for: nil), 0)
    }

    func testScrollPositionIsClampedToTheLastFullView() {
        let l = KanbanBoardLayout(width: 820, minColumn: 200, spacing: 12, inset: 28, peek: 44)   // 3 in view
        XCTAssertEqual(l.lastLeading, 1)
        XCTAssertEqual(l.inView(leading: 3), 1..<4)
        XCTAssertEqual(l.inView(leading: -2), 0..<3)
    }
}
