import Foundation

// Session / task history (servers with `capabilities.history`): every Hermes session and task,
// newest first, searchable (titles and message text), filterable by where it started. One opens to
// its messages, tool calls, helpers and anything it's waiting on; the user can type or speak a
// message to it. A message from a session page goes to that session BY ID (never through the
// voice's router), and its reply shows in Speakeasy only (never posted to Discord/Telegram).
//
// Endpoints (docs/API.md "History"):
//   GET  /voice/history?cursor=&q=&source=&limit=
//   GET  /voice/history/{session_id}
//   POST /voice/history/{session_id}/message
//   POST /voice/work                          (New chat)
//
// Decoding is tolerant: every field is optional except an id, and a couple of aliases are accepted
// so a server that names a field slightly differently still draws.

// MARK: - Sessions

/// Where a session started ("speakeasy", "discord", "telegram", "cli", "cron", ...).
public struct HistorySource: Hashable, Sendable, Identifiable {
    public var id: String
    public var label: String
    public var count: Int?
    public init(id: String, label: String? = nil, count: Int? = nil) {
        self.id = id; self.label = label ?? Self.label(for: id); self.count = count
    }

    public static func label(for id: String) -> String {
        switch id.lowercased() {
        case "speakeasy", "voice": return "Speakeasy"
        case "discord": return "Discord"
        case "telegram": return "Telegram"
        case "slack": return "Slack"
        case "cli", "terminal": return "Terminal"
        case "cron", "scheduled": return "Scheduled"
        case "api": return "API"
        case "whatsapp": return "WhatsApp"
        case "signal": return "Signal"
        case "email": return "Email"
        case "": return "Other"
        default: return id.prefix(1).uppercased() + id.dropFirst()
        }
    }

    /// The SF Symbol drawn next to a session from this source.
    public static func symbol(for id: String?) -> String {
        switch id?.lowercased() ?? "" {
        case "speakeasy", "voice": return "waveform"
        case "discord", "slack", "telegram", "whatsapp", "signal": return "bubble.left.and.bubble.right"
        case "cli", "terminal": return "terminal"
        case "cron", "scheduled": return "clock.arrow.circlepath"
        case "email": return "envelope"
        default: return "circle.grid.2x2"
        }
    }

    /// Shown when the server doesn't list its sources.
    public static let defaults: [HistorySource] = ["speakeasy", "discord", "telegram", "cli", "cron"].map { HistorySource(id: $0) }

    static func list(json: Any?) -> [HistorySource] {
        guard let items = json as? [Any] else { return [] }
        return items.compactMap { item in
            if let s = item as? String, !s.isEmpty { return HistorySource(id: s) }
            guard let o = item as? [String: Any], let id = nonEmpty(o["id"]) ?? nonEmpty(o["source"]) else { return nil }
            return HistorySource(id: id, label: nonEmpty(o["label"]), count: (o["count"] as? NSNumber)?.intValue)
        }
    }
}

public enum HistoryStatus: String, Equatable, Sendable {
    case running, waiting, done, failed, idle

    init(raw: String?) {
        switch raw?.lowercased() ?? "" {
        case "running", "working", "admitting", "active", "in_progress": self = .running
        case "waiting", "waiting_for_approval", "waiting_on_user", "needs_you", "question": self = .waiting
        case "completed", "done", "finished", "succeeded": self = .done
        case "failed", "error", "cancelled", "canceled": self = .failed
        default: self = .idle
        }
    }

    public var word: String {
        switch self {
        case .running: return "Working"
        case .waiting: return "Needs you"
        case .done: return "Done"
        case .failed: return "Didn't finish"
        case .idle: return "Chat"
        }
    }
}

/// One row in the history list.
public struct HistorySession: Equatable, Sendable, Identifiable {
    public var id: String
    public var title: String
    public var source: String?
    /// The last thing said, one line.
    public var preview: String?
    /// With a search: the matching text around the hit.
    public var snippet: String?
    public var startedAt: Date?
    public var updatedAt: Date?
    public var status: HistoryStatus
    public var messageCount: Int?
    /// The newest task's run id, when the session has one.
    public var runID: String?
    /// "session" (a chat) or "task" (a job Speakeasy started).
    public var kind: String?
    /// Ready to show ("Discord · #build"), when the server names it.
    public var sourceLabel: String?

