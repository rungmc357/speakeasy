import Foundation

// Steer or queue (docs/API.md "Steer or queue", servers with `message_modes`): while a task runs, a
// message goes into it now (steer) or waits and is sent as its next turn once it finishes (queue).
// The apps ask at Send (a small pop-up with both choices, nothing sent until a pick); with nothing
// running Send is a plain send.

/// A remembered choice (older composers kept one under the field; the current apps ask every time
/// and pass the pick straight through, so `selected` is only a default for callers that still use it).
public struct MessageModeChoice: Equatable, Sendable {
    public var selected: MessageMode
    public init(selected: MessageMode = .steer) { self.selected = selected }

    /// The mode one send carries: nil when no run is active or the server can't (a plain send,
    /// no `mode` in the body); else the selection, or the other one for the alternate key.
    public func mode(offered: Bool, alternate: Bool = false) -> MessageMode? {
        guard offered else { return nil }
        return alternate ? selected.other : selected
    }

    /// Pictures and files can't wait (the server refuses `queue` with attachments while a task runs).
    public func mode(offered: Bool, alternate: Bool = false, hasAttachments: Bool) -> MessageMode? {
        guard let mode = mode(offered: offered, alternate: alternate) else { return nil }
        return hasAttachments ? .steer : mode
    }

    /// The send button's label for the current choice (accessibility, tooltip).
    public func sendLabel(offered: Bool) -> String {
        guard offered else { return "Send" }
        return selected == .steer ? "Steer now" : "Queue for after"
    }
}

/// Messages this device steered into a running task, so the thread can tag them "Steered" once the
/// server's copy shows (same words, not older than the send).
public struct SteeredMarks: Equatable, Sendable {
    private var marks: [(text: String, at: Date)] = []
    public init() {}

    public static func == (a: SteeredMarks, b: SteeredMarks) -> Bool {
        a.marks.map(\.text) == b.marks.map(\.text) && a.marks.map(\.at) == b.marks.map(\.at)
    }

    public mutating func record(_ text: String, at: Date = Date()) {
        let key = Self.norm(text)
        guard !key.isEmpty else { return }
        marks.append((key, at))
        if marks.count > 50 { marks.removeFirst(marks.count - 50) }
    }

    /// True for a user message with these words, sent at or after a steer of them (a minute's slack).
    public func contains(_ text: String, at: Date?) -> Bool {
        let key = Self.norm(text)
        return marks.contains { m in m.text == key && (at.map { $0 >= m.at.addingTimeInterval(-60) } ?? true) }
    }

    public var isEmpty: Bool { marks.isEmpty }

    static func norm(_ s: String) -> String {
        s.lowercased().split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }
}

/// The label under a steered message.
public enum MessageModeCopy {
    public static let steered = "Sent to the running task"
    public static let queued = QueuedMessage.label
    public static let steerNow = "Steer now"
    public static let cancel = "Cancel"
}

/// What became of a message steered into a running task (`steered` on a session page or task,
/// docs/API.md "Steer or queue"): sent -> seen, or waiting, follow_up, unused.
public struct SteeredMessage: Equatable, Sendable, Identifiable {
    public enum State: String, Equatable, Sendable {
        case sent, waiting, seen
        case followUp = "follow_up"
        case unused
    }
    public var clientID: String
    public var text: String
    public var at: Date?
    public var state: State
    /// The server's words for it ("Seen by Todd", …).
    public var label: String
    public var seenAt: Date?
    /// A follow-up it became: the task to open.
    public var runID: String?
    public var id: String { clientID }

    public init(clientID: String, text: String, at: Date? = nil, state: State, label: String? = nil,
                seenAt: Date? = nil, runID: String? = nil) {
        self.clientID = clientID; self.text = text; self.at = at; self.state = state
        self.label = label ?? Self.defaultLabel(state); self.seenAt = seenAt; self.runID = runID
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = o["client_id"] as? String, !id.isEmpty,
              let text = o["text"] as? String,
              let state = (o["state"] as? String).flatMap(State.init(rawValue:)) else { return nil }
        let date = { (k: String) in (o[k] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) } }
        self.init(clientID: id, text: text, at: date("at"), state: state, label: o["label"] as? String,
                  seenAt: date("seen_at"), runID: o["run_id"] as? String)
    }

    public static func defaultLabel(_ state: State) -> String {
        switch state {
        case .sent: return "Sent to the running task"
        case .waiting: return "Waiting for the current step to finish"
        case .seen: return "Seen"
        case .followUp: return "It finished first · sent as a follow-up"
        case .unused: return "The task stopped before reading this"
        }
    }

    /// The tag under the bubble: the label, with the time it was read.
    public func tag(timeStyle: (Date) -> String = { DateFormatter.localizedString(from: $0, dateStyle: .none, timeStyle: .short) }) -> String {
        if state == .seen, let seenAt { return "\(label) · \(timeStyle(seenAt))" }
        return label
    }

    /// The symbol beside the tag.
    public var symbol: String {
        switch state {
        case .sent: return "arrow.turn.down.right"
        case .waiting: return "hourglass"
        case .seen: return "eye"
        case .followUp: return "arrow.uturn.forward"
        case .unused: return "xmark.circle"
        }
    }

    /// Whether a thread message is this one: the transcript keeps the words, maybe wrapped
    /// ("Sam typed to this task: … Fold it into …"), so it contains them.
    public func matches(_ message: String, at: Date?) -> Bool {
        let want = SteeredMarks.norm(text)
        guard !want.isEmpty, SteeredMarks.norm(message).contains(want) else { return false }
        guard let at, let sent = self.at else { return true }
        return at >= sent.addingTimeInterval(-60)
    }
}

public extension Array where Element == SteeredMessage {
    /// The receipt for a thread message, if it was steered (newest match first).
    func receipt(for message: String, at: Date?) -> SteeredMessage? {
        last { $0.matches(message, at: at) }
    }
}
