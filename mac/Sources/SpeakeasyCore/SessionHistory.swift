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
    /// Automated work nobody typed into (a Kanban board worker, a scheduled job): never unread or needs-you.
    public var automated: Bool
    /// Servers with `agents`: the Hermes profile this chat talks to, when it isn't Speakeasy's own.
    public var agent: AgentInfo?

    public init(id: String, title: String, source: String, sourceLabel: String, lastActivity: Date?,
                preview: String = "", state: State = .idle, runIDs: [String] = [], match: String? = nil,
                pinned: Bool = false, unread: Bool = false, archived: Bool = false, automated: Bool = false) {
        self.id = id; self.title = title; self.source = source; self.sourceLabel = sourceLabel
        self.lastActivity = lastActivity; self.preview = preview; self.state = state; self.runIDs = runIDs
        self.match = match; self.pinned = pinned; self.unread = unread && !automated; self.archived = archived
        self.automated = automated
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
                  unread: o["unread"] as? Bool ?? false, archived: o["archived"] as? Bool ?? false,
                  automated: o["automated"] as? Bool ?? false)
        if automated && state == .needsYou { state = .idle }
        agent = AgentInfo(json: o["agent"])
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
        case "kanban": return "kanban"
        case "subagent", "helpers": return "helpers"
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

/// `GET /voice/history`'s `automated`: how many automated sessions (Kanban workers, scheduled jobs)
/// there are, whether this page hid both, and (`kinds`) what the filter menu offers: each kind with
/// its count and whether it's shown. Plugins before 0.3.37 don't send it.
public struct HistoryAutomated: Equatable, Sendable {
    public struct Kind: Equatable, Sendable, Identifiable {
        public var id: String
        public var label: String
        public var count: Int
        public var shown: Bool
        public init(id: String, label: String, count: Int, shown: Bool) {
            self.id = id; self.label = label; self.count = count; self.shown = shown
        }
    }
    public var count: Int
    public var hidden: Bool
    public var kinds: [Kind]
    public init(count: Int, hidden: Bool, kinds: [Kind] = []) { self.count = count; self.hidden = hidden; self.kinds = kinds }
    public init?(json: Any?) {
        guard let o = json as? [String: Any] else { return nil }
        let kinds: [Kind] = (o["kinds"] as? [Any] ?? []).compactMap { raw in
            guard let k = raw as? [String: Any], let id = nonEmpty(k["id"]), HistoryShown.kinds.contains(id) else { return nil }
            return Kind(id: id, label: nonEmpty(k["label"]) ?? HistoryShown.label(id), count: (k["count"] as? NSNumber)?.intValue ?? 0,
                        shown: k["shown"] as? Bool ?? true)
        }
        self.init(count: (o["count"] as? NSNumber)?.intValue ?? 0, hidden: o["hidden"] as? Bool ?? false, kinds: kinds)
    }
}

/// What the conversation list shows of automated work: board workers and scheduled jobs (shown
/// unless turned off) and background helpers (off unless turned on). The filter menu's toggles.
public struct HistoryShown: Equatable, Sendable, Hashable {
    public static let kinds = ["kanban", "scheduled", "helpers"]
    public var kanban = true
    public var scheduled = true
    public var helpers = false
    public init(kanban: Bool = true, scheduled: Bool = true, helpers: Bool = false) {
        self.kanban = kanban; self.scheduled = scheduled; self.helpers = helpers
    }
    public static let standard = HistoryShown()
    public var isStandard: Bool { self == .standard }

    public subscript(kind: String) -> Bool {
        get { kind == "kanban" ? kanban : kind == "scheduled" ? scheduled : kind == "helpers" ? helpers : true }
        set {
            switch kind {
            case "kanban": kanban = newValue
            case "scheduled": scheduled = newValue
            case "helpers": helpers = newValue
            default: break
            }
        }
    }
    /// The menu's words for a kind (the server sends its own; these cover a missing label).
    public static func label(_ kind: String) -> String {
        ["kanban": "Board workers", "scheduled": "Scheduled jobs", "helpers": "Background helpers"][kind] ?? kind.capitalized
    }
    /// `?hide=` (the shown-by-default kinds turned off) and whether `?helpers=1`.
    public var hidden: [String] { [kanban ? nil : "kanban", scheduled ? nil : "scheduled"].compactMap { $0 } }
    /// A source filter this hides (its chip goes away, like on the server).
    public func hides(filter id: String) -> Bool { Self.kinds.contains(id) && !self[id] }
    /// Whether a listed session passes these choices (for local/demo lists; the server does the real one).
    public func lists(_ session: HistorySession) -> Bool {
        guard let kind = HistorySources.filterID(for: session.source), Self.kinds.contains(kind) else { return true }
        return self[kind]
    }
    /// The facet a server would send for these sessions (demo backends and tests).
    public func facet(for sessions: [HistorySession]) -> HistoryAutomated {
        let counts = Dictionary(grouping: sessions.compactMap { HistorySources.filterID(for: $0.source) }, by: { $0 }).mapValues(\.count)
        return HistoryAutomated(count: (counts["kanban"] ?? 0) + (counts["scheduled"] ?? 0), hidden: !kanban && !scheduled,
                                kinds: Self.kinds.map { .init(id: $0, label: Self.label($0), count: counts[$0] ?? 0, shown: self[$0]) })
    }
}

