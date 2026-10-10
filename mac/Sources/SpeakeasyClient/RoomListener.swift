import AVFoundation
import Foundation
import SpeakeasyCore
#if compiler(>=6.2)
import Speech
#endif
#if os(macOS)
import CoreAudio
#elseif os(iOS)
import WebRTC
#elseif os(visionOS)
import LiveKitWebRTC
#endif

/// Why listening mode couldn't start, or couldn't transcribe a file (`--room-smoke`).
public enum RoomListenerError: Error, Equatable {
    case unavailable(RoomUnavailability)
    case unreadableFile(String)

    public var message: String {
        switch self {
        case .unavailable(let reason): return reason.message
        case .unreadableFile(let why): return "Couldn't read the audio file: \(why)"
        }
    }
}

/// Listening mode's engine: transcribes the room on this device, for as long as it's on, and never
/// answers. Nothing leaves the Mac here; the text is handed to a call only when the user turns
/// listening mode off.
///
/// Built on Apple's SpeechAnalyzer + SpeechTranscriber (macOS 26+), made for long-form, distant
/// audio such as meetings, fully on-device, and needing no speech-recognition permission. The
/// older `SFSpeechRecognizer` (still used by `EarlyCapture`) restarts its text after pauses and
/// loses words over long sessions, so it isn't used here.
///
/// This class is always available so the app can hold one without availability checks: on an
/// older system, or a build made with a toolchain that has no SpeechAnalyzer, `start()` reports
/// `.unavailable(.systemTooOld)` and `isSupported` is false.
///
/// Mic contention has burned Speakeasy before: `stop()` releases the audio engine completely
/// before it returns, so the call that follows can take the mic at once.
@MainActor
public final class RoomListener {
    /// How long `stop()` waits for the transcriber to finish the words in progress.
    public static let finalizeTimeout: TimeInterval = 0.5
    /// Engine configuration changes come in bursts (a Bluetooth headset switching to call audio
    /// sends several); restart the mic once they settle.
    static let restartDebounce: TimeInterval = 0.3

    // MARK: Support

    /// True when this build and system can run listening mode: built with SpeechAnalyzer
    /// (Xcode 26+), running macOS/iOS/visionOS 26+, and the device has an on-device transcriber.
    /// The language and the model download are checked when listening starts
    /// (`unavailability()` checks them ahead of time).
    public static var isSupported: Bool {
        #if compiler(>=6.2)
        if #available(macOS 26, iOS 26, visionOS 26, *) { return SpeechTranscriber.isAvailable }
        #endif
        return false
    }

