#if os(macOS)
import AppKit
#endif
import AVFoundation
import CoreGraphics
import Foundation
import SpeakeasyCore
import os
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

/// One capture of the user's window for a capture request, encoded for upload (the Mac app makes it).
public struct CapturedScreen: Sendable {
    public let attachment: EncodedAttachment
    /// The captured app's name ("Xcode"), for the task's thumbnail and the one-time spoken notice.
    public let appName: String
    public init(attachment: EncodedAttachment, appName: String) { self.attachment = attachment; self.appName = appName }
}

/// Native call client: WebRTC transport + server SSE + pure reducer + SwiftUI panel.
@MainActor
public final class NativeVoiceClient: VoiceCallClient {
    public var onStatus: ((String) -> Void)?
    public var onError: ((String) -> Void)?
    /// The board's "can't load your work" notice went away (the board loaded again): an app that
    /// copied it from `onError` into its own banner drops it now. Never fired for a call error.
    public var onWorkNoticeCleared: (() -> Void)?
    public var onClosed: (() -> Void)?
    /// Paused (true) or resumed (false): the conversation stays open either way.
    public var onPauseChanged: ((Bool) -> Void)?
    /// A task settled while the call was paused (heads-up with a Resume action).
    public var onPausedTaskSettled: ((WorkNotice) -> Void)?
    public var startMuted = false
    /// Client preferences (Settings › General).
    public var showPanelOnStart = true
    /// Settings › General "Start calls folded into the orb" (was "slim mode").
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

    /// The call's audio transport: WebRTC, or the plugin's relay socket (Grok Voice).
    private var engine: CallTransport?
    private var startTask: Task<Void, Never>?
    private var streamTask: Task<Void, Never>?
    /// One task per loop, so starting one never cancels another: the paused call's task list, the
    /// end's finalization check, the event-stream fallback, and a dropped call's reconnect timer.
    private var pausedPollTask: Task<Void, Never>?
    private var finalizePollTask: Task<Void, Never>?
    private var eventsPollTask: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var workOnlyTask: Task<Void, Never>?
    /// Which `/voice/work/latest` answers may still touch the board (see `WorkPollGate`).
    private var workPoll = WorkPollGate()
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
    /// A call that hasn't gone live by then is stuck; retry once, then say so.
    static let connectStallAfter: TimeInterval = CallReconnect.connectStallAfter
    /// iPhone: the call has no mic because the app isn't in front (an Action Button / Siri start
    /// that landed in the background, or iOS took the input away). Reconnecting from the
    /// background can't fix that, since iOS hands the mic only to the app in front; it's fixed
    /// the moment the app is opened instead.
    private var waitingForApp = false
    private var activeObserver: NSObjectProtocol?

    private static func freshMicCheck() -> MicCheck {
        #if os(iOS) || os(visionOS)
        return MicCheck(firstSoundGrace: MicCheck.firstSoundWithinHandheld, canRestartAudio: true)
        #else
        return MicCheck()
        #endif
    }
    private var micTimer: Timer?
    /// 74.1 (F4): when each connect step happened since the tap (logged on the device, and in the
    /// call's mic-check note once the first transcript arrives).
    private var connectTimeline: ConnectTimeline?
    private static let connectLog = Logger(subsystem: Bundle.main.bundleIdentifier ?? "speakeasy", category: "call-connect")
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

    // MARK: Look at this (screen, pictures and files)