    public init(id: String, title: String, source: String? = nil, preview: String? = nil, snippet: String? = nil,
                startedAt: Date? = nil, updatedAt: Date? = nil, status: HistoryStatus = .idle, messageCount: Int? = nil,
                runID: String? = nil, kind: String? = nil, sourceLabel: String? = nil) {
        self.id = id; self.title = title; self.source = source; self.preview = preview; self.snippet = snippet
        self.startedAt = startedAt; self.updatedAt = updatedAt; self.status = status; self.messageCount = messageCount
        self.runID = runID; self.kind = kind; self.sourceLabel = sourceLabel
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) ?? nonEmpty(o["session_id"]) else { return nil }
        let preview = nonEmpty(o["preview"]) ?? nonEmpty(o["last_message"])
        self.init(id: id,
                  title: (nonEmpty(o["title"]) ?? preview.map { String($0.prefix(60)) } ?? "Untitled").trimmingCharacters(in: .whitespacesAndNewlines),
                  source: nonEmpty(o["source"]) ?? nonEmpty(o["platform"]),
                  preview: preview.map { Self.oneLine($0) },
                  snippet: (nonEmpty(o["snippet"]) ?? nonEmpty(o["match"])).map { Self.oneLine($0) },
                  startedAt: decodeHistoryDate(o["started_at"]) ?? decodeHistoryDate(o["created_at"]),
                  updatedAt: decodeHistoryDate(o["last_activity"]) ?? decodeHistoryDate(o["updated_at"])
                      ?? decodeHistoryDate(o["last_active"]) ?? decodeHistoryDate(o["last_message_at"]),
                  status: HistoryStatus(raw: (o["state"] ?? o["status"]) as? String),
                  messageCount: (o["message_count"] as? NSNumber)?.intValue,
                  runID: nonEmpty(o["run_id"]) ?? (o["run_ids"] as? [Any])?.first.flatMap { nonEmpty($0) },
                  kind: nonEmpty(o["kind"]), sourceLabel: nonEmpty(o["source_label"]))
    }

    /// What to call where it came from.
    public var sourceName: String? { sourceLabel ?? source.map { HistorySource.label(for: $0) } }

    /// The time the list sorts and groups by.
    public var sortDate: Date? { updatedAt ?? startedAt }

    public static func oneLine(_ s: String) -> String {
        s.split(whereSeparator: \.isNewline).joined(separator: " ").trimmingCharacters(in: .whitespaces)
    }
}

/// Seconds or milliseconds since 1970, or an ISO 8601 string.
func decodeHistoryDate(_ value: Any?) -> Date? {
    if let n = value as? NSNumber {
        let v = n.doubleValue
        return Date(timeIntervalSince1970: v > 1e12 ? v / 1000 : v)
    }
    if let s = value as? String, !s.isEmpty {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: s) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: s)
    }
    return nil
}

/// `GET /voice/history`: one page.
public struct HistoryPage: Equatable, Sendable {
    public var sessions: [HistorySession]
    public var nextCursor: String?
    public var sources: [HistorySource]
    public init(sessions: [HistorySession], nextCursor: String? = nil, sources: [HistorySource] = []) {
        self.sessions = sessions; self.nextCursor = nextCursor; self.sources = sources
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any] else { return nil }
        let raw = (o["sessions"] ?? o["items"]) as? [Any] ?? []
        self.init(sessions: raw.compactMap(HistorySession.init(json:)),
                  nextCursor: nonEmpty(o["next_cursor"]) ?? nonEmpty(o["cursor"]),
                  sources: HistorySource.list(json: o["filters"] ?? o["sources"]))
    }
}

// MARK: - The list: paging, search, filter

/// What the next `GET /voice/history` asks for.
public struct HistoryQuery: Equatable, Sendable {
    public var cursor: String?
    public var q: String?
    public var source: String?
    public var limit: Int
    /// Which list state this was asked for: a reply to an older one is dropped.
    public var generation: Int

    public init(cursor: String? = nil, q: String? = nil, source: String? = nil, limit: Int = HistoryList.pageSize, generation: Int = 0) {
        self.cursor = cursor; self.q = q; self.source = source; self.limit = limit; self.generation = generation
    }

    public var path: String {
        var items: [URLQueryItem] = [URLQueryItem(name: "limit", value: String(limit))]
        if let cursor { items.append(URLQueryItem(name: "cursor", value: cursor)) }
        if let q { items.append(URLQueryItem(name: "q", value: q)) }
        if let source { items.append(URLQueryItem(name: "source", value: source)) }
        var c = URLComponents(); c.path = "/voice/history"; c.queryItems = items
        // `+` would read as a space on the server.
        return (c.string ?? "/voice/history").replacingOccurrences(of: "+", with: "%2B")
    }
}

