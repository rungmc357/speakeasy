import Foundation

// The four voice states the panel makes obvious (contract §6): listening (mic open, quiet),
// hearing you (the user is speaking), thinking (the user stopped, the reply hasn't started),
// speaking (the assistant's voice is playing). Derived client-side; no server change.

public enum VoicePhase: Equatable, Sendable {
    /// No live voice: idle, connecting, paused, ended.
    case off
    case muted
    case listening
    case hearing
    case thinking
    case speaking

    /// The header's first line for this phase (nil: keep the presentation's own text).
    public func title(assistantName: String) -> String? {
        switch self {
        case .listening: return "Listening"
        case .hearing: return "Hearing you"
        case .thinking: return "Thinking…"
        case .speaking: return assistantName
        case .muted: return "Muted"
        case .off: return nil
        }
    }

    /// VoiceOver / tooltip text for the orb's ring.
    public var label: String {
        switch self {
        case .off: return ""
        case .muted: return "Mic off"
        case .listening: return "Listening"
        case .hearing: return "Hearing you"
        case .thinking: return "Thinking"
        case .speaking: return "Speaking"
        }
    }
}

public extension VoiceState {
    /// How long "thinking" may last with no reply before the panel goes back to Listening
    /// (a turn the model chose not to answer must not spin forever).
    static let thinkingFor: TimeInterval = 15

    /// The user said something and the reply hasn't started: end-of-speech → first reply word.
    var isThinking: Bool {
        guard connection == .live, mic == .live, speech == .idle, !exchange.replyStarted,
              !cleanTranscript(exchange.you).isEmpty, let last = lastInputAt else { return false }
        return now.timeIntervalSince(last) < Self.thinkingFor
    }

    /// The voice phase from state alone; `userTalking` (mic level over the speech threshold) turns
    /// listening or thinking into hearing.
    func voicePhase(userTalking: Bool) -> VoicePhase {
        guard connection == .live else { return .off }
        if case .speaking = speech { return .speaking }
        if mic == .muted { return .muted }
        if micHealth == .broken || micHealth == .repairing { return .off }
        if userTalking { return .hearing }
        if isThinking { return .thinking }
        return .listening
    }
}

// MARK: - The live caption

public enum LiveCaption {
    /// The end of `text`, at most `limit` characters, cut on a word boundary with a leading "…".
    /// Never cuts a word in half (the old caption showed "…rsday, for four").
    public static func tail(_ text: String, limit: Int = 84) -> String {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard t.count > limit else { return t }
        let window = String(t.suffix(limit))
        // Start at the latest sentence start inside the window, else the first whole word.
        if let r = window.range(of: #"[.!?]\s+"#, options: .regularExpression), window.distance(from: window.startIndex, to: r.upperBound) < limit - 20 {
            return String(window[r.upperBound...])
        }
        if let space = window.firstIndex(of: " ") {
            return "…" + window[window.index(after: space)...]
        }
        return "…" + window
    }

    /// Who is speaking the current line and what it says: the assistant's reply while it speaks
    /// (or once it has started), else the user's words. nil when there's nothing to show.
    public static func current(_ exchange: Exchange, phase: VoicePhase) -> (you: Bool, text: String)? {
        let e = exchange.cleaned
        let assistantFirst = phase == .speaking || (e.replyStarted && phase != .hearing && phase != .thinking)
        if assistantFirst, !e.assistant.isEmpty { return (false, tail(e.assistant)) }
        if !e.you.isEmpty { return (true, tail(e.you)) }
        if !e.assistant.isEmpty { return (false, tail(e.assistant)) }
        return nil
    }
}

// MARK: - Call problems with a fix

/// A call problem the panel can offer a one-click fix for (like Screen Recording's "Set up").
public struct CallProblem: Equatable, Sendable {
    public enum Fix: Equatable, Sendable {
        /// System Settings › Privacy & Security › Microphone.
        case openMicSettings
        /// Reopen the voice connection, keeping the conversation and tasks.
        case restartCall
        /// Start the call again.
        case retry
        /// Codex on the Hermes isn't signed in: the Sign in to ChatGPT sheet (plugins with codex_login).
        case signInCodex
        /// The ChatGPT plan has no voice through Codex: Settings › Voice to pick Grok or an API key.
        case chooseVoice
        /// The board couldn't load its work: load it again (never a call).
        case reloadBoard
    }
    public var title: String
    public var detail: String?
    public var fix: Fix
    public var fixLabel: String {
        switch fix {
        case .openMicSettings: return "Open Settings"
        case .restartCall: return "Restart call"
        case .retry, .reloadBoard: return "Try again"
        case .signInCodex: return CodexLoginCopy.button
        case .chooseVoice: return CodexLoginCopy.chooseVoice
        }
    }

    public init(title: String, detail: String? = nil, fix: Fix) { self.title = title; self.detail = detail; self.fix = fix }

    /// The problem in this state, if it has a fix.
    /// `canSignInCodex`: the server has `codex_login`, so a not-signed-in failure offers the sheet
    /// instead of a Try again that would redial into the same error.
    public static func of(_ s: VoiceState, canSignInCodex: Bool = false) -> CallProblem? {
        let failure: String? = {
            if case .failed(let m) = s.connection { return s.lastError ?? m }
            return nil
        }()
        if let failure {
            if failure.localizedCaseInsensitiveContains("microphone access") {
                return CallProblem(title: "Speakeasy can't use the microphone",
                                   detail: "Turn on Speakeasy under Privacy & Security › Microphone, then try again.",
                                   fix: .openMicSettings)
            }
            if CodexLoginState.errorMeansPlanHasNoVoice(failure) {
                return CallProblem(title: "Your ChatGPT plan has no voice here", detail: CodexLoginCopy.planNoVoice, fix: .chooseVoice)
            }
            if canSignInCodex, CodexLoginState.errorMeansNotSignedIn(failure) {
                return CallProblem(title: "Sign in to ChatGPT to talk", detail: CodexLoginCopy.notSignedIn, fix: .signInCodex)
            }
            if failure.localizedCaseInsensitiveContains("pair") { return nil }   // Settings › Connection says how
            return CallProblem(title: "Couldn't connect", detail: failure.replacingOccurrences(of: " Tap to try again.", with: "").replacingOccurrences(of: " Click Try again.", with: ""),
                               fix: .retry)
        }
        if s.connection == .live, s.micHealth == .broken, s.mic == .live {
            return CallProblem(title: "Your mic isn't reaching the call",
                               detail: "Restarting reopens the voice connection. The conversation and tasks are kept.",
                               fix: .restartCall)
        }
        // The board alone (no call): its own notice, in plain words. The HTTP code the notice may
        // carry is for the iPhone's reworded banner; here the short line says enough.
        if s.workOnly, !s.connection.isInCall, WorkPollGate.isNotice(s.lastError) {
            return CallProblem(title: "Can't load your work right now", detail: "Speakeasy keeps trying.", fix: .reloadBoard)
        }
        return nil
    }
}
