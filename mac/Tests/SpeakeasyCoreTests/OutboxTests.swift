import XCTest
@testable import SpeakeasyCore

/// SPK-80: every outbox matches by client id only, settles every send, and decodes the new replies.
final class OutboxTests: XCTestCase {
    private func conversation(_ entries: [[String: Any]], queued: [[String: Any]] = [], steered: [[String: Any]] = []) -> HistoryConversation {
        HistoryConversation(json: ["session": ["id": "s", "state": "running", "entries": entries,
                                               "queued": queued, "steered": steered]])!
    }

    func testARewordedEchoReplacesTheBubbleOnce() throws {
        var box = HistoryOutbox()
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        box.add(try XCTUnwrap(HistoryMessageRequest(sessionID: "s", text: "put tomatos first", clientID: "c1")), at: t0)
        box.settle("c1", .steer)
        // The server wrapped/reworded the words but kept our id.
        let c = conversation([["id": "m1", "kind": "user", "text": "Sam typed: Put the tomatoes first.", "client_id": "c1"]])
        let messages = HistoryThread(c, outbox: box).messages
        XCTAssertEqual(messages.map(\.id), ["m1"], "shown once: the server's copy, not ours as well")
        XCTAssertEqual(messages.first?.clientID, "c1")
        box.prune(HistoryThread(c).messages, session: "s")
        XCTAssertTrue(box.items.isEmpty)
    }

    func testIdenticalTextKeepsSeparateIDs() throws {
        var box = HistoryOutbox()
        let t0 = Date(timeIntervalSince1970: 1_000_000)
        box.add(try XCTUnwrap(HistoryMessageRequest(sessionID: "s", text: "yes", clientID: "a")), at: t0)
        box.add(try XCTUnwrap(HistoryMessageRequest(sessionID: "s", text: "yes", clientID: "b")), at: t0 + 1)
        box.settle("a", .steer); box.settle("b", .steer)
        let c = conversation([["id": "m1", "kind": "user", "text": "yes", "client_id": "a"]])
        let shown = HistoryThread(c, outbox: box).messages
        XCTAssertEqual(shown.map(\.id), ["m1", "local-b"], "b's twin words never hide b")
        box.prune(HistoryThread(c).messages, session: "s")
        XCTAssertEqual(box.items.map(\.id), ["b"])
    }

    func testSteerReceiptIsPickedByID() {
        let c = conversation([["id": "m1", "kind": "user", "text": "Skip it", "client_id": "a"],
                              ["id": "m2", "kind": "user", "text": "Skip it", "client_id": "b"]],
                             steered: [["client_id": "a", "text": "Skip it", "state": "seen"],
                                       ["client_id": "b", "text": "Skip it", "state": "unused"]])
        let t = HistoryThread(c)
        let marks = SteeredMarks()
        XCTAssertEqual(marks.receipt(clientID: t.messages[0].clientID, at: nil, in: c.steered)?.state, .seen)
        XCTAssertEqual(marks.receipt(clientID: t.messages[1].clientID, at: nil, in: c.steered)?.state, .unused)
        XCTAssertNil(marks.receipt(clientID: nil, at: nil, in: c.steered))
    }

    func testPendingIsNotSentAndAnInterruptedOneTurnsUnused() throws {
        var box = HistoryOutbox()
        box.add(try XCTUnwrap(HistoryMessageRequest(sessionID: "s", text: "Then stop", clientID: "p1")), at: Date())
        box.settle("p1", .pending)
        XCTAssertEqual(box.item("p1")?.delivery, .pending)
        XCTAssertNotEqual(box.item("p1")?.delivery, .sent)
        XCTAssertTrue(box.pending(in: "s"), "pending keeps the page polling")
        box.reconcile(conversation([], steered: [["client_id": "p1", "text": "Then stop", "state": "unused",
                                                  "label": "The task stopped before reading this"]]))
        XCTAssertEqual(box.item("p1")?.delivery, .failed("The task stopped before reading this"))
        XCTAssertFalse(box.pending(in: "s"), "nothing in flight: no fast polling")
        // Retry keeps the id and payload.
        var ledger = OutboxLedger<HistoryOutbox.Item>()
        ledger.add(try XCTUnwrap(box.item("p1")))
        ledger.retry("p1")
        XCTAssertEqual(ledger.item("p1")?.phase, .queued)
        XCTAssertEqual(ledger.item("p1")?.request.text, "Then stop")
    }

