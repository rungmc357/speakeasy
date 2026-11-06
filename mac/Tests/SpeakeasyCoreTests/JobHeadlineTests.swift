import XCTest
@testable import SpeakeasyCore

final class JobHeadlineTests: XCTestCase {
    private func info(_ result: [String: Any], request: String? = nil) -> WorkInfo {
        var o: [String: Any] = ["run_id": "r1", "status": "completed", "result": result]
        if let request { o["request"] = request }
        return WorkInfo(json: o)!
    }

    func testTheApiHeadlineWinsOverTheSpokenOpener() {
        let i = info(["spoken": "Okay, I'm dropping it.", "full": "Okay, I'm dropping it.", "headline": "Card fix dropped from build 66"])
        XCTAssertEqual(JobAnswer.headline(i), "Card fix dropped from build 66")
    }

    func testAnOlderResultSkipsTheOpener() {
        let i = info(["spoken": "Okay, I'm dropping it. Nothing goes into build 66.", "full": "x"])
        XCTAssertEqual(JobAnswer.headline(i), "Nothing goes into build 66.")
        XCTAssertEqual(JobAnswer.headline(info(["spoken": "Got it — the kitchen lights are off.", "full": "x"])), "The kitchen lights are off.")
        XCTAssertNil(JobAnswer.headline(info(["spoken": "Sure thing. Let me check.", "full": "x"])))
    }

    func testWhatWasAskedSurvivesWithoutTheRequestEvent() {
        let i = info(["spoken": "Done.", "full": "Done."], request: "find why the checkout test fails")
        XCTAssertEqual(i.request, "find why the checkout test fails")
    }
}
