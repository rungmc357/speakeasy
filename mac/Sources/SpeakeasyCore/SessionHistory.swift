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
    /// Pinned on any device (the plugin's mark): listed first, in the Pinned section.
    public var pinned: Bool
    /// Something new since it was last read on any device.
    public var unread: Bool
    /// Archived in Hermes (listed only under the Archived filter).
    public var archived: Bool

    public init(id: String, title: String, source: String, sourceLabel: String, lastActivity: Date?,
                preview: String = "", state: State = .idle, runIDs: [String] = [], match: String? = nil,
                pinned: Bool = false, unread: Bool = false, archived: Bool = false) {
        self.id = id; self.title = title; self.source = source; self.sourceLabel = sourceLabel
        self.lastActivity = lastActivity; self.preview = preview; self.state = state; self.runIDs = runIDs
        self.match = match; self.pinned = pinned; self.unread = unread; self.archived = archived
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
                  match: nonEmpty(o["match"]), pinned: o["pinned"] as? Bool ?? false,
                  unread: o["unread"] as? Bool ?? false, archived: o["archived"] as? Bool ?? false)
    }

    /// The deep link that opens this conversation in any Speakeasy app.
    public var link: URL? { HistorySession.link(id) }
    public static func link(_ id: String) -> URL? {
        var c = URLComponents()
        c.scheme = "speakeasy"; c.host = "session"; c.path = "/" + id
        return c.url
    }
    /// The session id in a `speakeasy://session/<id>` link.
    public static func sessionID(fromLink url: URL) -> String? {
        guard url.scheme == "speakeasy", url.host == "session" else { return nil }
        let id = url.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return id.isEmpty ? nil : id
    }

    public var isSpeakeasy: Bool { source == "speakeasy" }
    public var isActive: Bool { state != .idle }
}

public struct HistoryFilter: Equatable, Sendable, Identifiable, Hashable {
    public var id: String
    public var label: String
    public init(id: String, label: String) { self.id = id; self.label = label }
}

/// One entry of `GET /voice/history`'s `sources`: a filter id the person has sessions under, and how many.
public struct HistorySourceCount: Equatable, Sendable, Hashable {
    public var source: String
    public var count: Int
    public init(source: String, count: Int) { self.source = source; self.count = count }
}

/// Which source filters to offer: only ones the person has sessions from (Slack never shows for
/// someone who doesn't use it), and none at all when everything comes from one place.
public enum HistorySources {
    /// The filter id (`?source=`) a listed session falls under (the plugin's `FILTERS`).
    public static func filterID(for source: String) -> String? {
        switch source.lowercased() {
        case "speakeasy": return "speakeasy"
        case "discord", "telegram", "slack": return source.lowercased()
        case "cli", "tui", "terminal": return "terminal"
        case "gui", "local", "desktop": return "desktop"
        case "cron", "scheduled": return "scheduled"
        case "api_server", "api": return "api"
        default: return nil
        }
    }

    /// The filters to show, in the server's order. `sources` is the server's facet (counted over
    /// every visible session); nil from older plugins, which falls back to the sources of the rows
    /// loaded so far (`seen`, as filter ids). The chosen filter always stays, so it can be cleared.
    public static func shown(choices: [HistoryFilter], sources: [HistorySourceCount]?, seen: Set<String>,
                             selected: String?) -> [HistoryFilter] {
        let have = sources.map { Set($0.filter { $0.count > 0 }.map(\.source)) } ?? seen
        var out = choices.filter { have.contains($0.id) || $0.id == selected }
        // A source the server counts but didn't list as a filter (older lists): still offered.
        for id in (sources?.filter { $0.count > 0 }.map(\.source) ?? []) where !out.contains(where: { $0.id == id }) {
            if let c = choices.first(where: { $0.id == id }) { out.append(c) }
        }
        return out
    }

    /// The chip row (or the Mac's source menu) shows only with two or more sources to pick from,
    /// or while a filter is on (so it can be turned off).
    public static func showsPicker(_ shown: [HistoryFilter], selected: String?) -> Bool {
        shown.count > 1 || selected != nil
    }

