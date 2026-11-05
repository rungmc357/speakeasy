import Foundation

// The Mac history window's pure state, built on the shared history models (SessionHistory.swift,
// VoiceMessage.swift): the sidebar list (paging, search, source filter, groups), who the composer
// sends to, messages on their way, and a session's thread as drawn (tool calls folded under the
// answer they led to; approvals, questions and helpers pulled out of its jobs and sends).

// MARK: - The list

/// One `GET /voice/history` request, tagged with the list state it was asked for.
public struct HistoryQuery: Equatable, Sendable {
    public var cursor: String?
    public var q: String
    public var source: String?
    public var limit: Int
    /// A reply for an older generation (an older search or filter) is dropped.
    public var generation: Int

    public init(cursor: String? = nil, q: String = "", source: String? = nil, limit: Int = HistoryList.pageSize, generation: Int = 0) {
        self.cursor = cursor; self.q = q; self.source = source; self.limit = limit; self.generation = generation
    }

    public var path: String { HistoryPage.path(query: q, source: source, cursor: cursor, limit: limit) }
}

/// The sidebar list: sessions loaded so far, the cursor, the search and the filter. Changing the
/// search or the filter starts over (a new generation), so a slow reply never overwrites a newer search.
public struct HistoryList: Equatable, Sendable {
    public static let pageSize = 40
    public private(set) var sessions: [HistorySession] = []
    public private(set) var nextCursor: String?
    public private(set) var reachedEnd = false
    public private(set) var loading = false
    public private(set) var error: String?
    public private(set) var query = ""
    public private(set) var source: String?
    public private(set) var filters: [HistoryFilter] = []
    public private(set) var generation = 0

    public init() {}

    /// The trimmed search, empty for none.
    public var activeQuery: String { String(query.trimmingCharacters(in: .whitespacesAndNewlines).prefix(200)) }
    public var isFiltered: Bool { !activeQuery.isEmpty || source != nil }
    public var canLoadMore: Bool { !reachedEnd && !loading && error == nil }

    /// Type into the search: starts over. Nil when the effective search didn't change.
    public mutating func setQuery(_ text: String) -> HistoryQuery? {
        let before = activeQuery
        query = text
        return activeQuery == before ? nil : reset()
    }

    public mutating func setSource(_ id: String?) -> HistoryQuery? {
        guard id != source else { return nil }
        source = id
        return reset()
    }

    /// Start over (refresh, a new search or filter).
    public mutating func reset() -> HistoryQuery {
        generation += 1
        sessions = []; nextCursor = nil; reachedEnd = false; error = nil; loading = false
        return begin()!
    }

    /// The next page, or nil while one loads, after an error, or at the end.
    public mutating func loadMore() -> HistoryQuery? { canLoadMore ? begin() : nil }

    public mutating func retry() -> HistoryQuery? {
        error = nil
        return begin()
    }

    private mutating func begin() -> HistoryQuery? {
        guard !loading, !reachedEnd else { return nil }
        loading = true
        return HistoryQuery(cursor: nextCursor, q: activeQuery, source: source, limit: Self.pageSize, generation: generation)
    }

