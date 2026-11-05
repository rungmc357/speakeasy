import XCTest
@testable import SpeakeasyCore

/// In-place exploration inside informative cards: what a disclosure may show, and where a chart
/// read-out lands.
final class CardDetailTests: XCTestCase {
    func testDetailsOmitNullBlankAndUnsafeLinks() {
        let value = JSONValue(any: ["body": NSNull(), "preview": "  \n", "from": "Ada", "url": "javascript:alert(1)"])
        XCTAssertEqual(CardDetail.fields(value, keys: ["body", "preview", "from", "url"]).map(\.text), ["Ada"])
        XCTAssertNil(CardDetail.destination(value))
        for unsafe in ["http://example.com", "file:///etc/passwd", "https://", "https://localhost/a"] {
            XCTAssertNil(CardDetail.destination(JSONValue(any: ["url": unsafe])), unsafe)
        }
        XCTAssertEqual(CardDetail.destination(JSONValue(any: ["url": "https://example.com/a"]))?.absoluteString, "https://example.com/a")
    }

    func testOnlySuppliedFieldsAreShownInTheOrderAsked() {
        let row = JSONValue(any: ["from": "Ada", "when": "Today", "preview": "Hello", "body": "Full message"])
        XCTAssertEqual(CardDetail.fields(row, keys: ["from", "when", "preview", "body"]).map(\.text), ["Ada", "Today", "Hello", "Full message"])
        XCTAssertEqual(CardDetail.fields(row, keys: ["body", "address"]).map(\.id), ["body"], "a missing field is never invented")
        XCTAssertTrue(CardDetail.fields(.null, keys: ["body"]).isEmpty)
    }

    func testFieldLabelsAndNumbersReadNaturally() {
        let place = JSONValue(any: ["hours_today": "9–5", "rating": 4.5, "reviews": 120])
        let fields = CardDetail.fields(place, keys: ["hours_today", "rating", "reviews"])
        XCTAssertEqual(fields.map(\.label), ["Hours Today", "Rating", "Reviews"])
        XCTAssertEqual(fields.map(\.text), ["9–5", "4.5", "120"])
    }

    func testUnknownKindsAreStillSkipped() {
        XCTAssertNil(ViewCard(json: ["kind": "future"], number: 1))
    }

    func testChartPointsKeepTheirLabelAcrossNulls() {
        let chart = ViewCard(kind: "chart", data: ["labels": ["Mon", "Tue", "Wed"], "series": [
            ["name": "Revenue", "values": [1, NSNull(), 3]]
        ]])
        XCTAssertEqual(chart.chartPoints.map(\.label), ["Mon", "Wed"])
        XCTAssertEqual(chart.chartPoints.map(\.value), [1, 3])
        XCTAssertEqual(chart.chartPoints.map(\.series), ["Revenue", "Revenue"])
    }

    func testChartPointsAcrossSeriesAreUniqueAndIndexed() {
        let chart = ViewCard(kind: "chart", data: ["labels": ["Q1", "Q2"], "series": [
            ["name": "2025", "values": [10, 12]],
            ["values": [NSNull(), 14, 16]],
        ]])
        let points = chart.chartPoints
        XCTAssertEqual(Set(points.map(\.id)).count, points.count)
        XCTAssertEqual(points.map(\.seriesIndex), [0, 0, 1, 1])
        XCTAssertEqual(points.map(\.series), ["2025", "2025", "Series 2", "Series 2"])
        XCTAssertEqual(points.map(\.label), ["Q1", "Q2", "Q2", "3"], "a value past the labels is labelled by its position")
        XCTAssertEqual(points.filter { $0.label == "Q2" }.map(\.value), [12, 14], "the read-out for Q2 pairs both series' Q2 values")
    }

    func testALabelWhereTheFirstSeriesIsNullStillHasAPoint() {
        // The chart draws its hover rule from the first point at a label, which may not be series 0.
        let chart = ViewCard(kind: "chart", data: ["labels": ["Mon", "Tue"], "series": [
            ["name": "A", "values": [NSNull(), 2]],
            ["name": "B", "values": [5, 6]],
        ]])
        XCTAssertEqual(chart.chartPoints.first { $0.label == "Mon" }?.series, "B")
        XCTAssertEqual(chart.chartPoints.filter { $0.label == "Mon" }.map(\.value), [5])
    }
}
