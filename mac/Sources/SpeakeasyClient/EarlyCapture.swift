import AVFoundation
import Foundation
import Speech
#if canImport(LiveKitWebRTC)
import LiveKitWebRTC
#endif

/// Captures speech during call setup, so nothing said before the voice is ready gets dropped.
/// Transcribes on the device (Apple speech recognition, on-device only: no audio leaves the machine
/// here). The text is handed to the call as its first request once it connects.
///
/// The mic tap is stopped as soon as the call's own audio takes over; only the final text is kept.
@MainActor
public final class EarlyCapture {
    private var engine = AVAudioEngine()
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var recognizer: SFSpeechRecognizer?
    private var latest = ""
    private var lastHeardAt: Date?
    private var finalText: String?
    private var waiters: [CheckedContinuation<Void, Never>] = []
    private var running = false
    private var audioOn = false
    private var configObserver: NSObjectProtocol?

    /// Called on the main actor with the words so far (they grow and get corrected as you speak).
    public var onPartial: ((String) -> Void)?
    /// Called once on the main actor when the mic delivers real sound (not digital silence). Until
    /// then nothing is being heard, so the app must not say "Listening".
    public var onSignal: (() -> Void)?
    public private(set) var hasSignal = false
    private let signalFlag = SignalFlag()

    public init() {}

    /// True when this app may transcribe: it declares why (Info.plist) and on-device recognition exists.
    /// A bare debug binary has no Info.plist; asking for permission there would crash, so it never does.
    public static var available: Bool {
        guard Bundle.main.object(forInfoDictionaryKey: "NSSpeechRecognitionUsageDescription") != nil,
              let recognizer = SFSpeechRecognizer(), recognizer.isAvailable else { return false }
        return recognizer.supportsOnDeviceRecognition
    }

    /// Asks for speech recognition once. The first call after install shows the prompt and doesn't
    /// capture (the call carries on as before); later calls capture when allowed.
    public static func authorizedNow() -> Bool {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: return true
        case .notDetermined:
            SFSpeechRecognizer.requestAuthorization { _ in }
            return false
        default: return false
        }
    }

    /// Start listening. Returns false (and does nothing) when capture can't run.
    @discardableResult
    public func start() -> Bool {
        guard !running, Self.available, Self.authorizedNow(),
              let recognizer = SFSpeechRecognizer(), recognizer.supportsOnDeviceRecognition else { return false }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        request.taskHint = .dictation
        guard tapAndStart(request) else { return false }
        // A Bluetooth headset switches to call mode when its mic opens, which changes the input
        // format under the engine and stops it. Reopen on the new format and keep listening.
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self, self.audioOn, let request = self.request else { return }
                self.engine.inputNode.removeTap(onBus: 0)
                self.engine.stop()
                if !self.tapAndStart(request) { self.audioOn = false }
            }
        }
        self.request = request
        self.recognizer = recognizer
        running = true
        audioOn = true
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            let text = result?.bestTranscription.formattedString
            let isFinal = result?.isFinal ?? false
            Task { @MainActor [weak self] in
                guard let self else { return }
                if let text, !text.isEmpty {
                    if text != self.latest { self.lastHeardAt = Date() }
                    self.latest = text
                    self.onPartial?(text)
                }
                if isFinal || error != nil { self.finished() }
            }
        }
        return true
    }

    /// Words heard so far (may still grow).
    public var heardSoFar: String { latest }

    /// The call is ready. If you're mid-sentence, keep listening until you pause, so a sentence is
    /// never split between this listener and the call. Returns at once when nothing has been said;
    /// otherwise after `pause` seconds with no new words, or `limit` seconds at most.
    public func waitForPause(pause: TimeInterval = 0.8, limit: TimeInterval = 8) async {
        let deadline = Date().addingTimeInterval(limit)
        while running, audioOn, Date() < deadline {
            guard let last = lastHeardAt, !latest.trimmingCharacters(in: .whitespaces).isEmpty else { return }
            if Date().timeIntervalSince(last) >= pause { return }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
    }

    /// Stop the mic now (the call's own audio is taking over); the words are read with `finish()`.
    public func stopListening() {
        guard running else { return }
        stopAudio()
        request?.endAudio()
    }

    /// Stop the mic now and return what was said (empty when nothing), waiting up to `wait` seconds
    /// for the recognizer's final wording.
    public func finish(wait: TimeInterval = 1.5) async -> String {
        guard running else { return (finalText ?? latest).trimmingCharacters(in: .whitespacesAndNewlines) }
        stopListening()
        if finalText == nil {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                waiters.append(continuation)
                DispatchQueue.main.asyncAfter(deadline: .now() + wait) { [weak self] in self?.finished() }
            }
        }
        running = false
        task?.cancel(); task = nil; request = nil
        return (finalText ?? latest).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Throw it all away (call ended or failed before connecting).
    public func cancel() {
        stopAudio()
        task?.cancel(); task = nil; request = nil
        running = false
        finished()
    }

    private func tapAndStart(_ request: SFSpeechAudioBufferRecognitionRequest) -> Bool {
        #if os(iOS) || os(visionOS)
        // On iPhone nothing has opened the audio session yet at this point (WebRTC normally does it
        // when the call's audio starts), so the mic would read silence. Open it the way the call
        // will use it, through WebRTC's own session object so the two never disagree about it.
        let session = RTCAudioSession.sharedInstance()
        session.lockForConfiguration()
        defer { session.unlockForConfiguration() }
        do {
            try session.setConfiguration(RTCAudioSessionConfiguration.webRTC(), active: true)
        } catch {
            return false
        }
        #endif
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { return false }
        let flag = signalFlag
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            request.append(buffer)
            // A live mic always has a noise floor; an input that isn't really ours reads exact zeros.
            if !flag.isSet, let data = buffer.floatChannelData, buffer.frameLength > 0 {
                let samples = data[0]
                var heard = false
                for i in 0..<Int(buffer.frameLength) where samples[i] != 0 { heard = true; break }
                if heard, flag.set() {
                    Task { @MainActor [weak self] in
                        guard let self, !self.hasSignal else { return }
                        self.hasSignal = true
                        self.onSignal?()
                    }
                }
            }
        }
        engine.prepare()
        do {
            try engine.start()
            return true
        } catch {
            input.removeTap(onBus: 0)
            return false
        }
    }

    private func stopAudio() {
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        configObserver = nil
        guard audioOn else { return }
        audioOn = false
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        // Release the input unit entirely so the call's own audio can take the mic straight away:
        // a stopped engine still owns its input unit until it is deallocated, so replace it.
        engine.reset()
        engine = AVAudioEngine()
    }

    private func finished() {
        if finalText == nil { finalText = latest }
        let pending = waiters
        waiters.removeAll()
        pending.forEach { $0.resume() }
    }
}

/// Set once from the audio thread, read anywhere.
final class SignalFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    var isSet: Bool { lock.lock(); defer { lock.unlock() }; return value }
    /// True the first time only.
    func set() -> Bool { lock.lock(); defer { lock.unlock() }; if value { return false }; value = true; return true }
}
