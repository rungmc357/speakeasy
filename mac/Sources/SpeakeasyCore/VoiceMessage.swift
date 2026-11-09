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

    /// Voice notes (servers with `voice_notes`): the recording itself is the message. Stop = sent
    /// (`.sendClip`); nothing goes in the field, and no recognizer event ever ends the recording —
    /// only the user's stop (or the length cap) does, so a pause mid-sentence never cuts it in two.
    public private(set) var sendsOnStop = false

    /// Hermes transcribes the whole clip on stop (servers with `transcribe`, no voice notes): the
    /// on-device words are only a preview, so the recognizer ending never stops the recording.
    public private(set) var wholeClip = false

    public init(permitted: Bool = false, sendsOnStop: Bool = false, wholeClip: Bool = false) {
        self.permitted = permitted; self.sendsOnStop = sendsOnStop; self.wholeClip = wholeClip || sendsOnStop
    }

    public mutating func setWholeClip(_ on: Bool) {
        if case .recording = phase { return }
        wholeClip = on || sendsOnStop
    }

    /// Switch between voice notes and transcribe-then-review (the server's capability can arrive
    /// after the composer appeared). Only while idle.
    public mutating func setSendsOnStop(_ on: Bool) {
        if case .recording = phase { return }
        if case .asking = phase { return }
        sendsOnStop = on
        if on { wholeClip = true }
    }

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

    /// `sendClip`: stop the mic and send the whole recording as the message (voice notes).
    public enum Effect: Equatable, Sendable { case none, askPermissions, startRecording, stopRecording, cancelRecording, sendClip }

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
            guard onDevice || sendsOnStop else { phase = .unavailable(Self.noOnDeviceText); return .none }
            permitted = true
            phase = .recording(started: Date())
            return .startRecording
        case (.recording, .micTapped) where sendsOnStop:
            phase = .idle
            transcript = ""
            return .sendClip
        case (.recording, .micTapped):
            phase = .transcribing
            return .stopRecording
        case (.recording, .partial) where sendsOnStop, (.recording, .final) where sendsOnStop:
            return .none   // nothing on the device decides anything about a voice note
        case (.recording, .final(let text)) where wholeClip:
            // Hermes transcribes the whole clip on stop: a pause that ends the recognizer's segment
            // never ends the recording (it used to stop the mic and keep only the last half).
            transcript = text
            return .none
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

/// `POST /voice/history/{id}/voice-message` / `POST /voice/work/{id}/voice-message` (servers with
/// `voice_notes`): the recording itself as the message, raw body, the message's fields as headers.
/// Hermes transcribes it on arrival like a Telegram voice note; the reply adds `voice.transcript`.
public struct VoiceNoteRequest: Equatable, Sendable {
    /// `newJob`: no conversation yet — the recording starts a new job and chat (Home's composer,
    /// `POST /voice/work/voice-message`, servers with `new_voice_notes`).
    /// `home`: Home's chat coordinator (`POST /voice/home/voice-message`, servers with `home_chat`).
    public enum Target: Equatable, Sendable { case session(String), task(String), newJob, home }
    public var target: Target
    public var audio: Data
    public var mimeType: String
    public var clientID: String
    public var duration: TimeInterval
    public var mode: MessageMode?
    public var answers: String?
    /// Staged attachment ids that go with the recording.
    public var attachments: [String]
    public var replyTo: String? = nil

    public init(target: Target, audio: Data, mimeType: String = "audio/mp4", clientID: String = UUID().uuidString,
                duration: TimeInterval, mode: MessageMode? = nil, answers: String? = nil, attachments: [String] = []) {
        self.target = target; self.audio = audio; self.mimeType = mimeType; self.clientID = clientID
        self.duration = duration; self.mode = mode; self.answers = answers; self.attachments = attachments
    }

    public var path: String {
        switch target {
        case .session(let id): return HistoryConversation.path(id) + "/voice-message"
        case .task(let run): return "/voice/work/\(run)/voice-message"
        case .newJob: return "/voice/work/voice-message"
        case .home: return "/voice/home/voice-message"
        }
    }

    public var headers: [String: String] {
        var out = ["Content-Type": mimeType, "X-Speakeasy-Client-Id": clientID,
                   "X-Speakeasy-Duration": String(format: "%.1f", max(0, min(duration, 600)))]
        if let mode, target != .newJob { out["X-Speakeasy-Mode"] = mode.rawValue }
        if let answers, !answers.isEmpty, case .session = target { out["X-Speakeasy-Answers"] = answers }
        if let replyTo, case .home = target { out["X-Speakeasy-Reply-To"] = replyTo }
        if !attachments.isEmpty { out["X-Speakeasy-Attachments"] = attachments.joined(separator: ",") }
        return out
    }
}

/// The server's word on a voice note: what Hermes heard (nil: it couldn't write it down; the agent
/// still got the clip, as with a Telegram voice note).
public struct VoiceNoteReply: Equatable, Sendable {
    public var transcript: String?
    public var deliveredAs: TaskMessageDelivery
    public var runID: String?
    /// The chat a new job became (`newJob` target).
    public var sessionID: String?
    public init(transcript: String?, deliveredAs: TaskMessageDelivery = .unknown, runID: String? = nil, sessionID: String? = nil) {
        self.transcript = transcript; self.deliveredAs = deliveredAs; self.runID = runID; self.sessionID = sessionID
    }
    public init?(json: [String: Any]) {
        guard json["ok"] as? Bool == true else { return nil }
        let voice = json["voice"] as? [String: Any]
        let words = (voice?["transcript"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
        let session = (json["session_id"] as? String) ?? ((json["task"] as? [String: Any])?["session_id"] as? String)
        self.init(transcript: words,
                  deliveredAs: (json["delivered_as"] as? String).flatMap(TaskMessageDelivery.init(rawValue:)) ?? .unknown,
                  runID: (json["run_id"] as? String).flatMap { $0.isEmpty ? nil : $0 },
                  sessionID: session.flatMap { $0.isEmpty ? nil : $0 })
    }
}

/// `POST /voice/transcribe` (servers with `transcribe`): a voice message's recorded audio as the raw
/// body (`audio/mp4`), answered with `{"text": ...}` from Hermes' own transcription.
public enum VoiceTranscribeRoute {
    public static let path = "/voice/transcribe"
}