/// The sidebar list's state, pure: sessions loaded so far, the cursor, the search and the filter.
/// Changing the search or the filter starts over (a new generation); a page that arrives for an
/// older generation is ignored, so a slow reply never overwrites a newer search.
public struct HistoryList: Equatable, Sendable {
    public static let pageSize = 40
    public private(set) var sessions: [HistorySession] = []
    public private(set) var nextCursor: String?
    public private(set) var reachedEnd = false
    public private(set) var loading = false
    public private(set) var error: String?
    public private(set) var query = ""
    public private(set) var source: String?
    public private(set) var sources: [HistorySource] = []
    public private(set) var generation = 0

    public init() {}

    /// The trimmed search, nil when empty.
    public var activeQuery: String? {
        let t = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : String(t.prefix(200))
    }
    public var isFiltered: Bool { activeQuery != nil || source != nil }
    public var isEmpty: Bool { sessions.isEmpty && !loading }
    public var canLoadMore: Bool { !reachedEnd && !loading && error == nil }

    /// Type into the search: the list starts over. Returns the first page's request, or nil when the
    /// effective search didn't change (e.g. trailing space).
    public mutating func setQuery(_ text: String) -> HistoryQuery? {
        let before = activeQuery
        query = text
        guard activeQuery != before else { return nil }
        return reset()
    }

    public mutating func setSource(_ id: String?) -> HistoryQuery? {
        guard id != source else { return nil }
        source = id
        return reset()
    }

    /// Start over (pull to refresh, a new session appeared).
    public mutating func reset() -> HistoryQuery {
        generation += 1
        sessions = []; nextCursor = nil; reachedEnd = false; error = nil; loading = false
        return begin()!
    }

    /// The next page's request, or nil when one is already loading or there's no more.
    public mutating func loadMore() -> HistoryQuery? {
        guard canLoadMore else { return nil }
        return begin()
    }

    /// Retry after an error.
    public mutating func retry() -> HistoryQuery? {
        error = nil
        return begin()
    }

    private mutating func begin() -> HistoryQuery? {
        guard !loading, !reachedEnd else { return nil }
        loading = true
        return HistoryQuery(cursor: nextCursor, q: activeQuery, source: source, limit: Self.pageSize, generation: generation)
    }

    /// A page arrived. Sessions already shown are updated in place (not repeated); a page with no
    /// cursor ends the list. Returns false when it was for an older search and was dropped.
    @discardableResult
    public mutating func apply(_ page: HistoryPage, for request: HistoryQuery) -> Bool {
        guard request.generation == generation else { return false }
        loading = false; error = nil
        var index = Dictionary(uniqueKeysWithValues: sessions.enumerated().map { ($1.id, $0) })
        for s in page.sessions {
            if let i = index[s.id] { sessions[i] = s } else { index[s.id] = sessions.count; sessions.append(s) }
        }
        nextCursor = page.nextCursor
        reachedEnd = page.nextCursor == nil || page.sessions.isEmpty
        if !page.sources.isEmpty { sources = page.sources }
        return true
    }

    @discardableResult
    public mutating func fail(_ message: String, for request: HistoryQuery) -> Bool {
        guard request.generation == generation else { return false }
        loading = false; error = message
        return true
    }

    /// A session changed (a message sent, a new chat): move it to the top, or add it.
    public mutating func upsert(_ session: HistorySession) {
        sessions.removeAll { $0.id == session.id }
        sessions.insert(session, at: 0)
    }

    /// The filter's choices: what the server listed, else the usual ones.
    public var sourceChoices: [HistorySource] { sources.isEmpty ? HistorySource.defaults : sources }

    // MARK: Groups

    public enum Group: String, CaseIterable, Sendable {
        case today = "Today", yesterday = "Yesterday", earlier = "Earlier"
    }

    public func grouped(now: Date, calendar: Calendar = .current) -> [(Group, [HistorySession])] {
        var buckets: [Group: [HistorySession]] = [:]
        for s in sessions { buckets[Self.group(s.sortDate, now: now, calendar: calendar), default: []].append(s) }
        return Group.allCases.compactMap { g in buckets[g].map { (g, $0) } }
    }

