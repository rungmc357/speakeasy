import Foundation

// Listening mode, the pure parts: the controller's states, the dead-mic monitor, the rebuild
// budget, the wording the panel and menu bar show, and the take-off timing (what the call does
// right after listening mode is turned off). No audio or speech imports; views render this.

// MARK: - State

/// Where listening mode stands. It exists only while no call is open: it is not a call phase.
public enum RoomListeningState: Equatable, Sendable {
    /// Not listening, and nothing is held.
    case off
    /// Turned on, but not hearing the room yet. Never shown as listening (Buddy users recorded
    /// whole conversations believing they were being transcribed).
    case preparing(RoomPreparation)
    /// The mic delivers sound and it is being transcribed. `since` is when listening began; it
    /// carries over a call that failed before going live, so the elapsed time continues.
    case listening(since: Date)
    /// The mic is open but delivers digital silence (exact zeros): a dead or blocked input, not a
    /// quiet room. Returns to `listening` as soon as sound arrives. `since` as in `listening`.
    case notHearing(since: Date)
    /// Stopped by itself and couldn't recover (the transcriber or the mic kept failing).
    case failed(RoomFailure)
    /// Can't run on this Mac or setup right now.
    case unavailable(RoomUnavailability)

    /// Listening mode is on: the mic is (being) opened and the transcript is kept.
    public var isOn: Bool {
        switch self {
        case .preparing, .listening, .notHearing: return true
        case .off, .failed, .unavailable: return false
        }
    }

    /// The state once the mic is open, from the dead-mic monitor's reading. `since` is when
    /// listening began (see `listening(since:)`).
    public static func capturing(_ reading: RoomSignalMonitor.Reading, since: Date) -> RoomListeningState {
        switch reading {
        case .waiting: return .preparing(.waitingForSound)
        case .hearing: return .listening(since: since)
        case .silent: return .notHearing(since: since)
        }
    }
}

/// What "preparing" is waiting on.
public enum RoomPreparation: Equatable, Sendable {
    /// Checking the transcriber and loading its model.
    case starting
    /// The on-device speech model is downloading (first use of a language). `fraction` is 0...1
    /// when known.
    case downloadingModel(fraction: Double?)
    /// The mic is open; no sound has arrived yet.
    case waitingForSound
}

/// Why listening mode stopped by itself.
public enum RoomFailure: Equatable, Sendable {
    /// The on-device transcriber kept failing and couldn't be restarted.
    case transcriberStopped
    /// The mic couldn't be reopened (after a device change, for example).
    case micStopped

    public var message: String {
        switch self {
        case .transcriberStopped: return "Transcription kept stopping · turn it on again"
        case .micStopped: return "Lost the microphone · turn it on again"
        }
    }
}

/// Why listening mode can't run. Settings and the panel show `message`.
public enum RoomUnavailability: Equatable, Sendable {
    /// Needs macOS 26 or later (or this build was made without the transcriber).
    case systemTooOld
    /// The paired Hermes plugin doesn't take room text yet.
    case pluginTooOld
    /// This Mac has no on-device transcriber.
    case noTranscriber
    /// The transcriber doesn't cover this language (`language` is its display name).
    case languageNotSupported(language: String)
    /// The speech model couldn't be downloaded.
    case modelDownloadFailed
    /// Speakeasy isn't allowed to use the microphone.
    case micDenied
    /// There's no input device to listen with.
    case noMicrophone

    public var message: String {
        switch self {
        case .systemTooOld: return "Needs macOS 26 or later"
        case .pluginTooOld: return "Your Hermes runs an older Speakeasy plugin: update it, then run hermes voice reload"
        case .noTranscriber: return "On-device transcription isn't available on this Mac"
        case .languageNotSupported(let language): return "On-device transcription doesn't support \(language) yet"
        case .modelDownloadFailed: return "Couldn't download the speech model · check the connection and try again"
        case .micDenied: return "Allow Speakeasy to use the microphone in System Settings › Privacy & Security › Microphone"
        case .noMicrophone: return "No microphone found"
        }
    }
}