/// The filter menu for automated work at the end of the conversation list's filter row, remembered
/// on this device. Offered only when the server can filter them (`automated.kinds` in its reply) and
/// one of them has something in it, or while a choice differs from the standard one (to turn it back).
public enum HistoryAutomation {
    /// SPK-70's single switch ("hide automated"): carried over once to the menu's two kinds.
    public static let legacyKey = "history.hideAutomated"
    static func key(_ kind: String) -> String { "history.show.\(kind)" }

    public static var shown: HistoryShown {
        get { shown(in: .standard) }
        set { set(newValue, in: .standard) }
    }
    public static func shown(in defaults: UserDefaults) -> HistoryShown {
        var out = HistoryShown.standard
        let legacy = defaults.bool(forKey: legacyKey)
        for kind in HistoryShown.kinds {
            if let on = defaults.object(forKey: key(kind)) as? Bool { out[kind] = on }
            else if legacy, kind != "helpers" { out[kind] = false }
        }
        return out
    }
    public static func set(_ shown: HistoryShown, in defaults: UserDefaults) {
        for kind in HistoryShown.kinds { defaults.set(shown[kind], forKey: key(kind)) }
        defaults.removeObject(forKey: legacyKey)
    }

    /// The kinds the menu lists: ones with sessions, plus any not at its standard setting.
    public static func menu(_ facet: HistoryAutomated?, shown: HistoryShown) -> [HistoryAutomated.Kind] {
        guard let facet else { return [] }
        return facet.kinds.filter { $0.count > 0 || shown[$0.id] != HistoryShown.standard[$0.id] }
    }
    public static func offers(_ facet: HistoryAutomated?, shown: HistoryShown) -> Bool { !menu(facet, shown: shown).isEmpty }
    /// The icon: a filter mark, filled while something differs from the standard choice.
    public static func symbol(_ shown: HistoryShown) -> String {
        shown.isStandard ? "line.3.horizontal.decrease.circle" : "line.3.horizontal.decrease.circle.fill"
    }
    public static let title = "Show in the list"
    /// The icon's spoken label and tooltip.
    public static func accessibilityLabel(_ shown: HistoryShown) -> String {
        shown.isStandard ? "Filter automated work" : "Filter automated work, changed"
    }
    public static let help = "Choose which board workers, scheduled jobs and background helpers show"
    /// A kind's mark in the menu.
    public static func symbol(kind: String) -> String {
        ["kanban": "square.grid.3x2", "scheduled": "clock", "helpers": "person.2"][kind] ?? "circle"
    }
}

/// `GET /voice/history`'s page.
public struct HistoryPage: Equatable, Sendable {
    public var sessions: [HistorySession]
    public var nextCursor: String?
    public var filters: [HistoryFilter]
    /// The `sources` facet; nil from plugins that don't send it.
    public var sources: [HistorySourceCount]?
    /// The automated-work facet; nil from plugins that don't send it.
    public var automated: HistoryAutomated?

    public init(sessions: [HistorySession], nextCursor: String? = nil, filters: [HistoryFilter] = [],
                sources: [HistorySourceCount]? = nil, automated: HistoryAutomated? = nil) {
        self.sessions = sessions; self.nextCursor = nextCursor; self.filters = filters; self.sources = sources
        self.automated = automated
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
        automated = HistoryAutomated(json: o["automated"])
    }

