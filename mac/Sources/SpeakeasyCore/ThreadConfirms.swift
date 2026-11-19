import Foundation

/// SPK-522, SPK-540: an approval card (a `confirm` view: post, send, book, buy…) a task in this
/// conversation sent, drawn in the thread with its own Approve and Cancel. A chat thread used to draw
/// a job's work and words but never its cards, so the only way to approve was typing it, which the
/// plugin rightly refuses: the exact-preview approval comes from the card's button.
///
/// Each card sits under the reply that sent it (the newest agent message inside its task's run),
/// and changes state in place there (pending → approved → done, or cancelled); it never moves to the
/// end of the chat. One the thread can't place (its reply is on an earlier page, or was only the
/// card) goes at the end, above the composer. A card the task sent again unchanged (same preview
/// fingerprint) shows once: the newest copy, so one tap approves exactly one preview.
///
/// The iPhone's `SessionConfirms` is the same rule in its own repo; it is to adopt this one. The
/// name differs so the two never meet as one ambiguous name in the iPhone's tests.
public enum ThreadConfirms {
    public struct Item: Identifiable, Equatable, Sendable {
        /// Stable per task run and card position.
        public var id: String
        public var runID: String
        public var card: ViewCard

        public init(id: String, runID: String, card: ViewCard) {
            self.id = id; self.runID = runID; self.card = card
        }

        /// Still waits on the user. Approved, done, cancelled and unverified cards show their outcome.
        public var isPending: Bool { (card["state"].string ?? "pending") == "pending" }
        /// The buttons to draw: the card's own (Post, Send, Buy for $X, and Cancel) while it waits,
        /// none once it settled, whatever an older answer still lists.
        public var buttons: [CardAction] { isPending ? card.actions : [] }
    }

    public struct Placement: Equatable, Sendable {
        public var after: [String: [Item]] = [:]
        public var tail: [Item] = []
        public init() {}
    }

    /// Late tolerance: a reply is written a moment before its run settles. A later reply that lands
    /// inside it (an approval carried out within the window) takes the card with it.
    public static let slack: TimeInterval = 90

    /// The approval cards in `jobs`, oldest task first, with unchanged re-sends folded to the newest.
    public static func items(_ jobs: [WorkInfo]) -> [(job: WorkInfo, item: Item)] {
        let ordered = jobs.enumerated().sorted { a, b in
            switch (a.element.startedAt, b.element.startedAt) {
            case let (x?, y?) where x != y: return x < y
            // The server lists a conversation's tasks newest first: without times, reverse that.
            default: return a.offset > b.offset
            }
        }.map(\.element)
        var out: [(job: WorkInfo, item: Item)] = []
        for job in ordered {
            guard let run = job.runID else { continue }
            for card in job.views where card.kind == "confirm" {
                let item = Item(id: "confirm-\(run)-\(card.number)", runID: run, card: card)
                if let sha = card["sha256"].string, let i = out.firstIndex(where: { $0.item.card["sha256"].string == sha }) {
                    out.remove(at: i)   // the same preview sent again: only the newest copy stays
                }
                out.append((job, item))
            }
        }
        return out
    }

    /// Where each card goes among the thread's visible rows (`rows`: the entries actually drawn).
    public static func place(rows: [HistoryEntry], jobs: [WorkInfo]) -> Placement {
        var placement = Placement()
        for (job, item) in items(jobs) {
            if let anchor = anchor(for: job, in: rows) {
                placement.after[anchor, default: []].append(item)
            } else {
                placement.tail.append(item)
            }
        }
        return placement
    }

    /// The newest agent message written while the task ran.
    public static func anchor(for job: WorkInfo, in rows: [HistoryEntry]) -> String? {
        guard let start = job.startedAt else { return nil }
        let end = job.finishedAt.map { $0.addingTimeInterval(slack) }
        return rows.last { e in
            guard e.kind == .assistant, let at = e.at, at >= start.addingTimeInterval(-1) else { return false }
            return end.map { at <= $0 } ?? true
        }?.id
    }
}
