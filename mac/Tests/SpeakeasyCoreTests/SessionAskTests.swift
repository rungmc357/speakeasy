import XCTest
@testable import SpeakeasyCore

final class SessionAskTests: XCTestCase {
    private func job(_ request: String?, started: Double? = nil) -> WorkInfo {
        var o: [String: Any] = ["run_id": UUID().uuidString, "status": "completed"]
        if let request { o["request"] = request }
        if let started { o["started_at"] = started }
        return WorkInfo(json: o)!
    }

    private func page(source: String = "speakeasy", entries: [HistoryEntry] = [], hasMore: Bool = false,
                      jobs: [WorkInfo]) -> HistoryConversation {
        HistoryConversation(id: "s1", title: "Checkout test: why it fails", source: source, sourceLabel: "Speakeasy",
                            entries: entries, hasMore: hasMore, jobs: jobs)
    }

    func testShownWhenTheThreadNoLongerStartsWithTheAsk() {
        let c = page(entries: [HistoryEntry(id: "a", kind: .assistant, text: "Still running the tests.")], hasMore: true,
                     jobs: [job("Find why the checkout test fails", started: 100)])
        XCTAssertEqual(SessionAsk.line(c), "Find why the checkout test fails")
    }

    func testHiddenWhenTheFirstMessageAlreadySaysIt() {
        let c = page(entries: [HistoryEntry(id: "u", kind: .user, text: "find why the checkout test fails!"),
                               HistoryEntry(id: "a", kind: .assistant, text: "On it.")],
                     jobs: [job("Find why the checkout test fails")])
        XCTAssertNil(SessionAsk.line(c))
    }

    func testTheEarliestTaskIsTheAsk() {
        let c = page(hasMore: true, jobs: [job("Also check the login test", started: 300), job(nil, started: 50),
                                           job("Find why the checkout test fails", started: 100)])
        XCTAssertEqual(SessionAsk.line(c), "Find why the checkout test fails")
    }

    func testNoneForOtherAppsOrWithoutARecordedAsk() {
        XCTAssertNil(SessionAsk.line(page(source: "discord", hasMore: true, jobs: [job("Summarize the thread")])))
        XCTAssertNil(SessionAsk.line(page(hasMore: true, jobs: [job(nil)])))
        XCTAssertNil(SessionAsk.line(page(hasMore: true, jobs: [])))
    }
}
