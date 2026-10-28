import XCTest
@testable import SpeakeasyCore

final class ResultCardsTests: XCTestCase {
    func testDisclosureIsIndependentReversibleAndPrunesDescendants() {
        var state = CardExpansion()
        state.toggle("items/0")
        state.toggle("items/0/body")
        state.toggle("items/1")
        XCTAssertTrue(state.isExpanded("items/0/body"))
        state.toggle("items/0")
        XCTAssertFalse(state.isExpanded("items/0/body"))
        XCTAssertTrue(state.isExpanded("items/1"))
        state.toggle("items/0")
        XCTAssertFalse(state.isExpanded("items/0/body"))
    }

    func testDetailsOmitNullBlankAndUnsafeLinks() {
        let value = JSONValue(any: ["body": NSNull(), "preview": "  ", "from": "Ada", "url": "javascript:alert(1)"])
        XCTAssertEqual(CardDetail.fields(value, keys: ["body", "preview", "from", "url"]).map(\.text), ["Ada"])
        XCTAssertNil(CardDetail.destination(value))
        XCTAssertNil(CardDetail.destination(JSONValue(any: ["url": "http://example.com"])))
        XCTAssertEqual(CardDetail.destination(JSONValue(any: ["url": "https://example.com/a"]))?.absoluteString, "https://example.com/a")
    }

    func testAvailableDetailsArePreservedWithoutInventingFields() {
        let row = JSONValue(any: ["from": "Ada", "when": "Today", "preview": "Hello", "body": "Full message"])
        XCTAssertEqual(CardDetail.fields(row, keys: ["from", "when", "preview", "body"]).map(\.text), ["Ada", "Today", "Hello", "Full message"])
        XCTAssertTrue(CardDetail.fields(.null, keys: ["body"]).isEmpty)
        XCTAssertEqual(CardDetail.rows([.null, .object([:]), row], keys: ["from"]), [row])
        XCTAssertNil(ViewCard(json: ["kind": "future"], number: 1))
    }

    func testChartReadoutsKeepOriginalIndicesAcrossNulls() {
        let chart = ViewCard(kind: "chart", data: ["labels": ["Mon", "Tue", "Wed"], "series": [
            ["name": "Revenue", "values": [1, NSNull(), 3]]
        ]])
        XCTAssertEqual(chart.chartPoints.map(\.label), ["Mon", "Wed"])
        XCTAssertEqual(chart.chartPoints.map(\.value), [1, 3])
        XCTAssertEqual(chart.chartPoints.map(\.series), ["Revenue", "Revenue"])
    }

    func testNumberedLinksAndDeduplication() {
        let cards = ResultCard.links(in: "[Speaker](https://example.com/speaker) and https://example.com/speaker plus https://other.example/item.")
        XCTAssertEqual(cards.map(\.number), [1, 2])
        XCTAssertEqual(cards.map(\.title), ["Speaker", "other.example"])
    }

    func testRejectsLocalAndNonWebDestinations() {
        let cards = ResultCard.links(in: "http://localhost/private https://10.0.0.2/a https://shop.example.com/a javascript:alert(1)")
        XCTAssertEqual(cards.count, 1)
    }

    func testProductDecodingAndFallback() {
        let info = WorkInfo(json: ["status": "completed", "result": ["full": "[A](https://example.com/a)", "cards": [
            ["name": "Speaker", "url": "https://example.com/a", "image_url": "https://example.com/a.jpg",
             "price": "$999", "store": "Example", "rating": "4.5", "specs": ["AirPlay", "Stereo"]]
        ]]])
        XCTAssertEqual(info?.products.first?.price, "$999")
        XCTAssertEqual(info?.products.first?.specs, ["AirPlay", "Stereo"])
        XCTAssertEqual(info?.products.first?.number, 1)
        XCTAssertEqual(info?.cards.first?.number, 1)
    }
}