    public static func group(_ date: Date?, now: Date, calendar: Calendar = .current) -> Group {
        guard let date else { return .earlier }
        if calendar.isDate(date, inSameDayAs: now) { return .today }
        if let y = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: y) { return .yesterday }
        return .earlier
    }
}

// MARK: - One session

public struct HistoryMessage: Equatable, Sendable, Identifiable {
    public enum Role: String, Equatable, Sendable { case user, assistant, system }
    public enum Delivery: Equatable, Sendable { case sending, sent, failed(String) }

    public var id: String
    public var role: Role
    public var text: String
    public var at: Date?
    /// How the words arrived: "typed", "voice", "discord", ...
    public var source: String?
    /// The tool calls made while answering this message (assistant turns).
    public var toolCalls: [JobActivityItem]
    /// Local only: a message this device is sending.
    public var delivery: Delivery?

    public init(id: String, role: Role, text: String, at: Date? = nil, source: String? = nil,
                toolCalls: [JobActivityItem] = [], delivery: Delivery? = nil) {
        self.id = id; self.role = role; self.text = text; self.at = at; self.source = source
        self.toolCalls = toolCalls; self.delivery = delivery
    }

    public init?(json: Any?, index: Int) {
        guard let o = json as? [String: Any] else { return nil }
        let role: Role
        switch (o["role"] as? String)?.lowercased() ?? "" {
        case "user", "human": role = .user
        case "system", "note", "event": role = .system
        default: role = .assistant
        }
        let calls = ((o["tool_calls"] ?? o["activity"]) as? [Any] ?? []).enumerated()
            .compactMap { JobActivityItem(json: $1, index: $0) }
        let text = nonEmpty(o["text"]) ?? nonEmpty(o["content"]) ?? ""
        guard !text.isEmpty || !calls.isEmpty else { return nil }
        self.init(id: nonEmpty(o["id"]) ?? "m\(index)", role: role, text: text,
                  at: decodeHistoryDate(o["at"]) ?? decodeHistoryDate(o["timestamp"]),
                  source: nonEmpty(o["source"]), toolCalls: calls)
    }

    public var isVoice: Bool { ["voice", "voice_message"].contains(source?.lowercased() ?? "") }
}

/// A tool approval the session is waiting on.
public struct HistoryApproval: Equatable, Sendable, Identifiable {
    public var id: String
    public var description: String
    public var command: String?
    public var choices: [String]
    /// Set when a message sent from the app waits on it: answered with
    /// `POST /voice/history/{session}/sends/{send_id}/answer`. Nil: a task's approval, answered by a
    /// message to the session ("yes" / "no").
    public var sendID: String?
    public init(id: String, description: String, command: String? = nil, choices: [String] = ["once", "deny"], sendID: String? = nil) {
        self.id = id; self.description = description; self.command = command; self.choices = choices; self.sendID = sendID
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["request_id"]) ?? nonEmpty(o["id"]),
              let description = nonEmpty(o["description"]) ?? nonEmpty(o["text"]) else { return nil }
        let choices = (o["choices"] as? [String])?.filter { $0 != "always" } ?? ["once", "deny"]
        self.init(id: id, description: description, command: nonEmpty(o["command"]), choices: choices)
    }
}

/// A question the session asked the user.
public struct HistoryQuestion: Equatable, Sendable, Identifiable {
    public var id: String
    public var question: String
    public var options: [String]
    /// See `HistoryApproval.sendID`.
    public var sendID: String?
    public init(id: String, question: String, options: [String] = [], sendID: String? = nil) {
        self.id = id; self.question = question; self.options = options; self.sendID = sendID
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let q = nonEmpty(o["question"]) ?? nonEmpty(o["text"]) else { return nil }
        let options: [String] = (o["options"] as? [Any] ?? []).compactMap {
            if let s = $0 as? String { return s }
            return nonEmpty(($0 as? [String: Any])?["text"]) ?? nonEmpty(($0 as? [String: Any])?["label"])
        }
        self.init(id: nonEmpty(o["id"]) ?? nonEmpty(o["task_id"]) ?? "q", question: q, options: options)
    }
}

/// `GET /voice/history/{session_id}`.
public struct HistoryDetail: Equatable, Sendable {
    public var session: HistorySession
    public var messages: [HistoryMessage]
    public var helpers: [JobSubagent]
    public var approvals: [HistoryApproval]
    public var questions: [HistoryQuestion]
    /// False: read-only (the server says this one can't take messages).
    public var canMessage: Bool
    /// Older messages exist (`before` pages them; the Mac shows the newest 300).
    public var hasMore = false
    /// Which door a message takes: `task`, `session_job` or `hermes`.
    public var sendVia: String?

