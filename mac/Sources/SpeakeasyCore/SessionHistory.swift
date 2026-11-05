import Foundation

// Every Hermes session and Speakeasy task as a browsable, writable history (servers with
// `history`; docs/API.md "History"). Messages sent from a session page go to that session by id,
// never through the call router.

/// One row of `GET /voice/history`.
public struct HistorySession: Equatable, Sendable, Identifiable, Hashable {
    public enum State: String, Equatable, Sendable { case idle, running, needsYou = "needs_you" }

    public var id: String
    public var title: String
    /// `speakeasy` for Speakeasy's own sessions, else Hermes' source (`discord`, `telegram`, `cli`, …).
    public var source: String
    /// Ready to show: "Speakeasy", "Discord · #build", "Telegram", "Terminal".
    public var sourceLabel: String
    public var lastActivity: Date?
    public var preview: String
    public var state: State
    /// Its Speakeasy tasks, newest first (open with `GET /voice/work/{run_id}`).
    public var runIDs: [String]
    /// A snippet of the matching message (searches only).
    public var match: String?

    public init(id: String, title: String, source: String, sourceLabel: String, lastActivity: Date?,
                preview: String = "", state: State = .idle, runIDs: [String] = [], match: String? = nil) {
        self.id = id; self.title = title; self.source = source; self.sourceLabel = sourceLabel
        self.lastActivity = lastActivity; self.preview = preview; self.state = state; self.runIDs = runIDs
        self.match = match
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) else { return nil }
        self.init(id: id, title: String((nonEmpty(o["title"]) ?? "Untitled conversation").prefix(120)),
                  source: nonEmpty(o["source"]) ?? "hermes",
                  sourceLabel: String((nonEmpty(o["source_label"]) ?? "Hermes").prefix(60)),
                  lastActivity: decodeDate(o["last_activity"]),
                  preview: String((nonEmpty(o["preview"]) ?? "").prefix(200)),
                  state: (o["state"] as? String).flatMap(State.init(rawValue:)) ?? .idle,
                  runIDs: (o["run_ids"] as? [Any] ?? []).compactMap { nonEmpty($0) },
                  match: nonEmpty(o["match"]))
    }

    public var isSpeakeasy: Bool { source == "speakeasy" }
    public var isActive: Bool { state != .idle }
}

public struct HistoryFilter: Equatable, Sendable, Identifiable, Hashable {
    public var id: String
    public var label: String
    public init(id: String, label: String) { self.id = id; self.label = label }
}

/// `GET /voice/history`'s page.
public struct HistoryPage: Equatable, Sendable {
    public var sessions: [HistorySession]
    public var nextCursor: String?
    public var filters: [HistoryFilter]

    public init(sessions: [HistorySession], nextCursor: String? = nil, filters: [HistoryFilter] = []) {
        self.sessions = sessions; self.nextCursor = nextCursor; self.filters = filters
    }

    public init(json: Any?) {
        let o = json as? [String: Any] ?? [:]
        sessions = (o["sessions"] as? [Any] ?? []).compactMap(HistorySession.init(json:))
        nextCursor = nonEmpty(o["next_cursor"])
        filters = (o["filters"] as? [Any] ?? []).compactMap { raw in
            guard let f = raw as? [String: Any], let id = nonEmpty(f["id"]), let label = nonEmpty(f["label"]) else { return nil }
            return HistoryFilter(id: id, label: label)
        }
    }

    /// The query string for a page: search, source filter, cursor (all optional).
    public static func path(query: String = "", source: String? = nil, cursor: String? = nil, limit: Int? = nil) -> String {
        var items: [URLQueryItem] = []
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if !q.isEmpty { items.append(URLQueryItem(name: "q", value: String(q.prefix(200)))) }
        if let source, !source.isEmpty { items.append(URLQueryItem(name: "source", value: source)) }
        if let cursor { items.append(URLQueryItem(name: "cursor", value: cursor)) }
        if let limit { items.append(URLQueryItem(name: "limit", value: String(limit))) }
        var parts = URLComponents()
        parts.path = "/voice/history"
        parts.queryItems = items.isEmpty ? nil : items
        // "+" in a cursor or query must stay literal.
        parts.percentEncodedQuery = parts.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B")
        return parts.string ?? "/voice/history"
    }
}

