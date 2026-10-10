#if os(macOS)
import AppKit
#endif
import AVFoundation
import Foundation
import SpeakeasyCore
#if os(iOS)
import UIKit
#endif

/// The call surface the app delegate drives.
@MainActor
public protocol VoiceCallClient: AnyObject {
    var onStatus: ((String) -> Void)? { get set }
    var onError: ((String) -> Void)? { get set }
    var onClosed: (() -> Void)? { get set }
    /// Open the next call with the microphone muted (push-to-talk style).
    var startMuted: Bool { get set }
    /// True while a call is connecting/live, i.e. mute is meaningful.
    var micControllable: Bool { get }
    var micMuted: Bool { get }
    /// Local capture track only: never closes the call, never touches Hermes work.
    func setMicMuted(_ muted: Bool)
    func start()
    func end()
    func showWork(active: Bool)
    func hideIfIdle()
    /// Pause closes the billed voice session but keeps the conversation and its
    /// tasks; Resume continues it in a new session. False when unsupported.
    var supportsPause: Bool { get }
    var isPaused: Bool { get }
    func togglePause()
}

/// What the call client needs from the platform UI that shows it.
@MainActor
public protocol VoiceSurface: AnyObject {
    var isVisible: Bool { get }
    var onEscape: (() -> Void)? { get set }
    /// Mac: follow across Spaces. Ignored where it means nothing (iPhone).
    var onAllSpaces: Bool { get set }
    func show()
    func hide()
    func focus()
    func setNeedsResize()
}

/// Native call client: WebRTC transport + server SSE + pure reducer + SwiftUI panel.
@MainActor
public final class NativeVoiceClient: VoiceCallClient {
    public var onStatus: ((String) -> Void)?
    public var onError: ((String) -> Void)?
    public var onClosed: (() -> Void)?
    /// Paused (true) or resumed (false): the conversation stays open either way.
    public var onPauseChanged: ((Bool) -> Void)?
    /// A task settled while the call was paused (heads-up with a Resume action).
    public var onPausedTaskSettled: ((WorkNotice) -> Void)?
    public var startMuted = false
    /// Client preferences (Settings › General).
    public var showPanelOnStart = true
    /// Settings › General "Start calls in slim mode".
    public var startSlim = false
    public var followSystemAudio = true
    public var panelOnAllSpaces = true { didSet { surface.onAllSpaces = panelOnAllSpaces } }
    public var supportsPause: Bool { true }
    public var isPaused: Bool { model.state.connection == .paused }

    public var micControllable: Bool { model.state.connection == .live || model.state.connection == .connecting }
    public var micMuted: Bool { model.state.mic == .muted }
    public func setMicMuted(_ muted: Bool) {
        guard micControllable else { return }
        dispatch(.setMic(muted ? .muted : .live))
    }

    public private(set) var config: AppConfig
    public private(set) var api: ServerClient?
    /// Set by the app before `start()`: the next new call runs the first-call tour, naming these
    /// shortcuts. Consumed once the server admits the call (`onTourStarted`).
    public var pendingTour: [String: String]?
    public var onTourStarted: (() -> Void)?
    private var autoHide = PanelAutoHide()
    private var autoHideTimer: Timer?
    /// Fires when the panel hides itself or is closed (the app updates its menu).
    public var onPanelHidden: (() -> Void)?
    public let model = VoicePanelModel()
    /// The platform surface (Mac floating panel, iPhone call screen) the client shows and resizes.
    public private(set) lazy var surface: any VoiceSurface = makeSurface(model)
    private let makeSurface: @MainActor (VoicePanelModel) -> any VoiceSurface

    private var engine: NativeCallEngine?
    private var startTask: Task<Void, Never>?
    private var streamTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var workOnlyTask: Task<Void, Never>?
    private var ticker: Timer?
    private var levelTimer: Timer?
    /// Checks the call's mic really gets through (see MicCheck); runs even with the screen off.
    private var micCheck = NativeVoiceClient.freshMicCheck()
    /// A mic repair happened on this call: reconnect without listen-while-connecting from now on,
    /// so a repair never repeats the very mic handoff that may have failed.
    private var skipEarlyCapture = false
    /// Whether this connection started with the on-device listener (for the repair report).
    private var connectionUsedEarlyCapture = false
    /// Where the current connection attempt is (for the stall report).
    private var connectStage = "starting"
    private var connectWatchdog: DispatchWorkItem?
    private var connectAttempts = 0
    /// A call that hasn't gone live by then is stuck; retry once, then say so.
    static let connectStallAfter: TimeInterval = 12
    /// iPhone: the call has no mic because the app isn't in front (an Action Button / Siri start
    /// that landed in the background, or iOS took the input away). Reconnecting from the
    /// background can't fix that, since iOS hands the mic only to the app in front; it's fixed
    /// the moment the app is opened instead.
    private var waitingForApp = false
    private var activeObserver: NSObjectProtocol?

    private static func freshMicCheck() -> MicCheck {
        #if os(iOS) || os(visionOS)
        return MicCheck(firstSoundGrace: MicCheck.firstSoundWithinHandheld)
        #else
        return MicCheck()
        #endif
    }
    private var micTimer: Timer?
    /// Set while a dead mic is being repaired (pause → immediate resume).
    private var micRepairPending = false
    private var endDeadline: DispatchWorkItem?
    private var dwell = StatusDwell(minimumDwell: 1.5)
    private var closeNotified = false
    private var voiceProvider = "openai"
    private var codexStopRequested = false
    private var appliedMic: MicState?
    private var appliedRemote: Bool?
    /// Listens on the device while the call connects; nil when not listening.
    private var earlyCapture: EarlyCapture?
    /// Listen while connecting so a request said right away isn't lost (on-device transcription).
    public var listenWhileConnecting = true

    // Listening mode: the call it was turned off into carries what it heard (the "room").
    /// That call's room text and take-off state (nil for every other call). Shared by every leg of
    /// the call (connect retry, resume, device switch, mic repair); gone when the call ends.
    private var roomCall: RoomCall?
    private var roomNudgeTask: Task<Void, Never>?
    /// Bumped per nudge watcher, so a finished watcher clears `roomNudgeTask` only if it's still its own.
    private var roomNudgeGeneration = 0
    /// Listening mode learns whether its transcript was used (the call went live) or should come
    /// back (the call failed before it went live), so it can listen on with it intact.
    public var onRoomTakeoffOutcome: ((RoomTakeoffOutcome) -> Void)?
    /// Preview mode: canned state, no network, no audio.
    public private(set) var previewMode = false