    public init(session: HistorySession, messages: [HistoryMessage] = [], helpers: [JobSubagent] = [],
                approvals: [HistoryApproval] = [], questions: [HistoryQuestion] = [], canMessage: Bool = true) {
        self.session = session; self.messages = messages; self.helpers = helpers
        self.approvals = approvals; self.questions = questions; self.canMessage = canMessage
    }

    /// Reads `{"session": {..., "entries", "jobs", "sends"}}` (entries `user` / `assistant` / `note` /
    /// `tools`); a flat `messages` array is read too.
    public init?(json: Any?, id: String) {
        guard let root = json as? [String: Any] else { return nil }
        var o = (root["session"] as? [String: Any]) ?? root
        for key in ["entries", "messages", "jobs", "sends", "subagents", "helpers", "approvals", "questions"] where o[key] == nil {
            if let v = root[key] { o[key] = v }
        }
        if o["id"] == nil && o["session_id"] == nil { o["id"] = id }
        guard let session = HistorySession(json: o) else { return nil }

        var messages: [HistoryMessage] = []
        let raw = (o["entries"] ?? o["messages"]) as? [Any] ?? []
        for (i, item) in raw.enumerated() {
            guard let e = item as? [String: Any] else { continue }
            if (e["kind"] as? String) == "tools" {
                let calls = (e["items"] as? [Any] ?? []).enumerated().compactMap { JobActivityItem(json: $1, index: $0) }
                guard !calls.isEmpty else { continue }
                messages.append(HistoryMessage(id: nonEmpty(e["id"]) ?? "t\(i)", role: .assistant, text: "",
                                               at: calls.first?.startedAt ?? calls.first?.at, toolCalls: calls))
            } else {
                var m = e
                if m["role"] == nil { m["role"] = (e["kind"] as? String) == "note" ? "system" : e["kind"] }
                if let msg = HistoryMessage(json: m, index: i) { messages.append(msg) }
            }
        }
        // Tool calls belong under the answer they led to: fold a tools row into the next assistant message.
        var folded: [HistoryMessage] = []
        var pending: [JobActivityItem] = []
        var pendingAt: Date?
        for m in messages {
            if m.role == .assistant && m.text.isEmpty { pending += m.toolCalls; pendingAt = pendingAt ?? m.at; continue }
            if m.role == .system { folded.append(m); continue }   // a note doesn't split tools from their answer
            var m = m
            if m.role == .assistant && !pending.isEmpty { m.toolCalls = pending + m.toolCalls; pending = []; pendingAt = nil }
            else if !pending.isEmpty {
                folded.append(HistoryMessage(id: "tools-before-\(m.id)", role: .assistant, text: "", at: pendingAt, toolCalls: pending))
                pending = []; pendingAt = nil
            }
            folded.append(m)
        }
        if !pending.isEmpty { folded.append(HistoryMessage(id: "tools-end", role: .assistant, text: "", at: pendingAt, toolCalls: pending)) }

        // Its Speakeasy jobs: helpers, an approval, a question.
        var helpers = JobSubagent.list(json: o["subagents"] ?? o["helpers"])
        var approvals = (o["approvals"] as? [Any] ?? []).compactMap(HistoryApproval.init(json:))
            + [o["approval"]].compactMap(HistoryApproval.init(json:))
        var questions = (o["questions"] as? [Any] ?? []).compactMap(HistoryQuestion.init(json:))
        for job in (o["jobs"] as? [Any] ?? []).compactMap(WorkInfo.init(json:)) {
            helpers += job.subagents
            guard !job.setAside, let run = job.runID else { continue }
            if job.status == "waiting_for_approval" {
                approvals.append(HistoryApproval(id: "job-\(run)", description: job.detail ?? job.shortStatus ?? "It's waiting for your OK."))
            } else if let d = job.decision, !job.isTerminal || job.status == "completed" {
                questions.append(HistoryQuestion(id: "job-\(run)", question: d.question, options: d.options.map(\.text)))
            }
        }

        // Messages sent from the app through Hermes, still on their way (or just failed).
        let userTexts = Set(folded.filter { $0.role == .user }.map { $0.text.trimmingCharacters(in: .whitespacesAndNewlines) })
        for item in o["sends"] as? [Any] ?? [] {
            guard let send = item as? [String: Any], let sendID = nonEmpty(send["send_id"]),
                  let text = nonEmpty(send["text"]) else { continue }
            let status = (send["status"] as? String) ?? "sending"
            if let p = send["prompt"] as? [String: Any], status == "needs_you", let ptext = nonEmpty(p["text"]) {
                let choices = (p["choices"] as? [Any] ?? []).compactMap { nonEmpty($0) }.filter { $0 != "always" }
                if (p["kind"] as? String) == "approval" {
                    approvals.append(HistoryApproval(id: "send-\(sendID)", description: ptext,
                                                     choices: choices.isEmpty ? ["once", "deny"] : choices, sendID: sendID))
                } else {
                    questions.append(HistoryQuestion(id: "send-\(sendID)", question: ptext, options: choices, sendID: sendID))
                }
            }
            let echoed = userTexts.contains(text.trimmingCharacters(in: .whitespacesAndNewlines))
            let at = decodeHistoryDate(send["started"])
            switch status {
            case "failed":
                folded.append(HistoryMessage(id: "send-\(sendID)", role: .user, text: text, at: at, source: "typed",
                                             delivery: .failed(nonEmpty(send["error"]) ?? "That didn't go through.")))
            case "completed":
                if !echoed {
                    folded.append(HistoryMessage(id: "send-\(sendID)", role: .user, text: text, at: at, source: "typed"))
                    if let reply = nonEmpty(send["reply"]) {
                        folded.append(HistoryMessage(id: "send-\(sendID)-reply", role: .assistant, text: reply,
                                                     at: decodeHistoryDate(send["ended"])))
                    }
                }
            default:
                if !echoed { folded.append(HistoryMessage(id: "send-\(sendID)", role: .user, text: text, at: at, source: "typed", delivery: .sent)) }
            }
        }
        // A helper reported in several jobs is shown once.
        var seen = Set<String>()
        helpers = helpers.filter { seen.insert($0.id).inserted }
        self.init(session: session, messages: folded, helpers: helpers, approvals: approvals, questions: questions,
                  canMessage: o["can_message"] as? Bool ?? true)
        self.hasMore = o["has_more"] as? Bool ?? false
        self.sendVia = nonEmpty(o["send_via"])
    }

