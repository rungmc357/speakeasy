import AVFoundation
import Foundation
import Speech

/// Records a voice message for a session (no call needed) and transcribes it on this Mac with
/// Apple's on-device speech recognition. No audio leaves the Mac: only the reviewed text is sent.
@MainActor
protocol VoiceMessageRecording: AnyObject {
    /// Asks for the mic and speech recognition (prompts once). Completes on the main actor with
    /// (both granted, on-device transcription available).
    func authorize(_ done: @escaping @MainActor (Bool, Bool) -> Void)
    func start(partial: @escaping @MainActor (String) -> Void, level: @escaping @MainActor (Float) -> Void,
               finished: @escaping @MainActor (String?, String?) -> Void) -> Bool
    /// Stop listening; `finished` follows with the final text.
    func stop()
    /// Drop everything; `finished` is not called.
    func cancel()
}

@MainActor
final class VoiceMessageRecorder: VoiceMessageRecording {
    private var engine: AVAudioEngine?
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var latest = ""
    private var onFinished: (@MainActor (String?, String?) -> Void)?
    private var stopFallback: DispatchWorkItem?

    /// A debug binary without an Info.plist would crash asking for permission: it never asks.
    static var declared: Bool {
        Bundle.main.object(forInfoDictionaryKey: "NSSpeechRecognitionUsageDescription") != nil
            && Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") != nil
    }

    func authorize(_ done: @escaping @MainActor (Bool, Bool) -> Void) {
        guard Self.declared else { done(false, false); return }
        let onDevice = SFSpeechRecognizer()?.supportsOnDeviceRecognition ?? false
        AVCaptureDevice.requestAccess(for: .audio) { mic in
            guard mic else { Task { @MainActor in done(false, onDevice) }; return }
            SFSpeechRecognizer.requestAuthorization { status in
                Task { @MainActor in done(status == .authorized, onDevice) }
            }
        }
    }

    func start(partial: @escaping @MainActor (String) -> Void, level: @escaping @MainActor (Float) -> Void,
               finished: @escaping @MainActor (String?, String?) -> Void) -> Bool {
        guard task == nil, let recognizer = SFSpeechRecognizer(), recognizer.supportsOnDeviceRecognition else { return false }
        let request = SFSpeechAudioBufferRecognitionRequest()
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        request.taskHint = .dictation
        if #available(macOS 13, *) { request.addsPunctuation = true }
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else { return false }
        input.installTap(onBus: 0, bufferSize: 2048, format: format) { buffer, _ in
            request.append(buffer)
            let rms = Self.rms(buffer)
            Task { @MainActor in level(rms) }
        }
        engine.prepare()
        do { try engine.start() } catch { input.removeTap(onBus: 0); return false }
        self.engine = engine; self.request = request; self.latest = ""; self.onFinished = finished
        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            let text = result?.bestTranscription.formattedString
            let isFinal = result?.isFinal ?? false
            Task { @MainActor [weak self] in
                guard let self else { return }
                if let text, !text.isEmpty { self.latest = text; partial(text) }
                if isFinal { self.finish(text: self.latest, error: nil) }
                else if let error, (error as NSError).code != 216 /* cancelled */ {
                    self.finish(text: self.latest.isEmpty ? nil : self.latest,
                                error: self.latest.isEmpty ? "Couldn't transcribe that: \(error.localizedDescription)" : nil)
                }
            }
        }
        return true
    }

    func stop() {
        stopAudio()
        request?.endAudio()
        // The recognizer normally answers within a moment; never leave the composer stuck.
        let fallback = DispatchWorkItem { [weak self] in
            Task { @MainActor in self?.finish(text: self?.latest, error: nil) }
        }
        stopFallback = fallback
        DispatchQueue.main.asyncAfter(deadline: .now() + 3, execute: fallback)
    }

    func cancel() {
        onFinished = nil
        stopAudio()
        task?.cancel()
        teardown()
    }

    private func finish(text: String?, error: String?) {
        guard let done = onFinished else { return }
        onFinished = nil
        stopAudio()
        teardown()
        done(text, error)
    }

    private func stopAudio() {
        guard let engine else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        self.engine = nil
    }

    private func teardown() {
        stopFallback?.cancel(); stopFallback = nil
        task = nil; request = nil
    }

    nonisolated private static func rms(_ buffer: AVAudioPCMBuffer) -> Float {
        guard let data = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return 0 }
        var sum: Float = 0
        for i in 0..<Int(buffer.frameLength) { sum += data[i] * data[i] }
        let rms = sqrt(sum / Float(buffer.frameLength))
        // -50 dB … 0 dB → 0 … 1
        let db = 20 * log10(max(rms, 1e-6))
        return max(0, min(1, (db + 50) / 50))
    }
}
