import Foundation

/// The composer's voice message, when no call is open: tap the mic to record, tap again to stop,
/// review and edit the transcript in the text field, then send it like typed text. Cancel any time.
/// Transcription is on-device only; if this device can't do it for the user's language, the
/// composer says so plainly and the user types instead. During a call the mic is hidden (they just
/// talk), so none of this runs then.
///
/// A pure state machine: the view feeds it events from the recorder and the recognizer.
public struct VoiceMessageFlow: Equatable, Sendable {
    public enum Phase: Equatable, Sendable {
        case idle
        /// First tap: the mic / speech-recognition permission prompts are up.
        case asking
        case recording(started: Date)
        /// Stopped; the on-device recognizer is finishing the transcript.
        case transcribing
        /// The words are in the field to review and edit before sending.
        case reviewing
        case unavailable(String)
    }

    public enum Event: Equatable, Sendable {
        case micTapped(now: Date)
        case permissions(granted: Bool, onDevice: Bool)
        case partial(String)
        case final(String)
        case failed(String)
        case cancel
        case sent
    }

    public static let maxSeconds: TimeInterval = 120
    public static let deniedText = "Speakeasy needs the microphone and speech recognition for voice messages. Turn them on in Settings."
    public static let noOnDeviceText = "This device can't turn speech into text on its own for your language, so voice messages are off. You can still type."
    public static let nothingHeardText = "I didn't catch anything. Try again, or type it."

    public private(set) var phase: Phase = .idle
    /// What's been heard so far (live while recording, then the editable transcript).
    public private(set) var transcript = ""
    /// Permissions already granted once: the next tap starts recording straight away.
    public private(set) var permitted = false
    /// Text that was in the field before recording started; the transcript is appended to it.
    public private(set) var draftBefore = ""

    public init(permitted: Bool = false) { self.permitted = permitted }

    public var isRecording: Bool { if case .recording = phase { return true }; return false }
    public var isBusy: Bool {
        switch phase { case .asking, .recording, .transcribing: return true; default: return false }
    }
    public var unavailableMessage: String? { if case .unavailable(let m) = phase { return m }; return nil }

    /// What the field shows: the earlier draft plus the words.
    public var fieldText: String {
        let words = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        if draftBefore.isEmpty { return words }
        if words.isEmpty { return draftBefore }
        return draftBefore + (draftBefore.hasSuffix(" ") ? "" : " ") + words
    }

    /// Elapsed recording time (for the timer); nil unless recording.
    public func elapsed(now: Date) -> TimeInterval? {
        if case .recording(let started) = phase { return max(0, now.timeIntervalSince(started)) }
        return nil
    }

    /// Recording should stop on its own (the cap).
    public func shouldAutoStop(now: Date) -> Bool { (elapsed(now: now) ?? 0) >= Self.maxSeconds }

    public enum Effect: Equatable, Sendable { case none, askPermissions, startRecording, stopRecording, cancelRecording }

    /// Apply an event; returns what the view must do next.
    @discardableResult
    public mutating func handle(_ event: Event, draft: String = "") -> Effect {
        switch (phase, event) {
        case (.idle, .micTapped(let now)), (.reviewing, .micTapped(let now)), (.unavailable, .micTapped(let now)):
            draftBefore = draft.trimmingCharacters(in: .whitespacesAndNewlines)
            transcript = ""
            if permitted {
                phase = .recording(started: now)
                return .startRecording
            }
            phase = .asking
            return .askPermissions
        case (.asking, .permissions(let granted, let onDevice)):
            guard granted else { phase = .unavailable(Self.deniedText); return .none }
            guard onDevice else { phase = .unavailable(Self.noOnDeviceText); return .none }
            permitted = true
            phase = .recording(started: Date())
            return .startRecording
        case (.recording, .micTapped):
            phase = .transcribing
            return .stopRecording
        case (.recording, .partial(let text)), (.transcribing, .partial(let text)):
            transcript = text
            return .none
        case (.recording, .final(let text)), (.transcribing, .final(let text)):
            let wasRecording = isRecording   // the recognizer ended on its own: stop the mic too
            transcript = text
            if transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                phase = draftBefore.isEmpty ? .unavailable(Self.nothingHeardText) : .reviewing
            } else {
                phase = .reviewing
            }
            return wasRecording ? .stopRecording : .none
        case (.asking, .failed(let message)), (.recording, .failed(let message)), (.transcribing, .failed(let message)):
            phase = .unavailable(message)
            return .cancelRecording
        case (_, .cancel):
            let wasBusy = isBusy
            phase = .idle
            transcript = ""
            return wasBusy ? .cancelRecording : .none
        case (_, .sent):
            phase = .idle
            transcript = ""
            draftBefore = ""
            return .none
        default:
            return .none
        }
    }

    /// "0:07" for the timer.
    public static func clock(_ seconds: TimeInterval) -> String {
        let s = Int(seconds.rounded(.down))
        return String(format: "%d:%02d", s / 60, s % 60)
    }
}

/// `POST /voice/transcribe` (servers with `transcribe`): a voice message's recorded audio as the raw
/// body (`audio/mp4`), answered with `{"text": ...}` from Hermes' own transcription.
public enum VoiceTranscribeRoute {
    public static let path = "/voice/transcribe"
}