    /// Every tool call in the session, in order.
    public var toolCallCount: Int { messages.reduce(0) { $0 + $1.toolCalls.count } }
}

// MARK: - Sending

/// How a message was made.
public enum HistoryMessageSource: String, Equatable, Sendable {
    case typed
    /// Text transcribed on the device from a recorded voice message (no audio is sent).
    case voice = "voice_message"
}

/// `POST /voice/history/{session_id}/message`: to this session, by id. Never the router.
public struct HistoryMessageRequest: Equatable, Sendable {
    public var sessionID: String
    public var text: String
    public var source: HistoryMessageSource
    /// Idempotency key: a retry with the same id is never sent twice.
    public var clientID: String

    public init(sessionID: String, text: String, source: HistoryMessageSource = .typed, clientID: String = UUID().uuidString) {
        self.sessionID = sessionID; self.text = text; self.source = source; self.clientID = clientID
    }

    public var path: String {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
        return "/voice/history/\(sessionID.addingPercentEncoding(withAllowedCharacters: allowed) ?? sessionID)/message"
    }

    public var body: [String: Any] {
        ["text": TaskMessageRequest.clean(text) ?? "", "source": source.rawValue, "client_id": clientID]
    }
}

public struct HistoryMessageReply: Equatable, Sendable {
    public var sessionID: String?
    public var runID: String?
    public var deliveredAs: TaskMessageDelivery
    /// `task`, `session_job` or `hermes`.
    public var via: String?
    /// Set for `via: hermes`: the send tracked in the session's `sends`.
    public var sendID: String?
    public init(sessionID: String?, runID: String?, deliveredAs: TaskMessageDelivery, via: String? = nil, sendID: String? = nil) {
        self.sessionID = sessionID; self.runID = runID; self.deliveredAs = deliveredAs; self.via = via; self.sendID = sendID
    }
    public init?(json: [String: Any]) {
        guard json["ok"] as? Bool == true else { return nil }
        self.init(sessionID: nonEmpty(json["session_id"]), runID: nonEmpty(json["run_id"]),
                  deliveredAs: (json["delivered_as"] as? String).flatMap(TaskMessageDelivery.init(rawValue:)) ?? .unknown,
                  via: nonEmpty(json["via"]), sendID: nonEmpty(json["send_id"]))
    }
}

