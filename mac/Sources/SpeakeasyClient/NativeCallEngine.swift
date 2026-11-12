import AVFoundation
import Foundation
#if os(visionOS)
import LiveKitWebRTC
#else
import WebRTC
#endif

/// Native WebRTC transport: one peer connection, one captured audio track,
/// the `oai-events` data channel, and remote audio playout via the default ADM.
/// All public callbacks are delivered on the main queue.
public final class NativeCallEngine: NSObject, RTCPeerConnectionDelegate, RTCDataChannelDelegate {
    public enum EngineError: LocalizedError {
        case peerConnection, offer(String), iceTimeout, noLocalDescription, remote(String)
        public var errorDescription: String? {
            switch self {
            case .peerConnection: return "Could not create the voice connection"
            case .offer(let m): return "Could not create the voice offer: \(m)"
            case .iceTimeout: return "ICE gathering timed out"
            case .noLocalDescription: return "No local voice description"
            case .remote(let m): return "Voice answer rejected: \(m)"
            }
        }
    }

    /// One factory per call. The factory owns WebRTC's audio device module, which
    /// picks the Mac's default input/output (and their formats) when it is created
    /// and never follows a later change. A process-wide factory therefore kept a
    /// call on the MacBook speakers after AirPods connected, and a later call could
    /// play through a stale format (deep, slow "chopped" voice).
    private static let sslReady: Void = { RTCInitializeSSL() }()
    /// Created on first use (in `prepare`), after `warmAudioRoute()` has put a Bluetooth
    /// headset in call mode, so WebRTC's audio device opens at the final rate.
    private lazy var factory: RTCPeerConnectionFactory = {
        _ = NativeCallEngine.sslReady
        return RTCPeerConnectionFactory(encoderFactory: RTCDefaultVideoEncoderFactory(),
                                        decoderFactory: RTCDefaultVideoDecoderFactory())
    }()

    #if os(macOS)
    private let routeWarmer = AudioRouteWarmer()
    #endif
    /// Called on main if the output rate changes mid-call (WebRTC can't follow it).
    public var onAudioFormatChanged: (() -> Void)?
    private var answered = false
    private var formatChangedEarly = false

    /// AirPods (any Bluetooth headset) switch from 48 kHz music mode to 24 kHz call mode
    /// when their mic opens. WebRTC sets playout up at the rate it sees and can't adapt,
    /// so the reply plays at half speed (deep, slow). Settle the headset first.
    /// iOS: AVAudioSession (configured by the iPhone app) owns the route; nothing to warm.
    public func warmAudioRoute() async {
        #if os(macOS)
        await routeWarmer.warm()
        #endif
    }

    private func watchAudioFormat() {
        #if os(macOS)
        routeWarmer.watchOutputRate { [weak self] in
            guard let self, !self.closed else { return }
            if self.answered { self.onAudioFormatChanged?() } else { self.formatChangedEarly = true }
        }
        #endif
    }

    public var onMessage: ((Data) -> Void)?
    public var onChannelOpen: (() -> Void)?
    public var onChannelClosed: (() -> Void)?
    public var onConnectionFailed: (() -> Void)?

    private var peer: RTCPeerConnection?
    private var channel: RTCDataChannel?
    private var localTrack: RTCAudioTrack?
    private var iceComplete: (() -> Void)?
    private var remoteMuted = false
    private var closed = false

