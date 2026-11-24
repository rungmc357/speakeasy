import XCTest
@testable import SpeakeasyCore

/// SPK-581: the voice provider went away before a call ever went live. The plugin ends that attempt
/// at once with reason `connect_failed`; the app shows a failed connect it can retry, never a
/// "Reconnecting…" wait. A drop on a live call keeps the reconnect. All data here is invented.
final class ConnectFailedTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_000)

    private func admitted(reconnects: Bool = true, id: String = "vi_1") -> VoiceState {
        var s = VoiceState()
        s.reconnectsDroppedCalls = reconnects
        s = reduce(s, .startRequested, now: t0)
        s = reduce(s, .sessionAdmitted(interactionID: id), now: t0)
        guard case .connecting = s.phase else { XCTFail("still connecting: \(s.phase)"); return s }
        return s
    }

    private func closedEvent(_ reason: String?, error: String? = "Couldn't connect. Try again.") -> [VoiceEvent] {
        var payload: [String: Any] = ["finalization": "incomplete"]
        if let error { payload["error"] = error }
        if let reason { payload["reason"] = reason }
        let data = try! JSONSerialization.data(withJSONObject: payload)
        return parseServerStreamEvent(SSEEvent(name: "closed", data: String(decoding: data, as: UTF8.self), id: "7"))
    }

    func testAConnectFailedCloseFailsTheAttemptWithItsLine() {
        for reconnects in [true, false] {   // iPhone and Mac share this reducer
            let s = closedEvent("connect_failed").reduce(admitted(reconnects: reconnects)) { reduce($0, $1, now: t0) }
            XCTAssertEqual(s.phase, .failed("Couldn't connect. Try again."))
            XCTAssertEqual(s.lastError, "Couldn't connect. Try again.")
            XCTAssertEqual(s.phase.reconnectAttempt, 0, "a call that never went live has nothing to reconnect into")
        }
    }

    func testAConnectFailedCloseWithoutWordsUsesThePlainLine() {
        let s = closedEvent("connect_failed", error: nil).reduce(admitted()) { reduce($0, $1, now: t0) }
        XCTAssertEqual(s.phase, .failed(CallReconnect.connectFailedMessage))
    }

    func testOtherClosesWhileConnectingStillEndAsBefore() {
        let s = closedEvent("error", error: nil).reduce(admitted()) { reduce($0, $1, now: t0) }
        XCTAssertEqual(s.phase, .ended(.incomplete))
    }

    func testALiveCallIsNotTurnedIntoAFailedConnect() {
        var s = reduce(admitted(), .sessionStarted, now: t0)
        XCTAssertEqual(s.phase, .live(id: "vi_1"))
        s = reduce(s, .reconnectRequired(interactionID: "vi_1"), now: t0)
        guard case .reconnecting = s.phase else { return XCTFail("a live drop still reconnects: \(s.phase)") }
    }
}