/// `POST /voice/history/{session_id}/sends/{send_id}/answer`: a choice for the approval or question
/// a message sent through Hermes waits on.
public struct HistorySendAnswer: Equatable, Sendable {
    public var sessionID: String
    public var sendID: String
    public var choice: String
    public init(sessionID: String, sendID: String, choice: String) {
        self.sessionID = sessionID; self.sendID = sendID; self.choice = choice
    }
    public var path: String {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
        func enc(_ s: String) -> String { s.addingPercentEncoding(withAllowedCharacters: allowed) ?? s }
        return "/voice/history/\(enc(sessionID))/sends/\(enc(sendID))/answer"
    }
    public var body: [String: Any] { ["choice": choice] }
}

/// Who the composer is talking to. A session page sends to that session by id; only New chat
/// starts something new (`POST /voice/work`). Neither ever goes through the voice's router.
public enum HistoryTarget: Equatable, Sendable {
    case session(String)
    case newChat

    public enum Outgoing: Equatable, Sendable {
        case message(HistoryMessageRequest)
        case newChat(text: String, source: HistoryMessageSource, clientID: String)
    }

    /// What Send does with these words; nil when there's nothing to send.
    public func outgoing(_ text: String, source: HistoryMessageSource, clientID: String = UUID().uuidString) -> Outgoing? {
        guard let clean = TaskMessageRequest.clean(text) else { return nil }
        switch self {
        case .session(let id): return .message(HistoryMessageRequest(sessionID: id, text: clean, source: source, clientID: clientID))
        case .newChat: return .newChat(text: clean, source: source, clientID: clientID)
        }
    }

    public var sessionID: String? { if case .session(let id) = self { return id }; return nil }
}

/// Messages this device sent to a session, shown under its thread until the server's copy arrives
/// (matched on text), each with how it went. A failed one retries with the same client id.
public struct HistoryOutbox: Equatable, Sendable {
    public struct Item: Equatable, Sendable, Identifiable {
        public var id: String { request.clientID }
        public var request: HistoryMessageRequest
        public var at: Date
        public var delivery: HistoryMessage.Delivery
    }
    public private(set) var items: [Item] = []
    public init() {}

    public mutating func add(_ request: HistoryMessageRequest, at: Date) {
        items.removeAll { $0.id == request.clientID }
        items.append(Item(request: request, at: at, delivery: .sending))
    }
    public mutating func mark(_ clientID: String, _ delivery: HistoryMessage.Delivery) {
        if let i = items.firstIndex(where: { $0.id == clientID }) { items[i].delivery = delivery }
    }
    public func item(_ clientID: String) -> Item? { items.first { $0.id == clientID } }

    /// The thread to draw: the server's messages, then this device's not yet echoed back.
    /// Once the server has a user message with the same text, sent at or after ours, ours is dropped.
    public func merged(into messages: [HistoryMessage], session: String) -> [HistoryMessage] {
        var shown = messages
        var used = Set<String>()
        for item in items where item.request.sessionID == session {
            let text = TaskMessageRequest.clean(item.request.text) ?? ""
            if item.delivery == .sent, let echo = Self.echo(of: item, in: messages, skipping: used) { used.insert(echo); continue }
            shown.append(HistoryMessage(id: "local-\(item.id)", role: .user, text: text, at: item.at,
                                        source: item.request.source.rawValue, delivery: item.delivery))
        }
        return shown
    }

    /// Forget sent messages the server now shows.
    public mutating func prune(_ messages: [HistoryMessage], session: String) {
        var used = Set<String>()
        items.removeAll { item in
            guard item.request.sessionID == session, item.delivery == .sent,
                  let echo = Self.echo(of: item, in: messages, skipping: used) else { return false }
            used.insert(echo); return true
        }
    }

    /// The server's copy of a sent message: a user message with the same text, not older than ours
    /// (a minute's clock skew allowed), so an earlier identical "yes" never hides a new one.
    static func echo(of item: Item, in messages: [HistoryMessage], skipping used: Set<String>) -> String? {
        let text = TaskMessageRequest.clean(item.request.text) ?? ""
        return messages.first { m in
            m.role == .user && !used.contains(m.id) && m.text.trimmingCharacters(in: .whitespacesAndNewlines) == text
                && (m.at.map { $0 >= item.at.addingTimeInterval(-60) } ?? true)
        }?.id
    }
}

