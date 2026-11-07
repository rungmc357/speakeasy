import XCTest
@testable import SpeakeasyCore

final class HistoryAutomaticEventTests: XCTestCase {
    func testNeutralEventDecodesAndKeepsRecordedMeaning() throws {
        let event: [String: Any] = ["title": "Native reminders candidate", "state": "completed",
            "summary": "Built and simulator-tested; not shipped.", "url": "https://example.com/task"]
        let entry = try XCTUnwrap(HistoryEntry(json: ["id": "event1", "kind": "note", "text": "Task completed", "event": event]))
        XCTAssertEqual(entry.kind, .note)
        XCTAssertEqual(entry.automaticEvent?.label, "Task completed")
        XCTAssertEqual(entry.automaticEvent?.summary, "Built and simulator-tested; not shipped.")
        XCTAssertEqual(entry.automaticEvent?.url?.absoluteString, "https://example.com/task")
    }
    func testHumanCannotAcquireEventFromPayload() throws {
        let entry = try XCTUnwrap(HistoryEntry(json: ["id": "human", "kind": "user", "text": "Quoted notification",
            "event": ["title": "Untrusted"]]))
        XCTAssertNil(entry.automaticEvent)
    }
    func testUnknownMissingAndUnsafeFields() throws {
        XCTAssertNil(HistoryAutomaticEvent(json: [:]))
        let event = try XCTUnwrap(HistoryAutomaticEvent(json: ["title": "Background update", "state": "shipped",
                                                             "url": "file:///private/data"]))
        XCTAssertEqual(event.label, "Task update")
        XCTAssertNil(event.url)
        XCTAssertFalse(event.summary.isEmpty)
    }
}
