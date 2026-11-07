import XCTest
@testable import SpeakeasyCore

/// Work cards (Home v7): the new kinds decode and read out in plain words, and a day's overlaps
/// come from the same machine times the plugin uses (views.day_overlaps). Invented data only.
final class WorkCardsTests: XCTestCase {
    private func card(_ json: [String: Any]) -> ViewCard? { ViewCard(json: json, number: 1) }

    func testNewKindsDecodeAndCountAsArtifacts() {
        for kind in ["issue", "pr", "jobs", "site", "mockups"] {
            XCTAssertTrue(ViewCard.drawn.contains(kind), kind)
            XCTAssertTrue(JobAnswer.artifactKinds.contains(kind), kind)
        }
        XCTAssertTrue(ViewCard.drawn.isSuperset(of: JobAnswer.artifactKinds))
    }

    func testSummariesSayWhatTheCardSays() throws {
        let issue = try XCTUnwrap(card(["kind": "issue", "ref": "FN-142", "title": "Redesign notification settings",
                                        "state": "In review", "assignee": "Mara K."]))
        XCTAssertEqual(issue.summary, "FN-142, Redesign notification settings, In review, Mara K.")
        let pr = try XCTUnwrap(card(["kind": "pr", "number": 418, "title": "Offline drafts", "state": "conflicts",
                                     "reason": "The draft store changed on main."]))
        XCTAssertEqual(pr.summary, "Pull request #418, Offline drafts, Conflicts, The draft store changed on main.")
        let jobs = try XCTUnwrap(card(["kind": "jobs", "title": "Running now", "items": [
            ["name": "Release notes", "status": "Two headlines ready"], ["name": "Fix attachments", "status": "Running UI tests"]]]))
        XCTAssertEqual(jobs.summary, "Running now, Release notes: Two headlines ready, Fix attachments: Running UI tests")
        let mockups = try XCTUnwrap(card(["kind": "mockups", "title": "Onboarding", "items": [["image": 1], ["image": 2]]]))
        XCTAssertEqual(mockups.summary, "Onboarding, 2 screens")
    }

    func testPRStateWordsAndTones() {
        XCTAssertEqual(PRState.label("failing"), "Checks failing")
        XCTAssertEqual(PRState.tone("conflicts"), "warn")
        XCTAssertEqual(PRState.tone("failing"), "bad")
        XCTAssertEqual(PRState.tone("merged"), "good")
        XCTAssertNil(PRState.label("vibing"))
    }

    func testDayOverlapsFromMachineTimes() {
        let events: [JSONValue] = [
            ["start": "2026-11-09T08:45", "end": "2026-11-09T09:00", "title": "Standup"],
            ["start": "2026-11-09T13:00", "end": "2026-11-09T14:00", "title": "Design review"],
            ["start": "2026-11-09T13:30", "end": "2026-11-09T14:00", "title": "1:1"],
            ["start": "2026-11-09T14:00", "end": "2026-11-09T14:30", "title": "Back to back, not an overlap"],
        ].map { JSONValue(any: $0) }
        XCTAssertEqual(DayOverlaps.pairs(events).map { [$0.0, $0.1] }, [[1, 2]])
        XCTAssertEqual(DayOverlaps.clashing(events), [1, 2])
        let card = ViewCard(json: ["kind": "calendar_day", "title": "Monday", "events": events.map(\.foundation)], number: 1)
        XCTAssertEqual(card?.summary, "Monday, 4 events, 1 overlap")
    }

    func testWordTimesAndAllDayEventsNeverOverlap() {
        let events: [JSONValue] = [
            ["start": "1:00 PM", "end": "2:00 PM", "title": "A"], ["start": "1:30 PM", "end": "2:00 PM", "title": "B"],
            ["start": "2026-11-09T00:00", "end": "2026-11-10T00:00", "title": "Holiday", "all_day": true],
            ["start": "2026-11-09T13:00", "end": "2026-11-09T14:00", "title": "C"],
        ].map { JSONValue(any: $0) }
        XCTAssertTrue(DayOverlaps.pairs(events).isEmpty)
    }
}
