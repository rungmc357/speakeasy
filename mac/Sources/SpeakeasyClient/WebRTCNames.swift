#if canImport(LiveKitWebRTC)
// iPhone, iPad and Vision Pro link LiveKit's build of WebRTC, whose Objective-C names carry an "LK" prefix.
// These aliases let the shared call code use the usual names on every platform.
@_exported import LiveKitWebRTC

public typealias RTCAudioDeviceModule = LKRTCAudioDeviceModule
public typealias RTCAudioDeviceModuleDelegate = LKRTCAudioDeviceModuleDelegate
public typealias RTCAudioDuckingLevel = LKRTCAudioDuckingLevel
public typealias RTCAudioEngineAvailability = LKRTCAudioEngineAvailability
public typealias RTCAudioSession = LKRTCAudioSession
public typealias RTCAudioSessionConfiguration = LKRTCAudioSessionConfiguration
public typealias RTCAudioTrack = LKRTCAudioTrack
public typealias RTCConfiguration = LKRTCConfiguration
public typealias RTCDataBuffer = LKRTCDataBuffer
public typealias RTCDataChannel = LKRTCDataChannel
public typealias RTCDataChannelConfiguration = LKRTCDataChannelConfiguration
public typealias RTCDataChannelDelegate = LKRTCDataChannelDelegate
public typealias RTCDefaultVideoDecoderFactory = LKRTCDefaultVideoDecoderFactory
public typealias RTCDefaultVideoEncoderFactory = LKRTCDefaultVideoEncoderFactory
public typealias RTCIceCandidate = LKRTCIceCandidate
public typealias RTCIceConnectionState = LKRTCIceConnectionState
public typealias RTCIceGatheringState = LKRTCIceGatheringState
public typealias RTCMediaConstraints = LKRTCMediaConstraints
public typealias RTCMediaStream = LKRTCMediaStream
public typealias RTCPeerConnection = LKRTCPeerConnection
public typealias RTCPeerConnectionDelegate = LKRTCPeerConnectionDelegate
public typealias RTCPeerConnectionFactory = LKRTCPeerConnectionFactory
public typealias RTCPeerConnectionState = LKRTCPeerConnectionState
public typealias RTCRtpReceiver = LKRTCRtpReceiver
public typealias RTCRtpTransceiverInit = LKRTCRtpTransceiverInit
public typealias RTCSessionDescription = LKRTCSessionDescription
public typealias RTCSignalingState = LKRTCSignalingState

public let kRTCMediaConstraintsValueTrue = kLKRTCMediaConstraintsValueTrue
public let kRTCMediaConstraintsOfferToReceiveAudio = kLKRTCMediaConstraintsOfferToReceiveAudio

public let kRTCMediaStreamTrackKindAudio = kLKRTCMediaStreamTrackKindAudio

@discardableResult public func RTCInitializeSSL() -> Bool { LKRTCInitializeSSL() }
#endif