    /// Why listening mode can't run here, or nil when it can (the speech model may still need a
    /// download on first use). For Settings, which explains why the option is missing.
    public static func unavailability(locale: Locale = .current) async -> RoomUnavailability? {
        #if compiler(>=6.2)
        if #available(macOS 26, iOS 26, visionOS 26, *) {
            guard SpeechTranscriber.isAvailable else { return .noTranscriber }
            guard await SpeechTranscriber.supportedLocale(equivalentTo: locale) != nil else {
                return .languageNotSupported(language: RoomTranscription.displayName(locale))
            }
            return nil
        }
        #endif
        return .systemTooOld
    }

    // MARK: State

    /// Where listening stands. `.off` until `start()`, and again after `stop()` or `discard()`.
    public private(set) var state: RoomListeningState = .off {
        didSet { if state != oldValue { onStateChange?(state) } }
    }
    /// Everything heard so far (kept across `stop()`; dropped by `discard()`).
    public private(set) var transcript = RoomTranscript()
    /// The mic in use is a Bluetooth headset (AirPods): it mostly hears the wearer.
    public private(set) var inputIsBluetooth = false {
        didSet { if inputIsBluetooth != oldValue { onInputChange?(inputIsBluetooth) } }
    }
    /// The mic has delivered real sound (not digital silence) since `start()`.
    public private(set) var hasSignal = false

    /// Called on the main actor whenever `state` changes.
    public var onStateChange: ((RoomListeningState) -> Void)?
    /// Called on the main actor whenever words are added or the words in progress change.
    public var onTranscriptChange: ((RoomTranscript) -> Void)?
    /// Called once per `start()` when the mic first delivers real sound, like `EarlyCapture`.
    public var onSignal: (() -> Void)?
    /// The mic's peak level (linear 0...1), up to ten times a second while the mic is open. For
    /// meters; the dead-mic check already runs inside (see `RoomSignalMonitor`).
    public var onLevel: ((Double) -> Void)?
    /// Called when the input switches to or from a Bluetooth headset.
    public var onInputChange: ((_ bluetooth: Bool) -> Void)?
    /// What happened, in a line, for the app's log (restarts, failures, model downloads).
    public var onLog: ((String) -> Void)?

    private let locale: Locale
    private let now: () -> Date
    /// Bumped by every start/stop/discard; async work started earlier checks it before acting.
    private var generation = 0
    /// Bumped by start and discard; a transcriber's late results only land in the transcript
    /// they were heard for.
    private var epoch = 0
    /// When listening began (the elapsed time shown); carried over a resume.
    private var since: Date?
    private var session: (any RoomTranscriptionSession)?
    private var feed: RoomAudioFeed?
    private var engine: AVAudioEngine?
    /// The mic should be open (setup finished and listening hasn't stopped).
    private var micWanted = false
    private var monitor = RoomSignalMonitor()
    private var transcriberBudget = RoomRebuildBudget()
    private var micBudget = RoomRebuildBudget()
    private var configObserver: NSObjectProtocol?
    private var restartWork: DispatchWorkItem?
    private var tickTimer: Timer?
    #if os(macOS)
    private var deviceWatcher: DefaultAudioDeviceWatcher?
    /// The Mac's default input when the mic was last opened.
    private var openedOnInput = AudioObjectID(0)
    #else
    /// Listening mode activated WebRTC's shared audio session (once per session, however many
    /// times the mic reopens). Balanced by exactly one deactivation when it lets go, so the call
    /// that follows starts from the same clean state as any fresh call.
    private var activatedSession = false
    /// Interruption (phone call, Siri, another app) and media-services-reset observers.
    private var sessionObservers: [NSObjectProtocol] = []
    #endif

    /// `locale` is the language to transcribe (the user's, by default). `now` is the clock, so the
    /// timing rules can be driven deterministically.
    public init(locale: Locale = .current, now: @escaping () -> Date = Date.init) {
        self.locale = locale
        self.now = now
    }

    // MARK: Start and stop

    /// Turn listening on. `transcript` continues an earlier session (a call that failed before it
    /// went live returns to listening with its transcript intact); `since` keeps that session's
    /// elapsed time. Moves through `.preparing` (model download, mic opening) and shows
    /// `.listening` only once real sound arrives. No-op while already on.
    public func start(continuing transcript: RoomTranscript = RoomTranscript(), since: Date? = nil) {
        guard !state.isOn else { return }
        generation += 1
        epoch += 1
        self.transcript = transcript
        self.since = since
        hasSignal = false
        monitor = RoomSignalMonitor()
        transcriberBudget = RoomRebuildBudget()
        micBudget = RoomRebuildBudget()
        state = .preparing(.starting)
        #if compiler(>=6.2)
        if #available(macOS 26, iOS 26, visionOS 26, *) {
            let generation = self.generation
            Task { await self.prepareAndListen(generation: generation) }
            return
        }
        #endif
        state = .unavailable(.systemTooOld)
    }

    /// Turn listening off to start a call. The mic is fully released before this returns (the
    /// call opens it next). The words in progress are finalized in the background for at most
    /// `finalizeTimeout`; await the returned task for the transcript including them. `transcript`
    /// already holds everything heard, the words in progress as its volatile tail, for a caller
    /// that can't wait.
    @discardableResult
    public func stop() -> Task<RoomTranscript, Never> {
        generation += 1
        let stoppedEpoch = epoch
        let session = detachSession()
        feed = nil
        releaseMic()
        releaseAudioSession()
        micWanted = false
        stopTicking()
        state = .off
        var fallback = transcript
        fallback.commitVolatile()
        return Task { @MainActor [weak self] in
            if let session {
                let finished = await session.finish(within: Self.finalizeTimeout)
                if !finished { self?.log("words in progress didn't finalize within \(Self.finalizeTimeout)s; kept as heard") }
            }
            guard let self, self.epoch == stoppedEpoch else { return fallback }
            self.transcript.commitVolatile()
            self.onTranscriptChange?(self.transcript)
            return self.transcript
        }
    }

    /// Stop listening and forget everything heard (Discard, sleep, the 2-hour cap).
    public func discard() {
        generation += 1
        epoch += 1
        detachSession()?.cancel()
        feed = nil
        releaseMic()
        releaseAudioSession()
        micWanted = false
        stopTicking()
        since = nil
        transcript.removeAll()
        onTranscriptChange?(transcript)
        state = .off
    }

    // MARK: Setup

    #if compiler(>=6.2)
    @available(macOS 26, iOS 26, visionOS 26, *)
    private func prepareAndListen(generation: Int) async {
        if let denied = await Self.micUnavailability() {
            guard generation == self.generation else { return }
            state = .unavailable(denied)
            return
        }
        do {
            let session = try await makeSession(generation: generation)
            guard generation == self.generation else { session.cancel(); return }
            let feed = makeFeed()
            self.feed = feed
            self.session = session
            feed.attach(session)
            micWanted = true
            try openMic()
            startTicking()
        } catch RoomListenerError.unavailable(let reason) {
            abandonStart(generation: generation, as: .unavailable(reason))
        } catch RoomCaptureError.noMicrophone {
            abandonStart(generation: generation, as: .unavailable(.noMicrophone))
        } catch {
            log("couldn't start: \(error.localizedDescription)")
            abandonStart(generation: generation, as: .failed(.transcriberStopped))
        }
    }

    /// Setup didn't get to listening: let go of anything it opened and say why.
    private func abandonStart(generation: Int, as outcome: RoomListeningState) {
        guard generation == self.generation else { return }
        detachSession()?.cancel()
        feed = nil
        releaseMic()
        releaseAudioSession()
        micWanted = false
        stopTicking()
        state = outcome
    }

    /// A transcriber whose results land in this session's transcript (and only there).
    @available(macOS 26, iOS 26, visionOS 26, *)
    private func makeSession(generation: Int) async throws -> RoomTranscription {
        let epoch = self.epoch
        return try await RoomTranscription.make(
            locale: locale,
            onDownload: { [weak self] fraction in
                // Only while starting up: a rebuild behind an open mic keeps showing the mic's state.
                guard let self, generation == self.generation, !self.micWanted else { return }
                self.state = .preparing(.downloadingModel(fraction: fraction))
            },
            onLog: { [weak self] line in self?.log(line) },
            onResult: { [weak self] result in
                guard let self, self.epoch == epoch else { return }
                switch result {
                case .final(let text, let start):
                    self.transcript.appendFinal(text, at: start)
                    self.transcript.prune(now: self.now())
                case .volatile(let text, let start):
                    self.transcript.setVolatile(text, at: start)
                }
                self.onTranscriptChange?(self.transcript)
            },
            onFailure: { [weak self] session, error in
                self?.transcriberFailed(session, error: error)
            })
    }

    /// The transcriber threw: it's finished for good. Keep the words, rebuild it (the mic stays
    /// open meanwhile), and give up after the third failure within a minute.
    @available(macOS 26, iOS 26, visionOS 26, *)
    private func transcriberFailed(_ failed: RoomTranscription, error: Error) {
        guard let current = session, current === failed, state.isOn else { return }
        detachSession()
        failed.cancel()
        transcript.commitVolatile()
        onTranscriptChange?(transcript)
        retryTranscriber(after: error, what: "transcriber failed")
    }

    @available(macOS 26, iOS 26, visionOS 26, *)
    private func retryTranscriber(after error: Error, what: String) {
        guard state.isOn else { return }
        guard transcriberBudget.recordFailure(now: now()) else {
            log("\(what) (\(error.localizedDescription)); giving up")
            fail(.transcriberStopped)
            return
        }
        let delay = transcriberBudget.retryDelay
        log("\(what) (\(error.localizedDescription)); rebuilding in \(delay)s, words kept")
        let generation = self.generation
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard let self, generation == self.generation, self.state.isOn else { return }
            do {
                let session = try await self.makeSession(generation: generation)
                guard generation == self.generation, self.state.isOn else { session.cancel(); return }
                self.session = session
                self.feed?.attach(session)
                self.log("transcriber rebuilt")
            } catch {
                guard generation == self.generation else { return }
                self.retryTranscriber(after: error, what: "couldn't rebuild the transcriber")
            }
        }
    }
    #endif

    /// Listening can't go on: release the mic, keep what was heard, say so.
    private func fail(_ failure: RoomFailure) {
        generation += 1
        detachSession()?.cancel()
        feed = nil
        releaseMic()
        releaseAudioSession()
        micWanted = false
        stopTicking()
        transcript.commitVolatile()
        onTranscriptChange?(transcript)
        state = .failed(failure)
    }

    @discardableResult
    private func detachSession() -> (any RoomTranscriptionSession)? {
        let current = session
        session = nil
        feed?.detach()
        return current
    }

    private func makeFeed() -> RoomAudioFeed {
        let feed = RoomAudioFeed()
        feed.onLevel = { [weak self] level in
            Task { @MainActor [weak self] in self?.heard(level: level) }
        }
        feed.onFirstSignal = { [weak self] in
            Task { @MainActor [weak self] in
                guard let self, !self.hasSignal, self.micWanted else { return }
                self.hasSignal = true
                self.onSignal?()
            }
        }
        return feed
    }

    // MARK: Mic

    /// Open the mic on a fresh engine and tap it into the feed.
    private func openMic() throws {
        guard let feed else { return }
        #if os(iOS) || os(visionOS)
        // Nothing may have opened the audio session yet. Open it the way the call will use it,
        // through WebRTC's own session object so the two never disagree (as `EarlyCapture` does).
        // Activated once per session (a mic reopen only reapplies the configuration): every
        // activation must be balanced by one deactivation, which `releaseAudioSession()` does.
        let audioSession = RTCAudioSession.sharedInstance()
        audioSession.lockForConfiguration()
        defer { audioSession.unlockForConfiguration() }
        if activatedSession {
            try audioSession.setConfiguration(RTCAudioSessionConfiguration.webRTC())
        } else {
            try audioSession.setConfiguration(RTCAudioSessionConfiguration.webRTC(), active: true)
            activatedSession = true
            observeSessionInterruptions()
        }
        #endif
        let engine = AVAudioEngine()
        try tap(engine, into: feed)
        self.engine = engine
        observeConfigurationChanges(of: engine)
        #if os(macOS)
        openedOnInput = DefaultAudioDeviceWatcher.defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
        if deviceWatcher == nil {
            deviceWatcher = DefaultAudioDeviceWatcher { [weak self] in self?.defaultDevicesChanged() }
        }
        #endif
        if since == nil { since = now() }
        inputIsBluetooth = currentInputIsBluetooth()
        monitor.reopened(now: now())
        applyReading(monitor.reading)
    }

    private func tap(_ engine: AVAudioEngine, into feed: RoomAudioFeed) throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { throw RoomCaptureError.noMicrophone }
        feed.micOpened()
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
            feed.process(buffer)
        }
        engine.prepare()
        do {
            try engine.start()
        } catch {
            input.removeTap(onBus: 0)
            throw error
        }
    }

    /// Remove the tap, then stop, reset and drop the engine: a stopped engine still owns its input
    /// unit until it is deallocated, and the call needs the mic straight away (the macOS mic
    /// indicator goes off here). Same full release as `EarlyCapture.stopAudio()`.
    private func releaseMic() {
        restartWork?.cancel()
        restartWork = nil
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = nil
        #if os(macOS)
        deviceWatcher = nil
        #endif
        guard let engine else { return }
        self.engine = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        engine.reset()
    }

    /// iPhone, iPad, Vision Pro: give back the audio session listening mode activated (one
    /// balanced deactivation) and stop watching for interruptions. After `releaseMic()`, so no
    /// audio I/O is running. The call's own audio configures and activates it from scratch, as
    /// for any fresh call; nothing is held for it (holding WebRTC's audio left calls deaf in
    /// builds 18-25). No-op on the Mac.
    private func releaseAudioSession() {
        #if os(iOS) || os(visionOS)
        for observer in sessionObservers { NotificationCenter.default.removeObserver(observer) }
        sessionObservers = []
        guard activatedSession else { return }
        activatedSession = false
        let audioSession = RTCAudioSession.sharedInstance()
        audioSession.lockForConfiguration()
        defer { audioSession.unlockForConfiguration() }
        do {
            try audioSession.setActive(false)
        } catch {
            log("couldn't deactivate the audio session (\(error.localizedDescription))")
        }
        #endif
    }

    #if os(iOS) || os(visionOS)
    /// A phone or FaceTime call, Siri, an alarm or another app recording takes the audio: stop
    /// listening for good (keeping what was heard) and say so. It never restarts by itself.
    private func observeSessionInterruptions() {
        let center = NotificationCenter.default
        let session = AVAudioSession.sharedInstance()
        sessionObservers.append(center.addObserver(forName: AVAudioSession.interruptionNotification, object: session,
                                                   queue: .main) { [weak self] note in
            let raw = note.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt
            guard raw == AVAudioSession.InterruptionType.began.rawValue else { return }
            Task { @MainActor [weak self] in self?.interrupted("audio interrupted (call, Siri or another app)") }
        })
        sessionObservers.append(center.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification,
                                                   object: session, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in self?.interrupted("the system reset its audio services") }
        })
    }

    private func interrupted(_ why: String) {
        guard state.isOn else { return }
        log("\(why); stopped, words kept")
        fail(.interrupted)
    }
    #endif

    /// A Bluetooth headset switching to call audio (or any device format change) stops the engine.
    /// Restart once the burst settles. The engine is never torn down inside the handler itself.
    private func observeConfigurationChanges(of engine: AVAudioEngine) {
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in self?.scheduleMicRestart(newEngine: false, reason: "audio configuration changed") }
        }
    }

    #if os(macOS)
    /// The Mac's default input changed (AirPods connected, a pick in Control Center). On macOS 27
    /// the engine listens through Core Audio's default-device aggregate, which follows the change
    /// and sends a configuration change; an engine bound to the device itself would stay on the
    /// old one. Reopening on a fresh engine covers both. Output-only changes are ignored.
    private func defaultDevicesChanged() {
        guard micWanted, engine != nil else { return }
        let newInput = DefaultAudioDeviceWatcher.defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
        guard newInput != 0, newInput != openedOnInput else { return }
        scheduleMicRestart(newEngine: true, reason: "default input changed")
    }
    #endif

    private func scheduleMicRestart(newEngine: Bool, reason: String) {
        guard micWanted else { return }
        restartWork?.cancel()
        let generation = self.generation
        let work = DispatchWorkItem { [weak self] in
            MainActor.assumeIsolated { self?.restartMic(generation: generation, newEngine: newEngine, reason: reason) }
        }
        restartWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.restartDebounce, execute: work)
    }

    /// Reopen the mic on its new format, keeping the transcriber and the transcript. First the
    /// same engine with a new tap (what `EarlyCapture` does); if that fails, or the device itself
    /// changed, a fresh engine. Gives up after the third failure within a minute.
    private func restartMic(generation: Int, newEngine: Bool, reason: String) {
        guard generation == self.generation, micWanted, let feed else { return }
        restartWork = nil
        if !newEngine, let engine {
            engine.inputNode.removeTap(onBus: 0)
            engine.stop()
            do {
                try tap(engine, into: feed)
                log("mic restarted (\(reason))")
                #if os(macOS)
                // The default-device aggregate already followed an input change: no second reopen.
                openedOnInput = DefaultAudioDeviceWatcher.defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
                #endif
                inputIsBluetooth = currentInputIsBluetooth()
                monitor.reopened(now: now())
                return
            } catch {
                log("mic restart on the same engine failed (\(error.localizedDescription)); opening a new one")
            }
        }
        releaseMic()
        do {
            try openMic()
            log("mic reopened on a new engine (\(reason))")
        } catch {
            guard micBudget.recordFailure(now: now()) else {
                log("couldn't reopen the mic (\(error.localizedDescription)); giving up")
                fail(.micStopped)
                return
            }
            let delay = micBudget.retryDelay
            log("couldn't reopen the mic (\(error.localizedDescription)); trying again in \(delay)s")
            let work = DispatchWorkItem { [weak self] in
                MainActor.assumeIsolated { self?.restartMic(generation: generation, newEngine: true, reason: reason) }
            }
            restartWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + delay, execute: work)
        }
    }

    // MARK: Dead-mic monitor

    private func heard(level: Double) {
        guard micWanted else { return }
        onLevel?(level)
        applyReading(monitor.sample(level: level, now: now()))
    }

    /// Once a second: judge the mic even when no buffers arrive, and drop what's over 30 minutes old.
    private func startTicking() {
        stopTicking()
        tickTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated {
                guard let self, self.micWanted else { return }
                self.applyReading(self.monitor.tick(now: self.now()))
                let before = self.transcript
                self.transcript.prune(now: self.now())
                if self.transcript != before { self.onTranscriptChange?(self.transcript) }
            }
        }
    }

    private func stopTicking() {
        tickTimer?.invalidate()
        tickTimer = nil
    }

    private func applyReading(_ reading: RoomSignalMonitor.Reading) {
        guard micWanted, state.isOn else { return }
        let next = RoomListeningState.capturing(reading, since: since ?? now())
        if next != state {
            if case .notHearing = next { log("the mic is sending only silence") }
            if case .notHearing = state, case .listening = next { log("sound from the mic again") }
            state = next
        }
    }

    // MARK: Helpers

    private func log(_ line: String) { onLog?("listening mode: \(line)") }

    /// Whether the input the engine listens on is a Bluetooth headset.
    private func currentInputIsBluetooth() -> Bool {
        #if os(macOS)
        // On macOS 27 the engine's device is Core Audio's default-device aggregate ("grup"), never
        // the headset itself; the aggregate takes its input from the default input device.
        if let device = engine.flatMap(Self.device(of:)), Self.transport(of: device) != kAudioDeviceTransportTypeAggregate {
            return Self.isBluetooth(Self.transport(of: device))
        }
        let input = DefaultAudioDeviceWatcher.defaultDevice(kAudioHardwarePropertyDefaultInputDevice)
        return input != 0 && Self.isBluetooth(Self.transport(of: input))
        #else
        return AVAudioSession.sharedInstance().currentRoute.inputs.contains {
            $0.portType == .bluetoothHFP || $0.portType == .bluetoothLE
        }
        #endif
    }

    #if os(macOS)
    private static func transport(of device: AudioObjectID) -> UInt32 {
        var transport = UInt32(0)
        var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyTransportType,
                                                 mScope: kAudioObjectPropertyScopeGlobal,
                                                 mElement: kAudioObjectPropertyElementMain)
        var size = UInt32(MemoryLayout<UInt32>.size)
        guard AudioObjectGetPropertyData(device, &address, 0, nil, &size, &transport) == noErr else { return 0 }
        return transport
    }

    private static func isBluetooth(_ transport: UInt32) -> Bool {
        transport == kAudioDeviceTransportTypeBluetooth || transport == kAudioDeviceTransportTypeBluetoothLE
    }

    /// The device an engine's input is bound to (nil when it can't be read).
    private static func device(of engine: AVAudioEngine) -> AudioObjectID? {
        guard let unit = engine.inputNode.audioUnit else { return nil }
        var device = AudioObjectID(0)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let status = AudioUnitGetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                                          &device, &size)
        return status == noErr && device != 0 ? device : nil
    }
    #endif

    /// Asks for the mic once; nil when it may be used.
    private static func micUnavailability() async -> RoomUnavailability? {
        #if os(macOS)
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return nil
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio) ? nil : .micDenied
        default: return .micDenied
        }
        #else
        switch AVAudioApplication.shared.recordPermission {
        case .granted: return nil
        case .undetermined: return await AVAudioApplication.requestRecordPermission() ? nil : .micDenied
        default: return .micDenied
        }
        #endif
    }

    /// "16000 Hz Int16 mono", for logs.
    static func describe(_ format: AVAudioFormat) -> String {
        let sample: String
        switch format.commonFormat {
        case .pcmFormatInt16: sample = "Int16"
        case .pcmFormatInt32: sample = "Int32"
        case .pcmFormatFloat32: sample = "Float32"
        case .pcmFormatFloat64: sample = "Float64"
        default: sample = "other"
        }
        return "\(Int(format.sampleRate)) Hz \(sample) \(format.channelCount == 1 ? "mono" : "\(format.channelCount) ch")"
    }

    // MARK: Files (smoke test)

    /// Transcribe an audio file through the same converter and transcriber as the mic (no mic,
    /// no permission). Segment times start now and follow the file's own timeline. For
    /// `--room-smoke`; throws `RoomListenerError` when it can't run here.
    public static func transcribeFile(at url: URL, locale: Locale = .current,
                                      log: ((String) -> Void)? = nil) async throws -> RoomTranscript {
        #if compiler(>=6.2)
        if #available(macOS 26, iOS 26, visionOS 26, *) {
            let file: AVAudioFile
            do { file = try AVAudioFile(forReading: url) } catch {
                throw RoomListenerError.unreadableFile(error.localizedDescription)
            }
            let box = TranscriptBox()
            let session = try await RoomTranscription.make(
                locale: locale,
                onDownload: { fraction in log?("downloading the speech model \(fraction.map { "\(Int($0 * 100))%" } ?? "")") },
                onLog: { line in log?(line) },
                onResult: { result in
                    switch result {
                    case .final(let text, let start): box.transcript.appendFinal(text, at: start)
                    case .volatile(let text, let start): box.transcript.setVolatile(text, at: start)
                    }
                },
                onFailure: { _, error in box.failure = error })
            let feed = RoomAudioFeed()
            feed.attach(session)
            let format = file.processingFormat
            let seconds = Double(file.length) / format.sampleRate
            log?("file: \(String(format: "%.1f", seconds))s at \(Int(format.sampleRate)) Hz, \(format.channelCount) ch; "
                 + "transcriber takes \(Self.describe(session.analyzerFormat))")
            let chunk: AVAudioFrameCount = 16_384
            while file.framePosition < file.length {
                guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: chunk) else { break }
                do { try file.read(into: buffer, frameCount: chunk) } catch {
                    session.cancel()
                    throw RoomListenerError.unreadableFile(error.localizedDescription)
                }
                if buffer.frameLength == 0 { break }
                feed.process(buffer)
            }
            feed.flush()
            let finished = await session.finish(within: 60 + seconds)
            if let failure = box.failure { throw failure }
            if !finished { log?("the transcriber didn't finish within \(Int(60 + seconds))s; showing what it had") }
            box.transcript.commitVolatile()
            return box.transcript
        }
        #endif
        throw RoomListenerError.unavailable(.systemTooOld)
    }
}

