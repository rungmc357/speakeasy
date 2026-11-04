import Foundation

// Typed and held-to-talk messages to one task (contract: ~/.../se-typing/CONTRACT.md).
// The target is always picked by the user (a job's reply bar, a decision's "Something else…", or
// holding the mic on a job) and sent by run id: nothing here goes through the voice's router.

/// How the words reached the task.
public enum TaskMessageSource: String, Equatable, Sendable {
    case typed
    case heldVoice = "held_voice"
}

/// What the server made of a message (`delivered_as`).
public enum TaskMessageDelivery: String, Equatable, Sendable {
    /// The job was running: it reads it as a steer.
    case steer
    /// The job was waiting on a question or decision: this is the answer.
    case answer
    /// The job had finished: a linked follow-up job started.
    case followUp = "follow_up"
    case unknown
}

/// `POST /voice/work/{run_id}/message`.
public struct TaskMessageRequest: Equatable, Sendable {
    public static let maxLength = 4000
    public var runID: String
    public var text: String
    public var source: TaskMessageSource
    /// Idempotency key: the same id within 10 minutes gets the same reply and no second dispatch.
    public var clientID: String
    public var interactionID: String?
    /// The decision/choice task this answers ("Something else…").
    public var choiceTaskID: String?

    public init(runID: String, text: String, source: TaskMessageSource = .typed, clientID: String = UUID().uuidString,
                interactionID: String? = nil, choiceTaskID: String? = nil) {
        self.runID = runID; self.text = text; self.source = source; self.clientID = clientID
        self.interactionID = interactionID; self.choiceTaskID = choiceTaskID
    }

    /// The text as it is sent: trimmed, at most 4000 characters; nil when there's nothing to send.
    public static func clean(_ text: String) -> String? {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return t.isEmpty ? nil : String(t.prefix(maxLength))
    }

    public var path: String {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
        return "/voice/work/\(runID.addingPercentEncoding(withAllowedCharacters: allowed) ?? runID)/message"
    }

    public var body: [String: Any] {
        var b: [String: Any] = ["text": Self.clean(text) ?? "", "source": source.rawValue, "client_id": clientID]
        if let interactionID { b["interaction_id"] = interactionID }
        if let choiceTaskID { b["choice_task_id"] = choiceTaskID }
        return b
    }
}

/// The reply: which run got it and how.
public struct TaskMessageReply: Equatable, Sendable {
    public var runID: String?
    public var deliveredAs: TaskMessageDelivery
    public init(runID: String?, deliveredAs: TaskMessageDelivery) { self.runID = runID; self.deliveredAs = deliveredAs }
    public init?(json: [String: Any]) {
        guard json["ok"] as? Bool != false else { return nil }
        runID = json["run_id"] as? String
        deliveredAs = (json["delivered_as"] as? String).flatMap(TaskMessageDelivery.init(rawValue:)) ?? .unknown
    }
}

/// One message on its way to a task, as the reply bar shows it.
public struct OutgoingMessage: Identifiable, Equatable, Sendable {
    public enum State: Equatable, Sendable {
        /// Waiting behind a message still being sent.
        case queued
        case sending
        case sent(TaskMessageDelivery)
        /// The server's sentence (or a plain one), with Retry.
        case failed(String)
    }
    /// The idempotency `client_id`: a Retry sends the same id, so a send that did land isn't doubled.
    public var id: String
    public var jobID: String
    public var runID: String
    /// The task's short name, for "Sent to …".
    public var taskName: String
    public var text: String
    public var source: TaskMessageSource
    public var choiceTaskID: String?
    public var state: State = .queued
    public var at: Date

    public init(id: String = UUID().uuidString, jobID: String, runID: String, taskName: String, text: String,
                source: TaskMessageSource = .typed, choiceTaskID: String? = nil, at: Date = Date()) {
        self.id = id; self.jobID = jobID; self.runID = runID; self.taskName = taskName; self.text = text
        self.source = source; self.choiceTaskID = choiceTaskID; self.at = at
    }

    public var isFailed: Bool { if case .failed = state { return true }; return false }
    public var isSent: Bool { if case .sent = state { return true }; return false }
    public var isPending: Bool { state == .queued || state == .sending }

    public func request(interactionID: String?) -> TaskMessageRequest {
        TaskMessageRequest(runID: runID, text: text, source: source, clientID: id, interactionID: interactionID,
                           choiceTaskID: choiceTaskID)
    }
}

/// Messages to tasks, sent one at a time in the order written. A message typed while another is
/// sending waits (queued) and goes right after; a failure stays with Retry and doesn't hold up the rest.
public struct TaskOutbox: Equatable, Sendable {
    public private(set) var items: [OutgoingMessage] = []
    public init() {}

