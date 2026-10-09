import AppKit
import Speech
import SpeakeasyClient
import SpeakeasyCore

/// Listening mode on the Mac: owns the on-device room listener and what it heard, decides when it
/// may run, and turns it off into a call. It never answers; the call it's turned off into does.
///
/// It exists only during a call. Turning it on pauses the call (the voice stops hearing and
/// answering, nothing is billed); turning it off resumes that conversation with what was heard.
/// Every way of resuming or starting a call while it's on (button, menu, shortcut) turns it off
/// into that call. Ending the call stops it. What it heard stays in memory only, on this Mac.
@MainActor
final class RoomListeningController {
    /// Listening stops by itself after this long (battery, and nobody forgets it's on).
    static let maxSession: TimeInterval = 2 * 60 * 60

    /// Something the panel, menu or menu bar shows changed.
    var onChange: (() -> Void)?
    /// Turn listening off into a call: the rendered room transcript ("" when nothing was heard)
    /// and when listening was turned off.
    var onTakeoff: ((_ room: String, _ at: Date) -> Void)?
    /// Bring the panel forward (listening started, or a note about it to read).
    var onShowPanel: (() -> Void)?
    /// The paired plugin takes room text with a call (`room_listening` in its status); nil when
    /// the status isn't known (Hermes unreachable, or not asked yet).
    var pluginSupportsRoom: () -> Bool? = { nil }
    /// Ask the server for its status again (a launch-time answer goes stale: Hermes unreachable
    /// then, or the plugin updated since).
    var refreshStatus: () async -> Void = {}
    /// A call is connecting or ending (listening waits for it to settle).
    var callBusy: () -> Bool = { false }
    /// A call is live or paused: listening mode can only be turned on then.
    var callActive: () -> Bool = { false }
    /// Free the mic before listening starts: pause a live call (false if it couldn't be paused).
    var prepareMic: () async -> Bool = { true }

    private let listener = RoomListener()
    /// When the listening that was turned off into a call began, held until the call reports back
    /// (it resumes from there if the call fails before it goes live).
    private var pendingSince: Date?
    private var awaitingCall = false
    /// The call didn't come back after listening was turned off: what was heard is kept (not
    /// listening) so it can still be asked about, which starts a call, or discarded.
    private var heldAfterCall = false
    private(set) var notice: String?
    private var activity: NSObjectProtocol?
    private var capTimer: Timer?
    private var tickTimer: Timer?
    private var observers: [NSObjectProtocol] = []

