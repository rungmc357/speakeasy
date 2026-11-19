import XCTest
@testable import SpeakeasyCore

/// SPK-540: what the Mac session page is given to draw for a chat thread that waits on the user, from
/// two `/voice/history/{id}` answers in the server's own shape (`Contract/`, invented content).
/// The page draws `HistoryThread.waiting` and nothing else for "needs you".
final class MacThreadCardsReproTests: XCTestCase {
    private func conversation(_ name: String) throws -> HistoryConversation {
        let url = try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Contract"))
        return try XCTUnwrap(HistoryConversation(json: try JSONSerialization.jsonObject(with: Data(contentsOf: url))))
    }

    func testAPendingApprovalCardReachesTheThreadPage() throws {
        let c = try conversation("history_confirm_card")
        XCTAssertEqual(c.state, .needsYou)
        XCTAssertEqual(c.jobs.flatMap(\.views).filter { $0.kind == "confirm" }.count, 1)
        XCTAssertEqual(HistoryThread(c).waiting.count, 1, "the thread says Needs you but has no card to approve")
    }

    func testAnOpenDecisionReachesTheThreadPage() throws {
        let c = try conversation("history_open_decision")
        XCTAssertEqual(c.waitingQuestions.count, 1)
        XCTAssertEqual(HistoryThread(c).waiting.map(\.kind), [.question])
    }
}
