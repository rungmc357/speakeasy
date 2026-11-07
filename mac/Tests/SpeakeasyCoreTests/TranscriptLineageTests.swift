import XCTest
@testable import SpeakeasyCore

final class TranscriptLineageTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_000)

    private func live() -> VoiceState {
        var s = reduce(VoiceState(), .startRequested, now: now)
        s = reduce(s, .sessionAdmitted(interactionID: "vi_1"), now: now)
        return reduce(s, .sessionStarted, now: now)
    }

    private func resume(_ state: VoiceState, as id: String) -> VoiceState {
        var s = reduce(state, .pauseRequested, now: now)
        s = reduce(s, .sessionClosed, now: now)
        s = reduce(s, .resumeRequested, now: now)
        s = reduce(s, .sessionAdmitted(interactionID: id), now: now)
        return reduce(s, .sessionStarted, now: now)
    }

    func testResumedCallAcceptsTheOldSessionAfterGoingLive() {
        let s = resume(live(), as: "vi_2")
        XCTAssertEqual(s.phase, .live(id: "vi_2"))
        XCTAssertNil(s.resumeFrom, "a finished connection has no pending resume request")
        XCTAssertTrue(s.acceptsTranscriptTurn(from: "vi_1"))
        XCTAssertTrue(s.acceptsTranscriptTurn(from: "vi_2"))
        XCTAssertFalse(s.acceptsTranscriptTurn(from: "vi_other"))
    }

    func testMembershipSurvivesSeveralPausesAndAPauseFailure() {
        var s = resume(resume(live(), as: "vi_2"), as: "vi_3")
        s = reduce(s, .pauseRequested, now: now)
        s = reduce(s, .pauseFailed("Couldn't pause"), now: now)
        for id in ["vi_1", "vi_2", "vi_3"] {
            XCTAssertTrue(s.acceptsTranscriptTurn(from: id), id)
        }
    }

    func testResumeAcceptsTheOldTurnWhileConnectingAndAfterAdmission() {
        var s = reduce(live(), .pauseRequested, now: now)
        s = reduce(s, .sessionClosed, now: now)
        XCTAssertTrue(s.acceptsTranscriptTurn(from: "vi_1"))
        s = reduce(s, .resumeRequested, now: now)
        XCTAssertTrue(s.acceptsTranscriptTurn(from: "vi_1"))
        s = reduce(s, .sessionAdmitted(interactionID: "vi_2"), now: now)
        XCTAssertTrue(s.acceptsTranscriptTurn(from: "vi_1"))
    }

    func testDroppedCallReconnectKeepsTheConversation() {
        var s = live()
        s.reconnectsDroppedCalls = true
        s = reduce(s, .transportLost, now: now)
        s = reduce(s, .reconnectDue(attempt: 1), now: now)
        s = reduce(s, .sessionAdmitted(interactionID: "vi_2"), now: now)
        s = reduce(s, .sessionStarted, now: now)
        XCTAssertTrue(s.acceptsTranscriptTurn(from: "vi_1"))
    }

    func testRejectedResumeDropsAllEarlierMembership() {
        var s = resume(live(), as: "vi_2")
        s = reduce(s, .pauseRequested, now: now)
        s = reduce(s, .sessionClosed, now: now)
        s = reduce(s, .resumeRequested, now: now)
        s = reduce(s, .connectFallback(dropResume: true), now: now)
        s = reduce(s, .sessionAdmitted(interactionID: "vi_3"), now: now)
        s = reduce(s, .sessionStarted, now: now)
        XCTAssertFalse(s.acceptsTranscriptTurn(from: "vi_1"))
        XCTAssertFalse(s.acceptsTranscriptTurn(from: "vi_2"))
        XCTAssertTrue(s.acceptsTranscriptTurn(from: "vi_3"))
    }

    func testRoomFallbackKeepsTranscriptMembership() {
        var s = resume(live(), as: "vi_2")
        s = reduce(s, .pauseRequested, now: now)
        s = reduce(s, .sessionClosed, now: now)
        s = reduce(s, .resumeRequested, now: now)
        s = reduce(s, .connectFallback(dropResume: false), now: now)
        s = reduce(s, .sessionAdmitted(interactionID: "vi_3"), now: now)
        s = reduce(s, .sessionStarted, now: now)
        XCTAssertTrue(s.acceptsTranscriptTurn(from: "vi_1"))
        XCTAssertTrue(s.acceptsTranscriptTurn(from: "vi_2"))
    }

    func testEndedFailedAndNewCallsRejectTheOldConversation() {
        let s = resume(live(), as: "vi_2")
        for event in [VoiceEvent.endRequested, .failed("Couldn't connect")] {
            var closed = reduce(s, event, now: now)
            closed = reduce(closed, .sessionClosed, now: now)
            XCTAssertFalse(closed.acceptsTranscriptTurn(from: "vi_1"))
            closed = reduce(closed, .startRequested, now: now)
            closed = reduce(closed, .sessionAdmitted(interactionID: "vi_new"), now: now)
            closed = reduce(closed, .sessionStarted, now: now)
            XCTAssertFalse(closed.acceptsTranscriptTurn(from: "vi_1"))
            XCTAssertFalse(closed.acceptsTranscriptTurn(from: "vi_2"))
            XCTAssertTrue(closed.acceptsTranscriptTurn(from: "vi_new"))
        }
    }
}