    /// Build the peer connection, capture track (AEC/NS on), and data channel.
    /// `captureAudio=false` negotiates an audio transceiver without opening the mic.
    public func prepare(captureAudio: Bool = true) throws {
        let configuration = RTCConfiguration()
        configuration.sdpSemantics = .unifiedPlan
        configuration.iceServers = []
        configuration.bundlePolicy = .maxBundle
        configuration.rtcpMuxPolicy = .require
        let constraints = RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: nil)
        guard let peer = factory.peerConnection(with: configuration, constraints: constraints, delegate: self) else {
            throw EngineError.peerConnection
        }
        self.peer = peer
        if captureAudio {
            let audioConstraints = RTCMediaConstraints(mandatoryConstraints: nil, optionalConstraints: [
                "googEchoCancellation": kRTCMediaConstraintsValueTrue,
                "googNoiseSuppression": kRTCMediaConstraintsValueTrue,
                "googAutoGainControl": kRTCMediaConstraintsValueTrue,
                "googHighpassFilter": kRTCMediaConstraintsValueTrue,
            ])
            let source = factory.audioSource(with: audioConstraints)
            let track = factory.audioTrack(with: source, trackId: "speakeasy-mic")
            peer.add(track, streamIds: ["speakeasy"])
            localTrack = track
        } else {
            let init_ = RTCRtpTransceiverInit()
            init_.direction = .sendRecv
            peer.addTransceiver(of: .audio, init: init_)
        }
        let channelConfig = RTCDataChannelConfiguration()
        channelConfig.isOrdered = true
        guard let channel = peer.dataChannel(forLabel: "oai-events", configuration: channelConfig) else {
            throw EngineError.peerConnection
        }
        channel.delegate = self
        self.channel = channel
        watchAudioFormat()
    }

    /// Create the offer, set it locally, and wait for ICE gathering to finish so
    /// the returned SDP is complete (non-trickle).
    public func createOffer(iceTimeout: TimeInterval = 10) async throws -> String {
        guard let peer else { throw EngineError.peerConnection }
        let constraints = RTCMediaConstraints(mandatoryConstraints: [
            kRTCMediaConstraintsOfferToReceiveAudio: kRTCMediaConstraintsValueTrue,
        ], optionalConstraints: nil)
        let offer: RTCSessionDescription = try await withCheckedThrowingContinuation { continuation in
            peer.offer(for: constraints) { sdp, error in
                if let sdp { continuation.resume(returning: sdp) }
                else { continuation.resume(throwing: EngineError.offer(error?.localizedDescription ?? "unknown")) }
            }
        }
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            peer.setLocalDescription(offer) { error in
                if let error { continuation.resume(throwing: EngineError.offer(error.localizedDescription)) }
                else { continuation.resume() }
            }
        }
        if peer.iceGatheringState != .complete {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                let lock = NSLock()
                var resumed = false
                let finish: (Result<Void, Error>) -> Void = { result in
                    lock.lock(); defer { lock.unlock() }
                    guard !resumed else { return }
                    resumed = true
                    continuation.resume(with: result)
                }
                lock.lock()
                iceComplete = { finish(.success(())) }
                lock.unlock()
                if peer.iceGatheringState == .complete { finish(.success(())) }
                DispatchQueue.global().asyncAfter(deadline: .now() + iceTimeout) { finish(.failure(EngineError.iceTimeout)) }
            }
        }
        guard let sdp = peer.localDescription?.sdp else { throw EngineError.noLocalDescription }
        return sdp
    }

    public func setRemoteAnswer(_ sdp: String) async throws {
        guard let peer else { throw EngineError.peerConnection }
        let answer = RTCSessionDescription(type: .answer, sdp: sdp)
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            peer.setRemoteDescription(answer) { error in
                if let error { continuation.resume(throwing: EngineError.remote(error.localizedDescription)) }
                else { continuation.resume() }
            }
        }
        applyRemoteMute()
        answered = true
        if formatChangedEarly {
            DispatchQueue.main.async { [weak self] in
                guard let self, !self.closed else { return }
                self.onAudioFormatChanged?()
            }
        }
    }

    public var isChannelOpen: Bool { channel?.readyState == .open }

    @discardableResult
    public func send(json object: [String: Any]) -> Bool {
        guard let channel, channel.readyState == .open,
              let data = try? JSONSerialization.data(withJSONObject: object) else { return false }
        return channel.sendData(RTCDataBuffer(data: data, isBinary: false))
    }

    /// Mic mute: local track disabled; the call (and billing) stays open.
    public func setMicEnabled(_ enabled: Bool) { localTrack?.isEnabled = enabled }

    /// Quiet reply: disable remote audio tracks (playout silenced).
    public func setRemoteAudioEnabled(_ enabled: Bool) {
        remoteMuted = !enabled
        applyRemoteMute()
    }

    private func applyRemoteMute() {
        for receiver in peer?.receivers ?? [] where receiver.track?.kind == kRTCMediaStreamTrackKindAudio {
            receiver.track?.isEnabled = !remoteMuted
        }
    }

    /// Current audio levels (0...1) from WebRTC stats: `mic` = local capture ("media-source"),
    /// `voice` = the assistant's playout ("inbound-rtp"). Calls back on the main queue.
    public func audioLevels(_ completion: @escaping (_ mic: Double, _ voice: Double) -> Void) {
        guard let peer, !closed else { return }
        peer.statistics { report in
            var mic = 0.0, voice = 0.0
            for stats in report.statistics.values {
                guard (stats.values["kind"] as? String) == "audio",
                      let level = (stats.values["audioLevel"] as? NSNumber)?.doubleValue else { continue }
                if stats.type == "media-source" { mic = max(mic, level) }
                else if stats.type == "inbound-rtp" { voice = max(voice, level) }
            }
            DispatchQueue.main.async { completion(mic, voice) }
        }
    }

    /// Mic health probe: the capture level (nil when WebRTC reports none) and outgoing audio
    /// packets so far (nil when unknown). Calls back on the main queue.
    public func micProbe(_ completion: @escaping (_ level: Double?, _ packetsSent: Int?) -> Void) {
        guard let peer, !closed else { return }
        peer.statistics { report in
            var level: Double?, packets: Int?
            for stats in report.statistics.values where (stats.values["kind"] as? String) == "audio" {
                if stats.type == "media-source", let l = (stats.values["audioLevel"] as? NSNumber)?.doubleValue {
                    level = max(level ?? 0, l)
                } else if stats.type == "outbound-rtp", let p = (stats.values["packetsSent"] as? NSNumber)?.intValue {
                    packets = (packets ?? 0) + p
                }
            }
            DispatchQueue.main.async { completion(level, packets) }
        }
    }

    /// The orb's levels and the mic probe from one stats report (call energy).
    public func callStats(_ completion: @escaping (CallStatsSample) -> Void) {
        guard let peer, !closed else { return }
        peer.statistics { report in
            var mic = 0.0, voice = 0.0
            var level: Double?, packets: Int?
            for stats in report.statistics.values where (stats.values["kind"] as? String) == "audio" {
                let l = (stats.values["audioLevel"] as? NSNumber)?.doubleValue
                switch stats.type {
                case "media-source":
                    if let l { mic = max(mic, l); level = max(level ?? 0, l) }
                case "inbound-rtp":
                    if let l { voice = max(voice, l) }
                case "outbound-rtp":
                    if let p = (stats.values["packetsSent"] as? NSNumber)?.intValue { packets = (packets ?? 0) + p }
                default: break
                }
            }
            let sample = CallStatsSample(mic: mic, voice: voice, micLevel: level, packetsSent: packets)
            DispatchQueue.main.async { completion(sample) }
        }
    }

    public var sdpLineCount: Int? {
        peer?.localDescription?.sdp.components(separatedBy: .newlines).filter { !$0.isEmpty }.count
    }

    /// iPhone: keep the call's audio (WebRTC's voice unit) off while the on-device listener has the
    /// mic, so the two never fight over it; `releaseAudio()` hands the mic over. No-op on the Mac,
    /// where both can share the input.
    public func holdAudio() {
        #if os(iOS) || os(visionOS)
        let session = RTCAudioSession.sharedInstance()
        session.useManualAudio = true
        session.isAudioEnabled = false
        audioHeld = true
        #endif
    }

    /// Let the call's audio start (call after the on-device listener has stopped).
    public func releaseAudio() {
        #if os(iOS) || os(visionOS)
        guard audioHeld else { return }
        audioHeld = false
        let session = RTCAudioSession.sharedInstance()
        session.isAudioEnabled = true
        session.useManualAudio = false
        #endif
    }

    public var isAudioHeld: Bool { audioHeld }

    /// 74.1 (F4(a)): the mic gave nothing at the start of a call. Restart the device's audio unit on
    /// THIS peer connection instead of reopening the call: stop WebRTC's voice unit, re-activate the
    /// audio session the way the call uses it, and start the unit again. The connection, the server
    /// session and the conversation are untouched. Returns false where the device can't (the Mac, or
    /// the session wouldn't re-activate): the caller then reopens the connection as before.
    @discardableResult
    public func restartLocalAudio() -> Bool {
        #if os(iOS) || os(visionOS)
        guard !closed, peer != nil, !audioHeld else { return false }
        let session = RTCAudioSession.sharedInstance()
        // isAudioEnabled only drives the unit in manual mode: off, re-activate, on, then hand back.
        session.useManualAudio = true
        session.isAudioEnabled = false
        session.lockForConfiguration()
        var ok = true
        do {
            try session.setConfiguration(RTCAudioSessionConfiguration.webRTC(), active: true)
        } catch {
            ok = false
        }
        session.unlockForConfiguration()
        session.isAudioEnabled = true
        session.useManualAudio = false
        return ok
        #else
        return false
        #endif
    }

    /// iPhone: WebRTC's audio session is one object for the whole app. A hold from an earlier call
    /// (or a listener) that was never released leaves it in manual mode with audio off, and every
    /// later call is deaf: 0 packets, no input route. Start each call from a clean state.
    public static func resetSharedAudio() {
        #if os(iOS) || os(visionOS)
        let session = RTCAudioSession.sharedInstance()
        if session.useManualAudio || !session.isAudioEnabled {
            session.isAudioEnabled = true
            session.useManualAudio = false
        }
        #endif
    }

    /// The audio session as WebRTC sees it (for the mic report).
    public static func sharedAudioSummary() -> String {
        #if os(iOS) || os(visionOS)
        let s = RTCAudioSession.sharedInstance()
        let category = s.category.replacingOccurrences(of: "AVAudioSessionCategory", with: "")
        let mode = s.mode.replacingOccurrences(of: "AVAudioSessionMode", with: "")
        return "session \(s.isActive ? "active" : "inactive") \(category)/\(mode)"
            + (s.useManualAudio ? " manual" : "") + (s.isAudioEnabled ? "" : " audio-off")
        #else
        return ""
        #endif
    }
    private var audioHeld = false

    public func close() {
        releaseAudio()
        guard !closed else { return }
        closed = true
        localTrack?.isEnabled = false
        channel?.delegate = nil
        channel?.close()
        peer?.close()
        channel = nil
        localTrack = nil
        peer = nil
        #if os(macOS)
        routeWarmer.release()
        #endif
    }

    // MARK: RTCPeerConnectionDelegate (signaling thread → main)

    public func peerConnection(_ peerConnection: RTCPeerConnection, didChange stateChanged: RTCSignalingState) {}
    public func peerConnection(_ peerConnection: RTCPeerConnection, didAdd stream: RTCMediaStream) {}
    public func peerConnection(_ peerConnection: RTCPeerConnection, didRemove stream: RTCMediaStream) {}
    public func peerConnectionShouldNegotiate(_ peerConnection: RTCPeerConnection) {}
    public func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceConnectionState) {}
    public func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCIceGatheringState) {
        if newState == .complete { iceComplete?() }
    }
    public func peerConnection(_ peerConnection: RTCPeerConnection, didGenerate candidate: RTCIceCandidate) {}
    public func peerConnection(_ peerConnection: RTCPeerConnection, didRemove candidates: [RTCIceCandidate]) {}
    public func peerConnection(_ peerConnection: RTCPeerConnection, didOpen dataChannel: RTCDataChannel) {}
    public func peerConnection(_ peerConnection: RTCPeerConnection, didChange newState: RTCPeerConnectionState) {
        if newState == .failed {
            DispatchQueue.main.async { [weak self] in self?.onConnectionFailed?() }
        }
    }
    public func peerConnection(_ peerConnection: RTCPeerConnection, didAdd rtpReceiver: RTCRtpReceiver, streams mediaStreams: [RTCMediaStream]) {
        DispatchQueue.main.async { [weak self] in self?.applyRemoteMute() }
    }

    // MARK: RTCDataChannelDelegate

    public func dataChannelDidChangeState(_ dataChannel: RTCDataChannel) {
        let state = dataChannel.readyState
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if state == .open { onChannelOpen?() }
            if state == .closed { onChannelClosed?() }
        }
    }

    public func dataChannel(_ dataChannel: RTCDataChannel, didReceiveMessageWith buffer: RTCDataBuffer) {
        guard !buffer.isBinary else { return }
        let data = buffer.data
        DispatchQueue.main.async { [weak self] in self?.onMessage?(data) }
    }
}