/// Things that still work but not as well as they could, shown under the status.
public enum RoomWarning: Hashable, Sendable, CaseIterable {
    /// The input is a Bluetooth headset (AirPods): it mostly hears the wearer, not the room, and
    /// opening its mic switches it to call audio.
    case bluetoothInput
    /// Apple speech recognition is denied. Listening mode itself doesn't need it, but the words
    /// said right after turning it off are caught by the listen-while-connecting capture, which does.
    case speechRecognitionDenied

    public var message: String {
        switch self {
        case .bluetoothInput:
            return "Bluetooth mics like AirPods mostly hear the wearer, not the room, and switch to call audio"
        case .speechRecognitionDenied:
            return "Speech recognition is off for Speakeasy, so words said right after turning it off won't be caught"
        }
    }
}

// MARK: - Dead-mic monitor

/// Tells a quiet room from a dead mic. A working mic always has a noise floor, even in a silent
/// room; a dead, muted or blocked input reads exact zeros. Fed with each buffer's peak level and
/// ticked by a timer (so buffers that stop arriving altogether also count as silence).
/// Pure and clock-driven, like `MicCheck`.
public struct RoomSignalMonitor: Equatable, Sendable {
    public enum Reading: Equatable, Sendable {
        /// Nothing heard yet, and not long enough to call it dead.
        case waiting
        /// Sound has arrived recently.
        case hearing
        /// Only digital silence (or no audio at all) for `silentAfter`.
        case silent
    }

    /// Peaks at or below this are "no samples at all", not "quiet room" (same as `MicCheck`).
    public static let deadLevel = MicCheck.deadLevel
    /// This long without a single real sample means the mic isn't delivering.
    public static let silentAfter: TimeInterval = 10

    public private(set) var reading: Reading = .waiting
    private var watchingSince: Date?
    private var lastSound: Date?

    public init() {}

    /// One buffer's peak level (linear, 0...1).
    @discardableResult
    public mutating func sample(level: Double, now: Date) -> Reading {
        if watchingSince == nil { watchingSince = now }
        if level > Self.deadLevel { lastSound = now }
        return tick(now: now)
    }

    /// Re-judge without a new buffer.
    @discardableResult
    public mutating func tick(now: Date) -> Reading {
        if watchingSince == nil { watchingSince = now }
        let quietSince = lastSound ?? watchingSince!
        if now.timeIntervalSince(quietSince) >= Self.silentAfter {
            reading = .silent
        } else {
            reading = lastSound == nil ? .waiting : .hearing
        }
        return reading
    }

    /// The mic was reopened (new device or engine): judge it afresh, keeping what was learned
    /// about sound so a working mic doesn't flash back to "waiting".
    public mutating func reopened(now: Date) {
        watchingSince = now
        if lastSound != nil { lastSound = now }
        tick(now: now)
    }
}

// MARK: - Rebuild budget

/// How many times listening mode restarts a failed transcriber (or mic) before giving up: a
/// third failure within a minute ends in `failed`.
public struct RoomRebuildBudget: Equatable, Sendable {
    public static let maxFailures = 3
    public static let window: TimeInterval = 60

    public private(set) var failures: [Date] = []

    public init() {}

    /// Records one failure. True when another restart may be tried; false when it should give up.
    public mutating func recordFailure(now: Date) -> Bool {
        failures = failures.filter { now.timeIntervalSince($0) < Self.window }
        failures.append(now)
        return failures.count < Self.maxFailures
    }

    /// Wait this long before the next restart (grows with recent failures).
    public var retryDelay: TimeInterval { 0.5 * Double(max(1, failures.count)) }
}

// MARK: - Presentation

/// What the panel strip and the menu bar show for listening mode.
public struct RoomPresentation: Equatable, Sendable {
    public var title: String
    /// Elapsed time and what's going on, or the reason it can't run.
    public var detail: String
    /// Shown while on, so nobody waits for an answer that won't come.
    public var hint: String?
    public var tone: StatusTone
    /// Shown under the status while on, in a fixed order.
    public var warnings: [String]
    /// The mic is held for listening mode (the menu bar shows it's on).
    public var isOn: Bool

    public init(title: String, detail: String, hint: String? = nil, tone: StatusTone = .plain,
                warnings: [String] = [], isOn: Bool) {
        self.title = title; self.detail = detail; self.hint = hint
        self.tone = tone; self.warnings = warnings; self.isOn = isOn
    }
}

