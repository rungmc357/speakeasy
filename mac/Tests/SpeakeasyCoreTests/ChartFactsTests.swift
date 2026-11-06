import XCTest
@testable import SpeakeasyCore

/// Chart cards say what is plotted, stand their bars in the order they name, and only claim the
/// freshness the card carries (invented data throughout).
final class ChartFactsTests: XCTestCase {
    private func card(_ fields: [String: Any]) -> ViewCard { ViewCard(kind: "chart", data: fields) }

    private let prices: [String: Any] = [
        "title": "Ten large tech stocks", "style": "bar", "unit": "$",
        "labels": ["AAA", "BBBB", "CC", "DDD"],
        "series": [["name": "Share price", "values": [210.5, 1450, 98.25, 512]]],
    ]

    // MARK: Metric and order agree with the bars

    func testARankingWithNoOrderStandsHighestFirstAndSaysSo() {
        let f = ChartFacts(card(prices))
        XCTAssertEqual(f.rows.map(\.label), ["BBBB", "DDD", "AAA", "CC"])
        XCTAssertEqual(f.order, .highFirst)
        XCTAssertEqual(f.subtitle, "Share price · highest first")
        XCTAssertTrue(f.ranked)
        XCTAssertEqual(f.rows.compactMap { $0.values[0] }, f.rows.compactMap { $0.values[0] }.sorted(by: >))
    }

    func testLowFirstSortsAscending() {
        var c = prices; c["sort"] = "low_first"; c["metric"] = "Price per share"
        let f = ChartFacts(card(c))
        XCTAssertEqual(f.rows.map(\.label), ["CC", "AAA", "DDD", "BBBB"])
        XCTAssertEqual(f.subtitle, "Price per share · lowest first")
    }

    func testAListedOrderKeepsTheCardsOrderAndNamesWhatDecidedIt() {
        var c = prices; c["sort"] = "as_listed"; c["order_by"] = "index weight"
        let f = ChartFacts(card(c))
        XCTAssertEqual(f.rows.map(\.label), ["AAA", "BBBB", "CC", "DDD"])
        XCTAssertEqual(f.subtitle, "Share price · in order of index weight")
    }

    func testOrderByWithoutSortKeepsTheListedOrder() {
        var c = prices; c["order_by"] = "index weight"
        XCTAssertEqual(ChartFacts(card(c)).rows.map(\.label), ["AAA", "BBBB", "CC", "DDD"])
    }

    func testAnExplicitSortWinsOverOrderBy() {
        var c = prices; c["sort"] = "high_first"; c["order_by"] = "index weight"
        let f = ChartFacts(card(c))
        XCTAssertEqual(f.rows.first?.label, "BBBB")
        XCTAssertEqual(f.subtitle, "Share price · highest first")
    }

    func testTimeSeriesKeepTimeOrderAndClaimNoRanking() {
        let days = card(["title": "Signups per day", "style": "bar", "labels": ["Mon", "Tue", "Wed", "Thu", "Fri"],
                         "series": [["name": "Signups", "values": [120, 132, 101, 140, 155]]]])
        let f = ChartFacts(days)
        XCTAssertEqual(f.rows.map(\.label), ["Mon", "Tue", "Wed", "Thu", "Fri"])
        XCTAssertEqual(f.order, .listed(by: nil))
        XCTAssertEqual(f.subtitle, "Signups")
        XCTAssertFalse(f.ranked)
        XCTAssertEqual(f.restLine, "Latest 155 · High 155 (Fri) · Low 101 (Wed)")
        for labels in [["9 AM", "10 AM", "11 AM"], ["2023", "2024", "2025"], ["Q1", "Q2", "Q3"], ["Nov 3", "Nov 4", "Nov 5"],
                       ["2026-11-01", "2026-11-02", "2026-11-03"]] {
            XCTAssertTrue(ChartFacts.timeOrdered(labels), "\(labels)")
        }
        XCTAssertFalse(ChartFacts.timeOrdered(["GOOG", "GOOGL", "AAPL"]))
    }

    func testALineIsNeverReordered() {
        let line = card(["title": "Price today", "style": "line", "labels": ["A", "B", "C"],
                         "series": [["name": "Price", "values": [3, 1, 2]]]])
        XCTAssertEqual(ChartFacts(line).rows.map(\.label), ["A", "B", "C"])
    }

    func testMultiSeriesSortsByTheFirstSeriesAndKeepsRowsTogether() {
        let c = card(["title": "Plans", "labels": ["Basic", "Team", "Pro"],
                      "series": [["name": "Seats", "values": [5, 25, 12]], ["name": "Teams", "values": [40, 8, 15]]]])
        let f = ChartFacts(c)
        XCTAssertEqual(f.rows.map(\.label), ["Team", "Pro", "Basic"])
        XCTAssertEqual(f.rows.first?.values, [25, 8])
        XCTAssertEqual(f.text(f.rows[0]), "Seats 25 · Teams 8")
        XCTAssertEqual(f.subtitle, "Highest first")
    }

