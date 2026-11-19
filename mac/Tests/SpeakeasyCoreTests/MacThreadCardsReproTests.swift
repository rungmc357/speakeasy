import XCTest
@testable import SpeakeasyCore

/// SPK-540: what the Mac session page is given to draw for a chat thread that waits on the user, from
/// two `/voice/history/{id}` answers in the server's own shape (`Contract/`, invented content).
final class MacThreadCardsReproTests: XCTestCase {
    private func conversation(_ name: String) throws -> HistoryConversation {
        let url = try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Contract"))
        return try XCTUnwrap(HistoryConversation(json: try JSONSerialization.jsonObject(with: Data(contentsOf: url))))
    }

    func testAPendingApprovalCardReachesTheThreadPage() throws {
        let c = try conversation("history_confirm_card")
        XCTAssertEqual(c.state, .needsYou)
        XCTAssertEqual(c.jobs.flatMap(\.views).filter { $0.kind == "confirm" }.count, 1)
        let thread = HistoryThread(c)
        XCTAssertEqual(thread.messages.map(\.id), ["u1", "a1"])
        let placed = thread.confirms.after["a1"] ?? []
        XCTAssertEqual(placed.count, 1, "the thread says Needs you but has no card to approve")
        XCTAssertTrue(thread.confirms.tail.isEmpty)
        let card = try XCTUnwrap(placed.first)
        XCTAssertEqual(card.runID, "run_qa_confirm")
        XCTAssertEqual(card.card.viewIndex, 0)
        XCTAssertEqual(card.buttons.map(\.label), ["Post", "Cancel"])
        XCTAssertEqual(card.buttons.first?.value.string, String(repeating: "0a", count: 32))
    }

    func testAnOpenDecisionReachesTheThreadPage() throws {
        let c = try conversation("history_open_decision")
        XCTAssertEqual(c.waitingQuestions.count, 1)
        XCTAssertEqual(HistoryThread(c).waiting.map(\.kind), [.question])
    }

    func testAnOpenDecisionCarriesItsDetails() throws {
        let waiting = try XCTUnwrap(HistoryThread(try conversation("history_open_decision")).waiting.first)
        XCTAssertEqual(waiting.choices, ["Blue", "Green"])
        let decision = try XCTUnwrap(waiting.decision)
        XCTAssertEqual(decision.context, "Test question, nothing depends on it.")
        XCTAssertEqual(decision.why, "Blue is the control value.")
        XCTAssertEqual(decision.affects, "Nothing")
        XCTAssertEqual(decision.recommendedOption?.text, "Blue")
        XCTAssertEqual(decision.options.map(\.detail), ["Control value", "Alternate value"])
        XCTAssertEqual(decision.options.map(\.risk), ["low", "low"])
    }
}