/// The wording for `state`. `heardWords` is whether the transcript holds any words yet;
/// `warnings` are shown only while listening mode is on.
public func presentRoom(_ state: RoomListeningState, now: Date, heardWords: Bool,
                        warnings: Set<RoomWarning> = []) -> RoomPresentation {
    let notAnswering = "Won't answer until you turn it off"
    let shownWarnings = state.isOn ? RoomWarning.allCases.filter(warnings.contains).map(\.message) : []
    func make(_ title: String, _ detail: String, hint: String? = nil, tone: StatusTone = .plain) -> RoomPresentation {
        RoomPresentation(title: title, detail: detail, hint: hint, tone: tone,
                         warnings: shownWarnings, isOn: state.isOn)
    }
    switch state {
    case .off:
        return make("Listening mode off", "")
    case .preparing(let step):
        let detail: String
        switch step {
        case .starting: detail = "Getting ready · not hearing yet"
        case .downloadingModel(let fraction?):
            detail = "Downloading the speech model · \(Int((min(max(fraction, 0), 1) * 100).rounded()))%"
        case .downloadingModel(nil): detail = "Downloading the speech model"
        case .waitingForSound: detail = "Waiting for sound from the mic"
        }
        return make("Starting listening mode…", detail, hint: notAnswering)
    case .listening(let since):
        let elapsed = formatElapsed(now.timeIntervalSince(since))
        return make("Listening mode on", heardWords ? elapsed : "\(elapsed) · nothing heard yet", hint: notAnswering)
    case .notHearing:
        return make("Listening mode can't hear anything", "No sound from the mic · check the input in Sound settings",
                    hint: notAnswering, tone: .warning)
    case .failed(let failure):
        return make("Listening mode stopped", failure.message, tone: .error)
    case .unavailable(let reason):
        return make("Listening mode unavailable", reason.message, tone: .warning)
    }
}

// MARK: - Take-off

/// What the call should do about the request, right after listening mode was turned off.
public enum RoomTakeoffDecision: Equatable, Sendable {
    /// The listen-while-connecting capture caught words: they are the request.
    case handOverWords
    /// The call isn't live yet: decide again once it is.
    case waitForLive
    /// Nothing said yet: decide again at this time.
    case waitUntil(Date)
    /// Nothing said: ask the voice to respond from what it heard in the room.
    case nudgeNow
    /// The user has started talking on the live call: never nudge over them.
    case skip
}

/// The timing rule for a call started by turning listening mode off. Words said while the call
/// connects, or within `quietWindow` of turning listening off, are the request; if none come,
/// the voice responds from the room context, at take-off + `quietWindow` or as soon as the call is
/// live, whichever is later. Speech on the live call's mic cancels that for good.
public enum RoomTakeoffPolicy {
    /// How long after take-off silence means "respond from the room".
    public static let quietWindow: TimeInterval = 2
    /// A live-call mic level at or above this counts as the user talking (WebRTC's linear 0...1
    /// audio level, the same "clear speech" bar `MicCheck` uses).
    public static let speechLevel = MicCheck.speechLevel

    /// - Parameters:
    ///   - takeoffAt: when listening mode was turned off.
    ///   - liveAt: when the call went live (nil while it's still connecting).
    ///   - heardWords: the listen-while-connecting capture heard words.
    ///   - speechSinceLive: speech-level sound on the live call's mic since it went live.
    public static func decide(takeoffAt: Date, liveAt: Date?, now: Date,
                              heardWords: Bool, speechSinceLive: Bool) -> RoomTakeoffDecision {
        if heardWords { return .handOverWords }
        guard let liveAt else { return .waitForLive }
        if speechSinceLive { return .skip }
        let nudgeAt = max(takeoffAt.addingTimeInterval(quietWindow), liveAt)
        return now >= nudgeAt ? .nudgeNow : .waitUntil(nudgeAt)
    }
}

// MARK: - The call listening mode was turned off into

/// How a call started by turning listening mode off turned out, for listening mode.
public enum RoomTakeoffOutcome: Sendable, Equatable {
    /// The call went live with what was heard: listening mode can let its transcript go.
    case wentLive
    /// The call failed (or lost its connection) before it went live: listening mode resumes with
    /// its transcript intact.
    case failedBeforeLive
    /// The user ended the call before it went live.
    case endedBeforeLive
    /// The server couldn't take what was heard (an older plugin): the call goes on without it.
    case notUsed
}

