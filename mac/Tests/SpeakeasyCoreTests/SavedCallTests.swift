import XCTest
@testable import SpeakeasyCore

final class SavedCallTests: XCTestCase {
    func testSavedConversationPreservesMarkdownAndTurnKinds() {
        let full = "## Hotel\n**Harbor Inn**\n[Open](https://example.com)\n```text\nCourtyard room\n```"
        let call = SavedCall(json: ["interaction_id": "vi_saved", "turns": [
            ["role": "user", "text": "Near the station", "kind": "typed"],
            ["role": "assistant", "text": full, "kind": "text", "task_id": "d_hotel", "task_name": "Hotel"]
        ]])
        XCTAssertEqual(call.interactionID, "vi_saved")
        XCTAssertEqual(call.turns.count, 2)
        XCTAssertEqual(call.turns[0].speaker, .you)
        XCTAssertEqual(call.turns[0].kind, .typed)
        XCTAssertEqual(call.turns[1].kind, .text)
        XCTAssertEqual(call.turns[1].cleanedText, full)
        XCTAssertEqual(call.turns[1].taskID, "d_hotel")
    }

    func testCallIDNeverFallsBackToProviderSessionID() {
        XCTAssertEqual(SavedCall.interactionID(summaryID: "call:vi_saved"), "vi_saved")
        XCTAssertNil(SavedCall.interactionID(summaryID: "call:"))
        XCTAssertNil(SavedCall.interactionID(summaryID: "provider_session"))
    }

    func testInvalidAndEmptyTurnsNeverBecomeBubbles() {
        let call = SavedCall(json: ["turns": [["role": "system", "text": "internal"],
                                               ["role": "user", "text": " "],
                                               ["role": "assistant", "text": "Hello"]]])
        XCTAssertEqual(call.turns.count, 1)
        XCTAssertEqual(call.turns[0].text, "Hello")
    }
}
