import Foundation
import SpeakeasyCore
import SpeakeasyClient

/// `--relay-smoke <ws-url> [seconds]`: runs the relay call engine (Grok Voice transport) against a
/// local WebSocket that implements the relay contract (mac/scripts/relay-echo-server.py), with a
/// generated tone in place of the mic and playback muted. Prints counters as one JSON line.
@MainActor
enum RelaySmoke {
    static func run(url: String, seconds: Double) {
        guard let socketURL = URL(string: url) else { print("relay-smoke failed: bad url"); exit(2) }
        var request = URLRequest(url: socketURL)
        request.setValue("Bearer " + (ProcessInfo.processInfo.environment["SPEAKEASY_SMOKE_TOKEN"] ?? "relay-smoke"), forHTTPHeaderField: "Authorization")
        let engine = RelayCallEngine(options: .init(syntheticMic: true, playbackVolume: 0))
        var opened = false, started = false, closedCallback = false
        engine.onChannelOpen = { opened = true }
        engine.onMessage = { data in
            if parseDataChannelMessage(data).contains(.sessionStarted) { started = true }
        }
        engine.onChannelClosed = { closedCallback = true }
        engine.setMicEnabled(true)
        do { try engine.start(request: request) } catch {
            print("relay-smoke failed: \(error.localizedDescription)"); exit(1)
        }
        // Mute for half a second mid-run, so the mute frames go both ways.
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds * 0.5) { engine.setMicEnabled(false) }
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds * 0.5 + 0.5) { engine.setMicEnabled(true) }
        var levels: [Double] = []
        let meter = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
            engine.audioLevels { _, voice in levels.append(voice) }
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) {
            meter.invalidate()
            let hungUp = engine.send(json: ["type": "session.close"])
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) {
                let s = engine.currentStats
                engine.close()
                let report: [String: Any] = [
                    "opened": opened, "session_started": started, "voice_processing": s.voiceProcessing,
                    "bytes_sent": s.bytesSent, "frames_sent": s.framesSent,
                    "bytes_received": s.bytesReceived, "frames_received": s.framesReceived,
                    "flushes": s.flushes, "flushes_that_stopped_audio": s.flushesThatStoppedAudio,
                    "dropped_samples": s.droppedSamples, "max_voice_level": (s.maxVoiceLevel * 1000).rounded() / 1000,
                    "voice_level_samples_nonzero": levels.filter { $0 > 0 }.count,
                    "control_sent": s.controlSent, "control_received": s.controlReceived,
                    "hang_up_sent": hungUp, "closed_reason": s.closedReason ?? NSNull(), "closed_callback": closedCallback,
                ]
                let data = try! JSONSerialization.data(withJSONObject: report, options: [.sortedKeys])
                print("relay-smoke " + String(data: data, encoding: .utf8)!)
                let needFlush = ProcessInfo.processInfo.environment["SPEAKEASY_SMOKE_TOKEN"] == nil  // the echo server barges in; a real call may not
                let ok = opened && started && s.framesSent > 0 && s.framesReceived > 0 && (!needFlush || s.flushes > 0) && hungUp
                exit(ok ? 0 : 1)
            }
        }
    }
}
