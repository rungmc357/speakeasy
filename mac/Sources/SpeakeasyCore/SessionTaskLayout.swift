import Foundation

/// Where a session page shows its Speakeasy tasks.
///
/// A finished task lives in the thread where it was asked, as one quiet line ("✓ Fixed the login
/// redirect · 9:41 PM ›"), never re-stacked at the bottom. Only what is still going (running, or
/// waiting on you) stays at the end of the thread, just above the composer; when it finishes it
/// moves back to its place. Tasks asked again under the same name or ask show once. Finished tasks
/// asked before the loaded page are counted for one collapsed "N tasks finished in this chat" row.
public struct SessionTaskLayout: Equatable, Sendable {
    /// One message of the thread, as far as placement cares.
    public struct Anchor: Equatable, Sendable {
        public var id: String
        public var isUser: Bool
        public var at: Date?
        public var text: String
        public init(id: String, isUser: Bool, at: Date?, text: String) {
            self.id = id; self.isUser = isUser; self.at = at; self.text = text
        }
    }

    /// Finished tasks shown after a message, keyed by that message's id (oldest first per message): the
    /// last reply to the message the task was asked in.
    public var after: [String: [WorkInfo]] = [:]
    /// Finished tasks placed before the first loaded message (the page holds the whole chat).
    public var top: [WorkInfo] = []
    /// Running / needs-you tasks at the end of the thread, oldest first.
    public var pinned: [WorkInfo] = []
    /// Every finished task of the chat once (duplicates folded), newest first.
    public var finished: [WorkInfo] = []
    /// Finished tasks that have no place on this page (asked before its first message).
    public var earlier: [WorkInfo] = []

    public static let finishedStates: Set<String> = ["completed", "failed", "cancelled"]

    /// The "N tasks finished in this chat" row shows only when some finished task isn't in the thread.
    public var showsFinishedRow: Bool { !earlier.isEmpty }

    /// How far after a task's admission its ask may be stored (Hermes writes the turn as the run starts).
    static let askSlack: TimeInterval = 20

    /// - Parameters:
    ///   - jobs: the session's tasks as the server sends them (newest first).
    ///   - messages: the thread in order.
    ///   - hasMore: older messages exist that aren't loaded.
    ///   - asking: run ids whose approval/question card stands in for the task (not shown again
    ///     unless still running).
    public init(jobs: [WorkInfo], messages: [Anchor], hasMore: Bool, asking: Set<String> = []) {
        let shown = jobs.filter { !(asking.contains($0.runID ?? "") && $0.status != "running") }
        // Duplicates: what's still going claims its name first, then the newest finished one.
        var claimed = Set<String>()
        var live: [WorkInfo] = []
        for job in shown where !Self.finishedStates.contains(job.status) {
            claimed.insert(Self.key(job))
            live.append(job)
        }
        var done: [WorkInfo] = []
        for job in shown where Self.finishedStates.contains(job.status) {
            let key = Self.key(job)
            guard !claimed.contains(key) else { continue }
            claimed.insert(key)
            done.append(job)
        }
        pinned = live.reversed()
        finished = done
        for job in done.reversed() {
            switch Self.place(job, in: messages, hasMore: hasMore) {
            case .after(let id): after[Self.endOfTurn(id, in: messages), default: []].append(job)
            case .top: top.append(job)
            case .earlier: earlier.append(job)
            }
        }
    }

    public init() {}

    /// Every task shown in the thread or pinned (for tests and accessibility counts).
    public var inThread: [WorkInfo] { top + after.values.flatMap { $0 } }

    enum Place: Equatable { case after(String), top, earlier }