    /// What the paired plugin says about attachments (`/voice/status`). The app sets it whenever its
    /// status refreshes, and every call start refreshes it too. Nil (a plugin from before attachments)
    /// or unsupported: no screen button, drops refused, nothing declared.
    public var attachmentSupport: AttachmentSupport? {
        didSet { model.attachmentsSupported = attachmentSupport?.supported == true }
    }
    /// Screen Recording is allowed (never prompts). Set by the Mac app; nil where the screen can't be shared.
    public var screenPermitted: (@MainActor () -> Bool)?
    /// Captures the frontmost window for one capture request; throws an `AttachmentFailure`. Mac app only.
    public var captureScreen: (@MainActor () async throws -> CapturedScreen)?
    /// Opens System Settings › Screen Recording (Mac app).
    public var openScreenSettings: (@MainActor () -> Void)?
    /// The bytes of each pending picture and file (by local id), kept to re-send them after a resume.
    private var attachmentBytes: [String: EncodedAttachment] = [:]
    /// The latest `/screen` request: words said while connecting go after it.
    private var screenSend: Task<Void, Never>?
    private var uploadsScheduled = false

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
            if self.model.slim {
                self.model.workExpanded = false; self.model.selectedTaskID = nil
                if self.model.selectedJobID != nil { self.model.selectedJobID = nil; self.setFocus(taskID: nil) }
            } else {
                self.surface.focus()   // the board takes the arrow keys
            }
            self.surface.setNeedsResize()
        }
        model.onApproval = { [weak self] choice in self?.resolveApproval(choice) }
        model.onStopWork = { [weak self] in self?.stopWork() }
        model.onToggleWork = { [weak self] in self?.toggleWorkView() }
        model.onCloseWork = { [weak self] in self?.closeWorkView() }
        model.onTogglePause = { [weak self] in self?.togglePause() }
        model.onStopTask = { [weak self] runID in self?.stopWork(runID: runID) }
        model.onAnswer = { [weak self] taskID, text in self?.answerQuestion(taskID: taskID, text: text) }
        model.onSetAside = { [weak self] runID in self?.setAside(runID) }
        model.onDismissQuestion = { [weak self] runID in self?.dismissQuestion(runID) }
        model.onCardAction = { [weak self] taskID, card, action, done in
            self?.runCardAction(taskID: taskID, card: card, action: action, done: done)
        }
        model.onRetry = { [weak self] taskID in self?.retryTask(taskID) }
        model.onFixProblem = { [weak self] fix in self?.fixProblem(fix) }
        model.loadCardImage = { [weak self] runID, number in
            guard let api = await self?.api else { return nil }
            return try? await api.image(runID: runID, index: number)
        }
        model.onSelectTask = { [weak self] id in self?.selectTask(id) }
        model.onOpenTask = { [weak self] id in self?.openTask(id) }
        model.onOpenJob = { [weak self] id in self?.openJob(id) }
        model.onTellJob = { [weak self] id in self?.tellJob(id) }
        model.deliverMessage = { [weak self] message, done in
            OutboxCompletion.run(self, done: done) { $0.deliver(message, done: done) }
        }
        model.onMessageNotice = { [weak model] m in
            guard let model else { return }
            if case .failed(let why) = m.state { model.showToast(why, ok: false) }
            else if m.isSent && m.choiceTaskID == nil { model.showToast(TaskMessageCopy.sent(m)) }
        }
        model.onDismissTasks = { [weak self] runIDs in self?.dismissTasks(runIDs) }
        model.onDismissReview = { [weak self] runID in self?.dismissReview(runID) }
        model.onAnswerSeen = { [weak self] runID in
            guard let self, let api = self.api, !self.previewMode else { return }
            Task { try? await api.markAnswerSeen(runID: runID) }  // best effort: the app keeps its own list too
        }
        model.loadProductImage = { [weak self] runID, index in
            guard let self, let api = self.api else { return nil }
            return try? await api.image(runID: runID, index: index)
        }
        model.loadToolLog = { [weak self] runID in
            // The task's whole tool-call log with its redacted previews (`tool_log`): only what the
            // server reported; nothing in preview mode, where the demo carries its own.
            guard let self, !self.previewMode, let api = self.api else { return nil }
            return try? await api.work(runID: runID)?.activity
        }
        model.loadSubagents = { [weak self] runID in
            // The task's helpers with each one's steps (`job_subagents`); nothing in preview mode.
            guard let self, !self.previewMode, let api = self.api else { return nil }
            return try? await api.work(runID: runID)?.subagents
        }
        model.loadLiveImage = { [weak self] runID in
            guard let self, let api = self.api else { return nil }
            return try? await api.liveImage(runID: runID)
        }
        model.onToggleScreen = { [weak self] in self?.toggleScreen() }
        model.onAttach = { [weak self] items in self?.attach(items) }
        model.onAttachRefused = { [weak self] reason in self?.dispatch(.sharing(.notice(reason))) }
        model.onRemoveAttachment = { [weak self] id in self?.removeAttachment(id) }
        model.onRetryAttachment = { [weak self] id in
            guard let self, self.model.attachmentFailures.contains(id), let bytes = self.attachmentBytes[id] else { return }
            self.dispatch(.sharing(.removed(id)))
            self.attach([bytes])
        }
        model.onOpenScreenSettings = { [weak self] in self?.openScreenSettings?() }
        model.loadSharedImage = { [weak self] runID, number in
            guard let self, let api = self.api else { return nil }
            return try? await api.sharedImage(runID: runID, number: number)
        }
    }

    // MARK: Reducer plumbing

    /// iPhone: a live call that drops by itself is brought back (up to `CallReconnect.maxAttempts`,
    /// resuming the same conversation) instead of ending. The Mac ends it, as before.
    public var reconnectsDroppedCalls = false

    public func dispatch(_ event: VoiceEvent) {
        if model.state.reconnectsDroppedCalls != reconnectsDroppedCalls { model.state.reconnectsDroppedCalls = reconnectsDroppedCalls }
        let before = model.state
        let after = reduce(before, event, now: Date())
        model.state = after
        if after.interactionID != nil, after.interactionID != before.interactionID { postFocus() }
        postMicIfChanged()
        if after.connection != .connecting, connectWatchdog != nil { connectWatchdog?.cancel(); connectWatchdog = nil }
        closeAbandonedSessions()
        applySideEffects(from: before, to: after)
        watchMic(event, from: before, to: after)
        scheduleUploadsIfNeeded()
    }

    // MARK: Mic check

    private func watchMic(_ event: VoiceEvent, from old: VoiceState, to new: VoiceState) {
        switch event {
        case .inputDelta, .outputDelta: micCheck.heard()
        default: break
        }
        if case .inputDelta = event { markConnect(.firstTranscript) }
        // The call heard the user after listening mode was turned off: never answer over them.
        if case .inputDelta = event { roomCall?.noteHeardUser() }
        if old.connection != .live && new.connection == .live {
            markConnect(.live)
            micCheck.connectionOpened()
            micRepairPending = false
            if new.micHealth != micCheck.health { dispatch(.micHealth(micCheck.health)) }
            startMicTimer()
        }
        if !new.connection.isInCall && new.connection != .paused && micTimer != nil {
            micTimer?.invalidate(); micTimer = nil
        }
        if (old.connection.isInCall || old.connection == .paused), !new.connection.isInCall, new.connection != .paused {
            reportConnect(ended: true)
            connectTimeline = nil
        }
    }

    /// Marks a connect stage (first time only) and logs it on the device.
    private func markConnect(_ stage: ConnectTimeline.Stage) {
        guard var t = connectTimeline else { return }
        let now = Date()
        if t.mark(stage, at: now) {
            let s = String(format: "+%.1fs", now.timeIntervalSince(t.tap))
            Self.connectLog.notice("connect stage: \(stage.rawValue, privacy: .public) \(s, privacy: .public)")
        }
        connectTimeline = t
        if stage == .firstTranscript { reportConnect(ended: false) }
    }

    private func noteConnect(_ event: String) {
        guard connectTimeline != nil else { return }
        connectTimeline?.note(event, at: Date())
        Self.connectLog.notice("connect event: \(event, privacy: .public)")
    }

    /// The timeline's one summary line: on the device, and in the call's mic-check note (agent.log).
    private func reportConnect(ended: Bool) {
        guard var t = connectTimeline, t.due(ended: ended) else { return }
        t.markReported()
        connectTimeline = t
        let line = t.summary()
        Self.connectLog.notice("\(line, privacy: .public)")
        reportMic(line)
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
            if (packets ?? 0) > 0 { self.markConnect(.firstMicPacket) }
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
        // 74.1 (F4(a)): no sound at the very start is first met by restarting the audio unit on this
        // same connection (about a second); the connection is reopened (a new session, ~10 s) only
        // if that can't run or doesn't bring the sound back.
        if micCheck.repairStep(for: fault) == .restartAudio {
            if let engine, engine.restartLocalAudio() {
                micCheck.audioRestarted()
                noteConnect("mic restarted in place")
                reportMic("restarted the mic in place: \(fault.rawValue) (\(detail))")
                if model.state.micHealth != micCheck.health { dispatch(.micHealth(micCheck.health)) }
                return
            }
            micCheck.audioRestartFailed()
            if micCheck.health == .broken { dispatch(.micHealth(.broken)); return }
        }
        noteConnect("reconnect: \(fault.rawValue)")
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
        guard case .connecting(let attempt) = model.state.phase else { return }
        let stage = connectStage
        reportMic("connect stalled (attempt \(attempt.attempt)) at: \(stage); \(micReportDetail())")
        noteConnect("connect stalled at \(stage)")
        // A call from listening mode keeps what the user already said for the retried call
        // (which connects without the listener) instead of answering from the room over it.
        if roomCall != nil, let heard = earlyCapture?.heardSoFar, !cleanTranscript(heard).isEmpty {
            roomCall?.carriedWords = heard
        }
        roomNudgeTask?.cancel(); roomNudgeTask = nil   // the retried admission watches afresh
        stopEarlyCapture()
        startTask?.cancel(); startTask = nil
        streamTask?.cancel(); streamTask = nil
        eventsPollTask?.cancel(); eventsPollTask = nil
        engine?.close(); engine = nil
        #if os(macOS)
        let failure = "Couldn't connect to \(model.state.assistantName). Click Try again."
        #else
        let failure = "Couldn't connect to \(model.state.assistantName). Tap to try again."
        #endif
        // The reducer queues the session the server already admitted for this attempt (if any) in
        // sessionsToClose; dispatch closes it before the retry, so it isn't left open (and billed).
        dispatch(.connectStalled(failure: failure))
        guard case .connecting(let next) = model.state.phase, next.attempt > attempt.attempt else {
            connectWatchdog?.cancel(); connectWatchdog = nil
            return
        }
        skipEarlyCapture = true
        onStatus?("Reconnecting…")
        connect(api)
    }

    /// Close a session this device gave up on (a stalled connection attempt). Best effort: an older
    /// plugin ignores `abandon` and the session ends when its audio never arrives.
    private func closeAbandonedSessions() {
        guard !model.state.sessionsToClose.isEmpty else { return }
        let ids = model.state.sessionsToClose
        model.state.sessionsToClose = []
        guard let api, !previewMode else { return }
        for id in ids {
            reportMic("closing the stalled attempt's session")
            Task { try? await api.abandonSession(interactionID: id) }
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

    /// The task the call was last told is on screen (nil = none).
    public var focusedTaskID: String? { focusSent?.task }

    public func setFocus(taskID: String?, detail: String = "") {
        focusSent = (taskID, String(detail.prefix(200)))
        postFocus()
    }

    private func postFocus() {
        guard !previewMode, let focus = focusSent, let api, let id = model.state.interactionID else { return }
        if let p = focusPosted, p.interaction == id, p.task == focus.task, p.detail == focus.detail { return }
        focusPosted = (id, focus.task, focus.detail)
        let body: [String: Any] = ["task_id": focus.task ?? NSNull(), "detail": focus.detail]
        Task { _ = try? await api.post("/voice/interactions/\(id)/focus", body) }
    }

    /// The mic state the plugin was last told, per call: reported once when each call (or resumed
    /// call) starts, open or muted, then on every change. Muted calls get no "still working" pulse;
    /// an open mic gets the early, steady one (results, questions and approvals are spoken either way).
    private var micReport = MicReport()

    private func postMicIfChanged() {
        guard !previewMode, let api, let id = model.state.interactionID else { return }
        guard let muted = micReport.next(interaction: id, muted: model.state.mic == .muted) else { return }
        Task { _ = try? await api.post("/voice/interactions/\(id)/mic", ["muted": muted]) }
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
        if let toggle = new.sharing.outgoing, toggle != old.sharing.outgoing { sendScreen(toggle) }
        for captureID in new.sharing.captures where !old.sharing.captures.contains(captureID) { serveCapture(captureID) }
        if old.sharing.attachments != new.sharing.attachments { forgetAttachments(notIn: new.sharing.attachments) }
        if old.sharing.visibleAttachments.count != new.sharing.visibleAttachments.count || old.sharing.hold != new.sharing.hold {
            surface.setNeedsResize()
        }
        if old.exchange.isEmpty != new.exchange.isEmpty || old.approval != new.approval ||
            old.connection != new.connection || old.workInfo != new.workInfo || old.tasks != new.tasks {
            surface.setNeedsResize()
        }
        if new.connection == .paused, old.connection != .paused { callPaused() }
        if case .reconnecting(let attempt, let deadline, _) = new.phase, old.phase != new.phase { reconnectDropped(attempt, at: deadline) }
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
        if WorkPollGate.isNotice(old.lastError), new.lastError == nil { onWorkNoticeCleared?() }
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
                // Quiet for idle_pause_minutes: close the paid voice session. Pause, not End, so the
                // conversation and running tasks survive and Resume picks up where it left off.
                if model.state.idleExpired(at: Date()) {
                    let minutes = Int((model.state.idleTimeout / 60).rounded())
                    onStatus?("No audio for \(minutes) minute\(minutes == 1 ? "" : "s"): voice paused")
                    pause(quietFor: minutes)
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
        stopWaitingForApp()
        dwell = StatusDwell(minimumDwell: 1.5)
        model.workExpanded = false
        model.captionExpanded = false
        workOnlyTask?.cancel()
        connectTimeline = ConnectTimeline(tap: Date())
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
            dispatch(.failed(config.deviceToken == nil ? "Not paired yet. Pair in Settings › Connection."
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
        // What this call can share: re-checked against the plugin while the call connects.
        dispatch(.sharing(.declared(screenDeclaration())))
        refreshAttachmentSupport(api)
        connectStage = "starting"
        armConnectWatchdog(api)
        if skipEarlyCapture { stopEarlyCapture() } else { startEarlyCapture() }
        connectionUsedEarlyCapture = earlyCapture != nil
        if earlyCapture == nil { NativeCallEngine.resetSharedAudio() }
        let engine = NativeCallEngine()
        self.engine = engine
        if earlyCapture != nil { engine.holdAudio() }
        wire(engine)
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
            // The WebRTC engine until the answer says the plugin relays the audio instead.
            var call: CallTransport = engine
            do {
                await engine.warmAudioRoute()
                try engine.prepare(captureAudio: true)
                // Honour a mute chosen before/while connecting (start-muted or hotkey).
                engine.setMicEnabled(self?.model.state.localAudioEnabled ?? false)
                self?.connectStage = "creating offer"
                let sdp = try await engine.createOffer()
                self?.connectStage = "waiting for the server"
                guard let self, self.engine === engine, self.model.state.connection == .connecting else { engine.close(); return }
                // Resumes declare again: the resumed call starts with sharing off.
                let screen = self.model.state.sharing.declared
                var room: String?
                if let roomSupport, let context = self.roomCall?.room {
                    // Give a slow status a second; if it still hasn't answered, send the room
                    // anyway: a server that can't take it answers 400 and the call falls back below.
                    let supported = await Self.firstAnswer(of: roomSupport, within: 1)
                    if supported == false {
                        self.dropRoomContext("This needs a newer Speakeasy on your Hermes. Update it from Settings.")
                    } else {
                        room = context
                    }
                    guard self.engine === engine, self.model.state.connection == .connecting else { engine.close(); return }
                }
                sentRoom = room != nil
                let tour = self.model.state.resumeFrom == nil && room == nil ? self.pendingTour : nil
                let admission = try await api.admitSession(sdp: sdp, idempotencyKey: UUID().uuidString,
                                                              resumeFrom: self.model.state.resumeFrom, tour: tour,
                                                              screen: screen, room: room)
                guard self.engine === engine else { engine.close(); return }
                self.dispatch(.sharing(.declared(screen)))   // exactly what the plugin was told
                if sentRoom { self.roomCall?.markDelivered() }   // later resumes rely on the server's copy
                if tour != nil {
                    self.pendingTour = nil
                    self.model.tourActive = true
                    self.onTourStarted?()
                }
                self.voiceProvider = admission.voiceProvider
                self.codexStopRequested = false
                self.dispatch(.sessionAdmitted(interactionID: admission.interactionID))
                self.markConnect(.sessionCreated)
                self.connectStage = "admitted; connecting audio"
                let early = self.earlyCapture
                if let relay = admission.relay {
                    // Grok Voice: drop the prepared peer connection; the plugin relays PCM over a socket.
                    guard let request = api.relaySocketRequest(relay) else { throw RelayCallEngine.EngineError.badURL }
                    engine.close()
                    let relayEngine = RelayCallEngine()
                    self.engine = relayEngine
                    self.wire(relayEngine)
                    if early != nil { relayEngine.holdAudio() }
                    relayEngine.setMicEnabled(early == nil && self.model.state.localAudioEnabled)
                    call = relayEngine
                    self.connectStage = "admitted; opening the relay"
                    try relayEngine.start(request: request)
                } else {
                    if early != nil { engine.setMicEnabled(false) }
                    try await engine.setRemoteAnswer(admission.answerSDP)
                }
                self.appliedMic = nil; self.appliedRemote = nil
                self.dispatch(.tick)
                self.startServerEvents(api, interactionID: admission.interactionID)
                self.connectStage = "answer applied; waiting for the call to start"
                if let early {
                    // Mid-sentence? Let the listener hear you out before the call takes the mic.
                    await early.waitForPause()
                    early.stopListening()
                    call.releaseAudio()
                    self.handOverEarlyWords(early, api: api, interactionID: admission.interactionID)
                } else {
                    call.releaseAudio()
                    self.handOverRoomRequest("", api: api, interactionID: admission.interactionID)
                }
                if self.model.state.connection == .ending { self.sendClose() }
            } catch {
                guard let self, self.engine === call else { return }
                // The server wouldn't take what listening mode heard (an older plugin, or the
                // transcript was refused): don't loop back into listening, start a normal call.
                if sentRoom, let http = error as? ServerClient.HTTPError, http.status == 400 || http.status == 413,
                   self.model.state.connection == .connecting {
                    call.close()
                    self.dropRoomContext("Couldn't use what listening mode heard; carried on without it")
                    self.dispatch(.connectFallback(dropResume: false))
                    self.connect(api)
                    return
                }
                // The api no longer holds the paused call (restarted, or it was
                // already resumed): start a fresh call rather than failing.
                if self.model.state.resumeFrom != nil, let http = error as? ServerClient.HTTPError,
                   http.status == 404 || http.status == 409, self.model.state.connection == .connecting {
                    call.close()
                    self.dispatch(.connectFallback(dropResume: true))
                    self.dispatch(.error("Couldn't resume the paused call; started a new one"))
                    self.connect(api)
                    return
                }
                self.dispatch(.failed(ConnectionTrouble.message(for: error, server: self.config.serverURL)))
            }
        }
    }

    /// Same callbacks for either transport.
    private func wire(_ transport: CallTransport) {
        // A connection this call has dropped (a stalled or reconnected attempt, a paused call's)
        // can still deliver a last message or failure; only the current one speaks for the call.
        transport.onMessage = { [weak self, weak transport] data in
            guard let self, let transport, self.engine === transport else { return }
            for event in parseDataChannelMessage(data) {
                if case .sessionClosed = event, self.voiceProvider == "codex" {
                    self.finishCodexCall()
                } else {
                    self.dispatch(event)
                }
            }
        }
        transport.onChannelClosed = { [weak self, weak transport] in
            guard let self, let transport, self.engine === transport else { return }
            self.channelClosed()
        }
        transport.onChannelOpen = { [weak self] in
            self?.connectStage = "channel open; waiting for the call to start"
            self?.markConnect(.channelOpen)
        }
        transport.onAudioFormatChanged = { [weak self] in self?.audioDevicesChanged() }
        transport.onConnectionFailed = { [weak self, weak transport] in
            guard let self, let transport, self.engine === transport,
                  self.model.state.connection == .live || self.model.state.connection == .connecting else { return }
            self.dispatch(.transportLost)
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
    private func pause(quietFor minutes: Int? = nil) {
        guard model.state.canPause, let api, let id = model.state.interactionID else { return }
        dispatch(minutes.map { .quietPauseRequested(minutes: $0) } ?? .pauseRequested)
        Task { [weak self] in
            do {
                try await api.pause(interactionID: id)
                guard let self, self.model.state.pausing else { return }
                self.sendClose()
            } catch {
                self?.reconnectAfterPause = false
                NSLog("Speakeasy: couldn't pause: %@", String(describing: error))
                self?.dispatch(.pauseFailed("Couldn't pause. Try again."))
            }
        }
    }

    private func callPaused() {
        stopEarlyCapture()
        endDeadline?.cancel(); endDeadline = nil
        startTask?.cancel(); startTask = nil
        engine?.close(); engine = nil
        streamTask?.cancel(); streamTask = nil
        eventsPollTask?.cancel(); eventsPollTask = nil
        finalizePollTask?.cancel(); finalizePollTask = nil
        if reconnectAfterPause {
            // Audio device switch: reopen straight away on the new device.
            reconnectAfterPause = false
            resume()
            return
        }
        startPausedTaskPolling()
        onPauseChanged?(true)
    }

    // MARK: Reconnecting a dropped call (iPhone)

    /// The live call dropped by itself (or a reconnect didn't take): drop what's left of the
    /// connection, then try again at `deadline`, resuming the same conversation (a fresh call when
    /// the server no longer holds it). The reducer counts the attempts and gives up after the last.
    private func reconnectDropped(_ attempt: Int, at deadline: Date) {
        connectWatchdog?.cancel(); connectWatchdog = nil
        stopEarlyCapture()
        endDeadline?.cancel(); endDeadline = nil
        startTask?.cancel(); startTask = nil
        engine?.close(); engine = nil
        streamTask?.cancel(); streamTask = nil
        eventsPollTask?.cancel(); eventsPollTask = nil
        finalizePollTask?.cancel(); finalizePollTask = nil
        reportMic("call dropped; reconnect \(attempt) of \(CallReconnect.maxAttempts)")
        reconnectTask?.cancel()
        reconnectTask = Task { [weak self] in
            let wait = max(0, deadline.timeIntervalSinceNow)
            try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            guard !Task.isCancelled, let self else { return }
            self.dispatch(.reconnectDue(attempt: attempt))
            guard case .connecting(let c) = self.model.state.phase, c.reconnect == attempt, let api = self.api else { return }
            self.appliedMic = nil; self.appliedRemote = nil
            self.connect(api)
        }
    }

    /// "Stop" on the reconnect banner: no more attempts; the call ends.
    public func cancelReconnect() {
        reconnectTask?.cancel(); reconnectTask = nil
        dispatch(.reconnectCancelled)
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
        pausedPollTask?.cancel(); pausedPollTask = nil
        appliedMic = nil; appliedRemote = nil
        dispatch(.resumeRequested)
        if unmuted { dispatch(.setMic(.live)) }
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
        pausedPollTask?.cancel()
        pausedPollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                guard let self, self.model.state.connection == .paused else { return }
                var fresh: [String: WorkInfo] = [:]
                for task in self.model.state.tasks where task.isActive {
                    if let run = task.info.runID, let info = try? await api.work(runID: run) { fresh[task.id] = info }
                }
                guard self.model.state.connection == .paused else { return }
                // Onto the list as it is now (it may have changed while the rows were fetched).
                let tasks = TaskRefresh.merge(self.model.state.tasks, fresh: fresh)
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
        DispatchQueue.main.asyncAfter(deadline: .now() + VoiceState.endConfirmWithin, execute: deadline)
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
        finalizePollTask?.cancel()
        finalizePollTask = Task { [weak self] in
            while !Task.isCancelled {
                if let snapshot = try? await api.interaction(id) {
                    // An older server's snapshot doesn't name its call: it is this one's.
                    var own = snapshot
                    if own.interactionID == nil { own.interactionID = id }
                    self?.dispatch(.interaction(own))
                }
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
        // Sharing turned on while connecting reaches the plugin before these words do.
        await screenSend?.value
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
        eventsPollTask?.cancel(); eventsPollTask = nil
        finalizePollTask?.cancel(); finalizePollTask = nil
        pausedPollTask?.cancel(); pausedPollTask = nil
        reconnectTask?.cancel(); reconnectTask = nil
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
        if model.state.workOnly { workOnlyTask?.cancel(); workOnlyTask = nil; workPoll.stop(); dispatch(.reset) }
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
                NSLog("Speakeasy: couldn't %@ a draft: %@", action.rawValue, String(describing: error))
                self.model.draftNotice[draft.draftID] = "Couldn't \(action.rawValue) that. Try again."
            }
        }
    }

    /// Refetch tasks so the card shows the server's current draft (SSE also pushes it).
    private func refreshDraftSources() async {
        guard let api else { return }
        let ticket = workPoll.ticket()
        if let (work, tasks) = try? await api.latestWork(), workPoll.accepts(ticket) {
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

    /// Re-read the tasks now (a question answered on another device, a session changed): the board,
    /// Live Activity and widgets follow the model, so they clear within the feed's ~1 s.
    public func refreshTasks() {
        Task { [weak self] in await self?.refreshDraftSources() }
    }

    public func hideIfIdle() {
        if !model.state.connection.isOpen && !model.state.workOnly && !previewMode { hideNow() }
    }

    private func handleEscape() {
        if model.selectedJobID != nil { closeJob() }
        else if model.selectedTaskID != nil && model.workExpanded { model.selectedTaskID = nil; surface.setNeedsResize() }
        else if model.workExpanded && !model.state.workOnly { model.workExpanded = false; surface.setNeedsResize() }
        else if model.state.workOnly { closeWorkView() }
        // Escape folds an open call into the orb; from the orb it ends the call, as before.
        else if model.state.connection.isOpen && !model.showsSlim { model.onToggleSlim() }
        else if model.state.connection.isOpen { end() }
        else { hideNow() }
    }

    // MARK: Server events (SSE with polling fallback)

    /// How long the polling fallback runs before the event stream is tried again.
    static let streamRetryAfter: TimeInterval = 30

    private func startServerEvents(_ api: ServerClient, interactionID: String) {
        streamTask?.cancel()
        streamTask = Task { [weak self] in
            var parser = SSEParser()
            var attempt = 0
            while !Task.isCancelled {
                let outcome = await api.streamEvents(interactionID: interactionID, parser: &parser) { event in
                    await MainActor.run {
                        // A stream left over from a session this call no longer uses says nothing to it.
                        guard let self, self.model.state.interactionID == interactionID else { return }
                        for voiceEvent in parseServerStreamEvent(event) { self.dispatch(voiceEvent) }
                    }
                }
                guard let self, !Task.isCancelled else { return }
                switch outcome {
                case .unsupported:
                    self.startPollingFallback(api, interactionID: interactionID, retryStream: false)
                    return
                case .ended:
                    attempt = 0
                    if !self.model.state.connection.isInCall { return }
                case .failed:
                    attempt += 1
                    if attempt >= 4 { self.startPollingFallback(api, interactionID: interactionID, retryStream: true); return }
                }
                let delay = sseReconnectDelay(attempt: attempt, serverRetryMilliseconds: parser.retryMilliseconds)
                try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            }
        }
    }

    /// Server without `/events` (or a stream that keeps failing): poll instead. Capture requests come
    /// from the snapshot here. A stream that failed is tried again every `streamRetryAfter`; polling
    /// stops once it is back.
    private func startPollingFallback(_ api: ServerClient, interactionID: String, retryStream: Bool) {
        eventsPollTask?.cancel()
        let started = Date()
        eventsPollTask = Task { [weak self] in
            var lastWork = Date.distantPast
            while !Task.isCancelled {
                guard let self, self.model.state.connection.isInCall,
                      self.model.state.interactionID == interactionID else { return }
                if retryStream, Date().timeIntervalSince(started) >= Self.streamRetryAfter {
                    self.startServerEvents(api, interactionID: interactionID)
                    return
                }
                if var snapshot = try? await api.interaction(interactionID) {
                    if snapshot.interactionID == nil { snapshot.interactionID = interactionID }
                    for event in interactionEvents(snapshot) { self.dispatch(event) }
                }
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
        // A new loop: whatever the replaced one still has in flight is dropped, and its
        // cancellation is not an error (it was the "Work unavailable: cancelled" banner).
        let loop = workPoll.startLoop()
        // Reloading (Try again, back to the foreground): the old notice goes; if the board still
        // can't load, the new loop says so again after its own failures.
        if WorkPollGate.isNotice(model.state.lastError) { dispatch(.workNoticeCleared) }
        workOnlyTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let ticket = self?.workPoll.ticket(generation: loop) else { return }
                do {
                    let (work, tasks) = try await api.latestWork()
                    guard let self, !Task.isCancelled, self.model.state.workOnly else { return }
                    // Sent before a newer loop or before the user cleared tasks: may be older than the board.
                    guard self.workPoll.accepts(ticket) else { continue }
                    self.workPoll.succeeded(ticket)
                    // Loading again: the "can't load your work" notice goes by itself (a call error stays).
                    if WorkPollGate.isNotice(self.model.state.lastError) { self.dispatch(.workNoticeCleared) }
                    self.dispatch(.work(work))
                    if tasks != self.model.state.tasks { self.dispatch(.tasks(tasks)) }
                } catch {
                    guard let self else { return }
                    if let notice = self.workPoll.failed(error, ticket: ticket, cancelled: Task.isCancelled) {
                        self.dispatch(.error(notice))
                    }
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
        } else if let job = model.jobs.first(where: { j in j.tasks.contains { $0.id == show.taskID } }) {
            // Asked to see it: the review lives in its job's view.
            if let run = show.runID {
                model.focusedReviewID = run
                // Pictures folded under an answer: asked to see them, unfold them.
                if job.tasks.contains(where: { $0.id == show.taskID && $0.info.reviewIsIncidental }) {
                    model.cardOpen["\(job.id)|pictures:\(run)"] = true
                }
            }
            openJob(job.id)
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

    /// Opens a session page (the History window), set by the app. Every way into a task lands there
    /// (`VisualTaskRoute`); the panel's own job page is only the fallback.
    public var openSessionPage: ((String) -> Void)?

    /// The session page standing in for a job's or task's old page, or nil to keep the panel's.
    private func visualSession(job: Job? = nil, taskID: String? = nil) -> String? {
        guard !previewMode, openSessionPage != nil else { return nil }
        let history = model.capabilities?.history == true
        if let job { return VisualTaskRoute.sessionID(for: job, historySupported: history) }
        if let taskID {
            return VisualTaskRoute.sessionID(forTask: taskID, in: model.jobs, tasks: model.state.tasks, historySupported: history)
        }
        return nil
    }

    /// One task from the task list: its session page, else the panel's task view.
    private func openTask(_ id: String) {
        if let session = visualSession(taskID: id) {
            if let job = model.jobs.first(where: { j in j.tasks.contains { $0.id == id } }) { model.markAnswerSeen(job) }
            openSessionPage?(session)
            return
        }
        selectTask(id)
    }

    /// Open a job from the board (nil = back to the board). It opens in its session page when it has
    /// one (live line, So far, tool steps, helpers, approvals); otherwise in the panel, where the call
    /// is told which job is on screen, so "do the first one" goes there; leaving the job clears it.
    private func openJob(_ id: String?) {
        guard let id, let job = model.jobs.first(where: { $0.id == id }) else { closeJob(); return }
        if let session = visualSession(job: job) {
            if model.selectedJobID != nil { closeJob() }
            model.boardFocusID = id
            model.markAnswerSeen(job)
            openSessionPage?(session)
            return
        }
        if model.showsSlim { model.slim = false }
        model.selectedTaskID = nil
        model.selectedJobID = id
        model.boardFocusID = id
        model.markAnswerSeen(job)
        if model.tellingJobID != id { model.tellingJobID = nil }
        model.workExpanded = true
        setFocus(taskID: job.newest.id, detail: job.name)
        surface.setNeedsResize()
        surface.focus()
    }

    private func closeJob() {
        let open = model.selectedJobID != nil
        // Anything that arrived in the job while it was open has been seen.
        if let job = model.selectedJob { model.markAnswerSeen(job) }
        model.selectedJobID = nil
        model.tellingJobID = nil
        model.workExpanded = false
        if open { setFocus(taskID: nil) }
        surface.setNeedsResize()
    }

    // MARK: Messages to one task (contract: typed messages)

    /// Typed text to one task by run id (`POST /voice/work/{run}/message`). Works with or without a call.
    public func sendMessage(runID: String, text: String, source: TaskMessageSource = .typed,
                            clientID: String = UUID().uuidString, choiceTaskID: String? = nil,
                            attachments: [String] = []) async throws -> TaskMessageReply {
        guard let api else { throw ServerClient.HTTPError(status: 0, message: "Not paired") }
        let interaction = model.state.connection.isOpen ? (model.state.interactionID ?? model.state.resumeFrom) : nil
        return try await api.sendMessage(runID: runID, text: text, source: source, clientID: clientID,
                                         interactionID: interaction, choiceTaskID: choiceTaskID, attachments: attachments)
    }

    private func deliver(_ m: OutgoingMessage, done: @escaping (TaskMessageReply?, String?) -> Void) {
        let taskID = model.state.tasks.last(where: { $0.info.runID == m.runID })?.id ?? m.runID
        if previewMode {
            // Screenshots and the sample: as if it went (no network).
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) { [weak self] in
                if self?.model.state.connection.isOpen == true {
                    self?.dispatch(.typedToTask(text: m.text, taskID: taskID, taskName: m.taskName))
                }
                done(TaskMessageReply(runID: m.runID, deliveredAs: m.choiceTaskID == nil ? .steer : .answer), nil)
            }
            return
        }
        Task { [weak self] in
            // The client went away before the send started: the message still settles (failed, Retry).
            guard let self else { done(nil, OutboxCompletion.ownerGone); return }
            do {
                let reply = try await self.sendMessage(runID: m.runID, text: m.text, source: m.source, clientID: m.id,
                                                       choiceTaskID: m.choiceTaskID, attachments: m.attachments.map(\.id))
                // The user's own turn (typed, not spoken), only while a call is there to show it in.
                if self.model.state.connection.isOpen {
                    self.dispatch(.typedToTask(text: m.text, taskID: taskID, taskName: m.taskName))
                }
                done(reply, nil)
            } catch let http as ServerClient.HTTPError {
                done(nil, TaskMessageCopy.failure(status: http.status, serverMessage: http.message))
            } catch {
                done(nil, TaskMessageCopy.failure(status: nil, serverMessage: nil))
            }
        }
    }

    /// "Tell it something": the job is already in focus; open the mic so the next words go to it.
    private func tellJob(_ id: String) {
        guard let job = model.jobs.first(where: { $0.id == id }) else { return }
        model.tellingJobID = id
        setFocus(taskID: job.newest.id, detail: job.name)
        if model.state.mic == .muted && model.state.connection.isOpen { dispatch(.toggleMic) }
        surface.setNeedsResize()
    }

    private func toggleWorkView() {
        if model.showsSlim { model.slim = false; model.workExpanded = true }
        else { model.workExpanded.toggle() }
        surface.setNeedsResize()
        if model.workExpanded { surface.focus() }
    }

    private func closeWorkView() {
        if model.selectedJobID != nil { closeJob(); return }
        if model.selectedTaskID != nil && model.state.tasks.count > 1 {
            model.selectedTaskID = nil   // Back goes from one task to the task list
            surface.setNeedsResize()
            return
        }
        model.selectedTaskID = nil
        model.workExpanded = false
        if model.state.workOnly {
            workOnlyTask?.cancel(); workOnlyTask = nil
            workPoll.stop()
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
        // A list fetched before the server has the clear may still hold these tasks: drop it.
        workPoll.listChanged()
        surface.setNeedsResize()
        guard let api, !previewMode else { return }
        Task { [weak self] in
            do { try await api.dismissTasks(runIDs: runIDs); self?.workPoll.listChanged() }
            catch {
                self?.workPoll.listChanged()
                NSLog("Speakeasy: couldn't clear tasks: %@", String(describing: error))
                self?.dispatch(.error("Couldn't clear tasks. Try again."))
            }
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
            catch {
                NSLog("Speakeasy: couldn't dismiss a review: %@", String(describing: error))
                self?.dispatch(.error("Couldn't dismiss that. Try again."))
            }
        }
    }

    /// A decision's ✕: already off Needs you here; tell the api so every device clears it (the agent
    /// hears nothing). A plugin without question actions (404) keeps it hidden on this device only.
    private func dismissQuestion(_ runID: String) {
        surface.setNeedsResize()
        guard let api, !previewMode else { return }
        Task { try? await api.dismissQuestion(runID: runID) }
    }

    /// "Not needed": already hidden from Needs you; tell the api so the task, the run and the voice
    /// all let it go. On failure it comes back so it isn't silently lost.
    private func setAside(_ runID: String) {
        surface.setNeedsResize()
        guard let api, !previewMode else { return }
        Task { [weak self] in
            do { try await api.setAside(runID: runID) }
            catch {
                guard let self else { return }
                if let task = self.model.state.tasks.first(where: { $0.info.runID == runID }) {
                    self.model.setAsideTasks.remove(task.id)
                }
                self.dispatch(.error("Couldn't set that aside. Say it instead."))
            }
        }
    }

    private func answerQuestion(taskID: String, text: String) {
        guard let api, !previewMode else { return }
        let task = model.state.tasks.first(where: { $0.id == taskID })
        // No call open (the board on its own): the answer is addressed by the task's run id, so it
        // reaches the server instead of only looking answered here. A call still takes it, so the
        // voice hears it.
        let route = QuestionAnswerRoute.choose(callOpen: model.state.connection.isOpen,
                                               interactionID: model.state.interactionID ?? model.state.resumeFrom,
                                               runID: task?.info.runID,
                                               questionState: model.capabilities?.questionState == true,
                                               taskMessages: model.taskMessagesEnabled)
        switch route {
        case .byRun(let run), .byMessage(let run):
            let byRun = route == .byRun(run)
            let client = PickStore.clientID(questionID: run, text: text)
            Task { [weak self] in
                do {
                    if byRun { try await api.answerQuestion(runID: run, text: text, clientID: client) }
                    else { _ = try await api.sendMessage(runID: run, text: text, clientID: client, choiceTaskID: nil) }
                } catch let http as ServerClient.HTTPError where http.status == 409 && byRun {
                    // Already answered (here or on another device): the server's state shows it.
                    self?.model.answers[taskID] = nil
                } catch let http as ServerClient.HTTPError where (400..<500).contains(http.status) {
                    self?.model.answers[taskID] = nil
                    self?.dispatch(.error("Couldn't send that answer. Try again."))
                } catch {
                    self?.dispatch(.error("Your answer may still be on its way. If nothing happens, tap it again."))
                }
            }
        case .none:
            model.answers[taskID] = nil
            dispatch(.error("Couldn't send that answer. Say it instead."))
        case .call(let id):
            Task { [weak self] in
                do { try await api.answer(interactionID: id, taskID: taskID, text: text) }
                catch let http as ServerClient.HTTPError where (400..<500).contains(http.status) {
                    // The api refused it (call ended, task not in this call): the question is still open.
                    self?.model.answers[taskID] = nil
                    self?.dispatch(.error("Couldn't send that answer. Say it instead."))
                } catch {
                    // A timeout or a slow reply: the api may well have taken it (seen live, the follow-up
                    // was already working). Keep the pick settled; retapping the same answer is harmless.
                    self?.dispatch(.error("Your answer may still be on its way. If nothing happens, say it instead."))
                }
            }
        }
    }

    /// A card button (contract §2): through the call when one is open, else on the run itself.
    private func runCardAction(taskID: String, card: ViewCard, action: CardAction, done: @escaping (Bool, String) -> Void) {
        guard let api, !previewMode, model.capabilities?.cardActions == true,
              let run = model.state.tasks.first(where: { $0.id == taskID })?.info.runID else {
            done(false, "Ask out loud instead: this server can't run card buttons yet"); return
        }
        let interaction = model.state.connection.isOpen ? (model.state.interactionID ?? model.state.resumeFrom) : nil
        let value: Any? = { if case .null = action.value { return nil }; return action.value.foundation }()
        Task {
            do {
                let message = try await api.cardAction(interactionID: interaction, runID: run, view: card.viewIndex,
                                                       action: action.id, value: value)
                done(true, message ?? "Done")
            } catch let http as ServerClient.HTTPError {
                NSLog("Speakeasy: card action refused: HTTP %d %@", http.status, http.message)
                done(false, "That didn't go through. Try again.")
            } catch {
                done(false, "Couldn't reach your Hermes. Try again.")
            }
        }
    }

    /// Retry a failed task: ask its job to try again through the same answer route as anything
    /// else said to it (the agent re-runs it with what it already knows).
    private func retryTask(_ taskID: String) {
        guard let task = model.state.tasks.first(where: { $0.id == taskID }) else { return }
        let what = task.info.request ?? task.name
        answerQuestion(taskID: taskID, text: "That failed. Please try again: \(what)")
        model.showToast("Asked it to try again")
    }

    /// The fix button on a call problem.
    public func fixProblem(_ fix: CallProblem.Fix) {
        switch fix {
        case .openMicSettings:
            #if os(macOS)
            if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
                NSWorkspace.shared.open(url)
            }
            #else
            if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
            #endif
        case .restartCall:
            guard model.state.canPause, !reconnectAfterPause else { return }
            micRepairPending = true
            skipEarlyCapture = true
            dispatch(.micHealth(.repairing))
            reportMic("restart requested from the panel; \(micReportDetail())")
            reconnectAfterPause = true
            pause()
        case .retry:
            dispatch(.reset)
            start()
        case .signInCodex, .chooseVoice:
            dispatch(.reset)
            onAccountFix?(fix)
        case .updatePlugin:
            dispatch(.reset)
            model.onPluginUpdate()
        case .reloadBoard:
            dispatch(.workNoticeCleared)
            if !model.state.connection.isInCall { showWork(active: false) }
        }
    }

    /// The app opens the Sign in to ChatGPT sheet or Settings › Voice (CallProblem.Fix).
    public var onAccountFix: ((CallProblem.Fix) -> Void)?

    private func stopWork(runID run: String) {
        guard let api, let id = model.state.interactionID ?? model.state.resumeFrom else { return }
        Task { [weak self] in
            do { try await api.cancelBackend(interactionID: id, runID: run) }
            catch { self?.dispatch(.error(error.localizedDescription)) }
        }
    }

    // MARK: Look at this: screen sharing

    /// What this call tells the plugin: nothing unless the plugin takes attachments, Hermes can read
    /// images and this app can capture; then `ready`, or `no_permission` while Screen Recording is off.
    private func screenDeclaration() -> ScreenDeclaration? {
        guard attachmentSupport?.supported == true, let permitted = screenPermitted, captureScreen != nil else { return nil }
        return permitted() ? .ready : .noPermission
    }

    /// Re-reads `/voice/status` while the call connects (the plugin may have been updated, or was
    /// unreachable when the app last asked). Never delays the call: it applies only if it answers
    /// before the session is admitted; later answers count from the next call or resume.
    private func refreshAttachmentSupport(_ api: ServerClient) {
        guard screenPermitted != nil, !previewMode else { return }
        Task { [weak self] in
            guard let status = try? await api.status() else { return }
            guard let self else { return }
            self.attachmentSupport = status.attachments
            self.model.capabilities = status.capabilities
            if self.model.state.connection == .connecting, self.model.state.interactionID == nil {
                self.dispatch(.sharing(.declared(self.screenDeclaration())))
            }
        }
    }

    /// The screen button and shortcut. Pressed while connecting, it's sent once the call is admitted;
    /// without Screen Recording it shows the Set up line instead.
    public func toggleScreen() {
        guard model.state.connection.isInCall else { return }
        dispatch(.sharing(.toggle))
    }

    /// Sends one toggle; the reply is the plugin's state in force. A failure rolls the button back.
    private func sendScreen(_ toggle: ScreenToggle) {
        guard let api, let id = model.state.interactionID, !previewMode else { return }
        let previous = screenSend
        screenSend = Task { [weak self] in
            await previous?.value   // in order: the plugin ignores an older seq anyway, but keep the wire tidy
            do {
                let state = try await api.setScreen(interactionID: id, toggle)
                guard let self, self.model.state.interactionID == id else { return }
                self.dispatch(.sharing(.state(state)))
            } catch {
                guard let self, self.model.state.interactionID == id else { return }
                self.dispatch(.sharing(.toggleFailed(seq: toggle.seq, reason: (error as? ServerClient.HTTPError)?.reason)))
            }
        }
    }

    /// One capture request, while sharing is on: ask the plugin first (410 = it no longer wants one,
    /// so nothing is captured), capture, upload by capture id; or report why there's no capture.
    private func serveCapture(_ captureID: String) {
        guard let api, let id = model.state.interactionID, let capture = captureScreen, !previewMode else { return }
        Task { [weak self] in
            do { try await api.reportCapture(interactionID: id, captureID: captureID, .capturing) }
            catch { return }   // closed (410), or no answer: never capture without the plugin's go-ahead
            guard let self, self.model.state.interactionID == id, self.model.state.sharing.isOn else { return }
            let shot: CapturedScreen
            do {
                shot = try await capture()
            } catch {
                let failure = error as? AttachmentFailure ?? .noWindow
                try? await api.reportCapture(interactionID: id, captureID: captureID, .failed(failure))
                return
            }
            // Sharing turned off while capturing: the capture is dropped (the plugin closed the request).
            guard self.model.state.interactionID == id, self.model.state.sharing.isOn else { return }
            // Extends the plugin's wait while the bytes travel; a 410 here only means it already has them.
            Task { try? await api.reportCapture(interactionID: id, captureID: captureID, .uploading) }
            do {
                _ = try await api.uploadAttachment(interactionID: id, shot.attachment, as: .screen,
                                                   captureID: captureID, app: shot.appName)
            } catch let error as ServerClient.HTTPError where error.status == 410 {
                return   // the request closed meanwhile (timed out, sharing off): nothing kept
            } catch let error as ServerClient.HTTPError {
                let failure: AttachmentFailure = error.status == 413 || error.reason == "too_large" ? .tooLarge
                    : error.status == 415 ? .unsupported : .timeout
                try? await api.reportCapture(interactionID: id, captureID: captureID, .failed(failure))
            } catch {
                try? await api.reportCapture(interactionID: id, captureID: captureID, .failed(.timeout))
            }
        }
    }

    // MARK: Look at this: pictures and files

    /// Dropped or pasted pictures and files (already encoded, see `AttachmentEncoder`). They wait on
    /// the panel and go with the next request; up to three at a time. Only during a call (paused
    /// included: they're sent after Resume); refused when this Hermes can't take them.
    public func attach(_ items: [EncodedAttachment]) {
        guard model.state.connection.isOpen, !model.state.workOnly, !items.isEmpty else { return }
        guard attachmentSupport?.supported == true else {
            dispatch(.sharing(.notice(AttachmentSupport.refusalReason(attachmentSupport))))
            return
        }
        for item in items {
            let id = UUID().uuidString
            dispatch(.sharing(.added(PendingAttachment(id: id, kind: item.kind == .image ? .picture : .file,
                                                       name: item.filename, byteCount: item.data.count))))
            guard model.state.sharing.attachments.contains(where: { $0.id == id }) else { break }   // over the limit
            attachmentBytes[id] = item
            model.attachmentDurations[id] = item.videoDuration
            if item.kind == .image || item.videoPoster != nil { makeThumbnail(id, item) }
        }
    }

    /// The ✕ on a pending picture or file: hidden at once, then taken back off the plugin.
    public func removeAttachment(_ id: String) {
        guard let item = model.state.sharing.attachments.first(where: { $0.id == id }), !item.removing else { return }
        dispatch(.sharing(.removing(id)))
        switch item.phase {
        case .local: dispatch(.sharing(.removed(id)))
        case .uploading: break   // its upload's answer takes it back off
        case .pending, .sending: deleteAttachment(item)
        }
    }

    private func deleteAttachment(_ item: PendingAttachment) {
        guard let api, let serverID = item.serverID, let interactionID = item.interactionID, !previewMode else {
            dispatch(.sharing(.removed(item.id)))
            return
        }
        Task { [weak self] in
            do {
                try await api.removeAttachment(interactionID: interactionID, attachmentID: serverID)
                self?.dispatch(.sharing(.removed(item.id)))
            } catch let error as ServerClient.HTTPError where error.reason == "sent" {
                self?.dispatch(.sharing(.removed(item.id)))
                self?.dispatch(.sharing(.notice("sent")))
            } catch let error as ServerClient.HTTPError where error.status == 404 || error.reason == "ended" {
                self?.dispatch(.sharing(.removed(item.id)))   // the plugin no longer holds it
            } catch {
                self?.dispatch(.sharing(.removeFailed(item.id)))
            }
        }
    }

    /// Uploads what's waiting on this device once the call takes uploads (after a drop, at admission,
    /// after a resume). Runs after the dispatch that made it necessary, never inside it. An item this
    /// call already turned away (404 / `ended`) waits for the next session instead of looping.
    private func scheduleUploadsIfNeeded() {
        let s = model.state
        guard !uploadsScheduled, !previewMode, let interactionID = s.interactionID,
              !s.sharing.uploadable(to: interactionID).isEmpty,
              s.connection == .live || s.connection == .connecting, !s.pausing else { return }
        uploadsScheduled = true
        DispatchQueue.main.async { [weak self] in
            self?.uploadsScheduled = false
            self?.uploadWaitingAttachments()
        }
    }

    private func uploadWaitingAttachments() {
        let s = model.state
        guard let api, let interactionID = s.interactionID, s.connection == .live || s.connection == .connecting,
              !s.pausing else { return }
        for item in s.sharing.uploadable(to: interactionID) {
            guard let bytes = attachmentBytes[item.id] else { dispatch(.sharing(.removed(item.id))); continue }
            dispatch(.sharing(.uploading(item.id, interactionID: interactionID)))
            Task { [weak self] in
                do {
                    let serverID = try await api.uploadAttachment(interactionID: interactionID, bytes,
                                                                  as: item.kind == .picture ? .picture : .file,
                                                                  progress: { [weak self] value in
                        Task { @MainActor in self?.model.attachmentProgress[item.id] = value }
                    })
                    guard let self else { return }
                    self.dispatch(.sharing(.uploaded(item.id, serverID: serverID, interactionID: interactionID)))
                    // ✕ pressed while it was uploading: take it back off now.
                    if let current = self.model.state.sharing.attachments.first(where: { $0.id == item.id }),
                       current.removing, current.serverID == serverID {
                        self.deleteAttachment(current)
                    }
                } catch let error as ServerClient.HTTPError where error.reason == "ended" || error.status == 404 {
                    // The call paused (or the plugin lost it): kept here and sent to the next session,
                    // never again to this one (CallSharing.uploadable).
                    self?.dispatch(.sharing(.uploadDeferred(item.id, interactionID: interactionID)))
                } catch let error as ServerClient.HTTPError {
                    let reason = error.reason ?? (error.status == 413 ? "too_large" : error.status == 415 ? "unsupported" : nil)
                    if bytes.videoDuration != nil {
                        self?.model.attachmentFailures.insert(item.id)
                        self?.dispatch(.sharing(.uploadDeferred(item.id, interactionID: interactionID)))
                        self?.dispatch(.sharing(.notice(reason ?? "failed")))
                    } else { self?.dispatch(.sharing(.refused(item.id, reason: reason))) }
                } catch {
                    if bytes.videoDuration != nil {
                        self?.model.attachmentFailures.insert(item.id)
                        self?.dispatch(.sharing(.uploadDeferred(item.id, interactionID: interactionID)))
                    } else { self?.dispatch(.sharing(.refused(item.id, reason: nil))) }
                }
            }
        }
    }

    /// A small preview for the pending row, made off the main thread.
    private func makeThumbnail(_ id: String, _ item: EncodedAttachment) {
        Task.detached(priority: .utility) { [weak self] in
            guard let image = AttachmentEncoder.thumbnail(of: item) else { return }
            await self?.showThumbnail(image, for: id)
        }
    }

    private func showThumbnail(_ image: CGImage, for id: String) {
        guard attachmentBytes[id] != nil else { return }   // already sent or removed
        model.attachmentThumbnails[id] = image
    }

    /// Sent, removed, refused or the call ended: let go of their bytes and previews.
    private func forgetAttachments(notIn kept: [PendingAttachment]) {
        let ids = Set(kept.map(\.id))
        for id in attachmentBytes.keys where !ids.contains(id) { attachmentBytes[id] = nil }
        for id in model.attachmentThumbnails.keys where !ids.contains(id) { model.attachmentThumbnails[id] = nil }
        for id in model.attachmentDurations.keys where !ids.contains(id) { model.attachmentDurations[id] = nil }
        for id in model.attachmentProgress.keys where !ids.contains(id) { model.attachmentProgress[id] = nil }
        model.attachmentFailures.formIntersection(ids)
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