    init() {
        listener.onStateChange = { [weak self] state in
            guard let self else { return }
            if !state.isOn, !self.awaitingCall {
                self.endSessionChrome()
                if self.heldAfterFailure { self.expireHeldTranscript() }
            }
            self.onChange?()
        }
        listener.onInputChange = { [weak self] _ in self?.onChange?() }
        listener.onLog = { line in NSLog("speakeasy listening: %@", line) }
        let center = NSWorkspace.shared.notificationCenter
        for name in [NSWorkspace.willSleepNotification, NSWorkspace.sessionDidResignActiveNotification] {
            observers.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor [weak self] in self?.systemTookTheMac() }
            })
        }
        observers.append(center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.notice != nil else { return }
                self.onShowPanel?()
            }
        })
    }

    // MARK: State

    var state: RoomListeningState { listener.state }
    /// Listening mode is on (the mic is held for it).
    var isOn: Bool { listener.state.isOn }
    /// Listening stopped by itself (the mic or transcriber kept failing) but kept what it heard.
    var heldAfterFailure: Bool {
        if case .failed = listener.state { return !listener.transcript.isEmpty }
        return false
    }
    /// What it heard is kept without listening (after a failure), to ask about or discard.
    var held: Bool { heldAfterFailure || heldAfterCall }
    /// What it heard can be turned into the call (or discarded): while on, or while held.
    var canAsk: Bool { isOn || held }

    /// Why it can't be turned on right now, or nil when it can. An unknown plugin status isn't a
    /// reason: turning it on asks the server again first.
    var blocker: String? {
        if !RoomListener.isSupported { return RoomUnavailability.systemTooOld.message }
        if pluginSupportsRoom() == false { return RoomUnavailability.pluginTooOld.message }
        if callBusy() { return "Wait until the call has connected" }
        if !callActive() && !held { return "Listening mode works during a call" }
        return nil
    }

    /// Offered at all on this Mac (the panel hides the button otherwise).
    var isSupported: Bool { RoomListener.isSupported }

    /// What the strip shows, or nil when listening mode is off with nothing to say.
    func presentation(now: Date = Date()) -> RoomPresentation? {
        let state = listener.state
        if heldAfterCall {
            return RoomPresentation(title: "Listening mode kept what it heard", detail: "The call didn't come back",
                                    hint: "Ask about it (starts a call) or discard it · kept for 30 minutes",
                                    tone: .warning, warnings: [], isOn: false)
        }
        guard state != .off else { return nil }
        var shown = presentRoom(state, now: now, heardWords: !listener.transcript.isEmpty, warnings: warnings)
        if heldAfterFailure {
            shown.hint = "What it heard is kept for 30 minutes: ask about it, turn it on again, or discard it"
        }
        return shown
    }

    private var warnings: Set<RoomWarning> {
        var out: Set<RoomWarning> = []
        if listener.inputIsBluetooth { out.insert(.bluetoothInput) }
        switch SFSpeechRecognizer.authorizationStatus() {
        case .denied, .restricted: out.insert(.speechRecognitionDenied)
        default: break
        }
        return out
    }

    // MARK: Turning it on and off

    /// The button / menu / shortcut: on when off, off into a call when on.
    func toggle() { isOn ? takeOff() : turnOn() }

    func turnOn() {
        guard !isOn, !awaitingCall, !heldAfterCall else { return }
        // Only during a call, once it's settled; the button and menu item already say why.
        if callBusy() || !callActive() { return }
        if !RoomListener.isSupported { show(RoomUnavailability.systemTooOld.message); return }
        if pluginSupportsRoom() == true { start(); return }
        // Unknown, or an answer that may be stale: ask the server again before saying no.
        Task { @MainActor [weak self] in
            guard let self else { return }
            await self.refreshStatus()
            guard !self.isOn, !self.awaitingCall, !self.callBusy(), self.callActive() else { return }
            switch self.pluginSupportsRoom() {
            case true?: self.start()
            case false?: self.show(RoomUnavailability.pluginTooOld.message)
            case nil: self.show("Can't reach Hermes right now · try again in a moment")
            }
        }
    }

    private func start() {
        guard explainOnce() else { return }
        notice = nil
        Task { @MainActor [weak self] in
            guard let self else { return }
            // Mid-conversation: the call pauses first, so the voice stops hearing and answering.
            guard await self.prepareMic() else {
                self.show("Couldn't pause the call, so listening mode didn't start")
                return
            }
            guard !self.isOn, !self.awaitingCall else { return }
            // After a failure, turning it on again carries on with what it had heard.
            self.begin(continuing: self.heldAfterFailure ? self.listener.transcript : RoomTranscript(), since: nil)
        }
    }

    private func show(_ note: String) {
        notice = note
        onShowPanel?()
        onChange?()
    }

    /// Turn listening off into a call that carries what it heard. The mic is released before the
    /// call starts (the call opens it next); the snapshot includes the words still in progress.
    /// Also works after listening stopped by itself, with what it had heard until then.
    func takeOff() {
        guard canAsk else { return }
        let at = Date()
        let since = state.since
        heldAfterCall = false
        notice = nil   // an older note (e.g. "the call didn't connect") no longer applies
        let heard = listener.transcript   // the words in progress are its volatile tail
        _ = listener.stop()
        endSessionChrome()
        let text = heard.snapshot(now: at).text
        if text.isEmpty {
            // Nothing heard: an ordinary call (still unmuted, you turned it off to talk).
            listener.discard()
        } else {
            awaitingCall = true
            pendingSince = since
        }
        onChange?()
        onTakeoff?(text, at)
    }

    /// Stop listening and forget everything it heard, without a call.
    func discard() {
        listener.discard()
        heldAfterCall = false
        awaitingCall = false
        pendingSince = nil
        notice = nil
        endSessionChrome()
        onChange?()
    }

    func dismissNotice() {
        notice = nil
        onChange?()
    }

    /// The call listening mode was turned off into reports back.
    func callOutcome(_ outcome: RoomTakeoffOutcome) {
        guard awaitingCall else { return }
        awaitingCall = false
        switch outcome {
        case .failedBeforeLive:
            // Nothing heard is lost, but listening doesn't start again by itself (it only runs
            // during a call): keep what was heard to ask about (which starts a call) or discard.
            pendingSince = nil
            heldAfterCall = true
            notice = nil
            expireHeldTranscript()
            onShowPanel?()
        case .wentLive, .endedBeforeLive, .notUsed:
            // The call has (or had its chance at) the transcript: let it go here.
            pendingSince = nil
            notice = nil
            listener.discard()
            endSessionChrome()
        }
        onChange?()
    }

    // MARK: Session chrome (sleep assertion, 2-hour cap, elapsed-time tick)

    private func begin(continuing transcript: RoomTranscript, since: Date?) {
        listener.start(continuing: transcript, since: since)
        guard listener.state.isOn else { onShowPanel?(); onChange?(); return }
        // Meetings have no keyboard activity: keep idle sleep from stopping listening mid-meeting.
        // The screen may still sleep and lock; real sleep stops listening (systemTookTheMac).
        if activity == nil {
            activity = ProcessInfo.processInfo.beginActivity(options: [.userInitiated, .idleSystemSleepDisabled],
                                                            reason: "Listening mode is on")
        }
        let started = since ?? Date()
        capTimer?.invalidate()
        capTimer = Timer.scheduledTimer(withTimeInterval: max(1, Self.maxSession - Date().timeIntervalSince(started)),
                                        repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.stopAndDiscard(note: "Listening mode stopped after 2 hours · what it heard was discarded")
            }
        }
        tickTimer?.invalidate()
        tickTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.onChange?() }
        }
        onShowPanel?()
        onChange?()
    }

    private func endSessionChrome() {
        if let activity { ProcessInfo.processInfo.endActivity(activity) }
        activity = nil
        capTimer?.invalidate(); capTimer = nil
        tickTimer?.invalidate(); tickTimer = nil
    }

    /// Sleep or a switch to another user: listening stops and what it heard is dropped. That
    /// includes a transcript held for a call that's still connecting, so a call failing after wake
    /// never reopens the mic by itself.
    private func systemTookTheMac() {
        if awaitingCall {
            awaitingCall = false
            pendingSince = nil
            listener.discard()
            return
        }
        guard canAsk else { return }
        stopAndDiscard(note: "Listening mode stopped when your Mac went to sleep · what it heard was discarded")
    }

    /// A transcript kept after a failure goes after 30 minutes, like anything older would anyway.
    private func expireHeldTranscript() {
        capTimer?.invalidate()
        capTimer = Timer.scheduledTimer(withTimeInterval: RoomTranscript.window, repeats: false) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.held else { return }
                self.stopAndDiscard(note: "What listening mode heard before it stopped was discarded after 30 minutes")
            }
        }
    }

    /// The call ended while listening mode was on (or held what it heard after stopping by
    /// itself): listening mode only exists during a call, so it stops and forgets.
    func callEnded() {
        guard isOn || heldAfterFailure else { return }
        stopAndDiscard(note: "The call ended, so listening mode stopped · what it heard was discarded")
    }

    private func stopAndDiscard(note: String) {
        guard canAsk else { return }
        discard()
        notice = note
        onShowPanel?()
        onChange?()
    }

    // MARK: First use

    /// Once, before listening mode is first turned on: what it does, what leaves the Mac, and to
    /// tell the people in the room. Also asks for speech recognition, which catches the words said
    /// right after turning it off (listening itself doesn't need it). False when cancelled.
    private func explainOnce() -> Bool {
        guard !UserDefaults.standard.bool(forKey: Prefs.listeningExplained) else { return true }
        let alert = NSAlert()
        alert.icon = NSApp.applicationIconImage
        alert.messageText = "Listening mode"
        alert.informativeText = """
        It pauses this call and transcribes the room on this Mac without answering. It keeps the \
        last 30 minutes, as text only, and nothing leaves the Mac while it listens.

        When you turn it off, the call picks up again and knows what was said: ask about it, or say \
        nothing and it responds to the conversation. What it heard goes to the voice and to any \
        task from that call, then it's gone from Speakeasy. Hermes keeps what its tasks receive in its own \
        history, like any task.

        Let the people in the room know it's on.
        """
        alert.addButton(withTitle: "Turn On")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return false }
        UserDefaults.standard.set(true, forKey: Prefs.listeningExplained)
        _ = EarlyCapture.authorizedNow()   // shows the speech recognition prompt the first time
        return true
    }
}

private extension RoomListeningState {
    /// When this listening session began (carried over a call that failed before going live).
    var since: Date? {
        switch self {
        case .listening(let since), .notHearing(let since): return since
        default: return nil
        }
    }
}