/// The panel's groups: Today, Yesterday, Earlier (by last activity, in the user's calendar).
public enum HistoryGroup: String, CaseIterable, Sendable, Identifiable {
    case today = "Today", yesterday = "Yesterday", earlier = "Earlier"
    public var id: String { rawValue }

    public static func of(_ date: Date?, now: Date = Date(), calendar: Calendar = .current) -> HistoryGroup {
        guard let date else { return .earlier }
        if calendar.isDate(date, inSameDayAs: now) { return .today }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: yesterday) {
            return .yesterday
        }
        return .earlier
    }

    /// Sessions in their groups, newest first inside each; empty groups left out.
    public static func grouped(_ sessions: [HistorySession], now: Date = Date(),
                               calendar: Calendar = .current) -> [(HistoryGroup, [HistorySession])] {
        let buckets = Dictionary(grouping: sessions) { of($0.lastActivity, now: now, calendar: calendar) }
        return allCases.compactMap { group in
            guard let rows = buckets[group], !rows.isEmpty else { return nil }
            return (group, rows.sorted { ($0.lastActivity ?? .distantPast) > ($1.lastActivity ?? .distantPast) })
        }
    }
}

/// One entry of a session's conversation.
public struct HistoryEntry: Equatable, Sendable, Identifiable {
    public enum Kind: String, Equatable, Sendable { case user, assistant, note, tools }
    public var id: String
    public var kind: Kind
    public var at: Date?
    public var text: String
    /// `tools`: the calls made between two messages, in the job tool log's shape.
    public var tools: [JobActivityItem]

    public init(id: String, kind: Kind, at: Date? = nil, text: String = "", tools: [JobActivityItem] = []) {
        self.id = id; self.kind = kind; self.at = at; self.text = text; self.tools = tools
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]),
              let kind = (o["kind"] as? String).flatMap(Kind.init(rawValue:)) else { return nil }
        let tools = kind == .tools
            ? (o["items"] as? [Any] ?? []).enumerated().compactMap { JobActivityItem(json: $0.element, index: $0.offset) } : []
        let text = nonEmpty(o["text"]) ?? ""
        if kind == .tools ? tools.isEmpty : text.isEmpty { return nil }
        self.init(id: id, kind: kind, at: decodeDate(o["at"]), text: text, tools: tools)
    }
}

/// A message the app sent through Hermes into a session another surface owns (`sends`).
public struct HistorySend: Equatable, Sendable, Identifiable {
    public enum Status: String, Equatable, Sendable { case sending, running, needsYou = "needs_you", completed, failed }
    public struct Prompt: Equatable, Sendable {
        public var kind: String   // approval | question
        public var promptID: String?
        public var text: String
        public var choices: [String]
        public init(kind: String, promptID: String?, text: String, choices: [String]) {
            self.kind = kind; self.promptID = promptID; self.text = text; self.choices = choices
        }
    }
    public var id: String
    public var status: Status
    public var text: String
    public var reply: String?
    public var error: String?
    public var started: Date?
    public var prompt: Prompt?

    public init(id: String, status: Status, text: String, reply: String? = nil, error: String? = nil,
                started: Date? = nil, prompt: Prompt? = nil) {
        self.id = id; self.status = status; self.text = text; self.reply = reply; self.error = error
        self.started = started; self.prompt = prompt
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["send_id"]),
              let status = (o["status"] as? String).flatMap(Status.init(rawValue:)) else { return nil }
        var prompt: Prompt?
        if let p = o["prompt"] as? [String: Any], let text = nonEmpty(p["text"]) {
            prompt = Prompt(kind: nonEmpty(p["kind"]) ?? "approval", promptID: nonEmpty(p["prompt_id"]), text: text,
                            choices: (p["choices"] as? [Any] ?? []).compactMap { nonEmpty($0) })
        }
        self.init(id: id, status: status, text: nonEmpty(o["text"]) ?? "", reply: nonEmpty(o["reply"]),
                  error: nonEmpty(o["error"]), started: decodeDate(o["started"]), prompt: prompt)
    }

    public var isLive: Bool { status == .sending || status == .running || status == .needsYou }
}

