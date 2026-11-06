import XCTest
@testable import SpeakeasyCore

/// Home (SPK-124): the app decodes the plugin's real /voice/now reply (Contract/now.json, written by
/// plugin/tests/test_now.py from invented data) and draws it in the plugin's order.
final class NowHomeTests: XCTestCase {
    private func contract() throws -> [String: Any] {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "now", withExtension: "json", subdirectory: "Contract"))
        return try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    }

    func testDecodesThePluginsReplyInItsOrder() throws {
        let home = NowHome(json: try contract())
        XCTAssertEqual(home.items.map(\.kind), [.question, .stopped, .working, .newAnswer])
        XCTAssertEqual(home.sentence, "One question for you, one thing stopped, one thing in the works, and one new answer.")
        XCTAssertEqual(home.counts.needsYou, 1)
        XCTAssertEqual(home.counts.finishedUnread, 1)
        let q = try XCTUnwrap(home.items.first?.question)
        XCTAssertEqual(q.shape, .things)
        XCTAssertEqual(q.options.map(\.id), ["o1", "o2"])
        XCTAssertEqual(q.options.first?.link?.absoluteString, "https://desks.example/oakline")
        XCTAssertEqual(q.options.first?.facts, ["$389", "60 in. wide"])
        XCTAssertNil(q.options.last?.link, "only https links")
        XCTAssertTrue(q.comparable)
        XCTAssertEqual(home.items[1].fix, .credits)
        XCTAssertTrue(home.items[3].unread)
    }

    func testCapsAndOverflowWords() {
        XCTAssertEqual(NowCaps.small.cap(.question), 1)
        XCTAssertEqual(NowCaps.phone.cap(.working), 3)
        XCTAssertEqual(NowCaps.overflowLabel(.question, 2), "2 more questions")
        XCTAssertEqual(NowCaps.overflowLabel(.working, 3), "3 more in the works")
        XCTAssertEqual(NowCaps.overflowLabel(.newAnswer, 1), "1 more new answer")
    }

    func testEarlierKindsQueryAndMenu() {
        XCTAssertEqual(EarlierKind.query([]), "conversations")
        XCTAssertEqual(EarlierKind.query(["archived", "kanban", "conversations"]), "conversations,kanban,archived")
        let menu = EarlierKind.parse([["id": "conversations", "label": "Conversations", "count": 12, "on": true],
                                      ["id": "nope", "count": 1], ["id": "subagents", "count": 3, "on": false]])
        XCTAssertEqual(menu.map(\.id), ["conversations", "subagents"])
        XCTAssertEqual(menu.last?.label, "Subagents")
    }

    func testProvenanceLine() {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = TimeZone(identifier: "America/New_York")!
        let at = Date(timeIntervalSince1970: 1_794_000_000)  // invented
        let item = NowItem(id: "s:x", kind: .newAnswer, title: "Desk hunt", line: "Done.", provenance: .yesterday(at))
        XCTAssertTrue(item.provenanceLine(calendar: cal)?.hasPrefix("From yesterday · ") == true)
        XCTAssertFalse(item.provenanceLine(calendar: cal)?.contains("so far") == true)
    }
}
