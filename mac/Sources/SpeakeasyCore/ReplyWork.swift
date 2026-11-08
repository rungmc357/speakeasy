import Foundation

/// A reply's work is a presentation of recorded activity, never a second conversation.
public struct ReplyWork: Equatable, Sendable, Identifiable {
    public struct Section: Equatable, Sendable, Identifiable {
        public var id: String
        public var heading: String
        public var detail: String? = nil
        public var calls: [JobActivityItem]
    }
    public var id: String
    public var sections: [Section]
    public var running: Bool
    public var failed: Bool
    public var summary: String? = nil
    public var calls: [JobActivityItem] { sections.flatMap(\.calls) }
    public var line: String {
        if running, let heading = sections.last?.heading, !heading.isEmpty { return Self.sentence(heading) }
        let plain = ToolCallLog.plainSummary(calls)
        return plain.isEmpty ? (summary ?? (running ? "Working on your request" : "Looked into it")) : plain
    }
    public static func sentence(_ text: String) -> String {
        let first = text.components(separatedBy: " — ").first ?? text
        return first.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

public struct ReplyWorkRow: Equatable, Sendable, Identifiable {
    public var id: String
    public var entry: HistoryEntry?
    public var work: ReplyWork?
}

public enum ReplyWorkLayout {
    public static func fallback(_ job: WorkInfo) -> ReplyWork {
        let running = !["completed", "failed", "cancelled"].contains(job.status)
        let line = running ? (job.nowLine ?? job.shortStatus ?? "Working on your request") : (job.failureReason ?? "")
        let id = job.runID ?? job.sessionID ?? "work"
        return ReplyWork(id: id, sections: [.init(id: id, heading: ReplyWork.sentence(line),
            detail: job.soFar, calls: job.activity.filter(\.isToolCall))], running: running,
            failed: job.status == "failed", summary: job.title)
    }
    /// A separate task summary is needed only when its work is absent from the visible thread.
    /// Older calls in a long conversation must not hide a newly started task.
    public static func represents(_ job: WorkInfo, in conversation: HistoryConversation) -> Bool {
        let calls = conversation.entries.filter { $0.kind == .tools }.flatMap(\.tools)
        guard !calls.isEmpty else { return false }
        if let start = job.startedAt {
            return calls.contains { call in
                guard let at = call.startedAt ?? call.at else { return false }
                return at >= start && (job.finishedAt == nil || at <= job.finishedAt!)
            }
        }
        return conversation.jobs.count == 1 && (job.sessionID == nil || job.sessionID == conversation.id)
    }

    public static func job(for work: ReplyWork, in conversation: HistoryConversation) -> WorkInfo? {
        let times = work.calls.compactMap { $0.startedAt ?? $0.at }
        if let at = times.min(), let match = conversation.jobs.filter({
            guard let start = $0.startedAt else { return false }
            return at >= start && ($0.finishedAt == nil || at <= $0.finishedAt!)
        }).max(by: { ($0.startedAt ?? .distantPast) < ($1.startedAt ?? .distantPast) }) { return match }
        guard conversation.jobs.count == 1, let job = conversation.jobs.first else { return nil }
        return job.startedAt == nil || times.isEmpty ? job : nil
    }

    /// Assistant rows immediately preceding recorded tool use are narration. Do not guess from
    /// phrases: a plain answer with no subsequent tool use stays a message. User rows delimit turns,
    /// final answers delimit replies, and notes remain visible without becoming the user's words.
    public static func rows(_ entries: [HistoryEntry], running: Bool = false, failed: Bool = false) -> [ReplyWorkRow] {
        var out: [ReplyWorkRow] = []
        var sections: [ReplyWork.Section] = []
        var firstID: String?
        func flush(live: Bool = false, stopped: Bool = false) {
            guard let id = firstID else { return }
            let work = ReplyWork(id: "work-" + id, sections: sections, running: live, failed: stopped)
            out.append(ReplyWorkRow(id: work.id, work: work))
            sections = []; firstID = nil
        }
        for (index, entry) in entries.enumerated() {
            let beforeTools = index + 1 < entries.count && entries[index + 1].kind == .tools
            let liveCommentary = running && index == entries.count - 1
            let narration = entry.kind == .assistant && entry.files.isEmpty && !entry.update
                && (entry.narration || beforeTools || liveCommentary)
            if narration {
                firstID = firstID ?? entry.id
                sections.append(.init(id: entry.id, heading: entry.text, detail: entry.workDetail, calls: []))
            } else if entry.kind == .tools {
                firstID = firstID ?? entry.id
                if sections.isEmpty { sections.append(.init(id: entry.id, heading: "", calls: [])) }
                // Tool ids may restart for each recorded batch; qualify by the entry to preserve every call.
                let calls = entry.tools.filter(\.isToolCall).map { item in
                    var item = item; item.id = entry.id + "|" + item.id; return item
                }
                sections[sections.count - 1].calls.append(contentsOf: calls)
            } else {
                if entry.kind != .note { flush() }
                out.append(ReplyWorkRow(id: entry.id, entry: entry))
            }
        }
        flush(live: running, stopped: failed)
        return out
    }
}