/// `GET /voice/history/{session_id}`.
public struct HistoryConversation: Equatable, Sendable {
    public enum SendVia: String, Equatable, Sendable { case task, sessionJob = "session_job", hermes }
    public var id: String
    public var title: String
    public var source: String
    public var sourceLabel: String
    public var state: HistorySession.State
    public var sendVia: SendVia
    public var entries: [HistoryEntry]
    public var hasMore: Bool
    public var before: Int?
    /// Its Speakeasy tasks, newest first, with their cards (approvals, questions) and helpers.
    public var jobs: [WorkInfo]
    public var sends: [HistorySend]

    public init(id: String, title: String, source: String, sourceLabel: String, state: HistorySession.State = .idle,
                sendVia: SendVia = .hermes, entries: [HistoryEntry] = [], hasMore: Bool = false, before: Int? = nil,
                jobs: [WorkInfo] = [], sends: [HistorySend] = []) {
        self.id = id; self.title = title; self.source = source; self.sourceLabel = sourceLabel; self.state = state
        self.sendVia = sendVia; self.entries = entries; self.hasMore = hasMore; self.before = before
        self.jobs = jobs; self.sends = sends
    }

    public init?(json: Any?) {
        guard let root = json as? [String: Any], let o = root["session"] as? [String: Any],
              let id = nonEmpty(o["id"]) else { return nil }
        self.init(id: id, title: nonEmpty(o["title"]) ?? "Untitled conversation", source: nonEmpty(o["source"]) ?? "hermes",
                  sourceLabel: nonEmpty(o["source_label"]) ?? "Hermes",
                  state: (o["state"] as? String).flatMap(HistorySession.State.init(rawValue:)) ?? .idle,
                  sendVia: (o["send_via"] as? String).flatMap(SendVia.init(rawValue:)) ?? .hermes,
                  entries: (o["entries"] as? [Any] ?? []).compactMap(HistoryEntry.init(json:)),
                  hasMore: o["has_more"] as? Bool ?? false, before: (o["before"] as? NSNumber)?.intValue,
                  jobs: (o["jobs"] as? [Any] ?? []).compactMap(WorkInfo.init(json:)),
                  sends: (o["sends"] as? [Any] ?? []).compactMap(HistorySend.init(json:)))
    }

    /// Jobs waiting on the user: an approval, or a question card not set aside.
    public var waitingJobs: [WorkInfo] {
        jobs.filter { ($0.status == "waiting_for_approval" || ($0.status == "completed" && $0.decision != nil))
            && !$0.setAside && !$0.isTerminalFailure }
    }
    public var isWorking: Bool { state == .running || sends.contains(where: \.isLive) }

    public static func path(_ sessionID: String, before: Int? = nil) -> String {
        let id = sessionID.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-_."))) ?? sessionID
        return "/voice/history/\(id)" + (before.map { "?before=\($0)" } ?? "")
    }
}

private extension WorkInfo {
    var isTerminalFailure: Bool { status == "failed" || status == "cancelled" }
}

/// `POST /voice/history/{session_id}/message`.
public struct HistoryMessageRequest: Equatable, Sendable {
    public enum Source: String, Sendable { case typed, voiceMessage = "voice_message" }
    public var sessionID: String
    public var text: String
    public var source: Source
    public var clientID: String

    public init?(sessionID: String, text: String, source: Source = .typed, clientID: String = UUID().uuidString) {
        guard let clean = TaskMessageRequest.clean(text) else { return nil }
        self.sessionID = sessionID; self.text = clean; self.source = source; self.clientID = clientID
    }

    public var path: String { HistoryConversation.path(sessionID) + "/message" }
    public var body: [String: Any] { ["text": text, "source": source.rawValue, "client_id": clientID] }
}

public struct HistoryMessageReply: Equatable, Sendable {
    public var via: HistoryConversation.SendVia
    /// The task to follow like a job (task and session_job doors).
    public var runID: String?
    /// The send to follow in the session's `sends` (hermes door).
    public var sendID: String?

    public init(via: HistoryConversation.SendVia, runID: String?, sendID: String?) {
        self.via = via; self.runID = runID; self.sendID = sendID
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], o["ok"] as? Bool == true,
              let via = (o["via"] as? String).flatMap(HistoryConversation.SendVia.init(rawValue:)) else { return nil }
        self.init(via: via, runID: nonEmpty(o["run_id"]), sendID: nonEmpty(o["send_id"]))
    }
}
