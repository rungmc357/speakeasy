import Foundation

/// What `NativeVoiceClient` needs from a call's audio transport. Two engines satisfy it:
/// `NativeCallEngine` (WebRTC straight to the provider) and `RelayCallEngine` (PCM relayed by the
/// plugin over a WebSocket, Grok Voice). All callbacks arrive on the main queue.
public protocol CallTransport: AnyObject {
    /// Provider/plugin events in the data-channel shape (`parseDataChannelMessage`).
    var onMessage: ((Data) -> Void)? { get set }
    var onChannelOpen: (() -> Void)? { get set }
    var onChannelClosed: (() -> Void)? { get set }
    var onConnectionFailed: (() -> Void)? { get set }
    /// The audio device's format changed mid-call and the transport can't follow it.
    var onAudioFormatChanged: (() -> Void)? { get set }

    /// Send a control message. `session.close` is the hang-up.
    @discardableResult func send(json object: [String: Any]) -> Bool
    func setMicEnabled(_ enabled: Bool)
    func setRemoteAudioEnabled(_ enabled: Bool)
    /// Linear levels 0...1, called back on main.
    func audioLevels(_ completion: @escaping (_ mic: Double, _ voice: Double) -> Void)
    func micProbe(_ completion: @escaping (_ level: Double?, _ packetsSent: Int?) -> Void)
    /// Call energy: the orb's levels and the mic check's probe from ONE read (WebRTC builds a full
    /// stats report per read; the orb and the mic check used to read their own, ~16 a second).
    func callStats(_ completion: @escaping (CallStatsSample) -> Void)
    /// iPhone mic handover with the on-device listener (`EarlyCapture`).
    func holdAudio()
    func releaseAudio()
    var isAudioHeld: Bool { get }
    /// Restart the device's audio unit on the same connection (74.1); false = can't, reopen instead.
    func restartLocalAudio() -> Bool
    func close()
}

extension CallTransport {
    /// Transports that can't restart their audio in place reopen the connection instead.
    public func restartLocalAudio() -> Bool { false }
}

extension NativeCallEngine: CallTransport {}

/// One read of the call's audio: the orb's levels plus the mic check's probe.
public struct CallStatsSample: Sendable {
    public var mic: Double
    public var voice: Double
    public var micLevel: Double?
    public var packetsSent: Int?
    public init(mic: Double, voice: Double, micLevel: Double?, packetsSent: Int?) {
        self.mic = mic; self.voice = voice; self.micLevel = micLevel; self.packetsSent = packetsSent
    }
}

public extension CallTransport {
    /// Transports whose reads are cheap (the relay keeps its own levels): both reads, back to back.
    func callStats(_ completion: @escaping (CallStatsSample) -> Void) {
        audioLevels { mic, voice in
            self.micProbe { level, packets in
                completion(CallStatsSample(mic: mic, voice: voice, micLevel: level, packetsSent: packets))
            }
        }
    }
}