@MainActor
private final class TranscriptBox {
    var transcript = RoomTranscript()
    var failure: Error?
}

enum RoomCaptureError: Error {
    case noMicrophone
}

// MARK: - Audio feed

/// The transcriber, as the audio feed and the listener see it. Always available; the
/// SpeechAnalyzer implementation needs macOS 26.
protocol RoomTranscriptionSession: AnyObject, Sendable {
    /// The format the transcriber takes (16 kHz Int16 on macOS 27).
    var analyzerFormat: AVAudioFormat { get }
    /// One buffer already in `analyzerFormat`, owned by the session from here on. `resumed` marks
    /// the first buffer after the mic (re)opened, so the session can pin its timeline to the
    /// clock. Called on the audio thread.
    func feed(_ buffer: AVAudioPCMBuffer, resumed: Bool)
    /// No more audio: finalize the words in progress, waiting at most `timeout`. True when the
    /// transcriber finished in time (its last results have been delivered).
    func finish(within timeout: TimeInterval) async -> Bool
    /// Stop now; the words in progress are dropped.
    func cancel()
}

/// Carries audio from the mic tap (or a file) to the transcriber. Converts every buffer to the
/// transcriber's format into a new buffer (the tap reuses its own once the callback returns, and
/// the transcriber crashes on the mic's Float32 on macOS 27), reports the first real sound once,
/// and the peak level about ten times a second. Runs on the audio thread; locked.
final class RoomAudioFeed: @unchecked Sendable {
    private let lock = NSLock()
    private var sink: (any RoomTranscriptionSession)?
    private var converter: AVAudioConverter?
    /// The next buffer starts a new stretch of audio (the mic just (re)opened).
    private var resumed = true
    private let signal = SignalFlag()
    private var peakSinceReport: Float = 0
    private var lastReport: TimeInterval = 0
    static let levelInterval: TimeInterval = 0.1

