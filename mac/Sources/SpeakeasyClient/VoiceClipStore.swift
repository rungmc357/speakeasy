import AVFoundation
import Combine
import Foundation

/// Voice notes this device sent, kept so their bubbles can play them: the clip (AAC `audio/mp4`)
/// and its waveform, under Caches (the system may clear it; the bubble then just can't play), keyed
/// by the message's client id (the server echoes it on the page as `voice_clip`). Never synced, never
/// logged; old clips go after `keepDays`.
public enum VoiceClipStore {
    public static let keepDays: Double = 30

    /// The folder; tests point it elsewhere.
    nonisolated(unsafe) public static var folder: URL = {
        let base = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return base.appendingPathComponent("VoiceMessages", isDirectory: true)
    }()

    public struct Meta: Codable, Equatable, Sendable {
        public var duration: TimeInterval
        /// 0…1 bars for the bubble's waveform.
        public var levels: [Double]
        public init(duration: TimeInterval, levels: [Double]) { self.duration = duration; self.levels = levels }
    }

    static func valid(_ id: String) -> Bool {
        !id.isEmpty && id.count <= 80 && id.allSatisfy { $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }
    }

    public static func audioURL(_ id: String) -> URL? {
        guard valid(id) else { return nil }
        let url = folder.appendingPathComponent(id + ".m4a")
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }

    public static func save(_ id: String, audio: Data, meta: Meta) {
        guard valid(id) else { return }
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appendingPathComponent(id + ".m4a")
        // File protection exists on iPhone/iPad only; the Mac refuses the option, so fall back to a plain write.
        if (try? audio.write(to: url, options: [.atomic, .completeFileProtection])) == nil {
            try? audio.write(to: url, options: .atomic)
        }
        if let json = try? JSONEncoder().encode(meta) {
            try? json.write(to: folder.appendingPathComponent(id + ".json"), options: .atomic)
        }
        prune()
    }

    public static func audio(_ id: String) -> Data? { audioURL(id).flatMap { try? Data(contentsOf: $0) } }

    public static func meta(_ id: String) -> Meta? {
        guard valid(id), let data = try? Data(contentsOf: folder.appendingPathComponent(id + ".json")) else { return nil }
        return try? JSONDecoder().decode(Meta.self, from: data)
    }

    /// `count` bars from a recording's live levels (averaged buckets), for the bubble.
    public static func bars(_ levels: [Double], count: Int = 32) -> [Double] {
        guard !levels.isEmpty, count > 0 else { return [] }
        if levels.count <= count { return levels }
        let step = Double(levels.count) / Double(count)
        return (0..<count).map { i in
            let lo = Int(Double(i) * step), hi = max(lo + 1, Int(Double(i + 1) * step))
            let slice = levels[lo..<min(hi, levels.count)]
            return slice.reduce(0, +) / Double(slice.count)
        }
    }

    static func prune(now: Date = Date()) {
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: folder, includingPropertiesForKeys: [.contentModificationDateKey]) else { return }
        for file in files {
            let at = (try? file.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? now
            if now.timeIntervalSince(at) > keepDays * 86400 { try? fm.removeItem(at: file) }
        }
    }
}

/// Plays one voice note at a time (a second bubble's play stops the first).
@MainActor public final class VoiceClipPlayer: NSObject, ObservableObject, AVAudioPlayerDelegate {
    public static let shared = VoiceClipPlayer()
    /// The clip playing now, and how far in (0…1).
    @Published public private(set) var playing: String?
    @Published public private(set) var progress: Double = 0
    private var player: AVAudioPlayer?
    private var ticker: Timer?

    public func toggle(_ id: String) {
        if playing == id { stop(); return }
        stop()
        guard let url = VoiceClipStore.audioURL(id), let p = try? AVAudioPlayer(contentsOf: url) else { return }
        #if os(iOS)
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
        try? AVAudioSession.sharedInstance().setActive(true)
        #endif
        p.delegate = self
        p.play()
        player = p; playing = id; progress = 0
        ticker = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self, let p = self.player, p.duration > 0 else { return }
                self.progress = min(1, p.currentTime / p.duration)
            }
        }
    }

    public func stop() {
        ticker?.invalidate(); ticker = nil
        player?.stop(); player = nil
        playing = nil; progress = 0
    }

    nonisolated public func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        Task { @MainActor in self.stop() }
    }
}