    /// A page arrived: sessions already shown are updated in place, never repeated; no cursor ends
    /// the list. False when it was for an older search and was dropped.
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
        if !page.filters.isEmpty { filters = page.filters }
        return true
    }

    @discardableResult
    public mutating func fail(_ message: String, for request: HistoryQuery) -> Bool {
        guard request.generation == generation else { return false }
        loading = false; error = message
        return true
    }

    /// A session just changed (a message sent, a new chat): to the top.
    public mutating func upsert(_ session: HistorySession) {
        sessions.removeAll { $0.id == session.id }
        sessions.insert(session, at: 0)
    }

    public func grouped(now: Date, calendar: Calendar = .current) -> [(HistoryGroup, [HistorySession])] {
        HistoryGroup.grouped(sessions, now: now, calendar: calendar)
    }

    /// The filter's choices: what the server listed, else the usual ones.
    public var filterChoices: [HistoryFilter] { filters.isEmpty ? Self.defaultFilters : filters }
    public static let defaultFilters: [HistoryFilter] = [
        HistoryFilter(id: "speakeasy", label: "Speakeasy"), HistoryFilter(id: "discord", label: "Discord"),
        HistoryFilter(id: "telegram", label: "Telegram"), HistoryFilter(id: "cli", label: "Terminal"),
        HistoryFilter(id: "scheduled", label: "Scheduled"),
    ]

    /// The SF Symbol for where a session came from.
    public static func symbol(for source: String?) -> String {
        switch source?.lowercased() ?? "" {
        case "speakeasy", "voice": return "waveform"
        case "discord", "slack", "telegram", "whatsapp", "signal", "matrix": return "bubble.left.and.bubble.right"
        case "cli", "terminal": return "terminal"
        case "cron", "scheduled": return "clock.arrow.circlepath"
        case "email": return "envelope"
        case "gui", "desktop": return "macwindow"
        default: return "circle.grid.2x2"
        }
    }
}

// MARK: - Who the composer sends to

/// A session page sends to that session by id (`POST /voice/history/{id}/message`); only New chat
/// starts something new (`POST /voice/work`). Neither goes through the voice's router.
public enum HistoryTarget: Equatable, Sendable {
    case session(String)
    case newChat

    public enum Outgoing: Equatable, Sendable {
        case message(HistoryMessageRequest)
        case newChat(text: String, clientID: String)
    }

    /// What Send does with these words; nil when there's nothing to send.
    public func outgoing(_ text: String, source: HistoryMessageRequest.Source,
                         clientID: String = UUID().uuidString) -> Outgoing? {
        switch self {
        case .session(let id):
            return HistoryMessageRequest(sessionID: id, text: text, source: source, clientID: clientID).map(Outgoing.message)
        case .newChat:
            return TaskMessageRequest.clean(text).map { .newChat(text: $0, clientID: clientID) }
        }
    }

    public var sessionID: String? { if case .session(let id) = self { return id }; return nil }
}

// MARK: - A session's thread, as drawn

public struct HistoryThreadMessage: Equatable, Sendable, Identifiable {
    public enum Role: Equatable, Sendable { case user, assistant, note }
    public enum Delivery: Equatable, Sendable { case sending, sent, failed(String) }
    public var id: String
    public var role: Role
    public var text: String
    public var at: Date?
    public var voice: Bool
    /// The tool calls behind this answer (assistant), oldest first.
    public var tools: [JobActivityItem]
    /// A message this app sent that's still on its way (or failed).
    public var delivery: Delivery?
    /// Set on a local message: retry with the same client id.
    public var clientID: String?

    public init(id: String, role: Role, text: String, at: Date? = nil, voice: Bool = false, tools: [JobActivityItem] = [],
                delivery: Delivery? = nil, clientID: String? = nil) {
        self.id = id; self.role = role; self.text = text; self.at = at; self.voice = voice; self.tools = tools
        self.delivery = delivery; self.clientID = clientID
    }
}

/// Something the session waits on: a task's approval or question, or one a send through Hermes hit.
public struct HistoryWaiting: Equatable, Sendable, Identifiable {
    public enum Kind: Equatable, Sendable { case approval, question }
    public var id: String
    public var kind: Kind
    public var text: String
    public var command: String?
    /// Buttons. A send's own choices (never "always"); a task's are "yes"/"no" or its options.
    public var choices: [String]
    /// Set: answered with `POST …/sends/{send_id}/answer` (`choice`).
    public var sendID: String?
    /// Set: a task's approval, answered with `POST …/approval` (`run_id`, `choice`).
    /// Neither: a task's question, answered by a message to the session (the server hands it to the
    /// waiting task as the answer).
    public var runID: String?

    public init(id: String, kind: Kind, text: String, command: String? = nil, choices: [String],
                sendID: String? = nil, runID: String? = nil) {
        self.id = id; self.kind = kind; self.text = text; self.command = command; self.choices = choices
        self.sendID = sendID; self.runID = runID
    }
}