    /// Peak level (linear 0...1), throttled to `levelInterval`. Audio thread.
    var onLevel: (@Sendable (Double) -> Void)?
    /// The first buffer with a non-zero sample. Audio thread.
    var onFirstSignal: (@Sendable () -> Void)?

    /// Send audio to `session` from now on (a rebuilt transcriber starts its own timeline).
    func attach(_ session: any RoomTranscriptionSession) {
        lock.lock(); defer { lock.unlock() }
        sink = session
        resumed = true
        if let converter, converter.outputFormat != session.analyzerFormat { self.converter = nil }
    }

    /// Stop sending audio (the transcriber failed or listening stopped). The mic level and the
    /// first-sound report keep working.
    func detach() {
        lock.lock(); defer { lock.unlock() }
        sink = nil
    }

    /// The mic (re)opened, possibly on a new format.
    func micOpened() {
        lock.lock(); defer { lock.unlock() }
        resumed = true
    }

    func process(_ buffer: AVAudioPCMBuffer) {
        guard buffer.frameLength > 0 else { return }
        report(peak: Self.peak(of: buffer))
        lock.lock()
        guard let sink else { lock.unlock(); return }
        let converted = convert(buffer, to: sink.analyzerFormat, endOfStream: false)
        let startsStretch = resumed
        if converted != nil { resumed = false }
        lock.unlock()
        if let converted { sink.feed(converted, resumed: startsStretch) }
    }