// MARK: - Voice messages (not on a call)

/// The composer's voice-message flow: tap the mic, speak (on-device transcription), stop, read and
/// edit what it heard, send. Nothing leaves the Mac until Send, and what's sent is the text.
/// A live call takes the mic: the mic button hides and anything recording is dropped.
public struct VoiceMessageFlow: Equatable, Sendable {
    public enum Phase: Equatable, Sendable {
        case idle
        /// Waiting for the mic / speech-recognition prompt.
        case asking
        case recording(heard: String, since: Date)
        /// Stopped; the last words are being finalized.
        case finishing(heard: String)
        /// The transcript, editable, ready to send.
        case review(String)
        case sending(String)
        /// Couldn't record or transcribe (permission off, nothing heard).
        case problem(String)
    }

    public private(set) var phase: Phase = .idle
    public private(set) var callLive = false
    public static let maxSeconds: TimeInterval = 120

    public init() {}

    /// The mic button shows only when no call is live.
    public var micVisible: Bool { !callLive }
    public var isRecording: Bool { if case .recording = phase { return true }; return false }
    public var isBusy: Bool {
        switch phase { case .idle, .problem: return false; default: return true }
    }
    /// The text a review would send.
    public var reviewText: String? { if case .review(let t) = phase { return t }; return nil }

    /// Tap the mic. Returns true when the recorder should start (after permission).
    public mutating func tapMic() -> Bool {
        guard !callLive else { return false }
        switch phase {
        case .idle, .problem: phase = .asking; return true
        default: return false
        }
    }

    public mutating func permission(granted: Bool, now: Date) {
        guard case .asking = phase else { return }
        phase = granted ? .recording(heard: "", since: now)
            : .problem("Speakeasy can't hear you. Allow the microphone and speech recognition in System Settings › Privacy.")
    }

    public mutating func heard(_ text: String) {
        switch phase {
        case .recording(_, let since): phase = .recording(heard: text, since: since)
        case .finishing: phase = .finishing(heard: text)
        default: break
        }
    }

    /// Tap Stop (or the time limit).
    public mutating func stop() {
        guard case .recording(let heard, _) = phase else { return }
        phase = .finishing(heard: heard)
    }

    /// The recognizer is done (after a stop, or on its own).
    public mutating func finished(_ text: String?) {
        let current: String
        switch phase {
        case .recording(let h, _), .finishing(let h): current = h
        default: return
        }
        let final = (text?.isEmpty == false ? text! : current).trimmingCharacters(in: .whitespacesAndNewlines)
        phase = final.isEmpty ? .problem("Didn't catch anything. Try again, a little closer to the mic.") : .review(final)
    }

    public mutating func failed(_ message: String) {
        switch phase {
        case .asking, .recording, .finishing: phase = .problem(message)
        default: break
        }
    }

    /// Edit the transcript before sending.
    public mutating func edit(_ text: String) {
        if case .review = phase { phase = .review(text) }
    }

    /// Send the reviewed text. Returns the cleaned text, or nil when there's nothing to send.
    public mutating func send() -> String? {
        guard case .review(let t) = phase, let clean = TaskMessageRequest.clean(t) else { return nil }
        phase = .sending(clean)
        return clean
    }

    public mutating func sent() { if case .sending = phase { phase = .idle } }

    /// The send failed: back to review with the same words.
    public mutating func sendFailed() { if case .sending(let t) = phase { phase = .review(t) } }

    /// Cancel / Esc: drop the recording or the transcript.
    public mutating func cancel() { phase = .idle }

    /// A call went live (or ended). Live: the mic belongs to the call, so a recording is dropped;
    /// a transcript under review is kept (it's just text now).
    public mutating func setCallLive(_ live: Bool) {
        callLive = live
        guard live else { return }
        switch phase {
        case .asking, .recording, .finishing: phase = .idle
        default: break
        }
    }

    /// Seconds recorded so far, and whether the time limit is up.
    public func elapsed(now: Date) -> TimeInterval {
        if case .recording(_, let since) = phase { return max(0, now.timeIntervalSince(since)) }
        return 0
    }
    public func overLimit(now: Date) -> Bool { elapsed(now: now) >= Self.maxSeconds }
}
