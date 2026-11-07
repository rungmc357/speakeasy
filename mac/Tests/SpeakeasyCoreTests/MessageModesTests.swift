import XCTest
@testable import SpeakeasyCore

/// Steer or queue (docs/API.md "Steer or queue"). Invented data.
final class MessageModesTests: XCTestCase {
    func testTheChoiceIsOnlySentWhileARunIsActive() {
        var choice = MessageModeChoice()
        XCTAssertNil(choice.mode(offered: false))
        XCTAssertNil(choice.mode(offered: false, alternate: true))
        XCTAssertEqual(choice.mode(offered: true), .steer)              // the default: steer
        XCTAssertEqual(choice.mode(offered: true, alternate: true), .queue)   // ⌘↩
        choice.selected = .queue
        XCTAssertEqual(choice.mode(offered: true), .queue)
        XCTAssertEqual(choice.mode(offered: true, alternate: true), .steer)
        XCTAssertEqual(choice.mode(offered: true, hasAttachments: true), .steer)  // files can't wait
        XCTAssertEqual(choice.sendLabel(offered: true), "Queue for after")
        XCTAssertEqual(choice.sendLabel(offered: false), "Send")
    }

    func testThePopUpWords() {
        XCTAssertEqual(MessageMode.steer.label, "Steer now")
        XCTAssertEqual(MessageMode.queue.label, "Queue for after")
        XCTAssertEqual(MessageMode.steer.hint, "Adds this to the running task.")
        XCTAssertEqual(MessageMode.queue.hint, "Sends it when the task is done.")
    }

    func testRequestsCarryModeOnlyWhenSet() {
        let plain = HistoryMessageRequest(sessionID: "s1", text: "Add the oat milk", clientID: "c1")!
        XCTAssertNil(plain.body["mode"])
        var queued = plain
        queued.mode = .queue
        XCTAssertEqual(queued.body["mode"] as? String, "queue")
        let task = TaskMessageRequest(runID: "run_1", text: "Shorter", clientID: "c2", mode: .steer)
        XCTAssertEqual(task.body["mode"] as? String, "steer")
        XCTAssertNil(TaskMessageRequest(runID: "run_1", text: "x").body["mode"])
    }

    func testRepliesReadQueuedAndPosition() {
        let r = HistoryMessageReply(json: ["ok": true, "via": "task", "run_id": "run_1", "delivered_as": "queued",
                                           "queue_position": 2])
        XCTAssertEqual(r?.deliveredAs, .queued)
        XCTAssertEqual(r?.queuePosition, 2)
        XCTAssertEqual(HistoryMessageReply(json: ["ok": true, "via": "linked"])?.deliveredAs, .unknown)
        XCTAssertEqual(TaskMessageReply(json: ["ok": true, "delivered_as": "queued", "queue_position": 1])?.queuePosition, 1)
    }

    func testThePageListsHeldMessagesAndSaysItCanHoldThem() {
        let json: [String: Any] = ["session": ["id": "s1", "state": "running", "send_via": "task",
                                               "queued": [["client_id": "q1", "text": "Then the receipts", "at": 1_800_000_000,
                                                           "position": 1],
                                                          ["client_id": "", "text": "dropped"]]]]
        let c = HistoryConversation(json: json)!
        XCTAssertTrue(c.messageModes && c.offersMessageModes)
        XCTAssertEqual(c.queued.map(\.clientID), ["q1"])
        XCTAssertEqual(c.queued.first?.at, Date(timeIntervalSince1970: 1_800_000_000))
        // An older plugin (no `queued` key): no choice, even while running.
        let old = HistoryConversation(json: ["session": ["id": "s1", "state": "running"]])!
        XCTAssertFalse(old.messageModes || old.offersMessageModes)
        // Idle: plain send.
        let idle = HistoryConversation(json: ["session": ["id": "s1", "state": "idle", "queued": []]])!
        XCTAssertTrue(idle.messageModes)
        XCTAssertFalse(idle.offersMessageModes)
        XCTAssertEqual(QueuedMessage.path(session: "s1", clientID: "q1"), "/voice/history/s1/queued/q1")
        XCTAssertEqual(QueuedMessage.path(run: "run_1", clientID: "q1"), "/voice/work/run_1/queued/q1")
    }

    func testSteeredMarksMatchByClientIDOnly() {
        var marks = SteeredMarks()
        let at = Date(timeIntervalSince1970: 1_800_000_000)
        marks.record(clientID: "c1")
        XCTAssertTrue(marks.contains(clientID: "c1"))
        XCTAssertFalse(marks.contains(clientID: "c2"))
        XCTAssertFalse(marks.contains(clientID: nil))
        XCTAssertEqual(marks.receipt(clientID: "c1", at: at, in: [])?.state, .sent)
        XCTAssertNil(marks.receipt(clientID: nil, at: at, in: []), "no id, no receipt (never by words)")
    }

    func testTheOutboxDropsAMessageTheServerHolds() {
        var box = HistoryOutbox()
        box.add(HistoryMessageRequest(sessionID: "s1", text: "Later please", clientID: "c1")!, at: Date())
        XCTAssertTrue(box.pending(in: "s1"))
        box.remove("c1")
        XCTAssertFalse(box.pending(in: "s1"))
    }

    func testCapabilityFlag() throws {
        let caps = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"message_modes": true}"#.utf8))
        XCTAssertTrue(caps.messageModes)
        XCTAssertFalse(try JSONDecoder().decode(ServerCapabilities.self, from: Data("{}".utf8)).messageModes)
    }

    func testSteeredReceiptsReadEveryState() {
        let json: [String: Any] = ["session": ["id": "s1", "state": "running", "queued": [], "steered": [
            ["client_id": "a", "text": "Put the tomatoes first", "at": 1_800_000_000, "state": "seen",
             "label": "Seen by Todd", "seen_at": 1_800_000_030],
            ["client_id": "b", "text": "Skip the herbs", "at": 1_800_000_100, "state": "waiting",
             "label": "Waiting for the current step to finish"],
            ["client_id": "c", "text": "Add frost dates", "at": 1_800_000_200, "state": "follow_up", "run_id": "run_9"],
            ["client_id": "d", "text": "Red packets", "state": "unused"],
            ["client_id": "e", "text": "x", "state": "weird"]]]]
        let c = HistoryConversation(json: json)!
        XCTAssertEqual(c.steered.map(\.state), [.seen, .waiting, .followUp, .unused])
        let seen = c.steered[0]
        XCTAssertEqual(seen.tag(timeStyle: { _ in "10:42" }), "Seen by Todd · 10:42")
        XCTAssertEqual(c.steered[2].runID, "run_9")
        XCTAssertEqual(c.steered[2].label, "It finished first · sent as a follow-up")
        XCTAssertEqual(SteeredMessage(clientID: "z", text: "y", state: .sent).tag(), "Sent to the running task")
        // By client id only (SPK-80).
        XCTAssertEqual(c.steered.receipt(clientID: "a")?.state, .seen)
        XCTAssertNil(c.steered.receipt(clientID: "zz"))
    }
}