    public init(config: AppConfig, makeSurface: @escaping @MainActor (VoicePanelModel) -> any VoiceSurface) {
        self.config = config
        self.makeSurface = makeSurface
        self.api = ServerClient(config: config)
        model.onClosePanel = { [weak self] in self?.closePanel() }
        model.onDraftAction = { [weak self] draft, action, text in self?.draftAction(draft, action, instructions: text) }
        model.onToggleMic = { [weak self] in self?.dispatch(.toggleMic) }
        model.onStart = { [weak self] in self?.start() }
        model.onEnd = { [weak self] in self?.end() }
        model.onToggleSlim = { [weak self] in
            guard let self else { return }
            self.model.slim.toggle()
            if self.model.slim { self.model.workExpanded = false; self.model.selectedTaskID = nil }
            self.surface.setNeedsResize()
        }
        model.onApproval = { [weak self] choice in self?.resolveApproval(choice) }
        model.onStopWork = { [weak self] in self?.stopWork() }
        model.onToggleWork = { [weak self] in self?.toggleWorkView() }
        model.onCloseWork = { [weak self] in self?.closeWorkView() }
        model.onTogglePause = { [weak self] in self?.togglePause() }
        model.onStopTask = { [weak self] runID in self?.stopWork(runID: runID) }
        model.onAnswer = { [weak self] taskID, text in self?.answerQuestion(taskID: taskID, text: text) }
        model.loadCardImage = { [weak self] runID, number in
            guard let api = await self?.api else { return nil }
            return try? await api.image(runID: runID, index: number)
        }
        model.onSelectTask = { [weak self] id in self?.selectTask(id) }
        model.onDismissTasks = { [weak self] runIDs in self?.dismissTasks(runIDs) }
        model.onDismissReview = { [weak self] runID in self?.dismissReview(runID) }
        model.loadProductImage = { [weak self] runID, index in
            guard let self, let api = self.api else { return nil }
            return try? await api.image(runID: runID, index: index)
        }
        model.loadLiveImage = { [weak self] runID in
            guard let self, let api = self.api else { return nil }
            return try? await api.liveImage(runID: runID)
        }
    }

    // MARK: Reducer plumbing

    public func dispatch(_ event: VoiceEvent) {
        let before = model.state
        let after = reduce(before, event, now: Date())
        model.state = after
        if after.interactionID != nil, after.interactionID != before.interactionID { postFocus() }
        if after.connection != .connecting, connectWatchdog != nil { connectWatchdog?.cancel(); connectWatchdog = nil }
        applySideEffects(from: before, to: after)
        watchMic(event, from: before, to: after)
    }

    // MARK: Mic check

    private func watchMic(_ event: VoiceEvent, from old: VoiceState, to new: VoiceState) {
        switch event {
        case .inputDelta, .outputDelta: micCheck.heard()
        default: break
        }
        // The call heard the user after listening mode was turned off: never answer over them.
        if case .inputDelta = event { roomCall?.noteHeardUser() }
        if old.connection != .live && new.connection == .live {
            micCheck.connectionOpened()
            micRepairPending = false
            if new.micHealth != micCheck.health { dispatch(.micHealth(micCheck.health)) }
            startMicTimer()
        }
        if !new.connection.isInCall && new.connection != .paused && micTimer != nil {
            micTimer?.invalidate(); micTimer = nil
        }
    }