    func testQueuedRowsHeldOrUnused() {
        let c = conversation([], queued: [["client_id": "q1", "text": "later", "position": 1, "state": "queued"],
                                          ["client_id": "q2", "text": "never", "state": "unused", "position": NSNull(),
                                           "label": "Its task stopped"]])
        XCTAssertEqual(c.queued.map(\.position), [1, nil])
        XCTAssertEqual(c.queued[1].tag, "Its task stopped")
        XCTAssertEqual(OutboxServerView.of("q1", in: c), .queued)
        XCTAssertEqual(OutboxServerView.of("q2", in: c), .unused("Its task stopped"))
    }

    func testRepliesDecodeTheNewEnumsAndIDs() throws {
        let r = try XCTUnwrap(TaskMessageReply(json: ["ok": true, "run_id": "run_1", "client_id": "c1",
                                                      "receipt_id": "rc_1", "delivered_as": "new_run"]))
        XCTAssertEqual(r.deliveredAs, .newRun); XCTAssertEqual(r.clientID, "c1"); XCTAssertEqual(r.receiptID, "rc_1")
        let h = try XCTUnwrap(HistoryMessageReply(json: ["ok": true, "via": "task", "delivered_as": "pending",
                                                         "client_id": "c2", "receipt_id": "rc_2"]))
        XCTAssertEqual(h.deliveredAs, .pending); XCTAssertEqual(h.receiptID, "rc_2")
        for kind in ["steer", "queued", "follow_up", "answer", "linked", "new_run", "pending"] {
            XCTAssertNotEqual(TaskMessageDelivery(rawValue: kind), nil, kind)
        }
        let e = try XCTUnwrap(HistoryEntry(json: ["id": "m", "kind": "user", "text": "hi", "client_id": "c1", "receipt_id": "rc"]))
        XCTAssertEqual(e.clientID, "c1")
    }

    func testTaskOutboxPendingIsNotSentAndRetryKeepsThePayload() throws {
        var box = TaskOutbox()
        let att = OutgoingAttachment(id: "att_1", kind: .picture, name: "a.png")
        let m = try XCTUnwrap(box.enqueue(jobID: "j", runID: "run_1", taskName: "T", text: "Oak", choiceTaskID: "t1",
                                          attachments: [att], id: "c1"))
        XCTAssertEqual(box.startNext()?.id, m.id)
        box.markSent("c1", as: .pending)
        XCTAssertFalse(try XCTUnwrap(box.message("c1")).isSent, "pending isn't Sent")
        box.markFailed("c1", "no")
        box.retry("c1")
        let again = try XCTUnwrap(box.startNext())
        XCTAssertEqual([again.id, again.text, again.choiceTaskID], ["c1", "Oak", "t1"])
        XCTAssertEqual(again.attachments, [att])
    }

    func testCompletionRunsWhenTheWeakOwnerIsGone() {
        final class Owner {}
        var owner: Owner? = Owner()
        weak var weakOwner = owner
        owner = nil
        var settled: (TaskMessageReply?, String?)?
        OutboxCompletion.run(weakOwner, done: { (r: TaskMessageReply?, e: String?) in settled = (r, e) }) { _ in XCTFail("no owner") }
        XCTAssertNil(settled?.0)
        XCTAssertEqual(settled?.1, OutboxCompletion.ownerGone)
        // And a reply-bar message wired that way ends failed, never stuck "Sending…".
        var box = TaskOutbox()
        _ = box.enqueue(jobID: "j", runID: "r", taskName: "T", text: "hi", id: "c1")
        _ = box.startNext()
        if let why = settled?.1 { box.markFailed("c1", why) }
        XCTAssertTrue(try XCTUnwrap(box.message("c1")).isFailed)
    }
}
