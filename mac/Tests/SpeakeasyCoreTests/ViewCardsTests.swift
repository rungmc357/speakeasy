import XCTest
@testable import SpeakeasyCore

final class ViewCardsTests: XCTestCase {
    func testResultDecodesViewsAndSkipsUnknownKinds() {
        let json: [String: Any] = ["task_id": "t1", "status": "completed", "result": [
            "spoken": "Apple is at 333 dollars.",
            "views": [
                ["kind": "quote", "symbol": "AAPL", "name": "Apple", "price": 333.02, "change_pct": 1.1,
                 "points": [1, 2, 3]],
                ["kind": "hologram", "title": "from a newer server"],
                ["kind": "weather", "place": "Lisbon", "temp": 21, "unit": "°C", "condition": "clear"],
            ],
        ]]
        let task = TaskItem(json: json)
        XCTAssertEqual(task?.info.views.map(\.kind), ["quote", "weather"])
        XCTAssertEqual(task?.info.views.first?["points"].doubles, [1, 2, 3])
        XCTAssertEqual(task?.info.views.first?.summary, "Apple, $333.02, up 1.10%")
    }

    func testCardsAloneAreAResult() {
        let json: [String: Any] = ["task_id": "t2", "status": "completed",
                                   "result": ["views": [["kind": "fact", "title": "Height", "value": "828 m"]]]]
        XCTAssertNotNil(TaskItem(json: json)?.info.result)
    }

    func testOnlyHttpsUrlsLoad() {
        let card = ViewCard(kind: "place", data: ["name": "Cafe", "photo_url": "http://example.com/a.jpg",
                                                  "url": "https://example.com/cafe"])
        XCTAssertNil(card["photo_url"].url)
        XCTAssertEqual(card["url"].url?.host, "example.com")
    }

    func testBooleansStayBooleans() {
        let card = ViewCard(kind: "list", data: ["items": [["text": "Eggs", "done": true]]])
        XCTAssertEqual(card["items"].array.first?["done"].bool, true)
        XCTAssertNil(card["items"].array.first?["done"].double)
    }

    func testSignedChangeFormatting() {
        XCTAssertEqual(ViewCard.signed(-0.799, money: true), "−$0.80")
        XCTAssertEqual(ViewCard.signed(3.62, money: true), "+$3.62")
        XCTAssertEqual(ViewCard.signed(-0.24), "−0.24%")
    }

    /// A count sent as a progress card ("4 builds of 9 builds · 44%") on a finished task is a fact.
    func testFinishedTaskNeverDrawsAProgressBar() {
        let json: [String: Any] = ["task_id": "t3", "status": "completed", "result": [
            "spoken": "Four of the nine nightly builds kept their logs.",
            "views": [
                ["kind": "progress", "label": "Nightly builds with logs", "value": 4, "total": 9, "unit": "builds"],
                ["kind": "progress", "label": "Pages read", "value": 2.5, "detail": "Half a page left"],
                ["kind": "home", "title": "Office", "devices": []],
            ],
        ]]
        let views = TaskItem(json: json)?.info.views ?? []
        XCTAssertEqual(views.map(\.kind), ["fact", "fact", "home"], "no progress card survives; positions kept")
        XCTAssertEqual(views.map(\.number), [1, 2, 3])
        XCTAssertEqual(views[0]["title"].string, "Nightly builds with logs")
        XCTAssertEqual(views[0]["value"].string, "4 of 9")
        XCTAssertEqual(views[0]["unit"].string, "builds", "the unit is said once")
        XCTAssertEqual(views[0].summary, "Nightly builds with logs, 4 of 9 builds")
        XCTAssertEqual(views[1]["value"].string, "2.5")
        XCTAssertEqual(views[1]["subtitle"].string, "Half a page left")
        XCTAssertFalse(ViewCard.drawn.contains("progress"))
        XCTAssertNil(ViewCard(json: ["kind": "progress", "value": true], number: 1)?["value"].string, "a flag isn't a number")
    }

    func testAForecastWithNoNowLeadsWithItsHighAndLow() {
        XCTAssertEqual(ViewCard(kind: "weather", data: ["temp": 61.6, "high": 64, "low": 50]).weatherHeadline, "62°")
        XCTAssertEqual(ViewCard(kind: "weather", data: ["high": 55, "low": 35]).weatherHeadline, "55° / 35°")
        XCTAssertEqual(ViewCard(kind: "weather", data: ["high": 55]).weatherHeadline, "55°")
    }
}
