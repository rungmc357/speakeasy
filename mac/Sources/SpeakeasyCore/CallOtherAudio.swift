/// What a call does to audio other apps are playing (music, a podcast) on iPhone, iPad and Vision Pro.
/// The iPhone app's audio session reads `sessionOptions`; the call engine hands the ducking fields to
/// Apple's voice processing (`AVAudioVoiceProcessingOtherAudioDuckingConfiguration`).
public struct CallOtherAudioPolicy: Equatable, Sendable {
    /// Apple's ducking depth for other audio. `.default` is Apple's fixed voice-chat amount.
    public enum DuckingLevel: Equatable, Sendable { case `default`, min, mid, max }

    /// The other app keeps playing through the call instead of being interrupted.
    public var mixesWithOthers: Bool
    /// Duck only while someone (you or the assistant) is speaking, and less when nobody is.
    public var advancedDucking: Bool
    public var duckingLevel: DuckingLevel

    public init(mixesWithOthers: Bool, advancedDucking: Bool, duckingLevel: DuckingLevel) {
        self.mixesWithOthers = mixesWithOthers
        self.advancedDucking = advancedDucking
        self.duckingLevel = duckingLevel
    }

    /// Music keeps playing at its own volume and dips only while one of you talks.
    public static let iPhoneCall = CallOtherAudioPolicy(mixesWithOthers: true, advancedDucking: true, duckingLevel: .min)
    /// Other audio stops for the call (Apple's defaults; how every call behaved before).
    public static let interruptsOthers = CallOtherAudioPolicy(mixesWithOthers: false, advancedDucking: false, duckingLevel: .default)

    public static var forThisDevice: CallOtherAudioPolicy {
        #if os(visionOS)
        return .interruptsOthers
        #else
        return .iPhoneCall
        #endif
    }

    /// The call's audio-session options: the speaker unless headphones are in, AirPods in call mode and in
    /// music quality, plus mixing when this policy mixes.
    public var sessionOptions: CallSessionOptions {
        var options: CallSessionOptions = [.defaultToSpeaker, .bluetoothHFP, .bluetoothA2DP]
        if mixesWithOthers { options.insert(.mixWithOthers) }
        return options
    }
}

/// The `AVAudioSession` category options a call uses, without AVFoundation (it has no session on the Mac).
public struct CallSessionOptions: OptionSet, Sendable {
    public let rawValue: Int
    public init(rawValue: Int) { self.rawValue = rawValue }

    public static let defaultToSpeaker = CallSessionOptions(rawValue: 1 << 0)
    public static let bluetoothHFP = CallSessionOptions(rawValue: 1 << 1)
    public static let bluetoothA2DP = CallSessionOptions(rawValue: 1 << 2)
    public static let mixWithOthers = CallSessionOptions(rawValue: 1 << 3)
}