    /// End of a file: push out what the converter still holds.
    func flush() {
        lock.lock()
        guard let sink, let converter else { lock.unlock(); return }
        let rest = Self.drain(converter)
        lock.unlock()
        if let rest { sink.feed(rest, resumed: false) }
    }

    /// Convert into a newly allocated buffer, reusing the converter so resampling stays seamless
    /// across buffers. Called with the lock held.
    private func convert(_ buffer: AVAudioPCMBuffer, to output: AVAudioFormat, endOfStream: Bool) -> AVAudioPCMBuffer? {
        if converter == nil || converter?.inputFormat != buffer.format || converter?.outputFormat != output {
            guard let made = AVAudioConverter(from: buffer.format, to: output) else { return nil }
            // A multi-channel mic is mixed down rather than reduced to its first channel.
            made.downmix = true
            converter = made
        }
        guard let converter else { return nil }
        let ratio = output.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount((Double(buffer.frameLength) * ratio).rounded(.up)) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: output, frameCapacity: capacity) else { return nil }
        var supplied = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            if supplied {
                // .noDataNow keeps the resampler's state for the next buffer.
                inputStatus.pointee = endOfStream ? .endOfStream : .noDataNow
                return nil
            }
            supplied = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, out.frameLength > 0 else { return nil }
        return out
    }

    private static func drain(_ converter: AVAudioConverter) -> AVAudioPCMBuffer? {
        guard let out = AVAudioPCMBuffer(pcmFormat: converter.outputFormat, frameCapacity: 4096) else { return nil }
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            inputStatus.pointee = .endOfStream
            return nil
        }
        converter.reset()
        guard status != .error, out.frameLength > 0 else { return nil }
        return out
    }

    private func report(peak: Float) {
        // A live mic always has a noise floor; an input that isn't really ours reads exact zeros.
        if peak > 0, signal.set() { onFirstSignal?() }
        guard let onLevel else { return }
        let uptime = ProcessInfo.processInfo.systemUptime
        lock.lock()
        peakSinceReport = max(peakSinceReport, peak)
        guard uptime - lastReport >= Self.levelInterval else { lock.unlock(); return }
        let level = peakSinceReport
        peakSinceReport = 0
        lastReport = uptime
        lock.unlock()
        onLevel(Double(level))
    }

    /// The loudest sample in the buffer (first channel; linear 0...1).
    static func peak(of buffer: AVAudioPCMBuffer) -> Float {
        let frames = Int(buffer.frameLength)
        var peak: Float = 0
        if let data = buffer.floatChannelData {
            let samples = data[0]
            for i in 0..<frames { peak = max(peak, abs(samples[i])) }
        } else if let data = buffer.int16ChannelData {
            let samples = data[0]
            for i in 0..<frames { peak = max(peak, abs(Float(samples[i])) / 32_768) }
        }
        return peak
    }
}