    /// The query string for a page: search, source filter, cursor (all optional).
    public static func path(query: String = "", source: String? = nil, cursor: String? = nil, limit: Int? = nil,
                            archived: Bool = false, shown: HistoryShown = .standard) -> String {
        var items: [URLQueryItem] = []
        if archived { items.append(URLQueryItem(name: "archived", value: "1")) }
        if !shown.hidden.isEmpty { items.append(URLQueryItem(name: "hide", value: shown.hidden.joined(separator: ","))) }
        if shown.helpers { items.append(URLQueryItem(name: "helpers", value: "1")) }
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
    /// `user`: the words went in as a voice message (shown as waveform + transcript).
    public var voice: Bool
    /// `user` voice notes: the sending device's clip id (play it from `VoiceClipStore`) and its length.
    public var voiceClip: String?
    public var voiceDuration: TimeInterval?
    /// `user`: the request came from a call; what was said on it, oldest first (one card, transcript on tap).
    public var call: [HistoryCallLine]?
    /// `note`: the Kanban card a board worker was started with (a compact header, instructions folded away).
    public var card: HistoryWorkerTask?
    /// `assistant`: the worker's own update to the person (a tracker comment or its finish summary).
    public var automaticEvent: HistoryAutomaticEvent? = nil
    public var update = false
    /// `user`: the sending device's `client_id` (SPK-80): how an app knows this row is its message.
    public var clientID: String?
    /// `assistant`: the pictures and files this reply handed over (its `MEDIA:` lines), as Files rows:
    /// shown in place, fetched by id from `/voice/files` (SPK-214).
    public var narration: Bool = false
    public var workDetail: String? = nil
    public var files: [AgentFile] = []

    public var attribution: ReplyAttribution?

    public init(id: String, kind: Kind, at: Date? = nil, text: String = "", tools: [JobActivityItem] = [], voice: Bool = false,
                call: [HistoryCallLine]? = nil, card: HistoryWorkerTask? = nil, update: Bool = false, attribution: ReplyAttribution? = nil) {
        self.id = id; self.kind = kind; self.at = at; self.text = text; self.tools = tools; self.voice = voice; self.call = call
        self.card = card; self.update = update
        self.attribution = kind == .assistant ? attribution : nil
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]),
              let kind = (o["kind"] as? String).flatMap(Kind.init(rawValue:)) else { return nil }
        let tools = kind == .tools
            ? (o["items"] as? [Any] ?? []).enumerated().compactMap { JobActivityItem(json: $0.element, index: $0.offset) } : []
        let text = nonEmpty(o["text"]) ?? ""
        let voice = (o["voice"] as? Bool) ?? false
        let files = kind == .assistant ? (o["files"] as? [Any] ?? []).prefix(12).compactMap(AgentFile.init(json:)) : []
        // A voice note whose words Hermes couldn't write down still shows (as a voice message); so does
        // a reply that was only a picture.
        if kind == .tools ? tools.isEmpty : (text.isEmpty && !(voice && kind == .user) && files.isEmpty) { return nil }
        self.init(id: id, kind: kind, at: decodeDate(o["at"]), text: text, tools: tools, voice: voice,
                  call: ((o["call"] as? [String: Any])?["lines"] as? [Any]).map { $0.compactMap(HistoryCallLine.init(json:)) }.flatMap { $0.isEmpty ? nil : $0 },
                  card: kind == .note ? HistoryWorkerTask(json: o["task"]) : nil, update: o["update"] as? Bool ?? false)
        // The recording behind a voice row is named by the sending device's client id (SPK-80's id).
        voiceClip = nonEmpty(o["voice_clip"]) ?? (voice ? nonEmpty(o["client_id"]) : nil)
        voiceDuration = (o["voice_duration"] as? NSNumber)?.doubleValue
        clientID = kind == .user ? nonEmpty(o["client_id"]) : nil
        self.files = files
        self.attribution = kind == .assistant ? ReplyAttribution(json: o["model"]) : nil
        automaticEvent = kind == .note ? HistoryAutomaticEvent(json: o["event"]) : nil
        narration = kind == .assistant && (o["narration"] as? Bool ?? false)
        workDetail = narration ? nonEmpty(o["work_detail"]) : nil
    }
}

/// A board worker's card (`task` on its first note): "SPK-43 · Keyboard sometimes covers content",
/// a link to the issue, its one-line goal, and the full instructions for a collapsed disclosure.
public struct HistoryWorkerTask: Equatable, Sendable {
    public var title: String
    public var issue: String?
    public var url: URL?
    public var goal: String?
    public var instructions: String
    public init(title: String, issue: String? = nil, url: URL? = nil, goal: String? = nil, instructions: String = "") {
        self.title = title; self.issue = issue; self.url = url; self.goal = goal; self.instructions = instructions
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let title = nonEmpty(o["title"]) else { return nil }
        let url = nonEmpty(o["url"]).flatMap(URL.init(string:)).flatMap { $0.scheme == "https" ? $0 : nil }
        self.init(title: String(title.prefix(160)), issue: nonEmpty(o["issue"]), url: url,
                  goal: nonEmpty(o["goal"]).map { String($0.prefix(240)) }, instructions: nonEmpty(o["instructions"]) ?? "")
    }
    /// "SPK-43 · Keyboard sometimes covers content".
    public var heading: String { issue.map { "\($0) \u{00B7} \(title)" } ?? title }
}