    /// The filter ids of loaded rows (the fallback for plugins without `sources`).
    public static func seen(in sessions: [HistorySession]) -> Set<String> {
        Set(sessions.compactMap { filterID(for: $0.source) })
    }
}

/// `GET /voice/history`'s page.
public struct HistoryPage: Equatable, Sendable {
    public var sessions: [HistorySession]
    public var nextCursor: String?
    public var filters: [HistoryFilter]
    /// The `sources` facet; nil from plugins that don't send it.
    public var sources: [HistorySourceCount]?

    public init(sessions: [HistorySession], nextCursor: String? = nil, filters: [HistoryFilter] = [],
                sources: [HistorySourceCount]? = nil) {
        self.sessions = sessions; self.nextCursor = nextCursor; self.filters = filters; self.sources = sources
    }

    public init(json: Any?) {
        let o = json as? [String: Any] ?? [:]
        sessions = (o["sessions"] as? [Any] ?? []).compactMap(HistorySession.init(json:))
        nextCursor = nonEmpty(o["next_cursor"])
        filters = (o["filters"] as? [Any] ?? []).compactMap { raw in
            guard let f = raw as? [String: Any], let id = nonEmpty(f["id"]), let label = nonEmpty(f["label"]) else { return nil }
            return HistoryFilter(id: id, label: label)
        }
        sources = (o["sources"] as? [Any]).map { list in
            list.compactMap { raw in
                guard let f = raw as? [String: Any], let id = nonEmpty(f["source"]) else { return nil }
                return HistorySourceCount(source: id, count: (f["count"] as? NSNumber)?.intValue ?? 0)
            }
        }
    }

