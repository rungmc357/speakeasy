import XCTest
@testable import SpeakeasyCore

/// Music playing on the iPhone when a call starts: it keeps playing, and dips only while someone talks.
final class CallOtherAudioTests: XCTestCase {
    func testTheIPhoneCallKeepsMusicPlayingAndDucksItOnlyForVoiceAtTheShallowestLevel() {
        let policy = CallOtherAudioPolicy.iPhoneCall
        XCTAssertEqual(policy, CallOtherAudioPolicy(mixesWithOthers: true, advancedDucking: true, duckingLevel: .min))
    }

    func testTheIPhoneCallSessionMixesAndStillReachesTheSpeakerAndAirPods() {
        XCTAssertEqual(CallOtherAudioPolicy.iPhoneCall.sessionOptions,
                       [.defaultToSpeaker, .bluetoothHFP, .bluetoothA2DP, .mixWithOthers])
    }

    func testVisionProKeepsInterruptingOtherAudioWithApplesDefaultDucking() {
        let policy = CallOtherAudioPolicy.interruptsOthers
        XCTAssertEqual(policy, CallOtherAudioPolicy(mixesWithOthers: false, advancedDucking: false, duckingLevel: .default))
        XCTAssertEqual(policy.sessionOptions, [.defaultToSpeaker, .bluetoothHFP, .bluetoothA2DP])
    }

    func testThisDeviceUsesItsPlatformsPolicy() {
        #if os(visionOS)
        XCTAssertEqual(CallOtherAudioPolicy.forThisDevice, .interruptsOthers)
        #else
        XCTAssertEqual(CallOtherAudioPolicy.forThisDevice, .iPhoneCall)
        #endif
    }
}
