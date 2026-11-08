import XCTest
@testable import SpeakeasyCore

final class ReplyAttributionTests: XCTestCase {
    private func model(_ id: String = "example/first", fallback: Bool = false) -> ReplyAttribution {
        ReplyAttribution(model: ModelBadge(id: id, name: id, providerName: "Example", reasoning: .high,
                                          source: "message", served: true), fallback: fallback)
    }
    private func conversation(_ entries: [HistoryEntry]) -> HistoryConversation {
        HistoryConversation(id: "sample", title: "Report", source: "cli", sourceLabel: "Terminal", entries: entries)
    }
    func testFirstAndChangedRepliesOnlyIncludingFallback() {
        let first = model(), second = model("example/second"), fallback = model("example/second", fallback: true)
        let c = conversation([
            HistoryEntry(id: "a", kind: .assistant, text: "First", attribution: first),
            HistoryEntry(id: "u", kind: .user, text: "Continue"),
            HistoryEntry(id: "b", kind: .assistant, text: "Same", attribution: first),
            HistoryEntry(id: "t", kind: .tools),
            HistoryEntry(id: "c", kind: .assistant, text: "Changed", attribution: second),
            HistoryEntry(id: "d", kind: .assistant, text: "Fallback", attribution: fallback)])
        XCTAssertEqual(c.replyModelLabels, Set(["a", "c", "d"]))
        let thread = HistoryThread(c)
        XCTAssertEqual(thread.messages.filter(\.showsModel).map(\.id), ["a", "c", "d"])
        XCTAssertEqual(thread.messages.last?.attribution, fallback)
    }
    func testUnknownDoesNotInheritKnownModel() {
        XCTAssertNil(ReplyAttribution(json: ["id": "unknown"]))
        XCTAssertNil(ReplyAttribution(json: ["id": "example/unknown"]))
        XCTAssertNil(ReplyAttribution(json: [:]))
        XCTAssertTrue(ReplyAttribution.details(for: nil, at: Date(timeIntervalSince1970: 100)).contains("Time:"))
        let c = conversation([
            HistoryEntry(id: "a", kind: .assistant, text: "First", attribution: model()),
            HistoryEntry(id: "b", kind: .assistant, text: "Not recorded"),
            HistoryEntry(id: "c", kind: .assistant, text: "Known again", attribution: model())])
        XCTAssertEqual(c.replyModelLabels, Set(["a", "c"]))
        XCTAssertNil(HistoryThread(c).messages[1].attribution)
    }
    func testJSONAndDetailsKeepModelProviderEffortAndTime() throws {
        let e = try XCTUnwrap(HistoryEntry(json: ["id": "a", "kind": "assistant", "text": "Ready",
            "at": 100, "model": ["id": "example/first", "name": "First", "provider_name": "Example",
                                  "reasoning": "high", "fallback": true, "source": "message", "served": true]]))
        let attribution = try XCTUnwrap(e.attribution)
        XCTAssertEqual(attribution.label, "First · fallback")
        let details = attribution.details(at: e.at)
        for expected in ["Model: First", "Provider: Example", "Reasoning: High", "Fallback", "Time:"] {
            XCTAssertTrue(details.contains(expected), expected)
        }
    }
    func testPrependedPageRecomputesCollapseAndDoesNotUseHeader() {
        var c = conversation([HistoryEntry(id: "b", kind: .assistant, text: "Later", attribution: model())])
        c.model = ModelBadge(id: "other", name: "Not the author")
        XCTAssertEqual(c.replyModelLabels, Set(["b"]))
        c.entries.insert(HistoryEntry(id: "a", kind: .assistant, text: "Earlier", attribution: model()), at: 0)
        XCTAssertEqual(c.replyModelLabels, Set(["a"]))
    }
    func testEffortAndProviderChangeAreVisible() {
        let a = model()
        var b = a
        b.model.reasoning = .low
        XCTAssertTrue(b.differs(from: a))
        b = a; b.model.providerName = "Backup"
        XCTAssertTrue(b.differs(from: a))
        XCTAssertFalse(a.differs(from: a))
    }
}
