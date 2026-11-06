import XCTest
@testable import SpeakeasyCore

/// A session page's `usage` (plugin 0.3.37+): decoded for the header line and its details, and
/// always labelled an estimate.
final class ChatUsageTests: XCTestCase {
    private func json(_ s: String) -> Any { try! JSONSerialization.jsonObject(with: Data(s.utf8)) }

    func testPageCarriesUsage() {
        let c = HistoryConversation(json: json(#"""
        {"session": {"id": "d1", "title": "Garden plan", "source": "discord", "source_label": "Discord",
          "usage": {"estimate": true, "basis": "estimated", "summary": "About $2.50 · estimate", "cost_usd": 2.5,
                    "tokens": 14500, "helpers": 2,
                    "lines": [{"label": "Cost", "value": "About $2.50 (estimate)"}, {"label": "Tokens", "value": "14.5K total"},
                              {"label": "", "value": "dropped"}],
                    "models": [{"name": "Claude Opus 5.5", "tokens": 14500, "value": "14.5K tokens · about $2.50"}],
                    "note": "Estimated from Hermes' records."}}}
        """#))
        let u = try! XCTUnwrap(c?.usage)
        XCTAssertEqual(u.basis, .estimated)
        XCTAssertEqual(u.summary, "About $2.50 · estimate")
        XCTAssertEqual(u.costUSD, 2.5)
        XCTAssertEqual(u.lines.map(\.label), ["Cost", "Tokens"])
        XCTAssertEqual(u.models.first?.name, "Claude Opus 5.5")
        XCTAssertEqual(u.note, "Estimated from Hermes' records.")
        XCTAssertEqual(u.symbol, "dollarsign.circle")
    }

    func testOlderPluginOrNoTokensMeansNoUsage() {
        let old = HistoryConversation(json: json(#"{"session": {"id": "d1", "title": "x"}}"#))
        XCTAssertNil(old?.usage)
        XCTAssertNil(ChatUsage(json: json(#"{"tokens": 0, "summary": "nothing"}"#)))
    }

    func testPricedFigureIsAlwaysLabelledAnEstimate() {
        let u = ChatUsage(json: json(#"{"tokens": 900, "basis": "estimated", "summary": "About 40¢", "cost_usd": 0.4}"#))
        XCTAssertEqual(u?.summary, "About 40¢ · estimate")
        XCTAssertEqual(u?.note, ChatUsage.defaultNote)
    }

    func testPlanAndUnpriced() {
        let plan = ChatUsage(json: json(#"{"tokens": 42000, "basis": "included", "summary": "Included in your ChatGPT plan · 42K tokens", "cost_usd": null}"#))
        XCTAssertNil(plan?.costUSD)
        XCTAssertEqual(plan?.summary, "Included in your ChatGPT plan · 42K tokens")
        XCTAssertEqual(plan?.symbol, "checkmark.seal")
        let none = ChatUsage(json: json(#"{"tokens": 1500, "basis": "something-new"}"#))
        XCTAssertEqual(none?.basis, .unknown)
        XCTAssertEqual(none?.summary, "1500 tokens")
    }
}
