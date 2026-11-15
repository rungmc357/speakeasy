import XCTest
@testable import SpeakeasyCore

/// SPK-458: a deliberate hang-up and a reconnect notice can reach the app in either order. The
/// hang-up always wins: the call ends, never comes back, and the server holding it for a
/// reconnect is told it was ended. All data here is invented.
final class HangUpPrecedenceTests: XCTestCase {
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

    private func heldSnapshot(_ id: String = "vi_1") throws -> InteractionSnapshot {
        try XCTUnwrap(InteractionSnapshot(json: ["interaction_id": id, "status": "reconnecting", "finalization": "open"]))
    }

    private func apply(_ events: [VoiceEvent], to state: VoiceState, at now: Date) -> VoiceState {
        events.reduce(state) { reduce($0, $1, now: now) }
    }

    // MARK: Hang-up first, reconnect notice second

    func testHangUpThenTheReconnectEventEndsAndTellsTheServer() {
        for reconnects in [true, false] {   // iPhone and Mac share this reducer
            var s = reduce(live(reconnects: reconnects), .endRequested, now: t0)
            s = reduce(s, .reconnectRequired(interactionID: "vi_1"), now: t0)
            guard case .ending = s.phase else { return XCTFail("a hang-up never reconnects: \(s.phase)") }
            XCTAssertEqual(s.heldCallsToEnd, ["vi_1"], "the server holding it is told it was hung up")
            // A repeat (the event, then a polled snapshot) asks once.
            s = reduce(s, .reconnectRequired(interactionID: "vi_1"), now: t0)
            XCTAssertEqual(s.heldCallsToEnd, ["vi_1"])
            s = reduce(s, .serverClosed(.complete, error: nil, reason: "user"), now: t0.addingTimeInterval(1))
            XCTAssertEqual(s.phase, .ended(.complete))
            XCTAssertNil(s.lastError, "a hang-up is not a dropped call")
        }
    }

    func testHangUpThenAPolledHeldSnapshotEndsAndTellsTheServer() throws {
        var s = reduce(live(), .endRequested, now: t0)
        s = apply(interactionEvents(try heldSnapshot()), to: s, at: t0)
        guard case .ending = s.phase else { return XCTFail("\(s.phase)") }
        XCTAssertEqual(s.heldCallsToEnd, ["vi_1"])
        XCTAssertEqual(s.phase.reconnectAttempt, 0, "no recovery starts after a hang-up")
    }

    func testAReconnectNoticeForAnotherCallDoesNotEndIt() {
        var s = reduce(live(), .endRequested, now: t0)
        s = reduce(s, .reconnectRequired(interactionID: "vi_other"), now: t0)
        XCTAssertEqual(s.heldCallsToEnd, [])
    }

    func testAPlainHangUpTellsTheServerNothingExtra() {
        let s = reduce(live(), .endRequested, now: t0)
        XCTAssertEqual(s.heldCallsToEnd, [], "an ordinary End goes through the voice session's own close")
    }

    // MARK: Reconnect notice first, hang-up second

    func testReconnectNoticeThenHangUpEndsAndTellsTheServer() {
        var s = reduce(live(), .reconnectRequired(interactionID: "vi_1"), now: t0)
        XCTAssertEqual(s.phase.reconnectAttempt, 1)
        s = reduce(s, .endRequested, now: t0)
        XCTAssertEqual(s.connection, .ended(.incomplete))
        XCTAssertEqual(s.heldCallsToEnd, ["vi_1"], "otherwise the server holds it and logs a dropped call")
        // The reconnect timer that was already scheduled does nothing now.
        let after = reduce(s, .reconnectDue(attempt: 1), now: t0.addingTimeInterval(1))
        XCTAssertEqual(after.phase, s.phase)
    }

    func testHangUpDuringAReconnectAttemptNotYetAdmittedEndsAndTellsTheServer() {
        var s = reduce(live(), .reconnectRequired(interactionID: "vi_1"), now: t0)
        s = reduce(s, .reconnectDue(attempt: 1), now: t0.addingTimeInterval(0.2))
        guard case .connecting(let c) = s.phase, c.reconnect == 1 else { return XCTFail("\(s.phase)") }
        s = reduce(s, .endRequested, now: t0.addingTimeInterval(0.3))
        XCTAssertEqual(s.phase, .ended(.complete))
        XCTAssertEqual(s.heldCallsToEnd, ["vi_1"])
    }

    func testAnOrdinaryConnectCancelledTellsTheServerNothing() {
        var s = VoiceState()
        s = reduce(s, .startRequested, now: t0)
        s = reduce(s, .endRequested, now: t0)
        XCTAssertEqual(s.heldCallsToEnd, [])
    }

    // MARK: The server's hang-up notice against a pending reconnect, both orders

    func testHangUpNoticeAfterTheReconnectNoticeEndsTheCall() {
        let closedByUser = VoiceEvent.serverClosed(.complete, error: nil, reason: "user")
        // Waiting for the reconnect.
        var s = reduce(live(), .reconnectRequired(interactionID: "vi_1"), now: t0)
        s = reduce(s, closedByUser, now: t0)
        XCTAssertEqual(s.phase, .ended(.complete), "a hang-up wins over a pending reconnect")
        XCTAssertEqual(reduce(s, .reconnectDue(attempt: 1), now: t0.addingTimeInterval(1)).phase, .ended(.complete))
        // Mid reconnect attempt (admitted or not).
        for admitted in [false, true] {
            var r = reduce(live(), .reconnectRequired(interactionID: "vi_1"), now: t0)
            r = reduce(r, .reconnectDue(attempt: 1), now: t0)
            if admitted { r = reduce(r, .sessionAdmitted(interactionID: "vi_2"), now: t0) }
            r = reduce(r, closedByUser, now: t0)
            XCTAssertEqual(r.phase, .ended(.complete), "admitted: \(admitted)")
            XCTAssertEqual(r.heldCallsToEnd, admitted ? ["vi_2"] : [], "an admitted attempt's session is ended too")
        }
    }

    func testHangUpNoticeBeforeTheReconnectNoticeEndsTheCall() throws {
        var s = reduce(live(), .serverClosed(.complete, error: nil, reason: "user"), now: t0)
        XCTAssertEqual(s.phase, .ended(.complete))
        s = reduce(s, .reconnectRequired(interactionID: "vi_1"), now: t0)
        s = apply(interactionEvents(try heldSnapshot()), to: s, at: t0)
        XCTAssertEqual(s.phase, .ended(.complete), "a late reconnect notice never resumes an ended call")
        XCTAssertEqual(s.heldCallsToEnd, [])
    }

    func testAProviderCloseDuringAReconnectStillLetsItReconnect() {
        // Only a hang-up wins: the old session's own close (or a close with no reason from an
        // older plugin) is part of the same drop.
        let s = reduce(live(), .reconnectRequired(interactionID: "vi_1"), now: t0)
        for reason in [nil, "provider"] as [String?] {
            XCTAssertEqual(reduce(s, .serverClosed(.incomplete, error: nil, reason: reason), now: t0).phase, s.phase)
        }
    }

    func testTheClosedEventCarriesItsReason() {
        XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "closed",
                                                       data: #"{"finalization":"complete","reason":"user"}"#)),
                       [.serverClosed(.complete, error: nil, reason: "user")])
        XCTAssertEqual(parseServerStreamEvent(SSEEvent(name: "closed", data: #"{"finalization":"incomplete"}"#)),
                       [.serverClosed(.incomplete, error: nil)])
    }
}