/// One call started by turning listening mode off: what it carries, and how far its take-off got.
/// Pure (the call client owns the timers, the mic probe and the network); one per call, gone when
/// the call ends. Every leg of the call (connect retry, resume, device switch, mic repair) shares it.
public struct RoomCall: Equatable, Sendable {
    /// What listening mode heard (rendered "[HH:MM] text" lines), sent with each new admission.
    public let room: String
    /// When listening mode was turned off.
    public let takeoffAt: Date
    /// When the call first went live (nil while it's still connecting).
    public private(set) var liveAt: Date?
    /// The user has been heard on the live call: never answer from the room over them.
    public private(set) var speechSinceLive = false
    /// The request after take-off is settled (words handed over, or the nudge sent or skipped).
    /// Later legs of the call never repeat it.
    public private(set) var requestHandled = false
    /// The user ended the call before it went live (vs. it failing on its own).
    public private(set) var endRequested = false
    /// Words the connecting listener had heard when a stalled call was retried without it.
    public var carriedWords: String?
    /// The server has the room text (an admission carrying it succeeded).
    public private(set) var delivered = false
    private var loudSamples = 0
    private var outcomeReported = false

    /// Speech-level mic samples in a row (~0.1 s apart) that mean the user is talking; a single
    /// loud sample (a cough, a door) doesn't.
    public static let speechSamples = 3

    public init(room: String, takeoffAt: Date) {
        self.room = room
        self.takeoffAt = takeoffAt
    }

    public var wentLive: Bool { liveAt != nil }

    public mutating func noteLive(at now: Date) {
        if liveAt == nil { liveAt = now }
    }

    /// One reading of the live call's mic level (WebRTC's linear 0...1).
    public mutating func noteMicLevel(_ level: Double) {
        loudSamples = level >= RoomTakeoffPolicy.speechLevel ? loudSamples + 1 : 0
        if wentLive && loudSamples >= Self.speechSamples { speechSinceLive = true }
    }

    /// The call transcribed the user's own words after it went live.
    public mutating func noteHeardUser() {
        if wentLive { speechSinceLive = true }
    }

    public mutating func markRequestHandled() { requestHandled = true }

    public mutating func markDelivered() { delivered = true }

    /// Whether this admission carries the room text: every new admission does (first try, connect
    /// retry, a resume that fell back to a new call); a resume only until the server has it.
    public func sends(resuming: Bool) -> Bool { !resuming || !delivered }

    /// The user asked to end the call; it counts only before the call went live.
    public mutating func noteEndRequested() {
        if !wentLive { endRequested = true }
    }

    /// What happens next with nothing said (the nudge loop asks this every ~0.1 s).
    public func nextStep(now: Date) -> RoomTakeoffDecision {
        if requestHandled { return .skip }
        return RoomTakeoffPolicy.decide(takeoffAt: takeoffAt, liveAt: liveAt, now: now,
                                        heardWords: false, speechSinceLive: speechSinceLive)
    }

    /// How the call's connection changed, as far as listening mode cares.
    public enum Transition: Sendable { case wentLive, ended, failed }

    /// What to tell listening mode about `transition`, at most once per call: going live gives the
    /// transcript up; before that, only an end the user asked for does, while a failure or a lost
    /// connection (which also ends a connecting call) gives it back.
    public mutating func outcome(for transition: Transition) -> RoomTakeoffOutcome? {
        guard !outcomeReported else { return nil }
        let outcome: RoomTakeoffOutcome?
        switch transition {
        case .wentLive: outcome = .wentLive
        case .ended: outcome = wentLive ? nil : (endRequested ? .endedBeforeLive : .failedBeforeLive)
        case .failed: outcome = wentLive ? nil : .failedBeforeLive
        }
        if outcome != nil { outcomeReported = true }
        return outcome
    }

    /// The server couldn't take the room: tell listening mode once, unless it already heard.
    public mutating func notUsed() -> RoomTakeoffOutcome? {
        guard !outcomeReported else { return nil }
        outcomeReported = true
        return .notUsed
    }
}
