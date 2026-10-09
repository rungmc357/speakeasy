import XCTest
@testable import SpeakeasyCore

/// The call listening mode is turned off into: one take-off per call (every leg shares it), and
/// what listening mode is told when the call goes live, ends or fails.
final class RoomCallTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_760_000_000)
    private func call() -> RoomCall { RoomCall(room: "[10:42] Sam: Friday the 14th.", takeoffAt: t0) }

    func testGoingLiveGivesTheTranscriptUpOnce() {
        var c = call()
        c.noteLive(at: t0.addingTimeInterval(1))
        XCTAssertEqual(c.outcome(for: .wentLive), .wentLive)
        XCTAssertNil(c.outcome(for: .wentLive), "reported once")
        XCTAssertNil(c.outcome(for: .ended), "a call that went live then ended needs nothing more")
        XCTAssertNil(c.outcome(for: .failed))
    }

    func testALostConnectionBeforeLiveGivesTheTranscriptBack() {
        // .transportLost ends a connecting call (.ended), not .failed: it must still count as a failure.
        var c = call()
        XCTAssertEqual(c.outcome(for: .ended), .failedBeforeLive)
        var failed = call()
        XCTAssertEqual(failed.outcome(for: .failed), .failedBeforeLive)
    }

    func testOnlyAnEndTheUserAskedForBeforeLiveGivesItUp() {
        var c = call()
        c.noteEndRequested()
        XCTAssertEqual(c.outcome(for: .ended), .endedBeforeLive)
        var live = call()
        live.noteLive(at: t0.addingTimeInterval(1))
        _ = live.outcome(for: .wentLive)
        live.noteEndRequested()
        XCTAssertFalse(live.endRequested, "ending a live call is an ordinary end")
    }

    func testNotUsedIsReportedOnceAndNotAfterGoingLive() {
        var c = call()
        XCTAssertEqual(c.notUsed(), .notUsed)
        XCTAssertNil(c.notUsed())
        var live = call()
        live.noteLive(at: t0)
        _ = live.outcome(for: .wentLive)
        XCTAssertNil(live.notUsed())
    }

    func testNothingSaidWaitsForLiveThenTwoSecondsThenNudgesOnce() {
        var c = call()
        XCTAssertEqual(c.nextStep(now: t0.addingTimeInterval(0.5)), .waitForLive)
        c.noteLive(at: t0.addingTimeInterval(0.8))
        XCTAssertEqual(c.nextStep(now: t0.addingTimeInterval(1)), .waitUntil(t0.addingTimeInterval(2)))
        XCTAssertEqual(c.nextStep(now: t0.addingTimeInterval(2)), .nudgeNow)
        c.markRequestHandled()
        XCTAssertEqual(c.nextStep(now: t0.addingTimeInterval(30)), .skip, "a resumed leg never nudges again")
    }

    func testOneLoudSampleIsACoughThreeInARowAreTheUserTalking() {
        var c = call()
        c.noteLive(at: t0)
        c.noteMicLevel(0.4)
        c.noteMicLevel(0.01)
        c.noteMicLevel(0.4)
        XCTAssertFalse(c.speechSinceLive)
        XCTAssertEqual(c.nextStep(now: t0.addingTimeInterval(3)), .nudgeNow)
        for _ in 0..<RoomCall.speechSamples { c.noteMicLevel(0.3) }
        XCTAssertTrue(c.speechSinceLive)
        XCTAssertEqual(c.nextStep(now: t0.addingTimeInterval(3)), .skip)
    }

    func testSoundBeforeTheCallIsLiveDoesntCount() {
        var c = call()
        for _ in 0..<5 { c.noteMicLevel(0.5) }
        c.noteHeardUser()
        XCTAssertFalse(c.speechSinceLive, "the connecting listener owns that speech")
        c.noteLive(at: t0)
        c.noteHeardUser()
        XCTAssertTrue(c.speechSinceLive)
    }

    func testTheRoomRidesUntilTheServerHasItThenOnlyOnNewAdmissions() {
        var c = call()
        XCTAssertTrue(c.sends(resuming: false))
        XCTAssertTrue(c.sends(resuming: true), "listening mode turned off mid-call: the resume carries it")
        c.markDelivered()
        XCTAssertFalse(c.sends(resuming: true), "a later pause/resume relies on the server's copy")
        XCTAssertTrue(c.sends(resuming: false), "a resume that fell back to a new call sends it again")
    }

    func testCarriedWordsSurviveUntilHandedOver() {
        var c = call()
        c.carriedWords = "what did Sam say"
        XCTAssertEqual(c.carriedWords, "what did Sam say")
        c.carriedWords = nil
        XCTAssertNil(c.carriedWords)
    }
}