/// A running session's live line without a running Speakeasy task (`live`: a board worker, a chat mid-turn).
public struct HistoryLive: Equatable, Sendable {
    public var now: String
    public var soFar: String?
    public init(now: String, soFar: String? = nil) { self.now = now; self.soFar = soFar }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let now = nonEmpty(o["now"]) else { return nil }
        self.init(now: String(now.prefix(120)), soFar: nonEmpty(o["so_far"]).map { String($0.prefix(200)) })
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
    public enum SendVia: String, Equatable, Sendable { case task, sessionJob = "session_job", linked, answer }
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
    /// Servers with `model_choice`: the model that did the chat's latest work, what its next message
    /// runs on, and its own "Use for this chat" pick (nil = the default).
    public var model: ModelBadge?
    public var nextModel: ModelBadge?
    public var modelChoice: ModelChoice?
    /// Archived in Hermes (the page offers Unarchive) / pinned on any device.
    public var archived = false
    public var pinned = false
        /// Automated work (a board worker, a scheduled job): never needs-you.
        public var automated = false
        /// Its live line from its own steps while it runs with no Speakeasy task (plugins from 0.3.37).
        public var live: HistoryLive?
        /// A board worker's card (the first entry's), for the page's header.
        public var card: HistoryWorkerTask? { entries.first(where: { $0.card != nil })?.card }
        /// Messages held until the running task finishes, oldest first (servers with `message_modes`).
        public var queued: [QueuedMessage] = []
        /// What became of messages steered into its running tasks (sent / waiting / seen / follow_up / unused).
        public var steered: [SteeredMessage] = []
        /// The server can steer or queue (the page lists `queued`, even empty): the composer offers the choice.
        public var messageModes = false
        /// Servers with `agents`: the Hermes profile this chat talks to, when it isn't Speakeasy's own
        /// (it runs on that agent's own model, so the model chip doesn't apply).
        public var agent: AgentInfo?
        /// Plugin 0.3.37+: what the conversation used (tokens, an estimated cost); nil = nothing recorded / older plugin.
        public var usage: ChatUsage?
        /// Plugin 0.3.37+: the model on the chat's latest call and how full its context was; nil = not seen yet.
        public var context: ContextUse?

