import XCTest
@testable import SpeakeasyCore

/// The call's connection as one phase with its own data (SPK-81): connect retries, pause, the
/// dropped-call reconnects and snapshots from sessions the call no longer uses.
final class CallPhaseTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_000)

    private func live(reconnects: Bool = true, id: String = "vi_1") -> VoiceState {
        var s = VoiceState()
        s.reconnectsDroppedCalls = reconnects
        s = reduce(s, .startRequested, now: t0)
        s = reduce(s, .sessionAdmitted(interactionID: id), now: t0)
        s = reduce(s, .sessionStarted, now: t0)
        XCTAssertEqual(s.phase, .live(id: id))
        return s
    }

    // MARK: Reconnecting a dropped call

    func testDroppedCallGetsThreeReconnectAttemptsOnTheResumePath() {
        var s = live()
        var now = t0
        s = reduce(s, .transportLost, now: now)
        for attempt in 1...CallReconnect.maxAttempts {
            guard case .reconnecting(let n, let deadline, let from) = s.phase else {
                return XCTFail("attempt \(attempt): not reconnecting but \(s.phase)")
            }
            XCTAssertEqual(n, attempt)
            XCTAssertEqual(from, "vi_1", "every attempt resumes the same conversation")
            XCTAssertEqual(deadline.timeIntervalSince(now), CallReconnect.delay(attempt: attempt), accuracy: 0.001)
            XCTAssertEqual(s.connection, .connecting, "the call is still being brought back")
            now = deadline
            s = reduce(s, .reconnectDue(attempt: attempt), now: now)
            guard case .connecting(let c) = s.phase else { return XCTFail("attempt \(attempt) didn't connect") }
            XCTAssertEqual(c.reconnect, attempt)
            XCTAssertEqual(c.resumeFrom, "vi_1")
            XCTAssertNil(s.interactionID, "nothing admitted yet for this attempt")
            // The resume attempt itself fails (no network yet).
            s = reduce(s, .failed("The network connection was lost."), now: now)
        }
        XCTAssertEqual(s.phase, .failed("The network connection was lost."), "gives up after the third attempt")
        XCTAssertEqual(s.lastError, "The network connection was lost.")
    }

    func testReconnectThatComesBackGoesLiveOnTheNewSession() {
        var s = live()
        s = reduce(s, .transportLost, now: t0)
        s = reduce(s, .reconnectDue(attempt: 1), now: t0.addingTimeInterval(1))
        s = reduce(s, .sessionAdmitted(interactionID: "vi_2"), now: t0.addingTimeInterval(2))
        XCTAssertEqual(s.phase.reconnectAttempt, 1)
        s = reduce(s, .sessionStarted, now: t0.addingTimeInterval(3))
        XCTAssertEqual(s.phase, .live(id: "vi_2"))
        XCTAssertEqual(s.phase.reconnectAttempt, 0)
    }

    func testTransportLostOnAReconnectAttemptTriesTheNextOne() {
        var s = live()
        s = reduce(s, .transportLost, now: t0)
        s = reduce(s, .reconnectDue(attempt: 1), now: t0)
        s = reduce(s, .sessionAdmitted(interactionID: "vi_2"), now: t0)
        s = reduce(s, .transportLost, now: t0)
        guard case .reconnecting(2, _, let from) = s.phase else { return XCTFail("\(s.phase)") }
        XCTAssertEqual(from, "vi_2", "the next attempt resumes the session the last one admitted")
    }

    func testAStaleReconnectTimerDoesNothing() {
        var s = live()
        s = reduce(s, .transportLost, now: t0)
        let before = s
        s = reduce(s, .reconnectDue(attempt: 2), now: t0.addingTimeInterval(5))
        XCTAssertEqual(s.phase, before.phase)
    }

    func testStopOnTheBannerAndEndBothEndTheReconnect() {
        var s = reduce(live(), .transportLost, now: t0)
        XCTAssertEqual(reduce(s, .reconnectCancelled, now: t0).phase, .ended(.incomplete))
        XCTAssertEqual(reduce(s, .endRequested, now: t0).phase, .ended(.incomplete))
        s = reduce(s, .reconnectDue(attempt: 1), now: t0)
        XCTAssertEqual(reduce(s, .reconnectCancelled, now: t0).phase, .ended(.incomplete))
    }

    func testMacEndsADroppedCallAsBefore() {
        let s = reduce(live(reconnects: false), .transportLost, now: t0)
        XCTAssertEqual(s.phase, .ended(.incomplete))
        XCTAssertEqual(s.lastError, VoiceState.callDropped)  // SPK-153: plain words, tasks keep running
    }

    func testMicRefusalNeverReconnects() {
        let s = reduce(live(), .failed("Microphone access is off — enable it in Settings"), now: t0)
        XCTAssertEqual(s.phase, .failed("Microphone access is off — enable it in Settings"))
    }

    func testUserPauseIsNotADrop() {
        var s = reduce(live(), .pauseRequested, now: t0)
        XCTAssertEqual(s.phase, .pausing(id: "vi_1", reason: .user))
        s = reduce(s, .sessionClosed, now: t0)
        XCTAssertEqual(s.phase, .paused(id: "vi_1", reason: .user))
        XCTAssertEqual(s.phase.reconnectAttempt, 0)
    }

    // MARK: The connect watchdog

    func testWatchdogRetryDropsTheAdmittedSessionAndKeepsItsOwnBudget() {
        var s = VoiceState()
        s = reduce(s, .startRequested, now: t0)
        s = reduce(s, .sessionAdmitted(interactionID: "vi_stalled"), now: t0)
        s = reduce(s, .connectStalled(failure: "Couldn't connect"), now: t0.addingTimeInterval(12))
        guard case .connecting(let c) = s.phase else { return XCTFail("\(s.phase)") }
        XCTAssertEqual(c.attempt, 2)
        XCTAssertNil(c.admittedID, "the stalled attempt's session isn't this attempt's")
        XCTAssertNil(s.interactionID)
        XCTAssertEqual(s.sessionsToClose, ["vi_stalled"], "the retry closes the session the server already admitted")
        // End during the retry closes nothing: the stalled session was closed when it was dropped.
        XCTAssertEqual(reduce(s, .endRequested, now: t0).phase, .ended(.complete))
        // A fallback (room text or resume refused) doesn't use up the watchdog's attempts.
        s = reduce(s, .connectFallback(dropResume: true), now: t0.addingTimeInterval(13))
        guard case .connecting(let after) = s.phase else { return XCTFail("\(s.phase)") }
        XCTAssertEqual(after.attempt, 2)
        s = reduce(s, .connectStalled(failure: "Couldn't connect"), now: t0.addingTimeInterval(25))
        XCTAssertEqual(s.phase, .failed("Couldn't connect"))
    }

    func testStallBeforeAdmissionHasNothingToClose() {
        var s = reduce(VoiceState(), .startRequested, now: t0)
        s = reduce(s, .connectStalled(failure: "Couldn't connect"), now: t0.addingTimeInterval(12))
        XCTAssertEqual(s.sessionsToClose, [])
        s = reduce(s, .sessionAdmitted(interactionID: "vi_2"), now: t0.addingTimeInterval(13))
        s = reduce(s, .connectStalled(failure: "Couldn't connect"), now: t0.addingTimeInterval(25))
        XCTAssertEqual(s.phase, .failed("Couldn't connect"))
        XCTAssertEqual(s.sessionsToClose, ["vi_2"], "giving up closes the last attempt's session too")
    }

    func testFallbackKeepsTheWatchdogBudgetForTheNextStall() {
        var s = reduce(VoiceState(), .startRequested, now: t0)
        s = reduce(s, .connectFallback(dropResume: false), now: t0)
        s = reduce(s, .connectStalled(failure: "Couldn't connect"), now: t0.addingTimeInterval(12))
        guard case .connecting(let c) = s.phase else { return XCTFail("a fallback must not use up the retry: \(s.phase)") }
        XCTAssertEqual(c.attempt, 2)
    }

    func testResumeFallbackStartsAFreshCall() {
        var s = reduce(live(reconnects: false), .pauseRequested, now: t0)
        s = reduce(s, .sessionClosed, now: t0)
        s = reduce(s, .resumeRequested, now: t0)
        XCTAssertEqual(s.resumeFrom, "vi_1")
        s = reduce(s, .connectFallback(dropResume: true), now: t0)
        XCTAssertNil(s.resumeFrom)
        XCTAssertEqual(s.connection, .connecting)
    }

    func testARefusedReconnectAfterADropNeverSaysThePausedCallWasReplaced() {
        XCTAssertNil(CallReconnect.resumeRefusedNotice(reconnect: 1), "nobody paused a dropped call")
        XCTAssertNil(CallReconnect.resumeRefusedNotice(reconnect: 3))
        XCTAssertEqual(CallReconnect.resumeRefusedNotice(reconnect: 0), "Couldn't resume the paused call; started a new one")
    }

    // MARK: Snapshots from another session

    func testForeignFinalSnapshotDoesNotEndTheNewCall() {
        var s = live(id: "vi_new")
        var stale = InteractionSnapshot(interactionID: "vi_stalled")
        stale.finalization = "confirmed"
        s = reduce(s, .interaction(stale), now: t0)
        XCTAssertEqual(s.phase, .live(id: "vi_new"))
        var own = InteractionSnapshot(interactionID: "vi_new")
        own.finalization = "confirmed"
        XCTAssertEqual(reduce(s, .interaction(own), now: t0).phase, .ended(.complete))
    }

    func testForeignSnapshotLeavesApprovalAndErrorAlone() {
        var s = live(id: "vi_new")
        var stale = InteractionSnapshot(interactionID: "vi_old")
        stale.error = "voice session failed"
        stale.approval = ApprovalInfo(runID: "r1", requestID: "q1", description: "Run the cleanup")
        s = reduce(s, .interaction(stale), now: t0)
        XCTAssertNil(s.approval)
        XCTAssertNil(s.lastError)
    }

    // MARK: Pause

    func testPauseCarriesItsReason() {
        var s = reduce(live(reconnects: false), .quietPauseRequested(minutes: 15), now: t0)
        XCTAssertEqual(s.phase, .pausing(id: "vi_1", reason: .quiet(minutes: 15)))
        XCTAssertTrue(s.pausing)
        s = reduce(s, .serverClosed(.incomplete, error: "closed"), now: t0)
        XCTAssertEqual(s.phase, .paused(id: "vi_1", reason: .quiet(minutes: 15)))
        XCTAssertEqual(s.quietPausedMinutes, 15)
        XCTAssertNil(s.lastError, "a pause's close is no error")
        s = reduce(s, .resumeRequested, now: t0)
        XCTAssertNil(s.quietPausedMinutes)
    }

    /// Pause closes the transport on purpose. Both close notifications may arrive, in either order.
    func testPauseCloseNotificationsStayQuietInEitherOrder() {
        let lost = VoiceEvent.transportLost
        let closed = VoiceEvent.serverClosed(.incomplete, error: "Voice connection lost.")
        let sequences: [[VoiceEvent]] = [[lost, closed], [closed, lost], [.sessionClosed, closed], [.endTimedOut, closed]]
        for reconnects in [false, true] {
            for quiet in [false, true] {
                for events in sequences {
                    var s = reduce(live(reconnects: reconnects), quiet ? .quietPauseRequested(minutes: 15) : .pauseRequested, now: t0)
                    let reason: PauseReason = quiet ? .quiet(minutes: 15) : .user
                    for event in events {
                        s = reduce(s, event, now: t0.addingTimeInterval(1))
                        XCTAssertEqual(s.phase, .paused(id: "vi_1", reason: reason))
                        XCTAssertNil(s.lastError, "an intentional close must not gain a dropped-call banner")
                        XCTAssertEqual(s.phase.reconnectAttempt, 0)
                    }
                    s = reduce(s, .resumeRequested, now: t0.addingTimeInterval(2))
                    XCTAssertEqual(s.connection, .connecting)
                    XCTAssertEqual(s.resumeFrom, "vi_1")
                }
            }
        }
    }

    func testFailureWhilePausingLeavesNoPausingBehind() {
        var s = reduce(live(reconnects: false), .pauseRequested, now: t0)
        s = reduce(s, .failed("Couldn't reach your Hermes"), now: t0)
        XCTAssertEqual(s.phase, .failed("Couldn't reach your Hermes"))
        XCTAssertFalse(s.pausing)
    }

    func testEndWhilePausingEndsTheCall() {
        var s = reduce(live(reconnects: false), .pauseRequested, now: t0)
        s = reduce(s, .endRequested, now: t0)
        guard case .ending(let id, let deadline) = s.phase else { return XCTFail("\(s.phase)") }
        XCTAssertEqual(id, "vi_1")
        XCTAssertEqual(deadline, t0.addingTimeInterval(VoiceState.endConfirmWithin))
        XCTAssertEqual(reduce(s, .sessionClosed, now: t0).phase, .ended(.complete))
    }

    func testPauseTimeoutPausesInsteadOfEnding() {
        var s = reduce(live(reconnects: false), .pauseRequested, now: t0)
        s = reduce(s, .endTimedOut, now: t0)
        XCTAssertEqual(s.phase, .paused(id: "vi_1", reason: .user))
    }
}
