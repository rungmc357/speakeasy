import AVFoundation
import Foundation
import Speech

/// On-device dictation into a job's reply field when no call is open (in a call the mic button is
/// hold-to-talk instead). The words land in the field to review before sending; nothing is sent
/// by itself. Uses Apple's on-device recognizer when the device has one.
@MainActor
public final class ReplyDictation: ObservableObject {
    public static let shared = ReplyDictation()

    public enum Phase: Equatable { case idle, starting, listening, denied(String), failed(String) }
    @Published public private(set) var phase: Phase = .idle
    /// The job being dictated to.
    @Published public private(set) var jobID: String?
    /// What's been heard so far this time (appended to what was already in the field).
    @Published public private(set) var heard = ""

    private var recognizer: SFSpeechRecognizer?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private let engine = AVAudioEngine()
    private var onText: ((String) -> Void)?
    /// Preview frames and screenshots: show the listening state without a microphone.
    public var previewOnly = false

    public init() {}

    public var isListening: Bool { phase == .listening || phase == .starting }
    public func isListening(to jobID: String) -> Bool { isListening && self.jobID == jobID }

    /// Start (or stop, if already listening to this job). `onText` gets the running transcript.
    public func toggle(jobID: String, onText: @escaping (String) -> Void) {
        if isListening(to: jobID) { stop(); return }
        stop()
        self.jobID = jobID
        self.onText = onText
        heard = ""
        if previewOnly { phase = .listening; return }
        phase = .starting
        Task { [weak self] in
            guard let self else { return }
            guard await Self.speechAllowed() else {
                self.phase = .denied("Speech recognition is off for Speakeasy. Turn it on in Settings to dictate.")
                return
            }
            guard await Self.micAllowed() else {
                self.phase = .denied("The microphone is off for Speakeasy. Turn it on in Settings to dictate.")
                return
            }
            guard self.jobID == jobID, self.phase == .starting else { return }
            do { try self.begin() }
            catch {
                self.cleanUp()
                self.phase = .failed("Dictation didn't start. Type instead, or try again.")
            }
        }
    }

    /// Screenshots: show the listening state with some words heard.
    public func preview(jobID: String, heard: String) {
        previewOnly = true
        self.jobID = jobID; self.heard = heard; phase = .listening
    }

    public func stop() {
        guard phase != .idle || task != nil else { return }
        request?.endAudio()
        cleanUp()
        if case .denied = phase { return }
        if case .failed = phase { return }
        phase = .idle
    }

    public func clearNotice() { if case .denied = phase { phase = .idle }; if case .failed = phase { phase = .idle } }

    private func begin() throws {
        let recognizer = SFSpeechRecognizer(locale: Locale.current) ?? SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        guard let recognizer, recognizer.isAvailable else { throw NSError(domain: "dictation", code: 1) }
        self.recognizer = recognizer
        #if os(iOS)
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .measurement, options: [.duckOthers])
        try session.setActive(true, options: .notifyOthersOnDeactivation)
        #endif
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        if recognizer.supportsOnDeviceRecognition { request.requiresOnDeviceRecognition = true }
        request.addsPunctuation = true
        self.request = request
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak request] buffer, _ in request?.append(buffer) }
        engine.prepare()
        try engine.start()
        phase = .listening
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            let text = result?.bestTranscription.formattedString
            let final = result?.isFinal == true || error != nil
            Task { @MainActor [weak self] in
                guard let self else { return }
                if let text { self.heard = text; self.onText?(text) }
                if final { self.stop() }
            }
        }
    }

    private func cleanUp() {
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
        task?.cancel()
        task = nil
        request = nil
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        #endif
    }

    private static func speechAllowed() async -> Bool {
        switch SFSpeechRecognizer.authorizationStatus() {
        case .authorized: return true
        case .notDetermined:
            return await withCheckedContinuation { c in
                SFSpeechRecognizer.requestAuthorization { c.resume(returning: $0 == .authorized) }
            }
        default: return false
        }
    }

    private static func micAllowed() async -> Bool {
        #if os(macOS)
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return true
        case .notDetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        default: return false
        }
        #else
        switch AVAudioApplication.shared.recordPermission {
        case .granted: return true
        case .undetermined: return await AVAudioApplication.requestRecordPermission()
        default: return false
        }
        #endif
    }
}