    // MARK: Values read as text

    func testValuesCarryTheirUnit() {
        XCTAssertEqual(ChartFacts.text(1450.32, unit: "$"), "$1,450.32")
        XCTAssertEqual(ChartFacts.text(1450, unit: "USD"), "$1,450")
        XCTAssertEqual(ChartFacts.text(-2.4, unit: "%"), "−2.4%")
        XCTAssertEqual(ChartFacts.text(3.21e12, unit: "$"), "$3.21T")
        XCTAssertEqual(ChartFacts.text(18, unit: "runs"), "18 runs")
        XCTAssertEqual(ChartFacts.text(64, unit: "°F"), "64°F")
        XCTAssertEqual(ChartFacts.text(12.5, unit: "$"), "$12.50")
    }

    func testValuesOnOneCardShareTheirDecimals() {
        let f = ChartFacts(card(["title": "Prices", "unit": "$", "labels": ["A", "B", "C"],
                                 "series": [["values": [431.1, 97.6, 1450.32]]]]))
        XCTAssertEqual(f.rows.compactMap { $0.values[0] }.map(f.text), ["$1,450.32", "$431.10", "$97.60"])
        let counts = ChartFacts(card(["title": "Runs", "labels": ["A", "B"], "series": [["values": [3, 12]]]]))
        XCTAssertEqual(counts.rows.compactMap { $0.values[0] }.map(counts.text), ["12", "3"])
        let pct = ChartFacts(card(["title": "Change", "unit": "%", "labels": ["A", "B"], "series": [["values": [2.4, -1]]]]))
        XCTAssertEqual(pct.rows.compactMap { $0.values[0] }.map(pct.text), ["2.4%", "−1.0%"])
    }

    func testPlainTextListsEveryValueInDrawnOrder() {
        var c = prices; c["source"] = "Example Exchange"
        let text = ChartFacts(card(c), now: Date()).plainText
        XCTAssertEqual(text.split(separator: "\n").map(String.init),
                       ["Ten large tech stocks", "Share price · highest first", "BBBB: $1,450.00", "DDD: $512.00",
                        "AAA: $210.50", "CC: $98.25", "Example Exchange"])
    }

    // MARK: Distinct labels

    func testShortLabelsNeverCollide() {
        let short = ChartFacts.shortLabels(["GOOG", "GOOGL", "MSFT", "Alphabet Class A", "Alphabet Class C"], max: 3)
        XCTAssertEqual(Set(short).count, 5, "\(short)")
        XCTAssertNotEqual(short[0], short[1])
        XCTAssertEqual(ChartFacts.shortLabels(["AB", "CD"], max: 6), ["AB", "CD"])
    }

    // MARK: Freshness: only what the card says

    private var nyc: Calendar { var c = Calendar(identifier: .gregorian); c.timeZone = TimeZone(identifier: "America/New_York")!; return c }

    func testNoFreshnessFieldsSaysNothing() {
        let f = ChartFacts(card(prices))
        XCTAssertNil(f.freshness)
        XCTAssertFalse(f.stale)
    }

    func testSourceTimeAndDelayed() {
        let now = ISO8601DateFormatter().date(from: "2026-11-06T20:00:00Z")!
        var c = prices; c["source"] = "Example Exchange"; c["as_of"] = "2026-11-06T15:42:00-05:00"; c["delayed"] = true
        let f = ChartFacts(card(c), now: now, calendar: nyc)
        XCTAssertEqual(f.freshness, "Example Exchange · As of 3:42 PM · delayed")
        XCTAssertFalse(f.stale)
    }

    func testAnOldTimeShowsItsDateAndIsStale() {
        let now = ISO8601DateFormatter().date(from: "2026-11-06T20:00:00Z")!
        let f = ChartFacts.freshness(source: nil, asOf: "2026-11-03T16:00:00-05:00", delayed: false, now: now, calendar: nyc)
        XCTAssertEqual(f.text, "As of Nov 3, 4:00 PM")
        XCTAssertTrue(f.stale)
        let lastYear = ChartFacts.freshness(source: nil, asOf: "2025-12-31", delayed: false, now: now, calendar: nyc)
        XCTAssertEqual(lastYear.text, "As of Dec 31, 2025")
        XCTAssertTrue(lastYear.stale)
    }

    func testFreshnessWordsAreShownAsGivenAndDelayedAlone() {
        XCTAssertEqual(ChartFacts.freshness(source: nil, asOf: "Thursday's close", delayed: false).text, "As of Thursday's close")
        XCTAssertEqual(ChartFacts.freshness(source: nil, asOf: nil, delayed: true).text, "Delayed")
        XCTAssertEqual(ChartFacts.freshness(source: "  ", asOf: "", delayed: false).text, nil)
    }
}
