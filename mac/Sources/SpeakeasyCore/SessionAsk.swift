import Foundation

/// What was asked, shown at the top of a task's page when the thread itself no longer starts with it
/// (its first messages are still in Hermes, or Hermes stored a brief the page doesn't show as words).
public enum SessionAsk {
    /// The ask that opened this Speakeasy session: the earliest task's `request`. Nil for chats from
    /// other apps (their first message is the chat's, not a task's), when no task recorded one, or
    /// when the loaded thread already opens with those words.
    public static func line(_ c: HistoryConversation) -> String? {
        guard c.source == "speakeasy" else { return nil }
        let asked = c.jobs.enumerated()
            .filter { ($0.element.request?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "").isEmpty == false }
            .min { a, b in
                // Earliest start first; the list's own order breaks ties and covers missing times.
                switch (a.element.startedAt, b.element.startedAt) {
                case let (x?, y?) where x != y: return x < y
                case (_?, nil): return true
                case (nil, _?): return false
                default: return a.offset < b.offset
                }
            }?.element.request?.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let asked, !asked.isEmpty else { return nil }
        if !c.hasMore, let first = c.entries.first(where: { $0.kind == .user }), same(first.text, asked) { return nil }
        return asked.count > 280 ? String(asked.prefix(279)) + "…" : asked
    }

    /// The thread's first message says the same thing (ignoring case, spacing and punctuation).
    static func same(_ a: String, _ b: String) -> Bool {
        let norm: (String) -> String = { $0.lowercased().filter { $0.isLetter || $0.isNumber } }
        let x = norm(a), y = norm(b)
        guard !x.isEmpty, !y.isEmpty else { return false }
        return x == y || x.hasPrefix(y) || y.hasPrefix(x)
    }
}
