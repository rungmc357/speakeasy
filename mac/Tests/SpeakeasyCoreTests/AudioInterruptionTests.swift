import XCTest
@testable import SpeakeasyCore

/// A phone call or Siri in the middle of a relayed (Grok) call on iPhone: the engine comes back
/// when the interruption ends, and is left alone otherwise.
final class AudioInterruptionTests: XCTestCase {
    private let began: UInt = 1, ended: UInt = 0, shouldResume: UInt = 1

    func testAnEndedInterruptionThatMayResumeRestartsALiveCall() {
        XCTAssertEqual(AudioInterruption.action(typeRaw: ended, optionsRaw: shouldResume, audioRunning: true, closed: false),
                       .restart)
    }

    func testNothingRestartsWhenItShouldNot() {
        // the interruption is still going on
        XCTAssertEqual(AudioInterruption.action(typeRaw: began, optionsRaw: nil, audioRunning: true, closed: false), .none)
        // another app took the audio over (no shouldResume)
        XCTAssertEqual(AudioInterruption.action(typeRaw: ended, optionsRaw: 0, audioRunning: true, closed: false), .none)
        XCTAssertEqual(AudioInterruption.action(typeRaw: ended, optionsRaw: nil, audioRunning: true, closed: false), .none)
        // the call ended, or its audio never started (the listener holds the mic)
        XCTAssertEqual(AudioInterruption.action(typeRaw: ended, optionsRaw: shouldResume, audioRunning: true, closed: true), .none)
        XCTAssertEqual(AudioInterruption.action(typeRaw: ended, optionsRaw: shouldResume, audioRunning: false, closed: false), .none)
        // a malformed notification
        XCTAssertEqual(AudioInterruption.action(typeRaw: nil, optionsRaw: shouldResume, audioRunning: true, closed: false), .none)
    }
}
