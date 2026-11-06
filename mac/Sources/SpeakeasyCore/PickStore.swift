import Foundation

/// The one client-side record of decisions the user acted on that the server hasn't confirmed yet
/// (SPK-79): a tapped option or typed answer, a ✕ dismiss, a "Not needed". Keyed by question id (the
/// asking task's run id once it has one, else its task id). Everything else about a question (open,
/// answered, dismissed, moved on, expired, replaced) is shown from the server's `question_state`.
///
/// An entry leaves when the server reports the question no longer open (`reconcile`), when the send
/// fails (`remove`), or after `maxAge` with no confirmation (a server that never heard it shows the
/// card again rather than hiding it forever). Eviction is by age, oldest first, never by
/// Dictionary order (the old `answers.suffix(200)` dropped arbitrary entries).
public struct PickStore: Equatable, Sendable {
    public enum Kind: String, Codable, Sendable { case answer, dismissed, setAside = "set_aside" }

    public struct Entry: Codable, Equatable, Sendable {
        public var kind: Kind
        /// The answer's words (empty for a dismiss or set-aside).
        public var text: String
        public var at: Date
        public init(kind: Kind, text: String = "", at: Date = Date()) { self.kind = kind; self.text = text; self.at = at }
    }

    public static let maxEntries = 200
    /// Long enough for a slow server; short enough that an answer that never arrived doesn't hide
    /// the card for good.
    public static let maxAge: TimeInterval = 15 * 60

    public private(set) var entries: [String: Entry] = [:]

    public init(entries: [String: Entry] = [:]) { self.entries = entries }

    public subscript(id: String) -> Entry? { entries[id] }

    public func text(_ id: String) -> String? { entries[id].flatMap { $0.kind == .answer ? $0.text : nil } }

    public func has(_ id: String, _ kind: Kind) -> Bool { entries[id]?.kind == kind }

    public mutating func set(_ id: String, _ entry: Entry?) {
        entries[id] = entry
        trim()
    }

    public mutating func remove(_ id: String) { entries[id] = nil }

    /// Drop entries the server has settled. `state(id)` is the server's question state for that id
    /// (nil: the server doesn't know the task yet, or it isn't loaded: keep the entry).
    public mutating func reconcile(now: Date = Date(), state: (String) -> QuestionState?) {
        entries = entries.filter { id, entry in
            if now.timeIntervalSince(entry.at) > Self.maxAge { return false }
            guard let s = state(id) else { return true }
            return s == .open
        }
    }

    mutating func trim(now: Date = Date()) {
        entries = entries.filter { now.timeIntervalSince($0.value.at) <= Self.maxAge }
        guard entries.count > Self.maxEntries else { return }
        let keep = entries.sorted { $0.value.at > $1.value.at }.prefix(Self.maxEntries)
        entries = Dictionary(uniqueKeysWithValues: keep.map { ($0.key, $0.value) })
    }

    // -- persistence (so a relaunch mid-send still shows the pick until the server confirms) --

    public static func load(_ defaults: UserDefaults = .standard, key: String) -> PickStore {
        guard let data = defaults.data(forKey: key),
              let entries = try? JSONDecoder().decode([String: Entry].self, from: data) else { return PickStore() }
        var store = PickStore(entries: entries)
        store.trim()
        return store
    }

    public func save(_ defaults: UserDefaults = .standard, key: String) {
        if let data = try? JSONEncoder().encode(entries) { defaults.set(data, forKey: key) }
    }

    /// The client id an answer is sent under: the same pick for the same question always gets the same
    /// id, so a retry (a timeout, a relaunch mid-send, a second tap) is answered once by the server.
    public static func clientID(questionID: String, text: String) -> String {
        var hash: UInt64 = 0xcbf2_9ce4_8422_2325
        for byte in "\(questionID)\u{1F}\(text.lowercased())".utf8 { hash = (hash ^ UInt64(byte)) &* 0x100_0000_01b3 }
        return "pick-" + String(hash, radix: 16)
    }
}

/// A task's question, as the server records it (`question_state`, plugins with `question_state`).
/// Older plugins: derived from `answered` / `set_aside` / `question_closed`.
public enum QuestionState: String, Sendable {
    case open, answered, setAside = "set_aside", dismissed, movedOn = "moved_on", expired, superseded

    public var isOpen: Bool { self == .open }
}
