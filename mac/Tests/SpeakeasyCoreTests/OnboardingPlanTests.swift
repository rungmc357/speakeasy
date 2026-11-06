import XCTest
@testable import SpeakeasyCore

final class OnboardingPlanTests: XCTestCase {
    func testNormalSetupIsOneScreenAfterPairing() {
        // Paired by the agent's link, voice already signed in by `hermes voice setup`.
        let facts = OnboardingFacts(paired: true, voiceReady: true)
        XCTAssertEqual(OnboardingPlan.screens(facts), [.ready])
        XCTAssertEqual(OnboardingPlan.first(facts), .ready)
        XCTAssertNil(OnboardingPlan.next(after: .ready, facts))
        XCTAssertNil(OnboardingPlan.previous(before: .ready, facts), "no Back to a screen that's done")
    }

    func testFreshMacShowsAtMostThreeScreens() {
        let unpaired = OnboardingFacts(paired: false, voiceReady: nil)
        XCTAssertEqual(OnboardingPlan.screens(unpaired), [.connect, .voice, .ready])
        XCTAssertEqual(OnboardingPlan.first(unpaired), .connect)
        // Pairing brings a status where the voice is ready: straight to the ready screen.
        XCTAssertEqual(OnboardingPlan.next(after: .connect, OnboardingFacts(paired: true, voiceReady: true)), .ready)
        // ...or not ready: sign-in first.
        XCTAssertEqual(OnboardingPlan.next(after: .connect, OnboardingFacts(paired: true, voiceReady: false)), .voice)
        XCTAssertEqual(OnboardingPlan.screens(OnboardingFacts(paired: false, voiceReady: true)), [.connect, .ready])
    }

    func testVoiceSignInStaysWhileStatusIsUnknownOrNotReady() {
        for ready in [nil, false] as [Bool?] {
            let facts = OnboardingFacts(paired: true, voiceReady: ready)
            XCTAssertEqual(OnboardingPlan.first(facts), .voice)
            XCTAssertEqual(OnboardingPlan.next(after: .voice, facts), .ready)
            XCTAssertEqual(OnboardingPlan.previous(before: .ready, facts), .voice)
            XCTAssertNil(OnboardingPlan.previous(before: .voice, facts), "connect is never offered once paired")
        }
    }

    func testEveryOldQuestionLeftTheScreenSequence() {
        // Names, delivery, routing, home, brief, Dock and shortcut used to be screens of their own.
        XCTAssertEqual(OnboardingStep.allCases.map(\.rawValue), ["connect", "voice", "ready"])
    }
}