    /// The query string for a page: search, source filter, cursor (all optional).
    public static func path(query: String = "", source: String? = nil, cursor: String? = nil, limit: Int? = nil,
                            archived: Bool = false) -> String {
        var items: [URLQueryItem] = []
        if archived { items.append(URLQueryItem(name: "archived", value: "1")) }
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

    /// Pinned rows (newest pin first, as the server lists them), then the rest in their groups.
    public static func sectioned(_ sessions: [HistorySession], now: Date = Date(),
                                 calendar: Calendar = .current) -> (pinned: [HistorySession], groups: [(HistoryGroup, [HistorySession])]) {
        (sessions.filter(\.pinned), grouped(sessions.filter { !$0.pinned }, now: now, calendar: calendar))
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

/// An approval one of the session's Speakeasy tasks waits on (`approvals`).
public struct HistoryApproval: Equatable, Sendable, Identifiable {
    public var runID: String
    public var description: String
    public var choices: [String]
    public var id: String { runID }
    public init(runID: String, description: String, choices: [String]) {
        self.runID = runID; self.description = description; self.choices = choices
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let run = nonEmpty(o["run_id"]) else { return nil }
        let choices = (o["choices"] as? [Any] ?? []).compactMap { nonEmpty($0) }.filter { ["once", "session", "deny"].contains($0) }
        self.init(runID: run, description: nonEmpty(o["description"]) ?? "Hermes needs your OK to continue.",
                  choices: choices.isEmpty ? ["once", "deny"] : choices)
    }
    /// Button title for a choice.
    public static func label(_ choice: String) -> String {
        switch choice { case "once": return "Approve"; case "session": return "Approve for this task"; default: return "Deny" }
    }
}

/// `GET /voice/history/{session_id}`.
public struct HistoryConversation: Equatable, Sendable {
    /// `task`: a Speakeasy task's session. `sessionJob`: another Hermes API session (a job runs in it).
    /// `linked`: a chat (Discord, Telegram, terminal, Desktop): a Speakeasy task linked to it, never posted there.
    public enum SendVia: String, Equatable, Sendable { case task, sessionJob = "session_job", linked }
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
    public var approvals: [HistoryApproval]

    public init(id: String, title: String, source: String, sourceLabel: String, state: HistorySession.State = .idle,
                sendVia: SendVia = .linked, entries: [HistoryEntry] = [], hasMore: Bool = false, before: Int? = nil,
                jobs: [WorkInfo] = [], approvals: [HistoryApproval] = []) {
        self.id = id; self.title = title; self.source = source; self.sourceLabel = sourceLabel; self.state = state
        self.sendVia = sendVia; self.entries = entries; self.hasMore = hasMore; self.before = before
        self.jobs = jobs; self.approvals = approvals
    }

    public init?(json: Any?) {
        guard let root = json as? [String: Any], let o = root["session"] as? [String: Any],
              let id = nonEmpty(o["id"]) else { return nil }
        self.init(id: id, title: nonEmpty(o["title"]) ?? "Untitled conversation", source: nonEmpty(o["source"]) ?? "hermes",
                  sourceLabel: nonEmpty(o["source_label"]) ?? "Hermes",
                  state: (o["state"] as? String).flatMap(HistorySession.State.init(rawValue:)) ?? .idle,
                  sendVia: (o["send_via"] as? String).flatMap(SendVia.init(rawValue:)) ?? .linked,
                  entries: (o["entries"] as? [Any] ?? []).compactMap(HistoryEntry.init(json:)),
                  hasMore: o["has_more"] as? Bool ?? false, before: (o["before"] as? NSNumber)?.intValue,
                  jobs: (o["jobs"] as? [Any] ?? []).compactMap(WorkInfo.init(json:)),
                  approvals: (o["approvals"] as? [Any] ?? []).compactMap(HistoryApproval.init(json:)))
    }

    /// The ONE job whose question (a decision card) waits on the user, as the plugin decides it
    /// (`question_open`; answered anywhere, superseded or a repeat of an answered one → none). Plugins
    /// before it: the newest unanswered question only, never two cards for the same question.
    public var waitingQuestions: [WorkInfo] {
        let open = jobs.filter { $0.status == "completed" && $0.decision != nil && !$0.setAside && !$0.answered }
        guard let newest = open.first else { return [] }
        // Jobs are newest first: an older question with a newer job after it was moved past.
        return jobs.first?.runID == newest.runID ? [newest] : []
    }
    public var isWorking: Bool { state == .running }

    public static func path(_ sessionID: String, before: Int? = nil) -> String {
        let id = sessionID.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-_."))) ?? sessionID
        return "/voice/history/\(id)" + (before.map { "?before=\($0)" } ?? "")
    }
}

/// `POST /voice/history/{session_id}/message`.
public struct HistoryMessageRequest: Equatable, Sendable {
    public enum Source: String, Sendable { case typed, voiceMessage = "voice_message" }
    public var sessionID: String
    public var text: String
    public var source: Source
    public var clientID: String
    /// Ids staged with `ServerClient.stageAttachment` (servers with `attachments`); with any, `text`
    /// may be empty (the server says "Take a look at what I attached.").
    public var attachments: [String]

    public init?(sessionID: String, text: String, source: Source = .typed, clientID: String = UUID().uuidString,
                 attachments: [String] = []) {
        let clean = TaskMessageRequest.clean(text)
        guard clean != nil || !attachments.isEmpty else { return nil }
        self.sessionID = sessionID; self.text = clean ?? ""; self.source = source; self.clientID = clientID
        self.attachments = attachments
    }

    public var path: String { HistoryConversation.path(sessionID) + "/message" }
    public var body: [String: Any] {
        var out: [String: Any] = ["text": text, "source": source.rawValue, "client_id": clientID]
        if !attachments.isEmpty { out["attachments"] = attachments }
        return out
    }
}

public struct HistoryMessageReply: Equatable, Sendable {
    public var via: HistoryConversation.SendVia
    /// The task to follow like a job.
    public var runID: String?

    public init(via: HistoryConversation.SendVia, runID: String?) {
        self.via = via; self.runID = runID
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], o["ok"] as? Bool == true,
              let via = (o["via"] as? String).flatMap(HistoryConversation.SendVia.init(rawValue:)) else { return nil }
        self.init(via: via, runID: nonEmpty(o["run_id"]))
    }
}
