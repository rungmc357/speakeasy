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
    /// iPhone mic handover with the on-device listener (`EarlyCapture`).
    func holdAudio()
    func releaseAudio()
    var isAudioHeld: Bool { get }
    func close()
}

extension NativeCallEngine: CallTransport {}