/// Runs `work` and reports whether it finished within `timeout` seconds. On a timeout it returns
/// at once and `work` carries on in the background.
func finishes(within timeout: TimeInterval, _ work: @escaping @Sendable () async -> Void) async -> Bool {
    await withCheckedContinuation { (continuation: CheckedContinuation<Bool, Never>) in
        let once = SignalFlag()
        Task {
            await work()
            if once.set() { continuation.resume(returning: true) }
        }
        Task {
            try? await Task.sleep(nanoseconds: UInt64(max(0, timeout) * 1_000_000_000))
            if once.set() { continuation.resume(returning: false) }
        }
    }
}

// MARK: - SpeechAnalyzer

#if compiler(>=6.2)
/// One SpeechAnalyzer + SpeechTranscriber run. If the analyzer or its results throw, it is
/// finished for good; the listener builds a new one and keeps the transcript.
@available(macOS 26, iOS 26, visionOS 26, *)
final class RoomTranscription: RoomTranscriptionSession, @unchecked Sendable {
    enum Result {
        case final(String, start: Date)
        case volatile(String, start: Date)
    }

    let analyzerFormat: AVAudioFormat
    private let analyzer: SpeechAnalyzer
    private let transcriber: SpeechTranscriber
    private let input: AsyncStream<AnalyzerInput>.Continuation
    private let lock = NSLock()
    private var timeline = RoomTimeline()
    private var framesFed: AVAudioFramePosition = 0
    private var resultsTask: Task<Void, Never>?