    /// The message it was asked in: the user message that says what the task was asked, else the
    /// last user message at or before the task started.
    static func place(_ job: WorkInfo, in messages: [Anchor], hasMore: Bool) -> Place {
        let users = messages.filter(\.isUser)
        let start = job.startedAt
        let ask = job.request.map(Self.norm) ?? ""
        if ask.count >= 6 {
            let probe = String(ask.prefix(48))
            let matches = users.filter { m in
                let t = Self.norm(m.text)
                // The message is the ask, or the ask starts with the whole (not too short) message.
                return !t.isEmpty && (t.hasPrefix(probe) || (t.count >= 12 && probe.hasPrefix(String(t.prefix(48)))))
            }
            // The same words asked twice: the one nearest the task's start.
            if let start, let best = matches.min(by: { abs(($0.at ?? .distantPast).timeIntervalSince(start))
                                                        < abs(($1.at ?? .distantPast).timeIntervalSince(start)) }) {
                return .after(best.id)
            }
            if let only = matches.last { return .after(only.id) }
        }
        guard let start else {
            // No time to go by: right after the newest message, never in a pile below it.
            return messages.last.map { .after($0.id) } ?? (hasMore ? .earlier : .top)
        }
        let limit = start.addingTimeInterval(askSlack)
        if let ask = users.last(where: { ($0.at ?? .distantFuture) <= limit }) { return .after(ask.id) }
        if let any = messages.last(where: { ($0.at ?? .distantFuture) <= limit }) { return .after(any.id) }
        return hasMore ? .earlier : .top
    }

    /// A finished task closes its exchange: after the replies to its ask, before the next thing you said.
    static func endOfTurn(_ id: String, in messages: [Anchor]) -> String {
        guard var i = messages.firstIndex(where: { $0.id == id }) else { return id }
        while i + 1 < messages.count, !messages[i + 1].isUser { i += 1 }
        return messages[i].id
    }

    /// Two tasks are the same when they carry the same name (or, without one, the same ask).
    static func key(_ job: WorkInfo) -> String {
        let name = norm(job.title ?? "")
        if !name.isEmpty { return "t:" + name }
        let ask = norm(job.askedFor ?? "")
        return ask.isEmpty ? "r:" + (job.runID ?? UUID().uuidString) : "a:" + ask
    }

    static func norm(_ text: String) -> String {
        let folded = text.lowercased().folding(options: [.diacriticInsensitive], locale: nil)
        let words = folded.components(separatedBy: CharacterSet.alphanumerics.inverted).filter { !$0.isEmpty }
        return words.joined(separator: " ")
    }
}

/// The words of a finished task's quiet line.
public enum TaskLineCopy {
    /// "Fixed the login redirect · 9:41 PM" (failed/stopped tasks say so).
    public static func line(_ job: WorkInfo, now: Date = Date(), calendar: Calendar = .current,
                            locale: Locale = .current) -> String {
        let name = (job.title ?? job.summary ?? job.request ?? "Task").trimmingCharacters(in: .whitespacesAndNewlines)
        var parts = [name]
        if let closed = questionNote(job) { parts = [closed] }
        switch job.status {
        case "failed": parts.append("Didn't finish")
        case "cancelled": parts.append("Stopped")
        default: break
        }
        if let when = job.finishedAt ?? job.updatedAt ?? job.updated ?? job.startedAt {
            parts.append(time(when, now: now, calendar: calendar, locale: locale))
        }
        return parts.joined(separator: " · ")
    }

    /// A question nobody answered, as its quiet line's words: "Question · moved on · Paint the shed
    /// green or grey?" (or "· dismissed"); nil when the task's question was answered or never asked.
    public static func questionNote(_ job: WorkInfo) -> String? {
        guard let closed = job.questionClosed, let asked = job.decision?.question, !job.answered else { return nil }
        let how = closed == "dismissed" ? "dismissed" : "moved on"
        let words = asked.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        let short = words.count > 70 ? String(words.prefix(69)).trimmingCharacters(in: .whitespaces) + "…" : words
        return "Question · \(how) · \(short)"
    }

    /// "9:41 PM" today, "Tue 9:41 PM" this week, "Oct 3" before that.
    public static func time(_ date: Date, now: Date = Date(), calendar: Calendar = .current, locale: Locale = .current) -> String {
        let f = DateFormatter()
        f.locale = locale; f.calendar = calendar; f.timeZone = calendar.timeZone
        if calendar.isDate(date, inSameDayAs: now) {
            f.setLocalizedDateFormatFromTemplate("jmm")
        } else if let days = calendar.dateComponents([.day], from: date, to: now).day, days < 6 {
            f.setLocalizedDateFormatFromTemplate("EEEjmm")
        } else {
            f.setLocalizedDateFormatFromTemplate("MMMd")
        }
        return f.string(from: date)
    }

    /// "3 tasks finished in this chat".
    public static func finishedRow(_ count: Int) -> String {
        count == 1 ? "1 task finished in this chat" : "\(count) tasks finished in this chat"
    }
}
