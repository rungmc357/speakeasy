import Foundation

/// SPK-522 / SPK-540: an approval card (a `confirm` view: post, send, book, buy…) a task in this
/// conversation sent, drawn in the thread with its own Approve and Cancel. One rule for the iPhone
/// and the Mac chat pages: before it, a chat thread drew a job's work and words but never its
/// cards, so the only way to approve was typing it, which the plugin rightly refuses (the
/// exact-preview approval comes from the card's button).
///
/// Each card sits under the reply that sent it (the newest agent message inside its task's run),
/// and changes state in place there (pending → approved → done, or cancelled); it never moves to the
/// end of the chat. One the thread can't place (its reply is on an earlier page, or was only the
/// card) goes at the end, above the composer. A card the task sent again unchanged (same preview
/// fingerprint) shows once: the newest copy, so one tap approves exactly one preview.
public enum SessionConfirms {
    public struct Item: Identifiable, Equatable, Sendable {
        /// Stable per task run and card position.
        public var id: String
        public var runID: String
        public var card: ViewCard
        public init(id: String, runID: String, card: ViewCard) { self.id = id; self.runID = runID; self.card = card }
    }

    public struct Placement: Equatable, Sendable {
        public var after: [String: [Item]] = [:]
        public var tail: [Item] = []
        public var isEmpty: Bool { after.isEmpty && tail.isEmpty }
        /// Every placed card (the under-a-reply ones first, then the end).
        public var all: [Item] { after.values.flatMap { $0 } + tail }
        /// Approval cards still waiting on the user.
        public var pending: [Item] { all.filter(SessionConfirms.pending) }
        public init(after: [String: [Item]] = [:], tail: [Item] = []) { self.after = after; self.tail = tail }
    }

    /// A drawn row a card can sit under: its id, when it was written, and whether it is the agent's reply.
    public struct Anchor: Equatable, Sendable {
        public var id: String
        public var at: Date?
        public var isReply: Bool
        public init(id: String, at: Date?, isReply: Bool) { self.id = id; self.at = at; self.isReply = isReply }
    }

    /// Late tolerance: a reply is written a moment before its run settles.
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

    /// Where each card goes among the thread's drawn rows.
    public static func place(anchors: [Anchor], jobs: [WorkInfo]) -> Placement {
        var placement = Placement()
        for (job, item) in items(jobs) {
            if let anchor = anchor(for: job, in: anchors) {
                placement.after[anchor, default: []].append(item)
            } else {
                placement.tail.append(item)
            }
        }
        return placement
    }

    /// iPhone: `rows` are the entries actually drawn.
    public static func place(rows: [HistoryEntry], jobs: [WorkInfo]) -> Placement {
        place(anchors: rows.map { Anchor(id: $0.id, at: $0.at, isReply: $0.kind == .assistant) }, jobs: jobs)
    }

    /// Mac: the thread page's messages. A tools-only row (no words, no files) is not a reply.
    public static func place(messages: [HistoryThreadMessage], jobs: [WorkInfo]) -> Placement {
        place(anchors: messages.map {
            Anchor(id: $0.id, at: $0.at, isReply: $0.role == .assistant && (!$0.text.isEmpty || !$0.files.isEmpty))
        }, jobs: jobs)
    }

    /// The newest agent reply written while the task ran.
    public static func anchor(for job: WorkInfo, in rows: [Anchor]) -> String? {
        guard let start = job.startedAt else { return nil }
        let end = job.finishedAt.map { $0.addingTimeInterval(slack) }
        return rows.last { e in
            guard e.isReply, let at = e.at else { return false }
            return at >= start.addingTimeInterval(-1) && (end == nil || at <= end!)
        }?.id
    }

    /// A pending approval still waits on the user (the tail of a chat that has one is never quiet).
    public static func pending(_ item: Item) -> Bool { (item.card["state"].string ?? "pending") == "pending" }
}