    private func startMicTimer() {
        guard micTimer == nil else { return }
        micTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in self?.checkMic() }
        }
    }

    private func checkMic() {
        guard let engine, model.state.connection == .live, !waitingForApp else { return }
        let state = model.state
        var speaking = false
        if case .speaking = state.speech { speaking = true }
        let listening = state.mic == .live && earlyCapture == nil && !engine.isAudioHeld && !speaking
        engine.micProbe { [weak self] level, packets in
            guard let self, self.engine === engine else { return }
            let fault = self.micCheck.sample(level: level, packetsSent: packets, listening: listening, now: Date())
            if self.model.state.micHealth != self.micCheck.health { self.dispatch(.micHealth(self.micCheck.health)) }
            if let fault { self.repairMic(fault) }
        }
    }

    /// The mic isn't getting through: reopen the voice connection (conversation and tasks kept).
    /// This is what closing and reopening the app did, done for you within a few seconds.
    private func repairMic(_ fault: MicFault) {
        guard model.state.canPause, !micRepairPending, !reconnectAfterPause else { return }
        let detail = micReportDetail()
        #if os(iOS)
        if AVAudioSession.sharedInstance().currentRoute.inputs.isEmpty,
           UIApplication.shared.applicationState != .active {
            waitingForApp = true
            dispatch(.micHealth(.broken))
            onError?("Open Speakeasy to turn the mic on")
            reportMic("no mic while the app is in the background (\(detail)); reopening when the app is opened")
            activeObserver = NotificationCenter.default.addObserver(
                forName: UIApplication.didBecomeActiveNotification, object: nil, queue: .main) { [weak self] _ in
                Task { @MainActor [weak self] in self?.appOpenedForMic() }
            }
            return
        }
        #endif
        micCheck.repairStarted()
        micRepairPending = true
        skipEarlyCapture = true
        dispatch(.micHealth(.repairing))
        reportMic("repair \(micCheck.repairs): \(fault.rawValue) (\(detail))")
        reconnectAfterPause = true
        pause()
    }

    private func armConnectWatchdog(_ api: ServerClient) {
        connectWatchdog?.cancel()
        let item = DispatchWorkItem { [weak self] in
            Task { @MainActor [weak self] in self?.connectStalled(api) }
        }
        connectWatchdog = item
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.connectStallAfter, execute: item)
    }

    /// Still "connecting" long after it should be live. Before, it sat there showing "Listening"
    /// with nothing reaching the call. Now: retry once without the listener, then fail visibly.
    private func connectStalled(_ api: ServerClient) {
        guard model.state.connection == .connecting else { return }
        let stage = connectStage
        reportMic("connect stalled (attempt \(connectAttempts)) at: \(stage); \(micReportDetail())")
        // A call from listening mode keeps what the user already said for the retried call
        // (which connects without the listener) instead of answering from the room over it.
        if roomCall != nil, let heard = earlyCapture?.heardSoFar, !cleanTranscript(heard).isEmpty {
            roomCall?.carriedWords = heard
        }
        roomNudgeTask?.cancel(); roomNudgeTask = nil   // the retried admission watches afresh
        stopEarlyCapture()
        startTask?.cancel(); startTask = nil
        streamTask?.cancel(); streamTask = nil
        engine?.close(); engine = nil
        if connectAttempts < 2 {
            skipEarlyCapture = true
            onStatus?("Reconnecting…")
            connect(api)
        } else {
            connectWatchdog?.cancel(); connectWatchdog = nil
            dispatch(.failed("Couldn't connect to \(model.state.assistantName). Tap to try again."))
        }
    }

    /// The app is in front again: reopen the voice connection, which now gets the mic.
    private func appOpenedForMic() {
        guard waitingForApp else { return }
        stopWaitingForApp()
        guard model.state.canPause, !reconnectAfterPause else { return }
        micRepairPending = true
        skipEarlyCapture = true
        dispatch(.micHealth(.repairing))
        reportMic("app opened; reopening the voice connection for the mic")
        reconnectAfterPause = true
        pause()
    }

    private func stopWaitingForApp() {
        waitingForApp = false
        if let activeObserver { NotificationCenter.default.removeObserver(activeObserver) }
        activeObserver = nil
    }

    /// What the check saw, so a repair in the log says why (no audio content, just numbers).
    private func micReportDetail() -> String {
        var parts = [String(format: "watched %.1fs", micCheck.watchedFor),
                     "level \(micCheck.lastLevel.map { String(format: "%.5f", $0) } ?? "none")",
                     "packets \(micCheck.lastPacketCount.map(String.init) ?? "none")",
                     connectionUsedEarlyCapture ? "after listen-while-connecting" : "direct"]
        #if os(iOS) || os(visionOS)
        let route = AVAudioSession.sharedInstance().currentRoute
        let ins = route.inputs.map { $0.portType.rawValue }.joined(separator: "+")
        let outs = route.outputs.map { $0.portType.rawValue }.joined(separator: "+")
        parts.append("in " + (ins.isEmpty ? "none" : ins))
        parts.append("out " + (outs.isEmpty ? "none" : outs))
        parts.append(NativeCallEngine.sharedAudioSummary())
        #endif
        return parts.joined(separator: ", ")
    }

    /// Vision Pro: the task you're facing or holding (nil = none), and which part of it. Unnamed
    /// requests go to that task, and the voice is told what you're looking at.
    private var focusSent: (task: String?, detail: String)?
    private var focusPosted: (interaction: String, task: String?, detail: String)?

    public func setFocus(taskID: String?, detail: String = "") {
        focusSent = (taskID, String(detail.prefix(200)))
        postFocus()
    }

    private func postFocus() {
        guard let focus = focusSent, let api, let id = model.state.interactionID else { return }
        if let p = focusPosted, p.interaction == id, p.task == focus.task, p.detail == focus.detail { return }
        focusPosted = (id, focus.task, focus.detail)
        let body: [String: Any] = ["task_id": focus.task ?? NSNull(), "detail": focus.detail]
        Task { _ = try? await api.post("/voice/interactions/\(id)/focus", body) }
    }

    private func reportMic(_ note: String) {
        guard let api, let id = model.state.interactionID else { return }
        Task { _ = try? await api.post("/voice/interactions/\(id)/mic-check", ["note": String(note.prefix(400))]) }
    }

    private func applySideEffects(from old: VoiceState, to new: VoiceState) {
        if appliedMic != new.mic {
            appliedMic = new.mic
            // While the connecting listener still has the mic (you're finishing a sentence), the
            // call's mic stays off so it doesn't hear the end of that sentence a second time.
            engine?.setMicEnabled(new.localAudioEnabled && earlyCapture == nil)
        }
        if appliedRemote != new.remoteAudioEnabled {
            appliedRemote = new.remoteAudioEnabled
            engine?.setRemoteAudioEnabled(new.remoteAudioEnabled)
        }
        refreshStatusLine()
        if let show = new.showRequest, show != old.showRequest { act(on: show) }
        if old.exchange.isEmpty != new.exchange.isEmpty || old.approval != new.approval ||
            old.connection != new.connection || old.workInfo != new.workInfo || old.tasks != new.tasks {
            surface.setNeedsResize()
        }
        if new.connection == .paused, old.connection != .paused { callPaused() }
        if old.exchange != new.exchange { surface.setNeedsResize() }
        if new.connection == .live, old.connection != .live, roomCall != nil {
            roomCall?.noteLive(at: Date())
            if let outcome = roomCall?.outcome(for: .wentLive) { onRoomTakeoffOutcome?(outcome) }
        }
        if case .ended(let fin) = new.connection, old.connection != new.connection {
            // Before it went live, only an end the user asked for gives the transcript up; a lost
            // connection (.transportLost ends a connecting call too) gets it back to listening mode.
            if let outcome = roomCall?.outcome(for: .ended) { onRoomTakeoffOutcome?(outcome) }
            finishCall(fin)
        }
        if case .failed(let message) = new.connection, old.connection != new.connection {
            if let outcome = roomCall?.outcome(for: .failed) { onRoomTakeoffOutcome?(outcome) }
            onError?(message)
            finishCall(.incomplete)
        }
        if let error = new.lastError, error != old.lastError { onError?(error) }
        if old.connection != new.connection { onStatus?(present(new).primary + " · " + present(new).secondary) }
    }

    private func refreshStatusLine() {
        let p = model.presentation
        let urgent = p.tone == .attention || p.tone == .error || model.shownStatus.isEmpty
        if dwell.offer(p.secondary, urgent: urgent, now: Date()) {
            model.shownStatus = dwell.shown ?? ""
            model.shownTone = p.tone
        } else if dwell.shown == p.secondary, model.shownTone != p.tone {
            model.shownTone = p.tone   // same words, tone aged (e.g. glimmer → plain)
        }
    }

    private func startTicker() {
        guard ticker == nil else { return }
        ticker = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if previewMode { return }
                dispatch(.tick)
                // Five quiet minutes: close the paid voice session. Pause, not End, so the
                // conversation and running tasks survive and Resume picks up where it left off.
                if model.state.idleExpired(at: Date()) {
                    let minutes = Int((model.state.idleTimeout / 60).rounded())
                    onStatus?("No audio for \(minutes) minute\(minutes == 1 ? "" : "s"): voice paused")
                    pause()
                }
                if dwell.tick(now: Date()) {
                    model.shownStatus = dwell.shown ?? ""
                    model.shownTone = model.presentation.tone
                }
            }
        }
    }

    private func stopTicker() {
        ticker?.invalidate(); ticker = nil
        levelTimer?.invalidate(); levelTimer = nil
        model.orbLevel = 0
    }

    /// ~12 Hz: the orb follows the assistant's voice while it speaks, the mic while listening.
    private func startLevelMeter() {
        guard levelTimer == nil else { return }
        levelTimer = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, let engine = self.engine, self.model.state.connection == .live,
                      self.surface.isVisible else {
                    if self?.model.orbLevel != 0 { self?.model.orbLevel = 0 }
                    return
                }
                engine.audioLevels { [weak self] mic, voice in
                    guard let self else { return }
                    let speaking = self.model.presentation.mark == .speaking
                    let muted = self.model.state.mic == .muted
                    // Levels are linear; a square root makes quiet speech visible.
                    let raw = speaking ? voice : (muted ? 0 : mic)
                    let target = min(1, sqrt(max(0, raw)) * 1.4)
                    let current = self.model.orbLevel
                    // Fast attack, slower release, so it moves with syllables without flicker.
                    let next = target > current ? current + (target - current) * 0.6 : current + (target - current) * 0.25
                    if abs(next - current) > 0.01 { self.model.orbLevel = next }
                }
            }
        }
    }

    // MARK: Call lifecycle

    public func start() {
        startCall(room: nil, takeoffAt: nil)
    }

    /// Start the call listening mode was turned off into. It carries what was heard, opens
    /// unmuted whatever "start muted" says (you turned listening off to ask something), and when
    /// nothing is said it answers from what it heard about 2 s after `takeoffAt`.
    /// If listening mode was turned on mid-conversation (which paused this call), it resumes that
    /// same conversation instead, with what was heard added.
    public func start(room: String, takeoffAt: Date) {
        if model.state.connection == .paused {
            roomNudgeTask?.cancel(); roomNudgeTask = nil
            if !room.isEmpty { roomCall = RoomCall(room: room, takeoffAt: takeoffAt) }   // a new take-off
            resume(unmuted: true)
            return
        }
        guard !model.state.connection.isOpen else { return }
        startCall(room: room.isEmpty ? nil : room, takeoffAt: takeoffAt)
    }

    /// Listening mode turned on mid-conversation: pause this call (the voice stops hearing and
    /// answering, nothing is billed, the conversation and tasks are kept) and report whether it
    /// paused, so the mic is free for listening mode. Turning listening off resumes it.
    public func pauseForListening() async -> Bool {
        if model.state.connection == .paused { return true }
        guard model.state.canPause else { return false }
        pause()
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline {
            try? await Task.sleep(nanoseconds: 100_000_000)
            switch model.state.connection {
            case .paused: return true
            case .live where !model.state.pausing: return false   // the server couldn't hold the call
            case .idle, .ended, .failed: return false
            default: continue
            }
        }
        return false
    }

    private func startCall(room: String?, takeoffAt: Date?) {
        if model.state.connection == .paused { resume(); return }
        guard !model.state.connection.isInCall else { surface.show(); return }
        resetRoom()
        if let room, let takeoffAt { roomCall = RoomCall(room: room, takeoffAt: takeoffAt) }
        cancelAutoHide()
        closeNotified = false
        voiceProvider = "openai"
        codexStopRequested = false
        appliedMic = nil; appliedRemote = nil
        micCheck = NativeVoiceClient.freshMicCheck(); micRepairPending = false; skipEarlyCapture = false
        connectAttempts = 0
        stopWaitingForApp()
        dwell = StatusDwell(minimumDwell: 1.5)
        model.workExpanded = false
        model.captionExpanded = false
        workOnlyTask?.cancel()
        dispatch(.startRequested)
        // A call from listening mode always opens unmuted: the words said next are the request.
        if startMuted && takeoffAt == nil { dispatch(.setMic(.muted)) }
        surface.onEscape = { [weak self] in self?.handleEscape() }
        if startSlim { model.slim = true }
        if showPanelOnStart { surface.show() }
        startTicker()
        startLevelMeter()
        #if os(macOS)
        if followSystemAudio { watchAudioDevices() }
        #endif
        guard let api else {
            dispatch(.failed(config.deviceToken == nil ? "Not connected to Hermes — open Settings › Connection to pair"
                             : "The server address isn't trusted — pair again in Settings › Connection"))
            return
        }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            connect(api)
        case .notDetermined:
            onStatus?("Allow Speakeasy to use the microphone…")
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
                Task { @MainActor [weak self] in
                    guard let self, self.model.state.connection == .connecting else { return }
                    if granted { self.connect(api) }
                    else { self.dispatch(.failed("Microphone access is off")) }
                }
            }
        default:
            #if os(macOS)
            dispatch(.failed("Microphone access is off — enable Speakeasy in System Settings › Privacy & Security › Microphone"))
            #else
            dispatch(.failed("Microphone access is off — enable it in Settings › Speakeasy › Microphone"))
            #endif
        }
    }

    private func connect(_ api: ServerClient) {
        connectStage = "starting"
        connectAttempts += 1
        armConnectWatchdog(api)
        if skipEarlyCapture { stopEarlyCapture() } else { startEarlyCapture() }
        connectionUsedEarlyCapture = earlyCapture != nil
        if earlyCapture == nil { NativeCallEngine.resetSharedAudio() }
        let engine = NativeCallEngine()
        self.engine = engine
        if earlyCapture != nil { engine.holdAudio() }
        engine.onMessage = { [weak self] data in
            guard let self else { return }
            for event in parseDataChannelMessage(data) {
                if case .sessionClosed = event, self.voiceProvider == "codex" {
                    self.finishCodexCall()
                } else {
                    self.dispatch(event)
                }
            }
        }
        engine.onChannelClosed = { [weak self] in self?.channelClosed() }
        engine.onChannelOpen = { [weak self] in self?.connectStage = "channel open; waiting for the call to start" }
        engine.onAudioFormatChanged = { [weak self] in self?.audioDevicesChanged() }
        engine.onConnectionFailed = { [weak self] in
            guard let self, self.model.state.connection == .live || self.model.state.connection == .connecting else { return }
            self.dispatch(.transportLost)
        }
        // A new admission of a call from listening mode carries what was heard. Whether the
        // server still takes it (the plugin can be updated or rolled back during a long listen) is
        // asked while the offer is made, so the check never delays the call.
        // nil = the status didn't come back (unknown); a status without the flag = an older plugin.
        let resuming = model.state.resumeFrom != nil
        let roomSupport: Task<Bool?, Never>? = roomCall?.sends(resuming: resuming) == true
            ? Task {
                (try? await api.status()).map { ($0.roomListening ?? false) && (!resuming || ($0.roomOnResume ?? false)) }
            } : nil
        startTask = Task { [weak self] in
            var sentRoom = false
            do {
                await engine.warmAudioRoute()
                try engine.prepare(captureAudio: true)
                // Honour a mute chosen before/while connecting (start-muted or hotkey).
                engine.setMicEnabled(self?.model.state.localAudioEnabled ?? false)
                self?.connectStage = "creating offer"
                let sdp = try await engine.createOffer()
                self?.connectStage = "waiting for the server"
                guard let self, self.engine === engine, self.model.state.connection == .connecting else { engine.close(); return }
                var room: String?
                if let roomSupport, let context = self.roomCall?.room {
                    // Give a slow status a second; if it still hasn't answered, send the room
                    // anyway: a server that can't take it answers 400 and the call falls back below.
                    let supported = await Self.firstAnswer(of: roomSupport, within: 1)
                    if supported == false {
                        self.dropRoomContext("This Speakeasy plugin can't use what listening mode heard. Update it on the Hermes machine, then run hermes voice reload.")
                    } else {
                        room = context
                    }
                    guard self.engine === engine, self.model.state.connection == .connecting else { engine.close(); return }
                }
                sentRoom = room != nil
                let tour = self.model.state.resumeFrom == nil && room == nil ? self.pendingTour : nil
                let admission = try await api.admitSession(sdp: sdp, idempotencyKey: UUID().uuidString,
                                                              resumeFrom: self.model.state.resumeFrom, tour: tour,
                                                              room: room)
                guard self.engine === engine else { engine.close(); return }
                if sentRoom { self.roomCall?.markDelivered() }   // later resumes rely on the server's copy
                if tour != nil {
                    self.pendingTour = nil
                    self.model.tourActive = true
                    self.onTourStarted?()
                }
                self.voiceProvider = admission.voiceProvider
                self.codexStopRequested = false
                self.dispatch(.sessionAdmitted(interactionID: admission.interactionID))
                self.connectStage = "admitted; connecting audio"
                let early = self.earlyCapture
                if early != nil { engine.setMicEnabled(false) }
                try await engine.setRemoteAnswer(admission.answerSDP)
                self.appliedMic = nil; self.appliedRemote = nil
                self.dispatch(.tick)
                self.startServerEvents(api, interactionID: admission.interactionID)
                self.connectStage = "answer applied; waiting for the call to start"
                if let early {
                    // Mid-sentence? Let the listener hear you out before the call takes the mic.
                    await early.waitForPause()
                    early.stopListening()
                    engine.releaseAudio()
                    self.handOverEarlyWords(early, api: api, interactionID: admission.interactionID)
                } else {
                    engine.releaseAudio()
                    self.handOverRoomRequest("", api: api, interactionID: admission.interactionID)
                }
                if self.model.state.connection == .ending { self.sendClose() }
            } catch {
                guard let self, self.engine === engine else { return }
                // The server wouldn't take what listening mode heard (an older plugin, or the
                // transcript was refused): don't loop back into listening, start a normal call.
                if sentRoom, let http = error as? ServerClient.HTTPError, http.status == 400 || http.status == 413,
                   self.model.state.connection == .connecting {
                    engine.close()
                    self.dropRoomContext("Couldn't use what listening mode heard; carried on without it")
                    self.connect(api)
                    return
                }
                // The api no longer holds the paused call (restarted, or it was
                // already resumed): start a fresh call rather than failing.
                if self.model.state.resumeFrom != nil, let http = error as? ServerClient.HTTPError,
                   http.status == 404 || http.status == 409, self.model.state.connection == .connecting {
                    engine.close()
                    self.model.state.resumeFrom = nil
                    self.dispatch(.error("Couldn't resume the paused call; started a new one"))
                    self.connect(api)
                    return
                }
                self.dispatch(.failed(ConnectionTrouble.message(for: error, server: self.config.serverURL)))
            }
        }
    }

    public func end() {
        roomCall?.noteEndRequested()
        switch model.state.connection {
        case .paused:
            dispatch(.endRequested)
        case .connecting, .live:
            dispatch(.endRequested)
            if model.state.connection == .ending { sendClose() }
            else { startTask?.cancel() }
        case .ending:
            break
        default:
            if model.state.workOnly || model.workExpanded { closeWorkView() } else { hideNow() }
        }
    }

    // MARK: Pause / Resume

    public func togglePause() {
        switch model.state.connection {
        case .paused: resume()
        case .live: pause()
        default: break
        }
    }

    /// Ask the api to hold the conversation, then close the voice session.
    /// If the api cannot hold it the call stays live (nothing is lost).
    private func pause() {
        guard model.state.canPause, let api, let id = model.state.interactionID else { return }
        dispatch(.pauseRequested)
        Task { [weak self] in
            do {
                try await api.pause(interactionID: id)
                guard let self, self.model.state.pausing else { return }
                self.sendClose()
            } catch {
                self?.reconnectAfterPause = false
                self?.dispatch(.pauseFailed("Couldn't pause: \(error.localizedDescription)"))
            }
        }
    }

    private func callPaused() {
        stopEarlyCapture()
        endDeadline?.cancel(); endDeadline = nil
        startTask?.cancel(); startTask = nil
        engine?.close(); engine = nil
        streamTask?.cancel(); streamTask = nil
        pollTask?.cancel(); pollTask = nil
        if reconnectAfterPause {
            // Audio device switch: reopen straight away on the new device.
            reconnectAfterPause = false
            resume()
            return
        }
        startPausedTaskPolling()
        onPauseChanged?(true)
    }

    // MARK: Audio device changes (macOS)

    /// Set while a device switch is being handled as pause → immediate resume.
    private var reconnectAfterPause = false
    #if os(macOS)
    private var deviceWatcher: DefaultAudioDeviceWatcher?

    private func watchAudioDevices() {
        guard deviceWatcher == nil else { return }
        deviceWatcher = DefaultAudioDeviceWatcher { [weak self] in self?.audioDevicesChanged() }
    }

    /// WebRTC keeps playing to the device it opened. When the Mac's default output
    /// or input changes mid-call (AirPods in or out), reconnect the voice session on
    /// the new device. The conversation and tasks are kept; only the audio reopens.
    private func audioDevicesChanged() {
        guard model.state.canPause, !reconnectAfterPause else { return }
        onStatus?("Switching audio to \(DefaultAudioDeviceWatcher.outputName() ?? "the new device")…")
        reconnectAfterPause = true
        pause()
    }
    #else
    /// iOS: AVAudioSession moves WebRTC to AirPods / the speaker itself; nothing to reopen.
    private func audioDevicesChanged() {}
    #endif

    /// `unmuted`: listening mode was just turned off to talk, so the call comes back with the mic on.
    private func resume(unmuted: Bool = false) {
        guard model.state.connection == .paused, let api else { return }
        pollTask?.cancel(); pollTask = nil
        appliedMic = nil; appliedRemote = nil
        dispatch(.resumeRequested)
        if unmuted { dispatch(.setMic(.live)) }
        connectAttempts = 0
        surface.show()
        startTicker()
        startLevelMeter()
        onPauseChanged?(false)
        connect(api)
    }

    /// While paused there is no event stream: keep the task list fresh by
    /// polling the tasks' work rows (no voice session is open, nothing billed).
    private func startPausedTaskPolling() {
        guard let api else { return }
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                guard let self, self.model.state.connection == .paused else { return }
                var tasks = self.model.state.tasks
                for (index, task) in tasks.enumerated() where task.isActive {
                    if let run = task.info.runID, let info = try? await api.work(runID: run) {
                        tasks[index].info = info
                    }
                }
                guard self.model.state.connection == .paused else { return }
                if tasks != self.model.state.tasks {
                    for notice in PausedTaskNotices.settled(before: self.model.state.tasks, after: tasks) {
                        self.onPausedTaskSettled?(notice)
                    }
                    self.dispatch(.tasks(tasks))
                }
                if let run = self.model.state.runID, let info = tasks.first(where: { $0.info.runID == run })?.info {
                    self.dispatch(.work(info))
                }
            }
        }
    }

    private func sendClose() {
        guard model.state.interactionID != nil else { return }
        endDeadline?.cancel()
        let deadline = DispatchWorkItem { [weak self] in self?.dispatch(.endTimedOut) }
        endDeadline = deadline
        DispatchQueue.main.asyncAfter(deadline: .now() + 8, execute: deadline)
        if engine?.send(json: ["type": "session.close"]) != true {
            engine?.close()
            startFinalizationPolling()
        }
    }

    private func finishCodexCall() {
        guard !codexStopRequested, let api, let id = model.state.interactionID else { return }
        codexStopRequested = true
        // Provider session.closed is not Codex's app-server close. Wait for its
        // stop RPC and the api's finalization before showing a clean end.
        Task { [weak self] in
            do {
                try await api.finishCodexTransport(interactionID: id)
                guard let self, self.model.state.interactionID == id else { return }
                if let snapshot = try await api.interaction(id) {
                    self.dispatch(.interaction(snapshot))
                }
            } catch {
                guard let self, self.model.state.interactionID == id else { return }
                self.dispatch(.error("Codex call end could not be confirmed"))
                self.startFinalizationPolling()
            }
        }
    }

    private func channelClosed() {
        switch model.state.connection {
        case .ending:
            if voiceProvider == "codex" { finishCodexCall() }
            engine?.close()
            startFinalizationPolling()
        case .live, .connecting:
            dispatch(.transportLost)
        default: break
        }
    }

    private func startFinalizationPolling() {
        guard let api, let id = model.state.interactionID else { return }
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                if let snapshot = try? await api.interaction(id) { self?.dispatch(.interaction(snapshot)) }
                guard let self, self.model.state.connection == .ending else { return }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
        }
    }

    // MARK: Listening while connecting

    private func startEarlyCapture() {
        stopEarlyCapture()
        guard listenWhileConnecting, model.state.localAudioEnabled, !previewMode else { return }
        let capture = EarlyCapture()
        capture.onPartial = { [weak self] text in self?.dispatch(.earlyHeard(text)) }
        capture.onSignal = { [weak self, weak capture] in
            guard let self, let capture, self.earlyCapture === capture else { return }
            self.connectStage = "listener hearing"
            self.dispatch(.earlyListening(true))
        }
        guard capture.start() else { connectStage = "listener couldn't start"; return }
        earlyCapture = capture
        connectStage = "listener started"
        // No sound within 1.5 s (iOS hadn't given us the mic yet, e.g. right after an Action Button
        // launch): drop the listener and let the call take the mic itself. Never show "Listening"
        // for a mic that hears nothing.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self, weak capture] in
            guard let self, let capture, self.earlyCapture === capture, !capture.hasSignal else { return }
            self.connectStage = "listener heard no sound; call takes the mic"
            self.stopEarlyCapture()
            self.engine?.setMicEnabled(self.model.state.localAudioEnabled)
        }
    }

    private func stopEarlyCapture() {
        earlyCapture?.cancel()
        earlyCapture = nil
        engine?.releaseAudio()
        if model.state.earlyListening { dispatch(.earlyListening(false)) }
    }

    /// The words said while connecting become the call's first request. Muting before the call
    /// connected drops them (a mute means "that wasn't for you").
    private func handOverEarlyWords(_ capture: EarlyCapture, api: ServerClient, interactionID: String) {
        Task { [weak self] in
            let text = await capture.finish()
            guard let self, self.earlyCapture === capture else { return }
            self.earlyCapture = nil
            self.appliedMic = nil
            self.dispatch(.tick)   // the call's mic opens now (unless you muted)
            let keep = self.model.state.localAudioEnabled && self.model.state.connection.isInCall
            self.dispatch(.earlyListening(false))
            if self.roomCall != nil {
                if keep { self.handOverRoomRequest(text, api: api, interactionID: interactionID) }
                return
            }
            guard keep, !cleanTranscript(text).isEmpty else { return }
            await self.sendEarlyRequest(text, api: api, interactionID: interactionID)
        }
    }

    private func sendEarlyRequest(_ text: String, api: ServerClient, interactionID: String) async {
        dispatch(.inputDelta(text))
        do {
            _ = try await api.post("/voice/interactions/\(interactionID)/early-request", ["text": String(text.prefix(1000))])
        } catch {
            dispatch(.error("Couldn't pass on what you said while connecting. Say it again."))
        }
    }

    // MARK: Listening mode (the call it was turned off into)

    /// The words said since listening mode was turned off (plus any the listener had heard before
    /// a connect retry) are the request. With none, the call answers from what listening mode
    /// heard, once RoomTakeoffPolicy says the user isn't about to speak. No-op for other calls.
    private func handOverRoomRequest(_ text: String, api: ServerClient, interactionID: String) {
        guard let call = roomCall else { return }
        let words = [call.carriedWords ?? "", text].filter { !cleanTranscript($0).isEmpty }.joined(separator: " ")
        roomCall?.carriedWords = nil
        if !words.isEmpty {
            // Words said while a later leg reconnects are an ordinary request too.
            roomCall?.markRequestHandled()
            Task { [weak self] in await self?.sendEarlyRequest(words, api: api, interactionID: interactionID) }
            return
        }
        // Nothing said: the call keeps the room as context and waits for a request. It never speaks
        // first; it can't know what you want from a conversation it overheard.
        roomCall?.markRequestHandled()
    }

    /// The server can't take what listening mode heard: this call carries on without it.
    private func dropRoomContext(_ message: String) {
        let outcome = roomCall?.notUsed()
        roomCall = nil
        roomNudgeTask?.cancel(); roomNudgeTask = nil
        if let outcome { onRoomTakeoffOutcome?(outcome) }
        dispatch(.error(message))
    }

    private func resetRoom() {
        roomCall = nil
        roomNudgeTask?.cancel(); roomNudgeTask = nil
    }

    /// The task's answer if it arrives within `seconds`, else nil (the task keeps running in the
    /// background). Not a task group: a group waits for every child before it returns, and awaiting
    /// an unstructured task ignores cancellation, so the timeout would never cut the wait.
    private static func firstAnswer<T: Sendable>(of task: Task<T?, Never>, within seconds: Double) async -> T? {
        await withCheckedContinuation { (continuation: CheckedContinuation<T?, Never>) in
            let once = SignalFlag()
            Task {
                let value = await task.value
                if once.set() { continuation.resume(returning: value) }
            }
            Task {
                try? await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
                if once.set() { continuation.resume(returning: nil) }
            }
        }
    }

    public func skipTour() {
        guard model.tourActive else { return }
        model.tourActive = false
        guard let api, let id = model.state.interactionID else { return }
        Task { try? await api.skipTour(interactionID: id) }
    }

    private func finishCall(_ finalization: Finalization) {
        stopEarlyCapture()
        resetRoom()   // what listening mode heard lives only as long as the call it was used for
        model.selectedTaskID = nil
        model.tourActive = false
        endDeadline?.cancel(); endDeadline = nil
        startTask?.cancel(); startTask = nil
        engine?.close(); engine = nil
        streamTask?.cancel(); streamTask = nil
        pollTask?.cancel(); pollTask = nil
        let delay: TimeInterval = finalization == .complete ? 1.0 : 2.4
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self, !self.model.state.connection.isOpen else { return }
            // Show the finished state briefly, then hide (hover or anything that
            // needs the user holds it). The x button and Esc hide it at once.
            if self.surface.isVisible && !self.model.workExpanded { self.armAutoHide() }
            if !self.closeNotified { self.closeNotified = true; self.onClosed?() }
        }
    }

    private func hideNow() {
        cancelAutoHide()
        surface.hide()
        if !model.state.connection.isOpen && !model.state.workOnly && !model.workExpanded { stopTicker() }
        onPanelHidden?()
    }

    public var panelVisible: Bool { surface.isVisible }

    /// The x button: idle → hide (leaving any work-only view); in a call → hide only.
    public func closePanel() {
        if model.state.workOnly { workOnlyTask?.cancel(); workOnlyTask = nil; dispatch(.reset) }
        model.selectedTaskID = nil
        model.workExpanded = false
        hideNow()
    }

    public func showPanel() {
        cancelAutoHide()
        surface.onEscape = { [weak self] in self?.handleEscape() }
        surface.show()
    }

    private func armAutoHide() {
        autoHide.arm(now: Date())
        autoHideTimer?.invalidate()
        autoHideTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                if self.model.state.connection.isOpen || !self.surface.isVisible { self.cancelAutoHide(); return }
                if self.autoHide.shouldHide(now: Date(), hovering: self.model.hovering,
                                            needsAttention: panelNeedsAttention(self.model.state) || self.model.workExpanded) {
                    self.hideNow()
                }
            }
        }
    }

    private func cancelAutoHide() {
        autoHide.cancel()
        autoHideTimer?.invalidate(); autoHideTimer = nil
    }

    /// Pairing changed or settings reloaded.
    public func update(config: AppConfig) {
        self.config = config
        api = ServerClient(config: config)
    }

    public func apply(settings: ServerSettings) {
        model.state.assistantName = settings.resolvedAssistantName
        model.state.idleTimeout = settings.idleTimeout
    }

    public func setCallShortcutHint(_ hint: String) { model.state.callShortcutHint = hint }

    // MARK: Email drafts

    private func draftAction(_ draft: EmailDraft, _ action: DraftAction, instructions: String?) {
        guard draft.status == .pending, !model.draftBusy.contains(draft.draftID) else { return }
        let key = "\(draft.draftID)|\(draft.sha256)"
        model.draftNotice[draft.draftID] = nil
        model.draftLocalStatus[key] = action.optimisticStatus
        model.draftBusy.insert(draft.draftID)
        guard let api, !previewMode else {
            model.draftBusy.remove(draft.draftID)
            return
        }
        Task { [weak self] in
            do {
                try await api.draftAction(action, draft: draft, instructions: instructions)
                self?.model.draftBusy.remove(draft.draftID)
                await self?.refreshDraftSources()
            } catch DraftActionError.changed {
                guard let self else { return }
                self.model.draftLocalStatus[key] = nil
                self.model.draftBusy.remove(draft.draftID)
                self.model.draftNotice[draft.draftID] = draftChangedMessage
                await self.refreshDraftSources()
            } catch {
                guard let self else { return }
                self.model.draftLocalStatus[key] = nil
                self.model.draftBusy.remove(draft.draftID)
                self.model.draftNotice[draft.draftID] = "Couldn't \(action.rawValue): \(error.localizedDescription)"
            }
        }
    }

    /// Refetch tasks so the card shows the server's current draft (SSE also pushes it).
    private func refreshDraftSources() async {
        guard let api else { return }
        if let (work, tasks) = try? await api.latestWork() {
            if !tasks.isEmpty, tasks.map(\.id) == model.state.tasks.map(\.id) || model.state.workOnly || !model.state.connection.isOpen {
                dispatch(.tasks(tasks))
            } else if !tasks.isEmpty {
                var merged = model.state.tasks
                for task in tasks { if let i = merged.firstIndex(where: { $0.id == task.id }) { merged[i] = task } }
                dispatch(.tasks(merged))
            }
            if let work, work.runID == model.state.runID { dispatch(.work(work)) }
        }
    }

    public func hideIfIdle() {
        if !model.state.connection.isOpen && !model.state.workOnly && !previewMode { hideNow() }
    }

    private func handleEscape() {
        if model.selectedTaskID != nil && model.workExpanded { model.selectedTaskID = nil; surface.setNeedsResize() }
        else if model.workExpanded && !model.state.workOnly { model.workExpanded = false; surface.setNeedsResize() }
        else if model.state.workOnly { closeWorkView() }
        else if model.state.connection.isOpen { end() }
        else { hideNow() }
    }

    // MARK: Server events (SSE with polling fallback)

    private func startServerEvents(_ api: ServerClient, interactionID: String) {
        streamTask?.cancel()
        streamTask = Task { [weak self] in
            var parser = SSEParser()
            var attempt = 0
            while !Task.isCancelled {
                let outcome = await api.streamEvents(interactionID: interactionID, parser: &parser) { event in
                    await MainActor.run {
                        guard let self else { return }
                        for voiceEvent in parseServerStreamEvent(event) { self.dispatch(voiceEvent) }
                    }
                }
                guard let self, !Task.isCancelled else { return }
                switch outcome {
                case .unsupported:
                    self.startPollingFallback(api, interactionID: interactionID)
                    return
                case .ended:
                    attempt = 0
                    if !self.model.state.connection.isInCall { return }
                case .failed:
                    attempt += 1
                    if attempt >= 4 { self.startPollingFallback(api, interactionID: interactionID); return }
                }
                let delay = sseReconnectDelay(attempt: attempt, serverRetryMilliseconds: parser.retryMilliseconds)
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            }
        }
    }

    /// Server without `/events`: poll instead.
    private func startPollingFallback(_ api: ServerClient, interactionID: String) {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            var lastWork = Date.distantPast
            while !Task.isCancelled {
                guard let self, self.model.state.connection.isInCall else { return }
                if let snapshot = try? await api.interaction(interactionID) { self.dispatch(.interaction(snapshot)) }
                if let run = self.model.state.runID, Date().timeIntervalSince(lastWork) > 2 {
                    lastWork = Date()
                    if let work = try? await api.work(runID: run) { self.dispatch(.work(work)) }
                }
                try? await Task.sleep(nanoseconds: 700_000_000)
            }
        }
    }

    // MARK: Work view, approval, stop

    public func showWork(active: Bool) {
        if model.state.connection.isOpen {
            model.workExpanded = true
            surface.setNeedsResize()
            surface.focus()
            return
        }
        guard let api else { return }
        dispatch(.workOnlyOpened)
        model.workExpanded = true
        surface.onEscape = { [weak self] in self?.handleEscape() }
        surface.focus()
        startTicker()
        workOnlyTask?.cancel()
        workOnlyTask = Task { [weak self] in
            while !Task.isCancelled {
                do {
                    let (work, tasks) = try await api.latestWork()
                    guard let self, self.model.state.workOnly else { return }
                    self.dispatch(.work(work))
                    if tasks != self.model.state.tasks { self.dispatch(.tasks(tasks)) }
                } catch {
                    self?.dispatch(.error("Work unavailable: \(error.localizedDescription)"))
                }
                try? await Task.sleep(nanoseconds: 2_000_000_000)
            }
        }
    }

    /// "Show me": open the task's live image (detail view) or bring its review card forward.
    private func act(on show: ShowRequest) {
        if model.showsSlim { model.slim = false }
        if show.opensDetail {
            if model.state.tasks.contains(where: { $0.id == show.taskID }) { selectTask(show.taskID) }
        } else {
            model.workExpanded = false
            model.selectedTaskID = nil
            if let run = show.runID { model.focusedReviewID = run }
            model.objectWillChange.send()
            surface.setNeedsResize()
        }
        if !surface.isVisible { showPanel() }
    }

    private func selectTask(_ id: String?) {
        model.selectedTaskID = id
        model.workExpanded = true
        surface.setNeedsResize()
        surface.focus()
    }

    private func toggleWorkView() {
        if model.showsSlim { model.slim = false; model.workExpanded = true }
        else { model.workExpanded.toggle() }
        surface.setNeedsResize()
        if model.workExpanded { surface.focus() }
    }

    private func closeWorkView() {
        if model.selectedTaskID != nil && model.state.tasks.count > 1 {
            model.selectedTaskID = nil   // Back goes from one task to the task list
            surface.setNeedsResize()
            return
        }
        model.selectedTaskID = nil
        model.workExpanded = false
        if model.state.workOnly {
            workOnlyTask?.cancel(); workOnlyTask = nil
            dispatch(.reset)
            surface.hide()
            stopTicker()
        } else {
            surface.setNeedsResize()
        }
    }

    private func resolveApproval(_ choice: String) {
        guard choice == "once" || choice == "deny", let api,
              let approval = model.state.approval, let id = model.state.interactionID, !model.busy else { return }
        model.busy = true
        Task { [weak self] in
            do {
                try await api.resolveApproval(interactionID: id, approval: approval, choice: choice)
                self?.dispatch(.approvalResolved)
            } catch {
                self?.dispatch(.error(error.localizedDescription))
            }
            self?.model.busy = false
        }
    }

    private func stopWork() {
        guard let run = model.state.runID else { return }
        stopWork(runID: run)
    }

    /// Stop one task. While paused the adopted runs still belong to the paused
    /// call's interaction, so Stop goes there.
    /// The x on finished tasks: hide now, then tell the api so they stay gone.
    private func dismissTasks(_ runIDs: [String]) {
        guard !runIDs.isEmpty else { return }
        if let id = model.selectedTaskID, let task = model.state.tasks.first(where: { $0.id == id }),
           runIDs.contains(task.info.runID ?? "") {
            model.selectedTaskID = nil
        }
        dispatch(.tasksDismissed(runIDs))
        surface.setNeedsResize()
        guard let api, !previewMode else { return }
        Task { [weak self] in
            do { try await api.dismissTasks(runIDs: runIDs) }
            catch { self?.dispatch(.error("Couldn't clear tasks: \(error.localizedDescription)")) }
        }
    }

    /// Dismiss on a review card: hide now, then tell the api so it stays gone on the next call.
    private func dismissReview(_ runID: String) {
        guard let review = model.state.pendingReviews.first(where: { $0.runID == runID }) else { return }
        let cards = review.images.map(\.number)
        dispatch(.reviewDismissed(runID, cards))
        model.reviewIndex[runID] = nil
        surface.setNeedsResize()
        guard let api, !previewMode else { return }
        Task { [weak self] in
            do { try await api.dismissReview(runID: runID, cards: cards) }
            catch { self?.dispatch(.error("Couldn't dismiss: \(error.localizedDescription)")) }
        }
    }

    private func answerQuestion(taskID: String, text: String) {
        guard let api, !previewMode, let id = model.state.interactionID ?? model.state.resumeFrom else { return }
        Task { [weak self] in
            do { try await api.answer(interactionID: id, taskID: taskID, text: text) }
            catch {
                self?.model.answers[taskID] = nil
                self?.dispatch(.error("Couldn't send that answer. Say it instead."))
            }
        }
    }

    private func stopWork(runID run: String) {
        guard let api, let id = model.state.interactionID ?? model.state.resumeFrom else { return }
        Task { [weak self] in
            do { try await api.cancelBackend(interactionID: id, runID: run) }
            catch { self?.dispatch(.error(error.localizedDescription)) }
        }
    }

    // MARK: Preview

    public func showPreview(_ state: VoiceState, workExpanded: Bool, captionExpanded: Bool = false) {
        previewMode = true
        model.state = state
        model.workExpanded = workExpanded
        model.captionExpanded = captionExpanded
        let p = present(state)
        model.shownStatus = p.secondary
        model.shownTone = p.tone
        #if os(macOS)
        surface.onEscape = { NSApp.terminate(nil) }
        #endif
        surface.show()
    }
}
