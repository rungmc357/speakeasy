import XCTest
@testable import SpeakeasyCore

/// A dropped call the plugin holds for recovery must reconnect whether the app hears about it from
/// the `reconnect_required` event or from a polled / streamed interaction snapshot.
final class SnapshotRecoveryTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_000)

    private func liveCall(_ id: String = "vi_probe") -> VoiceState {
        var state = VoiceState()
        state.reconnectsDroppedCalls = true
        for event in [VoiceEvent.startRequested, .sessionAdmitted(interactionID: id), .sessionStarted] {
            state = reduce(state, event, now: now)
        }
        return state
    }

    private func apply(_ json: [String: Any], to start: VoiceState) throws -> VoiceState {
        let snapshot = try XCTUnwrap(InteractionSnapshot(json: json))
        var state = start
        for event in interactionEvents(snapshot) { state = reduce(state, event, now: now) }
        return state
    }

    private let held: [String: Any] = ["interaction_id": "vi_probe", "status": "reconnecting", "finalization": "open"]

    func testPolledSnapshotOfAHeldDroppedCallStartsRecovery() throws {
        XCTAssertEqual(try apply(held, to: liveCall()).phase.reconnectAttempt, 1)
    }

    func testStreamedInteractionEventStartsRecoveryToo() throws {
        let data = #"{"interaction_id":"vi_probe","status":"reconnecting","finalization":"open","reconnecting":true}"#
        var state = liveCall()
        for event in parseServerStreamEvent(SSEEvent(name: "interaction", data: data)) { state = reduce(state, event, now: now) }
        XCTAssertEqual(state.phase.reconnectAttempt, 1)
    }

    func testRepeatedSnapshotWhileReconnectingDoesNotRestartRecovery() throws {
        let once = try apply(held, to: liveCall())
        XCTAssertEqual(try apply(held, to: once).phase, once.phase)
    }

    func testEventThenSnapshotStartsOneRecovery() throws {
        let afterEvent = reduce(liveCall(), .reconnectRequired(interactionID: "vi_probe"), now: now)
        XCTAssertEqual(try apply(held, to: afterEvent).phase, afterEvent.phase)
    }

    func testAnotherCallsHeldSnapshotLeavesThisCallAlone() throws {
        var other = held
        other["interaction_id"] = "vi_other"
        XCTAssertEqual(try apply(other, to: liveCall()).phase.reconnectAttempt, 0)
    }

    func testFinishedOrOrdinarySnapshotsDoNotReconnect() throws {
        var finished = held
        finished["finalization"] = "incomplete"
        XCTAssertEqual(try apply(finished, to: liveCall()).phase.reconnectAttempt, 0)
        var active = held
        active["status"] = "active"
        XCTAssertEqual(try apply(active, to: liveCall()).phase.reconnectAttempt, 0)
    }
}
