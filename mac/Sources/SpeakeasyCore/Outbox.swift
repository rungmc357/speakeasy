import Foundation

// One lifecycle for every message this device sends (SPK-80): the job reply bar's `TaskOutbox`, the
// session page's `HistoryOutbox` (typed and voice notes) and the iPhone's local bubbles all keep
// their entries in an `OutboxLedger`. A message is known by its `client_id` only: the send's reply,
// the server's history row, its steer receipt and its queued row all carry that id. Text is never
// used to match (a reworded echo is still ours; two identical messages are two messages).

/// Where one message is.
public enum OutboxPhase: Equatable, Sendable {
    /// Waiting behind a message still being sent.
    case queued
    case sending
    /// The server took it but no turn has read it yet (`delivered_as: pending`): not "Sent".
    case pending
    case sent(TaskMessageDelivery)
    /// The server's sentence (or a plain one), with Retry.
    case failed(String)
    /// Its task stopped before reading it (a receipt in state `unused`).
    case unused(String)

    /// Still on its way: pages keep polling fast only while some message is here.
    public var isInFlight: Bool { self == .queued || self == .sending || self == .pending }
    public var isSettled: Bool { !isInFlight }
    public var canRetry: Bool {
        switch self { case .failed, .unused: return true; default: return false }
    }

    /// The phase a send's reply settles to.
    public static func settled(_ reply: TaskMessageDelivery) -> OutboxPhase {
        reply == .pending ? .pending : .sent(reply)
    }
}

/// An outbox entry: its client id is its identity.
public protocol OutboxItem: Equatable, Sendable {
    var id: String { get }
    var phase: OutboxPhase { get set }
}

/// Ordered storage + lifecycle shared by every outbox. Adding the same id again replaces the
/// entry (a Retry or a re-sent request keeps one bubble).
public struct OutboxLedger<Item: OutboxItem>: Equatable, Sendable {
    public private(set) var items: [Item] = []
    /// How many settled entries to keep (in-flight ones always stay); nil keeps all.
    public var keepSettled: Int?
    public init(keepSettled: Int? = nil) { self.keepSettled = keepSettled }

    public mutating func add(_ item: Item) {
        items.removeAll { $0.id == item.id }
        items.append(item)
        trim()
    }
    public func item(_ id: String) -> Item? { items.first { $0.id == id } }
    public mutating func set(_ id: String, _ phase: OutboxPhase) {
        guard let i = items.firstIndex(where: { $0.id == id }) else { return }
        items[i].phase = phase
        trim()
    }
    public mutating func update(_ id: String, _ change: (inout Item) -> Void) {
        guard let i = items.firstIndex(where: { $0.id == id }) else { return }
        change(&items[i])
    }
    /// Back in line (failed or unused only), same id and payload.
    public mutating func retry(_ id: String) {
        guard let i = items.firstIndex(where: { $0.id == id }), items[i].phase.canRetry else { return }
        items[i].phase = .queued
    }
    public mutating func remove(_ id: String) { items.removeAll { $0.id == id } }
    public mutating func removeAll(where drop: (Item) -> Bool) { items.removeAll(where: drop) }
    public var hasInFlight: Bool { items.contains { $0.phase.isInFlight } }

    /// Settles each entry from the server's page, by client id: echoed ones leave (the transcript
    /// shows them), held ones leave (the queued list shows them), a stopped task's turn them unused.
    public mutating func reconcile(_ c: HistoryConversation, only include: (Item) -> Bool = { _ in true }) {
        for item in items where include(item) {
            switch OutboxServerView.of(item.id, in: c) {
            case .echoed, .queued: remove(item.id)
            case .unused(let why): set(item.id, .unused(why))
            case .delivered: if item.phase == .pending { set(item.id, .sent(.steer)) }
            case .unknown: break
            }
        }
    }

    private mutating func trim() {
        guard let keep = keepSettled else { return }
        let settled = items.filter { $0.phase.isSettled }
        guard settled.count > keep else { return }
        let drop = Set(settled.prefix(settled.count - keep).map(\.id))
        items.removeAll { drop.contains($0.id) }
    }
}

/// What the server's page says about one of our messages, by client id only.
public enum OutboxServerView: Equatable, Sendable {
    /// A user row with our client id (or our voice clip): the transcript shows it now.
    case echoed
    /// The page's queued list holds it.
    case queued
    /// Its task stopped before reading it (the server's words).
    case unused(String)
    /// A steer receipt that it reached the task (sent, waiting, seen, follow-up).
    case delivered
    case unknown

    public static func of(_ clientID: String, in c: HistoryConversation?) -> OutboxServerView {
        guard let c, !clientID.isEmpty else { return .unknown }
        if c.entries.contains(where: { $0.kind == .user && ($0.clientID == clientID || $0.voiceClip == clientID) }) { return .echoed }
        if let q = c.queued.first(where: { $0.clientID == clientID }) { return q.isUnused ? .unused(q.tag) : .queued }
        if let r = c.steered.receipt(clientID: clientID) { return r.state == .unused ? .unused(r.label) : .delivered }
        return .unknown
    }
}

/// A send whose owner (a client, a page) may be gone by the time it would start or finish: the
/// completion still runs, with a failure, so the message never stays "Sending…".
public enum OutboxCompletion {
    public static let ownerGone = "That didn't send. Try again."

    public static func run<Owner: AnyObject, Reply>(_ owner: Owner?, done: @escaping (Reply?, String?) -> Void,
                                                    _ body: (Owner) -> Void) {
        guard let owner else { done(nil, ownerGone); return }
        body(owner)
    }
}
