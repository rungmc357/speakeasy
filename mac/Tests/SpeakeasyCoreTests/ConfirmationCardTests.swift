import XCTest
@testable import SpeakeasyCore

final class ConfirmationCardTests: XCTestCase {
    func testConfirmationDecodesWithoutChangingMoneyOrApprovalValue() throws {
        let hash = String(repeating: "a", count: 64)
        let card = try XCTUnwrap(ViewCard(json: ["kind": "confirm", "type": "checkout", "title": "Headphones",
            "state": "pending", "sha256": hash, "details": ["total": "86.40", "currency": "USD", "payment_last4": "4242"],
            "actions": [["id": "approve", "label": "Buy for $86.40", "value": hash, "style": "primary"],
                        ["id": "cancel", "label": "Cancel", "value": hash]]], number: 2))
        XCTAssertEqual(card["details"]["total"].string, "86.40")
        XCTAssertEqual(card.actions.first?.value.string, hash)
        XCTAssertEqual(card.actions.first?.label, "Buy for $86.40")
        XCTAssertFalse(try XCTUnwrap(card.actions.first).confirm, "the full preview is the confirmation, no second popup")
        XCTAssertEqual(card.viewIndex, 1)
    }
    func testReceiptHasNoApprovalButtons() throws {
        let card = try XCTUnwrap(ViewCard(json: ["kind": "confirm", "state": "done", "title": "Headphones",
            "receipt": ["reference": "EXAMPLE-001", "summary": "Ordered", "track_url": "https://example.com/track"]], number: 1))
        XCTAssertTrue(card.actions.isEmpty)
        XCTAssertEqual(card["receipt"]["track_url"].url?.absoluteString, "https://example.com/track")
    }
    func testBadTrackingLinkNeverBecomesAnAction() {
        XCTAssertNil(JSONValue.string("javascript:alert(1)").url)
        XCTAssertNil(JSONValue.string("file:///private/order").url)
    }
}
