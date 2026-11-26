import XCTest
@testable import SpeakeasyCore

/// SPK-522 / SPK-540 (shared rule, iPhone and Mac): an approval card a task sent in a chat thread is drawn in that thread, under the reply
/// that sent it, once per preview. Invented content only.
final class SessionConfirmsTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_800_000_000)
    private func at(_ minutes: Double) -> Date { t0.addingTimeInterval(minutes * 60) }

    private func card(sha: String = "abc123", state: String = "pending") -> [String: Any] {
        ["kind": "confirm", "type": "post", "title": "Ask on the shed ticket",
         "details": ["account": "Workshop board", "body": "Did the cedar arrive?"],
         "sha256": sha, "state": state,
         "actions": [["id": "approve", "label": "Post", "style": "primary", "value": sha],
                     ["id": "cancel", "label": "Cancel", "style": "secondary", "value": sha]]]
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
        let p = SessionConfirms.place(rows: rows, jobs: [job("r1", from: 0, to: 1.2, views: [card()])])
        XCTAssertEqual(p.after["a1"]?.map(\.runID), ["r1"])
        XCTAssertTrue(p.tail.isEmpty)
        let item = p.after["a1"]![0]
        XCTAssertEqual(item.card.kind, "confirm")
        XCTAssertEqual(item.card.actions.map(\.id), ["approve", "cancel"])
        XCTAssertEqual(item.card.actions.first?.label, "Post")
        XCTAssertTrue(SessionConfirms.pending(item))
    }

    func testTheSameCardSentAgainShowsOnceUnderTheNewestReply() {
        // The server lists newest first.
        let jobs = [job("r2", from: 3, to: 4.1, views: [card()]), job("r1", from: 0, to: 1.2, views: [card()])]
        let p = SessionConfirms.place(rows: rows, jobs: jobs)
        XCTAssertNil(p.after["a1"])
        XCTAssertEqual(p.after["a2"]?.map(\.runID), ["r2"])
        XCTAssertEqual(SessionConfirms.items(jobs).count, 1)
    }

    func testAChangedPreviewIsItsOwnCard() {
        let jobs = [job("r2", from: 3, to: 4.1, views: [card(sha: "new")]), job("r1", from: 0, to: 1.2, views: [card(sha: "old")])]
        let p = SessionConfirms.place(rows: rows, jobs: jobs)
        XCTAssertEqual(p.after["a1"]?.count, 1)
        XCTAssertEqual(p.after["a2"]?.count, 1)
    }

    func testACardWhoseReplyIsntOnThePageGoesAtTheEnd() {
        let p = SessionConfirms.place(rows: rows, jobs: [job("r9", from: 10, to: 11, views: [card()]),
                                                         job("r8", from: nil, to: nil, views: [card(sha: "x")])])
        XCTAssertTrue(p.after.isEmpty)
        XCTAssertEqual(Set(p.tail.map(\.runID)), ["r8", "r9"])
    }

    func testOnlyApprovalCardsAndTheirStateIsKept() {
        let weather: [String: Any] = ["kind": "weather", "place": "Lakeside", "now": ["temp": 61, "condition": "Sunny"]]
        let p = SessionConfirms.place(rows: rows, jobs: [job("r1", from: 0, to: 1.2, views: [weather, card(state: "done")])])
        let items = p.after["a1"] ?? []
        XCTAssertEqual(items.count, 1)
        XCTAssertFalse(SessionConfirms.pending(items[0]))   // a receipt stays in place, no longer waiting
    }
}