/// `HistoryConversation` turned into what the session page draws.
public struct HistoryThread: Equatable, Sendable {
    public var messages: [HistoryThreadMessage]
    public var waiting: [HistoryWaiting]
    public var helpers: [JobSubagent]
    public var toolCallCount: Int

    public init(_ c: HistoryConversation, outbox: HistoryOutbox = HistoryOutbox()) {
        // Tools rows fold into the answer that follows (notes don't split them).
        var out: [HistoryThreadMessage] = []
        var pending: [JobActivityItem] = []
        var pendingAt: Date?
        for e in c.entries {
            switch e.kind {
            case .tools:
                pending += e.tools; pendingAt = pendingAt ?? e.at
            case .note:
                out.append(HistoryThreadMessage(id: e.id, role: .note, text: e.text, at: e.at))
            case .assistant:
                out.append(HistoryThreadMessage(id: e.id, role: .assistant, text: e.text, at: e.at, tools: pending))
                pending = []; pendingAt = nil
            case .user:
                if !pending.isEmpty {
                    out.append(HistoryThreadMessage(id: "tools-\(e.id)", role: .assistant, text: "", at: pendingAt, tools: pending))
                    pending = []; pendingAt = nil
                }
                out.append(HistoryThreadMessage(id: e.id, role: .user, text: e.text, at: e.at))
            }
        }
        if !pending.isEmpty { out.append(HistoryThreadMessage(id: "tools-end", role: .assistant, text: "", at: pendingAt, tools: pending)) }

        var waiting: [HistoryWaiting] = []
        // Sends through Hermes not yet in the transcript: a bubble with how it's going.
        let userTexts = Set(out.filter { $0.role == .user }.map { $0.text.trimmingCharacters(in: .whitespacesAndNewlines) })
        for send in c.sends {
            let echoed = userTexts.contains(send.text.trimmingCharacters(in: .whitespacesAndNewlines))
            if let p = send.prompt, send.status == .needsYou {
                let choices = p.choices.filter { $0 != "always" }
                waiting.append(HistoryWaiting(id: "send-\(send.id)", kind: p.kind == "question" ? .question : .approval, text: p.text,
                                              choices: choices.isEmpty && p.kind != "question" ? ["once", "deny"] : choices,
                                              sendID: send.id))
            }
            guard !echoed, !send.text.isEmpty else { continue }
            switch send.status {
            case .failed:
                out.append(HistoryThreadMessage(id: "send-\(send.id)", role: .user, text: send.text, at: send.started,
                                                delivery: .failed(send.error ?? "That didn't go through.")))
            case .completed:
                out.append(HistoryThreadMessage(id: "send-\(send.id)", role: .user, text: send.text, at: send.started))
                if let reply = send.reply {
                    out.append(HistoryThreadMessage(id: "send-\(send.id)-reply", role: .assistant, text: reply))
                }
            default:
                out.append(HistoryThreadMessage(id: "send-\(send.id)", role: .user, text: send.text, at: send.started, delivery: .sent))
            }
        }
        // Its Speakeasy tasks waiting on the user: approvals (their own route), then questions.
        for a in c.approvals {
            waiting.append(HistoryWaiting(id: "approval-\(a.runID)", kind: .approval, text: a.description,
                                          choices: a.choices, runID: a.runID))
        }
        for job in c.waitingQuestions {
            guard let d = job.decision else { continue }
            waiting.append(HistoryWaiting(id: "job-\(job.runID ?? d.question)", kind: .question, text: d.question,
                                          choices: d.options.map(\.text)))
        }
        var seen = Set<String>()
        self.helpers = c.jobs.flatMap(\.subagents).filter { seen.insert($0.id).inserted }
        self.messages = outbox.merged(into: out, session: c.id)
        self.waiting = waiting
        self.toolCallCount = out.reduce(0) { $0 + $1.tools.count }
    }
}

// MARK: - Messages on their way

