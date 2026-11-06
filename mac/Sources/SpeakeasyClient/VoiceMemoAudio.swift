import AVFoundation
import Foundation

/// The audio of a voice message, kept while it records so Hermes can transcribe the whole clip
/// (servers with `transcribe`: `ServerClient.transcribeVoice`), the way it does a Telegram voice note.
///
/// On-device recognition alone lost words: it can drop the start of a message, and after a pause
/// its result restarts and the earlier part is gone. The live words still show while recording,
/// but the text that lands in the field comes from Hermes when the server can transcribe.
///
/// Usage: `let audio = VoiceMemoAudio(format: input.outputFormat(forBus: 0))`, `audio?.append(buffer)`
/// from the mic tap (any thread), then `audio.finish()` for the AAC (`audio/mp4`) bytes, or
/// `discard()`. The file lives in a private temp folder and is deleted by either call.
public final class VoiceMemoAudio: @unchecked Sendable {
    public static let mimeType = "audio/mp4"
    /// The plugin refuses more than this (Hermes' own cap).
    public static let maxBytes = 25 * 1024 * 1024

    private let lock = NSLock()
    private var file: AVAudioFile?
    private let url: URL
    private var frames: AVAudioFramePosition = 0

    public init?(format: AVAudioFormat) {
        guard format.sampleRate > 0, format.channelCount > 0 else { return nil }
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("voice-memo-" + UUID().uuidString, isDirectory: true)
        do {
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
        } catch { return nil }
        url = folder.appendingPathComponent("memo.m4a")
        // Mono AAC at the mic's rate (capped at 48 kHz): about 0.5 MB a minute, speech-clear.
        let settings: [String: Any] = [
            AVFormatIDKey: kAudioFormatMPEG4AAC,
            AVSampleRateKey: min(format.sampleRate, 48_000),
            AVNumberOfChannelsKey: 1,
            AVEncoderBitRateKey: 64_000,
        ]
        do {
            file = try AVAudioFile(forWriting: url, settings: settings, commonFormat: format.commonFormat,
                                   interleaved: format.isInterleaved)
        } catch {
            try? FileManager.default.removeItem(at: folder)
            return nil
        }
    }

    /// One buffer from the mic tap. Buffers whose format doesn't match are skipped, never fatal.
    public func append(_ buffer: AVAudioPCMBuffer) {
        lock.lock(); defer { lock.unlock() }
        guard let file, buffer.frameLength > 0 else { return }
        do {
            if buffer.format.channelCount != file.processingFormat.channelCount, let mono = Self.firstChannel(buffer, as: file.processingFormat) {
                try file.write(from: mono)
            } else {
                try file.write(from: buffer)
            }
            frames += AVAudioFramePosition(buffer.frameLength)
        } catch {}
    }

    /// Seconds recorded so far.
    public var seconds: Double {
        lock.lock(); defer { lock.unlock() }
        guard let rate = file?.processingFormat.sampleRate, rate > 0 else { return 0 }
        return Double(frames) / rate
    }

    /// The finished clip's bytes (nil when nothing was recorded or it's over the cap). The file is removed.
    public func finish() -> Data? {
        lock.lock()
        file = nil  // closes and finalizes the m4a
        let recorded = frames
        lock.unlock()
        defer { cleanup() }
        guard recorded > 0, let data = try? Data(contentsOf: url), !data.isEmpty, data.count <= Self.maxBytes else { return nil }
        return data
    }

    public func discard() {
        lock.lock(); file = nil; lock.unlock()
        cleanup()
    }

    deinit { cleanup() }

    private func cleanup() { try? FileManager.default.removeItem(at: url.deletingLastPathComponent()) }

    private static func firstChannel(_ buffer: AVAudioPCMBuffer, as format: AVAudioFormat) -> AVAudioPCMBuffer? {
        guard let src = buffer.floatChannelData, let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: buffer.frameLength),
              let dst = out.floatChannelData else { return nil }
        out.frameLength = buffer.frameLength
        dst[0].update(from: src[0], count: Int(buffer.frameLength))
        return out
    }
}