    public init(id: String, title: String, source: String, sourceLabel: String, state: HistorySession.State = .idle,
                sendVia: SendVia = .linked, entries: [HistoryEntry] = [], hasMore: Bool = false, before: Int? = nil,
                jobs: [WorkInfo] = [], approvals: [HistoryApproval] = [], model: ModelBadge? = nil,
                nextModel: ModelBadge? = nil, modelChoice: ModelChoice? = nil) {
        self.id = id; self.title = title; self.source = source; self.sourceLabel = sourceLabel; self.state = state
        self.sendVia = sendVia; self.entries = entries; self.hasMore = hasMore; self.before = before
        self.jobs = jobs; self.approvals = approvals
        self.model = model; self.nextModel = nextModel; self.modelChoice = modelChoice
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
                  approvals: (o["approvals"] as? [Any] ?? []).compactMap(HistoryApproval.init(json:)),
                  model: ModelBadge(json: o["model"]), nextModel: ModelBadge(json: o["next_model"]),
                  modelChoice: ModelChoice(json: o["model_choice"]))
        archived = o["archived"] as? Bool ?? false
        pinned = o["pinned"] as? Bool ?? false
        automated = o["automated"] as? Bool ?? false
        live = HistoryLive(json: o["live"])
        if automated && state == .needsYou { state = .running }
        if let held = o["queued"] as? [Any] {
            messageModes = true
            queued = held.compactMap(QueuedMessage.init(json:))
        }
        steered = (o["steered"] as? [Any] ?? []).compactMap(SteeredMessage.init(json:))
        agent = AgentInfo(json: o["agent"])
        usage = ChatUsage(json: o["usage"])
        context = ContextUse(json: o["context"])
    }

    /// Send asks Steer now / Queue for after: the server can, and a run is active.
    public var offersMessageModes: Bool { messageModes && isWorking }

    /// The jobs whose question (a decision card) waits on the user, as the plugin decides it
    /// (`question_open`; answered, dismissed or moved on anywhere, superseded or a repeat of an
    /// answered one → none). Two pieces of work can each wait on their own question (newest first).
    /// Plugins before it: the newest unanswered question only, never two cards for the same question.
    public var waitingQuestions: [WorkInfo] {
        // `question_state` (plugins with it) is the server's one word; the older fields still apply.
        let open = jobs.filter { $0.status == "completed" && $0.decision != nil && !$0.setAside && !$0.answered
            && $0.questionClosed == nil && ($0.questionState.map(\.isOpen) ?? true) }
        let flagged = open.filter { $0.questionOpen == true }
        if !flagged.isEmpty {
            var seen = Set<String>()   // the same question asked twice is one card
            return flagged.filter { seen.insert($0.decision?.question ?? $0.runID ?? "").inserted }
        }
        if open.contains(where: { $0.questionOpen == false }) {
            return []   // the plugin decided: none of these still waits
        }
        // Older plugins: one card at most (jobs are newest first), never the same question twice.
        return open.first.map { [$0] } ?? []
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
    /// Servers with `model_choice`: a model / level for this message, kept for the chat (nil = as is).
    public var choice: ModelChoice?
    /// Servers with `question_actions`: the run id of the task whose question this answers (a
    /// decision card's button or its "Reply…"); delivered to that task even when a newer one exists.
    public var answers: String?
    /// Servers with `message_modes`, while a run is active: steer or queue (nil = not sent; a steer).
    public var mode: MessageMode?

    public init?(sessionID: String, text: String, source: Source = .typed, clientID: String = UUID().uuidString,
                 attachments: [String] = [], choice: ModelChoice? = nil, answers: String? = nil,
                 mode: MessageMode? = nil) {
        let clean = TaskMessageRequest.clean(text)
        guard clean != nil || !attachments.isEmpty else { return nil }
        self.sessionID = sessionID; self.text = clean ?? ""; self.source = source; self.clientID = clientID
        self.attachments = attachments; self.choice = choice; self.answers = answers
        self.mode = mode
    }

    public var path: String { HistoryConversation.path(sessionID) + "/message" }
    public var body: [String: Any] {
        var out: [String: Any] = ["text": text, "source": source.rawValue, "client_id": clientID]
        if !attachments.isEmpty { out["attachments"] = attachments }
        if let answers, !answers.isEmpty { out["answers"] = answers }
        if let choice, !choice.isEmpty { out.merge(choice.body()) { _, new in new } }
        if let mode { out["mode"] = mode.rawValue }
        return out
    }
}

public struct HistoryMessageReply: Equatable, Sendable {
    public var via: HistoryConversation.SendVia
    /// The task to follow like a job.
    public var runID: String?
    /// How it went: `steer`, `answer`, `follow_up`, or `queued` (held until the task finishes).
    public var deliveredAs: TaskMessageDelivery
    /// A queued message: 1 = sent next.
    public var queuePosition: Int?
    /// The send's `client_id`, echoed, and the server's durable receipt (SPK-80).
    public var clientID: String?
    public var receiptID: String?

    public init(via: HistoryConversation.SendVia, runID: String?, deliveredAs: TaskMessageDelivery = .unknown,
                queuePosition: Int? = nil, clientID: String? = nil, receiptID: String? = nil) {
        self.via = via; self.runID = runID; self.deliveredAs = deliveredAs; self.queuePosition = queuePosition
        self.clientID = clientID; self.receiptID = receiptID
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], o["ok"] as? Bool == true,
              let via = (o["via"] as? String).flatMap(HistoryConversation.SendVia.init(rawValue:)) else { return nil }
        self.init(via: via, runID: nonEmpty(o["run_id"]),
                  deliveredAs: (o["delivered_as"] as? String).flatMap(TaskMessageDelivery.init(rawValue:)) ?? .unknown,
                  queuePosition: (o["queue_position"] as? NSNumber)?.intValue,
                  clientID: nonEmpty(o["client_id"]), receiptID: nonEmpty(o["receipt_id"]))
    }
}

/// One line said on a call (`entries[].call.lines`).
public struct HistoryCallLine: Equatable, Sendable {
    public enum Who: String, Equatable, Sendable { case you, agent }
    public var who: Who
    public var text: String
    public init(who: Who, text: String) { self.who = who; self.text = text }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let text = o["text"] as? String, !text.isEmpty else { return nil }
        self.init(who: Who(rawValue: o["who"] as? String ?? "") ?? .you, text: text)
    }
}
