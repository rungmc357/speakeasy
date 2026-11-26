import XCTest
@testable import SpeakeasyCore

/// SPK-540: the Mac chat page (built from `/voice/history/{id}` through `HistoryThread`) draws a
/// pending approval card under the reply that sent it, and an open decision with its details.
/// Before the fix the approval card never reached the page (`waiting` read only Hermes command
/// approvals and questions) while the server said "needs you". Invented content only.
final class MacThreadCardsReproTests: XCTestCase {
    private func conversation(_ text: String) throws -> HistoryConversation {
        let json = try JSONSerialization.jsonObject(with: Data(text.utf8))
        return try XCTUnwrap(HistoryConversation(json: json))
    }

    private let approvalThread = #"""
    {"session": {"id": "s_shed", "title": "Shed ticket", "source": "api_server", "state": "needs_you",
      "entries": [
        {"kind": "user", "id": "u1", "at": 1800000000.0, "text": "Ask on the shed ticket whether the cedar arrived"},
        {"kind": "assistant", "id": "a1", "at": 1800000060.0, "text": "Here's the question. Tap Post on the card to send it."},
        {"kind": "user", "id": "u2", "at": 1800000180.0, "text": "Approve"},
        {"kind": "assistant", "id": "a2", "at": 1800000240.0, "text": "Tap Post on the approval card in this chat."}],
      "approvals": [],
      "jobs": [
        {"run_id": "r2", "status": "completed", "started_at": 1800000180.0, "finished_at": 1800000245.0,
         "result": {"views": [{"kind": "confirm", "type": "post", "title": "Ask on the shed ticket",
            "details": {"account": "Workshop board", "body": "Did the cedar arrive?"},
            "sha256": "abc123", "state": "pending",
            "actions": [{"id": "approve", "label": "Post", "style": "primary", "value": "abc123"},
                        {"id": "cancel", "label": "Cancel", "value": "abc123"}]}]}},
        {"run_id": "r1", "status": "completed", "started_at": 1800000000.0, "finished_at": 1800000065.0,
         "result": {"views": [{"kind": "confirm", "type": "post", "title": "Ask on the shed ticket",
            "details": {"account": "Workshop board", "body": "Did the cedar arrive?"},
            "sha256": "abc123", "state": "pending",
            "actions": [{"id": "approve", "label": "Post", "style": "primary", "value": "abc123"},
                        {"id": "cancel", "label": "Cancel", "value": "abc123"}]}]}}]}}
    """#

    func testAPendingApprovalCardReachesTheThreadPage() throws {
        let c = try conversation(approvalThread)
        let thread = HistoryThread(c)
        XCTAssertEqual(c.state, .needsYou)
        // The same preview sent twice (the "Approve" turn re-sent it) shows once: the newest copy,
        // under the reply that sent it.
        XCTAssertEqual(thread.confirms.pending.count, 1)
        let item = try XCTUnwrap(thread.confirms.after["a2"]?.first)
        XCTAssertEqual(item.runID, "r2")
        XCTAssertNil(thread.confirms.after["a1"], "the older copy of the same preview folds into the newest")
        XCTAssertTrue(thread.confirms.tail.isEmpty)
        XCTAssertEqual(item.card.actions.map(\.id), ["approve", "cancel"])
        XCTAssertEqual(item.card.actions.first?.label, "Post")
        XCTAssertEqual(item.card.viewIndex, 0, "the card-action route addresses result.views[0]")
    }

    func testADoneApprovalCardShowsItsOutcomeNotLiveButtons() throws {
        let c = try conversation(approvalThread.replacingOccurrences(of: #""state": "pending""#, with: #""state": "done""#))
        let thread = HistoryThread(c)
        XCTAssertEqual(thread.confirms.all.count, 1, "the card stays in place with its receipt")
        XCTAssertTrue(thread.confirms.pending.isEmpty, "nothing left to approve: no live buttons")
    }

    func testAnOpenDecisionReachesTheThreadPageWithItsDetails() throws {
        let c = try conversation(#"""
        {"session": {"id": "s_deck", "title": "Deck boards", "source": "api_server", "state": "needs_you",
          "entries": [
            {"kind": "user", "id": "u1", "at": 1800000000.0, "text": "Which boards for the deck?"},
            {"kind": "assistant", "id": "a1", "at": 1800000060.0, "text": "Two good picks; which one?"}],
          "jobs": [
            {"run_id": "r1", "status": "completed", "question_open": true, "started_at": 1800000000.0,
             "result": {"views": [{"kind": "question", "question": "Cedar or composite?",
               "context": "The deck is 12 by 16 feet and gets full sun.",
               "why": "Cedar costs less now and you said you like the look.",
               "affects": "The order going in on Friday",
               "recommended": 0,
               "options": [{"text": "Cedar", "detail": "About $1,900; reseal every two years", "risk": "low"},
                           {"text": "Composite", "detail": "About $3,400; no sealing", "risk": "medium", "reversible": false}]}]}}]}}
        """#)
        let thread = HistoryThread(c)
        XCTAssertEqual(thread.waiting.count, 1)
        let w = try XCTUnwrap(thread.waiting.first)
        XCTAssertEqual(w.kind, .question)
        let d = try XCTUnwrap(w.decision, "the page gets the whole decision, not only its titles")
        XCTAssertEqual(d.context, "The deck is 12 by 16 feet and gets full sun.")
        XCTAssertEqual(d.why, "Cedar costs less now and you said you like the look.")
        XCTAssertEqual(d.affects, "The order going in on Friday")
        XCTAssertEqual(d.recommended, 0)
        XCTAssertEqual(d.options.map(\.detail), ["About $1,900; reseal every two years", "About $3,400; no sealing"])
        XCTAssertEqual(d.options.map(\.risk), ["low", "medium"])
        XCTAssertTrue(thread.confirms.isEmpty, "a question is not an approval card")
    }
}