    private init(analyzer: SpeechAnalyzer, transcriber: SpeechTranscriber, format: AVAudioFormat,
                 input: AsyncStream<AnalyzerInput>.Continuation) {
        self.analyzer = analyzer
        self.transcriber = transcriber
        self.analyzerFormat = format
        self.input = input
    }

    /// Check the transcriber, reserve the language and install its model when needed (that's
    /// "preparing"), then start an analyzer waiting for audio.
    /// Results arrive on the main actor. A throw from the analyzer or its results ends the run and
    /// reaches `onFailure` (a normal end after `finish` doesn't).
    @MainActor
    static func make(locale: Locale, onDownload: @escaping @MainActor (Double?) -> Void,
                     onLog: @escaping @MainActor (String) -> Void,
                     onResult: @escaping @MainActor (Result) -> Void,
                     onFailure: @escaping @MainActor (RoomTranscription, Error) -> Void) async throws -> RoomTranscription {
        guard SpeechTranscriber.isAvailable else { throw RoomListenerError.unavailable(.noTranscriber) }
        guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            throw RoomListenerError.unavailable(.languageNotSupported(language: displayName(locale)))
        }
        let transcriber = SpeechTranscriber(locale: supported, transcriptionOptions: [],
                                            reportingOptions: [.volatileResults],
                                            attributeOptions: [.audioTimeRange])
        try await installModel(for: transcriber, locale: supported, onDownload: onDownload, onLog: onLog)
        guard let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw RoomListenerError.unavailable(.noTranscriber)
        }
        let analyzer = SpeechAnalyzer(modules: [transcriber],
                                      options: SpeechAnalyzer.Options(priority: .userInitiated, modelRetention: .lingering))
        try await analyzer.prepareToAnalyze(in: format)
        let (stream, continuation) = AsyncStream.makeStream(of: AnalyzerInput.self)
        let session = RoomTranscription(analyzer: analyzer, transcriber: transcriber, format: format, input: continuation)
        // Listen for results before the analyzer starts, so none can be missed.
        session.consumeResults(onResult: onResult, onFailure: onFailure)
        do {
            try await analyzer.start(inputSequence: stream)
        } catch {
            session.cancel()
            throw error
        }
        return session
    }

    /// Reserve the language for this app and install its model if an installation is offered. An
    /// asset status of `.supported` alone doesn't block: transcription can already work then.
    @MainActor
    private static func installModel(for transcriber: SpeechTranscriber, locale: Locale,
                                     onDownload: @escaping @MainActor (Double?) -> Void,
                                     onLog: @escaping @MainActor (String) -> Void) async throws {
        do {
            try await AssetInventory.reserve(locale: locale)
        } catch {
            onLog("couldn't reserve \(locale.identifier) (\(error.localizedDescription)); carrying on")
        }
        let request: AssetInstallationRequest?
        do {
            request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber])
        } catch {
            // Can't tell whether a download is needed; if the model is missing, preparing the
            // analyzer fails next and says so.
            onLog("couldn't check the speech model (\(error.localizedDescription)); trying it as is")
            return
        }
        guard let request else { return }
        onLog("downloading the speech model for \(locale.identifier)")
        onDownload(nil)
        let progress = request.progress
        let watcher = Task { @MainActor in
            while !Task.isCancelled {
                onDownload(progress.totalUnitCount > 0 ? progress.fractionCompleted : nil)
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
        }
        defer { watcher.cancel() }
        do {
            try await request.downloadAndInstall()
        } catch {
            onLog("speech model download failed: \(error.localizedDescription)")
            throw RoomListenerError.unavailable(.modelDownloadFailed)
        }
        onLog("speech model installed")
    }

    private func consumeResults(onResult: @escaping @MainActor (Result) -> Void,
                                onFailure: @escaping @MainActor (RoomTranscription, Error) -> Void) {
        let transcriber = self.transcriber
        resultsTask = Task { [weak self] in
            do {
                for try await result in transcriber.results {
                    let text = String(result.text.characters)
                    let audioStart = result.range.start.isNumeric ? result.range.start.seconds : 0
                    let start = self?.wallClock(atAudioSeconds: audioStart) ?? Date()
                    let item: Result = result.isFinal ? .final(text, start: start) : .volatile(text, start: start)
                    await onResult(item)
                }
            } catch is CancellationError {
                return
            } catch {
                guard let self else { return }
                await onFailure(self, error)
            }
        }
    }

    func feed(_ buffer: AVAudioPCMBuffer, resumed: Bool) {
        lock.lock()
        if resumed || timeline.anchors.isEmpty {
            // This buffer was captured about its own length ago.
            let heardAt = Date().addingTimeInterval(-Double(buffer.frameLength) / analyzerFormat.sampleRate)
            timeline.anchor(audioSeconds: Double(framesFed) / analyzerFormat.sampleRate, wallClock: heardAt)
        }
        framesFed += AVAudioFramePosition(buffer.frameLength)
        lock.unlock()
        input.yield(AnalyzerInput(buffer: buffer))
    }

    func finish(within timeout: TimeInterval) async -> Bool {
        input.finish()
        let analyzer = self.analyzer
        let results = resultsTask
        let finished = await finishes(within: timeout) {
            try? await analyzer.finalizeAndFinishThroughEndOfInput()
            await results?.value
        }
        if !finished { cancel() }
        return finished
    }

    func cancel() {
        input.finish()
        resultsTask?.cancel()
        let analyzer = self.analyzer
        Task { await analyzer.cancelAndFinishNow() }
    }

    private func wallClock(atAudioSeconds seconds: Double) -> Date? {
        lock.lock(); defer { lock.unlock() }
        return timeline.wallClock(atAudioSeconds: seconds)
    }

    static func displayName(_ locale: Locale) -> String {
        Locale.current.localizedString(forIdentifier: locale.identifier) ?? locale.identifier
    }
}
#endif