    /// Adds a message; nil when the text is empty.
    @discardableResult
    public mutating func enqueue(jobID: String, runID: String, taskName: String, text: String,
                                 source: TaskMessageSource = .typed, choiceTaskID: String? = nil,
                                 id: String = UUID().uuidString, at: Date = Date()) -> OutgoingMessage? {
        guard let clean = TaskMessageRequest.clean(text) else { return nil }
        let m = OutgoingMessage(id: id, jobID: jobID, runID: runID, taskName: taskName, text: clean,
                                source: source, choiceTaskID: choiceTaskID, at: at)
        items.append(m)
        trim()
        return m
    }

    /// Something is on the wire now.
    public var isSending: Bool { items.contains { $0.state == .sending } }

    /// The next message to send, marked sending; nil while one is already on the wire or none waits.
    public mutating func startNext() -> OutgoingMessage? {
        guard !isSending, let i = items.firstIndex(where: { $0.state == .queued }) else { return nil }
        items[i].state = .sending
        return items[i]
    }

    public mutating func markSent(_ id: String, as delivery: TaskMessageDelivery) { set(id, .sent(delivery)) }
    public mutating func markFailed(_ id: String, _ message: String) { set(id, .failed(message)) }
    /// Retry: back in line with the same client id.
    public mutating func retry(_ id: String) {
        guard let i = items.firstIndex(where: { $0.id == id }), items[i].isFailed else { return }
        items[i].state = .queued
    }
    public mutating func discard(_ id: String) { items.removeAll { $0.id == id } }

    public func messages(forJob jobID: String) -> [OutgoingMessage] { items.filter { $0.jobID == jobID } }
    public func message(_ id: String) -> OutgoingMessage? { items.first { $0.id == id } }

    private mutating func set(_ id: String, _ state: OutgoingMessage.State) {
        guard let i = items.firstIndex(where: { $0.id == id }) else { return }
        items[i].state = state
    }

    /// Keeps the newest 60 settled messages (pending ones always stay).
    private mutating func trim() {
        let settled = items.filter { !$0.isPending }
        guard settled.count > 60 else { return }
        let drop = Set(settled.prefix(settled.count - 60).map(\.id))
        items.removeAll { drop.contains($0.id) }
    }
}

/// Words the reply bar and toasts use.
public enum TaskMessageCopy {
    /// The reply bar's placeholder while a decision waits: what's typed answers it.
    public static let somethingElse = "Something else? Type it here"

    /// A job name short enough for a toast or a one-line label: cut at a word, about 28 characters.
    public static func shortName(_ name: String, limit: Int = 28) -> String {
        let t = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard t.count > limit else { return t }
        let cut = t.prefix(limit)
        let base = cut.lastIndex(of: " ").map { String(cut[..<$0]) } ?? String(cut)
        return base.trimmingCharacters(in: CharacterSet(charactersIn: " ,.;:·-")) + "…"
    }

    public static func sent(_ m: OutgoingMessage) -> String {
        switch m.state {
        case .sent(.followUp): return "Started a follow-up to \(shortName(m.taskName))"
        default: return "Sent to \(shortName(m.taskName))"
        }
    }

    /// The hold-to-talk banner while the mic button is held.
    public static func holding(_ taskName: String) -> String { "Talking to \(shortName(taskName)) · release to stop" }

    public static func placeholder(_ taskName: String) -> String { "Type to \(shortName(taskName, limit: 24))" }

    /// A plain sentence for a failed send: the server's own when it gives one.
    public static func failure(status: Int?, serverMessage: String?) -> String {
        if let m = serverMessage?.trimmingCharacters(in: .whitespacesAndNewlines), !m.isEmpty,
           !m.hasPrefix("Server HTTP"), m.count <= 200 {
            return m.hasSuffix(".") || m.hasSuffix("!") || m.hasSuffix("?") ? m : m + "."
        }
        switch status {
        case 404?: return "That task isn't on your Hermes anymore."
        case 409?: return "That task was stopped, so it can't take messages."
        case 400?: return "That message couldn't be sent. Check it and try again."
        case nil, 0?: return "Couldn't reach your Hermes. Check your connection and try again."
        default: return "Your Hermes couldn't take that message right now."
        }
    }
}

/// Hold-to-talk to one task during a call (`POST /voice/interactions/{id}/target`).
public enum HoldToTalk {
    /// The server lets go after 60 s without a refresh; the client re-posts every 20 s while held.
    public static let refreshInterval: TimeInterval = 20

    public static func body(runID: String, hold: Bool) -> [String: Any] { ["run_id": runID, "hold": hold] }

    public static func path(interactionID: String) -> String {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
        return "/voice/interactions/\(interactionID.addingPercentEncoding(withAllowedCharacters: allowed) ?? interactionID)/target"
    }
}
