import XCTest
@testable import SpeakeasyCore

final class TaskMessagesTests: XCTestCase {
    func testCapabilityFlagDecodes() throws {
        let on = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"card_actions":true,"task_messages":true}"#.utf8))
        XCTAssertTrue(on.taskMessages)
        let old = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"card_actions":true}"#.utf8))
        XCTAssertFalse(old.taskMessages, "an older plugin hides the reply bar")
    }

    func testRequestBodyAndPath() {
        let r = TaskMessageRequest(runID: "run/1 a", text: "  Skip the admin pages  ", source: .typed, clientID: "c1",
                                   interactionID: "vi_1", choiceTaskID: "t9")
        XCTAssertEqual(r.path, "/voice/work/run%2F1%20a/message")
        XCTAssertEqual(r.body["text"] as? String, "Skip the admin pages")
        XCTAssertEqual(r.body["source"] as? String, "typed")
        XCTAssertEqual(r.body["client_id"] as? String, "c1")
        XCTAssertEqual(r.body["interaction_id"] as? String, "vi_1")
        XCTAssertEqual(r.body["choice_task_id"] as? String, "t9")
        let bare = TaskMessageRequest(runID: "r", text: "x", clientID: "c")
        XCTAssertNil(bare.body["interaction_id"]); XCTAssertNil(bare.body["choice_task_id"])
        XCTAssertEqual(bare.body["source"] as? String, "typed")
    }

    func testCleanTrimsAndCaps() {
        XCTAssertNil(TaskMessageRequest.clean("   \n "))
        XCTAssertEqual(TaskMessageRequest.clean(String(repeating: "a", count: 5000))?.count, 4000)
    }

    func testReplyParses() {
        let r = TaskMessageReply(json: ["ok": true, "run_id": "run_2", "delivered_as": "follow_up"])
        XCTAssertEqual(r, TaskMessageReply(runID: "run_2", deliveredAs: .followUp))
        XCTAssertEqual(TaskMessageReply(json: ["ok": true, "delivered_as": "weird"])?.deliveredAs, .unknown)
        XCTAssertNil(TaskMessageReply(json: ["ok": false]))
    }

    func testOutboxSendsOneAtATimeInOrder() {
        var box = TaskOutbox()
        XCTAssertNil(box.enqueue(jobID: "d", runID: "r", taskName: "Speed up the dashboard", text: "  "))
        let a = box.enqueue(jobID: "d", runID: "r", taskName: "Speed up the dashboard", text: "first")!
        let b = box.enqueue(jobID: "d", runID: "r", taskName: "Speed up the dashboard", text: "second")!
        XCTAssertEqual(box.startNext()?.id, a.id)
        XCTAssertNil(box.startNext(), "the second waits while the first is on the wire")
        XCTAssertEqual(box.message(b.id)?.state, .queued)
        box.markFailed(a.id, "Couldn't reach your Hermes.")
        XCTAssertEqual(box.startNext()?.id, b.id, "a failure doesn't hold up the rest")
        box.markSent(b.id, as: .steer)
        box.retry(a.id)
        let again = box.startNext()
        XCTAssertEqual(again?.id, a.id, "Retry keeps the same client id")
        XCTAssertEqual(again?.request(interactionID: nil).clientID, a.id)
    }

    func testRetryOnlyForFailed() {
        var box = TaskOutbox()
        let a = box.enqueue(jobID: "d", runID: "r", taskName: "T", text: "x")!
        _ = box.startNext(); box.markSent(a.id, as: .answer)
        box.retry(a.id)
        XCTAssertEqual(box.message(a.id)?.state, .sent(.answer))
    }

    func testCopy() {
        XCTAssertEqual(TaskMessageCopy.shortName("Speed up the dashboard"), "Speed up the dashboard")
        XCTAssertEqual(TaskMessageCopy.shortName("Compare our pricing to our top three competitors"), "Compare our pricing to our…")
        XCTAssertEqual(TaskMessageCopy.failure(status: 409, serverMessage: "That task was cancelled"), "That task was cancelled.")
        XCTAssertEqual(TaskMessageCopy.failure(status: 502, serverMessage: "Server HTTP 502"), "Your Hermes couldn't take that message right now.")
        XCTAssertEqual(TaskMessageCopy.failure(status: 0, serverMessage: nil), "Couldn't reach your Hermes. Check your connection and try again.")
        var m = OutgoingMessage(jobID: "d", runID: "r", taskName: "Speed up the dashboard", text: "x")
        m.state = .sent(.steer)
        XCTAssertEqual(TaskMessageCopy.sent(m), "Sent to Speed up the dashboard")
        m.state = .sent(.followUp)
        XCTAssertEqual(TaskMessageCopy.sent(m), "Started a follow-up to Speed up the dashboard")
    }

    func testTypedTurnIsTheUsersAndNeverSpoken() {
        var s = VoiceState(); s.connection = .live
        s = reduce(s, .typedToTask(text: " Skip the admin pages ", taskID: "d", taskName: "Speed up the dashboard"), now: Date())
        let turn = try! XCTUnwrap(s.transcript.last)
        XCTAssertEqual(turn.speaker, .you)
        XCTAssertEqual(turn.kind, .typed)
        XCTAssertEqual(turn.cleanedText, "Skip the admin pages")
        // Speech after a typed turn starts its own turn instead of being glued onto the typed text.
        s = reduce(s, .inputDelta("How's it going"), now: Date())
        XCTAssertEqual(s.transcript.last?.kind, .speech)
        XCTAssertEqual(s.transcript.filter { $0.kind == .typed }.first?.text, "Skip the admin pages")
    }
}
