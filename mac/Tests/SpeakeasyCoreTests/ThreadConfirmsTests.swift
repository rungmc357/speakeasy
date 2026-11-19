import XCTest
@testable import SpeakeasyCore

/// SPK-522, SPK-540: an approval card a task sent in a chat thread is drawn in that thread, under the
/// reply that sent it, once per preview, with live buttons only while it waits. Invented content only.
final class ThreadConfirmsTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_800_000_000)
    private func at(_ minutes: Double) -> Date { t0.addingTimeInterval(minutes * 60) }

    private func card(sha: String = "abc123", state: String = "pending", buttons: Bool = true) -> [String: Any] {
        ["kind": "confirm", "type": "post", "title": "Ask on the shed ticket",
         "details": ["account": "Workshop board", "body": "Did the cedar arrive?"],
         "sha256": sha, "state": state,
         "actions": buttons ? [["id": "approve", "label": "Post", "style": "primary", "value": sha],
                               ["id": "cancel", "label": "Cancel", "style": "secondary", "value": sha]] : []]
    }
    private func job(_ run: String, from: Double?, to: Double?, views: [[String: Any]]) -> WorkInfo {
        var json: [String: Any] = ["run_id": run, "status": "completed", "result": ["views": views]]
        if let from { json["started_at"] = at(from).timeIntervalSince1970 }
        if let to { json["finished_at"] = at(to).timeIntervalSince1970 }
        return WorkInfo(json: json)!
    }
    private var rows: [HistoryEntry] {
        [.init(id: "u1", kind: .user, at: at(0), text: "Ask on the shed ticket"),
         .init(id: "a1", kind: .assistant, at: at(1), text: "Here's the question. Tap Post."),
         .init(id: "u2", kind: .user, at: at(3), text: "Approve"),
         .init(id: "a2", kind: .assistant, at: at(4), text: "Tap Post on the card and I'll send it.")]
    }

    func testTheCardSitsUnderTheReplyThatSentIt() {
        let p = ThreadConfirms.place(rows: rows, jobs: [job("r1", from: 0, to: 1.2, views: [card()])])
        XCTAssertEqual(p.after.keys.sorted(), ["a1"])
        XCTAssertEqual(p.after["a1"]?.map(\.runID), ["r1"])
        XCTAssertTrue(p.tail.isEmpty)
        let item = p.after["a1"]![0]
        XCTAssertEqual(item.card.kind, "confirm")
        XCTAssertTrue(item.isPending)
        XCTAssertEqual(item.buttons.map(\.id), ["approve", "cancel"])
        XCTAssertEqual(item.buttons.map(\.label), ["Post", "Cancel"])
        XCTAssertEqual(item.buttons.map(\.value.string), ["abc123", "abc123"], "each button carries the preview's fingerprint")
    }

    func testTheSameCardSentAgainShowsOnceUnderTheNewestReply() {
        // The server lists newest first.
        let jobs = [job("r2", from: 3, to: 4.1, views: [card()]), job("r1", from: 0, to: 1.2, views: [card()])]
        let p = ThreadConfirms.place(rows: rows, jobs: jobs)
        XCTAssertNil(p.after["a1"])
        XCTAssertEqual(p.after["a2"]?.map(\.runID), ["r2"])
        XCTAssertTrue(p.tail.isEmpty)
        XCTAssertEqual(ThreadConfirms.items(jobs).count, 1)
    }

    func testAChangedPreviewIsItsOwnCard() {
        let jobs = [job("r2", from: 3, to: 4.1, views: [card(sha: "new")]), job("r1", from: 0, to: 1.2, views: [card(sha: "old")])]
        let p = ThreadConfirms.place(rows: rows, jobs: jobs)
        XCTAssertEqual(p.after["a1"]?.map(\.runID), ["r1"])
        XCTAssertEqual(p.after["a2"]?.map(\.runID), ["r2"])
    }

    func testACardWhoseReplyIsntOnThePageGoesAtTheEnd() {
        let p = ThreadConfirms.place(rows: rows, jobs: [job("r9", from: 10, to: 11, views: [card()]),
                                                         job("r8", from: nil, to: nil, views: [card(sha: "x")])])
        XCTAssertTrue(p.after.isEmpty)
        XCTAssertEqual(Set(p.tail.map(\.runID)), ["r8", "r9"])
    }

    func testASettledCardKeepsItsPlaceWithNoLiveButtons() {
        // The server empties a settled card's buttons; an answer that still lists them offers none either.
        for (state, listed) in [("done", false), ("done", true), ("cancelled", true), ("approved", true), ("unverified", true)] {
            let p = ThreadConfirms.place(rows: rows, jobs: [job("r1", from: 0, to: 1.2, views: [card(state: state, buttons: listed)])])
            let items = p.after["a1"] ?? []
            XCTAssertEqual(items.map(\.card.kind), ["confirm"], "\(state): the card stays where it was sent")
            XCTAssertEqual(items.first?.card["state"].string, state, "\(state): its outcome is what it shows")
            XCTAssertEqual(items.first?.isPending, false, state)
            XCTAssertEqual(items.first?.buttons, [], "\(state): no live buttons")
        }
    }

    func testOnlyApprovalCardsArePlaced() {
        let weather: [String: Any] = ["kind": "weather", "place": "Lakeside", "now": ["temp": 61, "condition": "Sunny"]]
        let p = ThreadConfirms.place(rows: rows, jobs: [job("r1", from: 0, to: 1.2, views: [weather, card()])])
        XCTAssertEqual(p.after["a1"]?.map(\.id), ["confirm-r1-2"])
        XCTAssertEqual(p.after["a1"]?.first?.card.viewIndex, 1, "the card-action route addresses it by its place in the task's views")
    }

    func testTheButtonAddressesTheServersIndexNotTheAppsList() {
        // A kind this app doesn't draw is dropped from its list; the server still counts it.
        let unknown: [String: Any] = ["kind": "hologram", "title": "Not drawn here"]
        let work = job("r1", from: 0, to: 1.2, views: [unknown, card()])
        XCTAssertEqual(work.views.map(\.kind), ["confirm"])
        let item = ThreadConfirms.place(rows: rows, jobs: [work]).after["a1"]?.first
        XCTAssertEqual(item?.card.viewIndex, 1)
        XCTAssertEqual(item?.id, "confirm-r1-2")
    }

    func testACardWhoseReplyWasFoldedIntoItsWorkGoesAtTheEnd() throws {
        // The page draws the folded thread: a reply followed by tool calls becomes part of the work
        // card, so it is not a row a card could sit under.
        let c = try XCTUnwrap(HistoryConversation(json: ["session": [
            "id": "s1", "title": "Shed ticket", "source": "speakeasy", "state": "needs_you",
            "entries": [["id": "u1", "kind": "user", "at": at(0).timeIntervalSince1970, "text": "Ask on the shed ticket"],
                        ["id": "a1", "kind": "assistant", "at": at(0.5).timeIntervalSince1970, "text": "Looking up the ticket."],
                        ["id": "t1", "kind": "tools", "at": at(0.7).timeIntervalSince1970, "items": [
                            ["id": "read", "kind": "read", "text": "Read the shed ticket", "tool": "read_file", "status": "done"]]]],
            "jobs": [["run_id": "r1", "status": "completed", "started_at": at(0).timeIntervalSince1970,
                      "finished_at": at(1.2).timeIntervalSince1970, "result": ["views": [card()]]]]]]))
        XCTAssertEqual(HistoryThread(c).confirms.after["a1"]?.map(\.id), ["confirm-r1-1"], "unfolded, the reply is a row")
        let folded = HistoryThread(ReplyWorkThread(entries: c.entries).conversation(c))
        XCTAssertEqual(folded.messages.map(\.id), ["u1"])
        XCTAssertEqual(folded.confirms.after, [:])
        XCTAssertEqual(folded.confirms.tail.map(\.id), ["confirm-r1-1"])
    }

    func testTheThreadPlacesCardsAmongItsOwnMessages() throws {
        let c = try XCTUnwrap(HistoryConversation(json: ["session": [
            "id": "s1", "title": "Shed ticket", "source": "speakeasy", "state": "needs_you",
            "entries": [["id": "u1", "kind": "user", "at": at(0).timeIntervalSince1970, "text": "Ask on the shed ticket"],
                        ["id": "a1", "kind": "assistant", "at": at(1).timeIntervalSince1970, "text": "Here's the question."]],
            "jobs": [["run_id": "r1", "status": "completed", "started_at": at(0).timeIntervalSince1970,
                      "finished_at": at(1.2).timeIntervalSince1970, "result": ["views": [card()]]]]]]))
        let thread = HistoryThread(c)
        XCTAssertEqual(thread.messages.map(\.id), ["u1", "a1"])
        XCTAssertEqual(thread.confirms.after["a1"]?.map(\.id), ["confirm-r1-1"])
        XCTAssertEqual(thread.waiting, [], "an approval card is drawn in place, not in the strip at the end")
    }
}