/// Messages this app sent to a session, shown at the end of its thread until the server's copy
/// arrives (same text, not older than ours), each with how it's going. A failed one retries with
/// the same client id (the server never sends it twice).
public struct HistoryOutbox: Equatable, Sendable {
    public struct Item: Equatable, Sendable, Identifiable {
        public var id: String { request.clientID }
        public var request: HistoryMessageRequest
        public var at: Date
        public var delivery: HistoryThreadMessage.Delivery
    }
    public private(set) var items: [Item] = []
    public init() {}

    public mutating func add(_ request: HistoryMessageRequest, at: Date) {
        items.removeAll { $0.id == request.clientID }
        items.append(Item(request: request, at: at, delivery: .sending))
    }
    public mutating func mark(_ clientID: String, _ delivery: HistoryThreadMessage.Delivery) {
        if let i = items.firstIndex(where: { $0.id == clientID }) { items[i].delivery = delivery }
    }
    public func item(_ clientID: String) -> Item? { items.first { $0.id == clientID } }
    public func pending(in session: String) -> Bool { items.contains { $0.request.sessionID == session } }

    public func merged(into messages: [HistoryThreadMessage], session: String) -> [HistoryThreadMessage] {
        var shown = messages
        var used = Set<String>()
        for item in items where item.request.sessionID == session {
            if item.delivery == .sent, let echo = Self.echo(of: item, in: messages, skipping: used) { used.insert(echo); continue }
            shown.append(HistoryThreadMessage(id: "local-\(item.id)", role: .user, text: item.request.text, at: item.at,
                                              voice: item.request.source == .voiceMessage, delivery: item.delivery,
                                              clientID: item.id))
        }
        return shown
    }

    /// Forget sent messages the server now shows.
    public mutating func prune(_ messages: [HistoryThreadMessage], session: String) {
        var used = Set<String>()
        items.removeAll { item in
            guard item.request.sessionID == session, item.delivery == .sent,
                  let echo = Self.echo(of: item, in: messages, skipping: used) else { return false }
            used.insert(echo); return true
        }
    }

    static func echo(of item: Item, in messages: [HistoryThreadMessage], skipping used: Set<String>) -> String? {
        messages.first { m in
            m.role == .user && m.delivery == nil && !used.contains(m.id)
                && m.text.trimmingCharacters(in: .whitespacesAndNewlines) == item.request.text
                && (m.at.map { $0 >= item.at.addingTimeInterval(-60) } ?? true)
        }?.id
    }
}

// MARK: - Voice messages on the Mac

/// The shared `VoiceMessageFlow` plus the Mac's one extra rule: a live call takes the mic, so the
/// mic button hides and a recording in progress is dropped (a transcript under review stays: it's
/// just text in the field).
public struct MacVoiceMessage: Equatable, Sendable {
    public private(set) var flow = VoiceMessageFlow()
    public private(set) var callLive = false
    public init() {}

    public var micVisible: Bool { !callLive }
    /// The words go out as a voice message (they came from the mic and are being reviewed).
    public var reviewing: Bool { flow.phase == .reviewing }

    @discardableResult
    public mutating func handle(_ event: VoiceMessageFlow.Event, draft: String = "") -> VoiceMessageFlow.Effect {
        if callLive, case .micTapped = event { return .none }
        return flow.handle(event, draft: draft)
    }

    /// Returns the effect to apply (cancel the recorder when a call took the mic).
    @discardableResult
    public mutating func setCallLive(_ live: Bool) -> VoiceMessageFlow.Effect {
        callLive = live
        guard live, flow.isBusy else { return .none }
        return flow.handle(.cancel)
    }

    /// The message to show when voice messages can't run, in the Mac's words.
    public var problem: String? {
        guard let m = flow.unavailableMessage else { return nil }
        if m == VoiceMessageFlow.noOnDeviceText {
            return "This Mac can't turn speech into text on the device for your language, so voice messages are off. You can still type."
        }
        if m == VoiceMessageFlow.deniedText {
            return "Speakeasy needs the microphone and speech recognition for voice messages. Turn them on in System Settings › Privacy & Security."
        }
        return m
    }
}
