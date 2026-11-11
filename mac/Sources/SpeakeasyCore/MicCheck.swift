import Foundation

/// Whether the call's microphone is really reaching the voice.
/// "Listening" is only shown once it is; a dead mic is repaired automatically.
public enum MicHealth: Equatable, Sendable {
    /// Call just went live; waiting for proof the mic works.
    case checking
    /// Sound is arriving from the mic and being sent.
    case ok
    /// The mic wasn't getting through; the voice connection is being reopened.
    case repairing
    /// Still not working after reopening; the user has to act.
    case broken
}

/// Why the mic was judged dead (for logs and tests).
public enum MicFault: String, Equatable, Sendable {
    case noSound = "no sound from the mic"
    case notSending = "mic audio not being sent"
    case unanswered = "spoke but the voice never heard it"
}

/// What to do about a dead mic (74.1, F4(a)).
public enum MicRepairStep: Equatable, Sendable {
    /// Restart the device's audio unit on the SAME connection (iPhone: toggle WebRTC's audio and
    /// re-activate the audio session). No new server session; about a second instead of ~10.
    case restartAudio
    /// Reopen the voice connection (a new server session).
    case reconnect
}

/// Checks, from the device's own call stats, that the mic is really getting through:
///  1. the mic delivers samples (a working mic never reads a flat zero, even in a silent room);
///  2. those samples are leaving the device (audio packets sent keeps growing);
///  3. when you clearly speak, the voice hears you (a transcript or a reply follows).
/// Pure and clock-driven so every rule is testable without audio.
public struct MicCheck: Sendable {
    /// Levels at or below this count as "no samples at all" (not "quiet room").
    public static let deadLevel = 0.000_01
    /// Clear speech (WebRTC's linear 0...1 audio level).
    public static let speechLevel = 0.05
    /// A newly live, unmuted call must show sound and outgoing audio within this long.
    public static let firstSoundWithin: TimeInterval = 2.5
    /// iPhone/Vision Pro: after the on-device listener hands the mic over, the call's audio unit
    /// has to start (and AirPods switch to call mode, 1–3 s). 2.5 s read that start-up as a dead
    /// mic and reconnected every call. Give it room; a really dead mic still shows by then.
    public static let firstSoundWithinHandheld: TimeInterval = 7
    /// Later: this long of flat zero, or of no audio sent, means the mic died mid-call.
    public static let deadAfter: TimeInterval = 4
    /// Speech at least this long counts as talking to the assistant…
    public static let utteranceMin: TimeInterval = 1.2
    /// …and ends after this much quiet…
    public static let utteranceGap: TimeInterval = 0.7
    /// …and the voice must show it heard within this long after it ends.
    public static let answerWithin: TimeInterval = 10
    /// Automatic reopen attempts per call before asking the user to restart.
    public static let maxRepairs = 2
    /// After restarting the audio unit in place, sound must show within this long, or the
    /// connection is reopened. (The first-sound window itself is unchanged.)
    public static let restartGrace: TimeInterval = 2.5

    public private(set) var health: MicHealth = .checking
    public private(set) var repairs = 0
    public private(set) var lastFault: MicFault?
    /// The audio unit was restarted in place on this connection (at most once per connection).
    public private(set) var restartedAudio = false
    /// The last fault came before the mic ever gave sound on this connection (the first-sound window).
    public private(set) var lastFaultInFirstWindow = false
    private var watchingSince: Date?
    private var lastSound: Date?
    private var lastPackets: Int?
    private var lastPacketsGrew: Date?
    private var speechStart: Date?
    private var lastSpeech: Date?
    private var unansweredSince: Date?
    /// The voice has heard the user on this connection. Rule 3 only guards the start of a
    /// connection (the failure seen: calls that never register a word), so a noisy room
    /// mid-conversation can't trigger needless reconnects.
    private var heardAny = false
    /// The first-sound grace after an in-place restart (`restartGrace`); nil = `firstSoundGrace`.
    private var graceOverride: TimeInterval?

    /// How long a new connection may stay silent before it counts as dead.
    public var firstSoundGrace: TimeInterval
    /// The last sample judged (for the repair report).
    public private(set) var lastLevel: Double?
    public private(set) var lastPacketCount: Int?
    public private(set) var watchedFor: TimeInterval = 0

    /// The device can restart its audio unit in place (iPhone, Vision Pro); the Mac reopens.
    public var canRestartAudio: Bool

    public init(firstSoundGrace: TimeInterval = MicCheck.firstSoundWithin, canRestartAudio: Bool = false) {
        self.firstSoundGrace = firstSoundGrace
        self.canRestartAudio = canRestartAudio
    }

    /// A voice connection is live (call start, or after a repair): judge it afresh.
    public mutating func connectionOpened() {
        reset()
        restartedAudio = false
        if health != .broken { health = .checking }
    }

