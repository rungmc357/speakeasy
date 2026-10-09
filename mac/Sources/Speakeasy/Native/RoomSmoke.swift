import Foundation
import SpeakeasyCore
import SpeakeasyClient

/// `--room-smoke <audio file>`: runs listening mode's on-device transcriber over a recording, through
/// the same converter and SpeechAnalyzer path the mic uses (no mic, no network, no permission).
/// Prints the transcript as a call would receive it, then each line's offset into the recording.
/// Exits 0 when it heard words with timestamps in order, 1 when it heard nothing or failed, and 2
/// when listening mode can't run on this Mac or build.
@MainActor
enum RoomSmoke {
    static func run(path: String) {
        setvbuf(stdout, nil, _IOLBF, 0)
        let url = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        guard FileManager.default.fileExists(atPath: url.path) else {
            print("room-smoke failed: no file at \(url.path)"); exit(1)
        }
        guard RoomListener.isSupported else {
            print("room-smoke failed: \(RoomUnavailability.systemTooOld.message) with on-device transcription"); exit(2)
        }
        Task { @MainActor in
            let started = Date()
            do {
                let transcript = try await RoomListener.transcribeFile(at: url, log: { print("room-smoke: \($0)") })
                let text = transcript.render()
                let starts = transcript.segments.map(\.start)
                let inOrder = zip(starts, starts.dropFirst()).allSatisfy { $0 <= $1 }
                print("room-smoke transcript:")
                print(text.isEmpty ? "(nothing heard)" : text)
                if let first = starts.first {
                    let offsets = starts.map { String(format: "%.1f", $0.timeIntervalSince(first)) }
                    print("room-smoke offsets (s): \(offsets.joined(separator: " "))")
                }
                print("room-smoke \(text.isEmpty || !inOrder ? "failed" : "ok") segments=\(transcript.segments.count) "
                      + "scalars=\(text.unicodeScalars.count) in_order=\(inOrder) "
                      + "took=\(String(format: "%.1f", Date().timeIntervalSince(started)))s")
                exit(text.isEmpty || !inOrder ? 1 : 0)
            } catch let error as RoomListenerError {
                print("room-smoke failed: \(error.message)")
                if case .unavailable = error { exit(2) }
                exit(1)
            } catch {
                print("room-smoke failed: \(error.localizedDescription)")
                exit(1)
            }
        }
    }
}
