import Foundation

/// Invented boards for screenshots and demo builds (no real projects, names or content).
@MainActor
public final class DemoBoardBackend: BoardBackend {
    private let now: Date
    /// Card session → a session that exists in the app's own demo history, so a tap opens a page.
    private let links: [String: String]
    public init(now: Date = Date(), links: [String: String] = [:]) { self.now = now; self.links = links }

    /// The Mac History window's demo sessions.
    public static let macLinks = ["demo-garden-rsvp": "s_deploy", "demo-garden-calendar": "s_pricing",
                                  "demo-garden-color": "s_flaky", "demo-garden-photos": "s_dash"]
    /// The iPhone/iPad demo sessions.
    public static let phoneLinks = ["demo-garden-rsvp": "lantern", "demo-garden-calendar": "bench",
                                    "demo-garden-color": "garden", "demo-garden-photos": "kiln", "demo-bake-orders": "recipes"]

    public static let boardList: [KanbanBoardSummary] = [
        KanbanBoardSummary(slug: "garden-site", name: "Garden club website", ready: 3, running: 2, blocked: 1, done: 4),
        KanbanBoardSummary(slug: "bake-sale", name: "Bake sale", ready: 1, running: 0, blocked: 0, done: 2),
        KanbanBoardSummary(slug: "default", name: "Main board", ready: 0, running: 0, blocked: 0, done: 1),
    ]

    public func boards() async throws -> KanbanBoardList {
        KanbanBoardList(boards: Self.boardList, suggested: "garden-site")
    }

    public func board(_ slug: String) async throws -> KanbanBoardPage {
        var page = try page(slug)
        for i in page.columns.indices {
            for j in page.columns[i].cards.indices {
                if let id = page.columns[i].cards[j].sessionID, let to = links[id] { page.columns[i].cards[j].sessionID = to }
            }
        }
        return page
    }

    private func page(_ slug: String) throws -> KanbanBoardPage {
        let ago = { (minutes: Double) in self.now.addingTimeInterval(-minutes * 60) }
        switch slug {
        case "garden-site":
            return KanbanBoardPage(slug: slug, name: "Garden club website", columns: [
                KanbanColumn(key: .ready, cards: [
                    KanbanCard(id: "g1", title: "Add a photo gallery for the spring show", column: .ready, since: ago(12)),
                    KanbanCard(id: "g2", title: "Write the volunteer sign-up page", column: .ready, since: ago(40)),
                    KanbanCard(id: "g3", title: "Check the site on a small phone", status: "todo", column: .ready, since: ago(41),
                               note: "Waiting on another card"),
                ]),
                KanbanColumn(key: .running, cards: [
                    KanbanCard(id: "g4", title: "Fix the RSVP form so it saves the plot number", column: .running,
                               since: ago(25), sessionID: "demo-garden-rsvp"),
                    KanbanCard(id: "g5", title: "Move the events calendar to the top of the home page", column: .running,
                               since: ago(68), sessionID: "demo-garden-calendar"),
                ]),
                KanbanColumn(key: .blocked, cards: [
                    KanbanCard(id: "g6", title: "Pick a header color", status: "blocked", column: .blocked, since: ago(6),
                               needsYou: true, sessionID: "demo-garden-color",
                               reason: "Two options are ready: sage green or warm brick. Which one should the header use?"),
                ]),
                KanbanColumn(key: .done, cards: [
                    KanbanCard(id: "g7", title: "Shrink the hero photos so the page loads faster", column: .done, since: ago(18),
                               sessionID: "demo-garden-photos",
                               summary: "Photos are about a third of the size; the home page opens in under a second on a phone."),
                    KanbanCard(id: "g8", title: "Add the plot map to the About page", column: .done, since: ago(190),
                               sessionID: "demo-garden-map"),
                    KanbanCard(id: "g9", title: "Turn the newsletter form on again", column: .done, since: ago(26 * 60)),
                    KanbanCard(id: "g10", title: "List the 2027 dues in dollars", column: .done, since: ago(3 * 24 * 60)),
                ]),
            ], change: "1")
        case "bake-sale":
            return KanbanBoardPage(slug: slug, name: "Bake sale", columns: [
                KanbanColumn(key: .ready, cards: [
                    KanbanCard(id: "b1", title: "Price list for the table sign", column: .ready, since: ago(90)),
                ]),
                KanbanColumn(key: .done, cards: [
                    KanbanCard(id: "b2", title: "Count the cupcake orders", column: .done, since: ago(300), sessionID: "demo-bake-orders"),
                    KanbanCard(id: "b3", title: "Draft the flyer text", column: .done, since: ago(2 * 24 * 60)),
                ]),
            ], change: "1")
        case "default":
            return KanbanBoardPage(slug: slug, name: "Main board", columns: [
                KanbanColumn(key: .done, cards: [
                    KanbanCard(id: "m1", title: "Tidy the shared notes folder", column: .done, since: ago(4 * 24 * 60)),
                ]),
            ], change: "1")
        default:
            throw ScheduleProblem(status: 404, message: "That board isn't on your Hermes any more.")
        }
    }
}
