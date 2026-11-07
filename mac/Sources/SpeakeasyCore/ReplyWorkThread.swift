import Foundation

/// Preserve the current messaging renderer, attachments and model attribution. Only recorded work
/// leaves the bubble stream; its disclosure anchors before the answer, or at the live tail.
public struct ReplyWorkThread {
    public var entries: [HistoryEntry] = []
    public var before: [String: [ReplyWork]] = [:]
    public var tail: [ReplyWork] = []
    public func conversation(_ original: HistoryConversation) -> HistoryConversation {
        var copy = original
        copy.entries = entries
        return copy
    }
    public init(entries: [HistoryEntry], running: Bool = false, failed: Bool = false) {
        var pending: [ReplyWork] = []
        for row in ReplyWorkLayout.rows(entries, running: running, failed: failed) {
            if let work = row.work { pending.append(work) }
            if let entry = row.entry {
                if !pending.isEmpty { before[entry.id] = pending; pending = [] }
                self.entries.append(entry)
            }
        }
        tail = pending
    }
}
