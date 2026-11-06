import Foundation

/// The idle board's background poll of `/voice/work/latest`: which answers may still touch the
/// board, and when a failure is worth telling the user about.
///
/// - Every new poll loop (opening the board, coming back to the foreground) is a new generation;
///   an answer to an older loop's request is dropped, never written over newer state.
/// - Every local change the user makes to the list (clearing or dismissing tasks) bumps the list
///   epoch, before the request goes out and again when the server has it. An answer to a request
///   sent before that may still hold what was just cleared, so it is dropped too: the next poll
///   reads the server after it honoured the clear.
/// - Cancellation (a newer loop replacing this one, the board closing) is never an error, and a
///   passing failure is never a banner: only `failuresBeforeNotice` real failures in a row are.
/// - The notice is plain words and goes away by itself: the next answer clears it
///   (`VoiceAction.workNoticeCleared`), which never touches a call's own error.
public struct WorkPollGate: Sendable {
    public static let failuresBeforeNotice = 3
    /// The banner's lead-in: the board couldn't load (its Try again reloads the board, never starts a call).
    public static let noticePrefix = "Can't load your work right now. "
    /// The whole notice when there's nothing more specific to say.
    public static let notice = noticePrefix + "Speakeasy keeps trying."

    /// True for the board's own notice (also after the iPhone rewords its HTTP code), never a call error.
    public static func isNotice(_ text: String?) -> Bool { text?.hasPrefix(noticePrefix) == true }

    /// The notice for this failure: plain words, never the system's error text. A server status code
    /// rides along as "(HTTP 502)" so the iPhone can say what it means (`PlainError`); nothing else does.
    public static func notice(for error: Error) -> String {
        let text = error.localizedDescription
        guard let range = text.range(of: #"HTTP\s+\d{3}"#, options: .regularExpression) else { return notice }
        return notice + " (HTTP " + String(text[range].filter(\.isNumber)) + ")"
    }

    /// What a request was sent under; checked against the gate when its answer arrives.
    public struct Ticket: Equatable, Sendable {
        public let generation: Int?
        public let epoch: Int
    }

    public private(set) var generation = 0
    public private(set) var listEpoch = 0
    public private(set) var consecutiveFailures = 0

    public init() {}

    /// A new poll loop starts; the previous loop's answers no longer count. Returns its generation.
    @discardableResult
    public mutating func startLoop() -> Int {
        generation += 1
        consecutiveFailures = 0
        return generation
    }

    /// The board closed: no loop's answers count any more.
    public mutating func stop() { startLoop() }

    /// The user changed the list here (cleared or dismissed tasks).
    public mutating func listChanged() { listEpoch += 1 }

    /// A request from loop `generation` (nil: a one-off refresh outside the loop).
    public func ticket(generation: Int? = nil) -> Ticket { Ticket(generation: generation, epoch: listEpoch) }

    /// Whether an answer to a request sent under `ticket` may be written to the board.
    public func accepts(_ ticket: Ticket) -> Bool {
        (ticket.generation == nil || ticket.generation == generation) && ticket.epoch == listEpoch
    }

    public mutating func succeeded(_ ticket: Ticket) {
        if ticket.generation == nil || ticket.generation == generation { consecutiveFailures = 0 }
    }

    /// A request failed. Returns the banner to show, or nil: cancellation, a replaced loop's
    /// request, or a failure that hasn't persisted yet stay quiet. A persisting failure shows once.
    public mutating func failed(_ error: Error, ticket: Ticket, cancelled: Bool = false) -> String? {
        if cancelled || Self.isCancellation(error) { return nil }
        if let loop = ticket.generation, loop != generation { return nil }
        consecutiveFailures += 1
        guard consecutiveFailures == Self.failuresBeforeNotice else { return nil }
        return Self.notice(for: error)
    }

    /// A cancelled task or URL request (Swift `CancellationError`, `URLError.cancelled`, NSURL -999).
    public static func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        if let url = error as? URLError, url.code == .cancelled { return true }
        let ns = error as NSError
        return ns.domain == NSURLErrorDomain && ns.code == NSURLErrorCancelled
    }
}