    /// What to do about `fault`: no sound during the first-sound window is first met by restarting
    /// the audio unit on the same connection (once per connection, and only where the device can);
    /// everything else, or a restart that didn't help, reopens the connection.
    public func repairStep(for fault: MicFault) -> MicRepairStep {
        guard canRestartAudio, fault == .noSound, lastFaultInFirstWindow, !restartedAudio else { return .reconnect }
        return .restartAudio
    }

    /// The audio unit is being restarted in place: judge the same connection afresh, with
    /// `restartGrace` for the first sound. Not a reopen (doesn't count toward `maxRepairs`).
    public mutating func audioRestarted() {
        restartedAudio = true
        reset()
        graceOverride = Self.restartGrace
        if health != .broken { health = .checking }
    }

    /// The in-place restart couldn't run (the session wouldn't re-activate): the connection is
    /// reopened instead, unless the reopens already ran out (then the mic is broken).
    public mutating func audioRestartFailed() {
        restartedAudio = true
        if repairs >= Self.maxRepairs { health = .broken }
    }

    /// A repair (reopen of the voice connection) is starting.
    public mutating func repairStarted() {
        repairs += 1
        health = .repairing
        reset()
    }

    /// The voice showed it heard the user (a transcript of their words, or it replied).
    public mutating func heard() {
        heardAny = true
        unansweredSince = nil
        speechStart = nil
    }

    /// Feed one stats sample. `listening` = call live, mic unmuted, the call owns the mic and the
    /// assistant isn't talking. `packetsSent` = outgoing audio packets so far (nil when unknown).
    /// Returns a fault when the mic is judged dead and a repair should start now.
    public mutating func sample(level: Double?, packetsSent: Int?, listening: Bool, now: Date) -> MicFault? {
        guard listening, health != .repairing, health != .broken else {
            // Nothing to judge; restart the clocks when listening resumes.
            watchingSince = nil; speechStart = nil
            return nil
        }
        if watchingSince == nil { watchingSince = now }
        let since = watchingSince!
        lastLevel = level; lastPacketCount = packetsSent; watchedFor = now.timeIntervalSince(since)
        if let packetsSent {
            if lastPackets == nil || packetsSent > lastPackets! { lastPacketsGrew = now }
            lastPackets = packetsSent
        }
        // No level reported at all (stats shape unknown): don't guess, assume fine.
        guard let level else { if health == .checking { health = .ok }; return nil }
        if level > Self.deadLevel { lastSound = now }

        let soundOK = lastSound.map { now.timeIntervalSince($0) < Self.deadAfter } ?? false
        let sendingOK = packetsSent == nil || (lastPacketsGrew.map { now.timeIntervalSince($0) < Self.deadAfter } ?? false)
        if soundOK && sendingOK && health == .checking { health = .ok }

        let firstWindow = lastSound == nil
        let grace = firstWindow ? (graceOverride ?? firstSoundGrace) : Self.deadAfter
        if now.timeIntervalSince(since) >= grace {
            // Once the voice has heard you on this connection, the mic is proven: a stretch of
            // pure zeros after that is a headset gating silence while you pause (AirPods do),
            // not a dead mic. Only audio that stops leaving the device still counts then.
            if !soundOK && !heardAny { return fault(.noSound, firstWindow: firstWindow) }
            if !sendingOK { return fault(.notSending, firstWindow: firstWindow) }
        }

        // Clear speech that the voice never acknowledged.
        if heardAny { return nil }
        if level >= Self.speechLevel {
            if speechStart == nil { speechStart = now }
            lastSpeech = now
        } else if let start = speechStart, let last = lastSpeech, now.timeIntervalSince(last) >= Self.utteranceGap {
            if last.timeIntervalSince(start) >= Self.utteranceMin, unansweredSince == nil { unansweredSince = last }
            speechStart = nil
        }
        if let pending = unansweredSince, now.timeIntervalSince(pending) >= Self.answerWithin {
            return fault(.unanswered, firstWindow: false)
        }
        return nil
    }

    private mutating func fault(_ kind: MicFault, firstWindow: Bool) -> MicFault? {
        lastFault = kind
        lastFaultInFirstWindow = firstWindow
        // An in-place restart is still open to a first-window "no sound" even after the reopens ran out.
        if repairs >= Self.maxRepairs, repairStep(for: kind) == .reconnect {
            health = .broken
            return nil
        }
        return kind
    }

    private mutating func reset() {
        watchingSince = nil; lastSound = nil; lastPackets = nil; lastPacketsGrew = nil
        speechStart = nil; lastSpeech = nil; unansweredSince = nil; heardAny = false
        graceOverride = nil
    }
}
