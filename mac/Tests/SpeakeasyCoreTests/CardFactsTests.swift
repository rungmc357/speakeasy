import XCTest
@testable import SpeakeasyCore

/// SPK-231 final card decisions drawn on the apps: a PR card's checks (D5) and a document preview's
/// lines (D3). Invented data only.
final class CardFactsTests: XCTestCase {
    private func checks(_ list: [[String: Any]]) -> [JSONValue] { list.map { JSONValue(any: $0) } }
    private func names(_ rows: [JSONValue]) -> [String] { rows.map { $0["name"].string ?? "" } }

    func testIdenticalWaitingChecksAreOneLineAndEveryFailureIsListed() {
        var list: [[String: Any]] = (1...6).map { ["name": "Lint \($0)", "state": "failed"] }
        list += (1...6).map { ["name": "Build \($0)", "state": "pending"] }
        list += [["name": "Docs", "state": "passed"]]
        let rows = PRChecks.rows(checks(list))
        XCTAssertEqual(Array(names(rows.shown).prefix(6)), (1...6).map { "Lint \($0)" })   // past the cap of four
        XCTAssertEqual(names(rows.shown)[6], "6 checks waiting")
        XCTAssertEqual(names(rows.shown).last, "Docs")       // one more row beats a "1 more" line
        XCTAssertNil(rows.rest)
    }

    func testOneWaitingCheckStaysByName() {
        let rows = PRChecks.rows(checks([["name": "Build", "state": "pending"], ["name": "Lint", "state": "passed"]]))
        XCTAssertEqual(names(rows.shown), ["Build", "Lint"])
        XCTAssertNil(rows.rest)
    }

    func testAServerCollapsedRowCountsItsChecks() {
        let rows = PRChecks.rows(checks([["name": "Tests", "state": "failed"],
                                         ["name": "6 checks waiting", "state": "pending", "count": 6]]), total: 9, restState: "passed")
        XCTAssertEqual(names(rows.shown), ["Tests", "6 checks waiting"])
        XCTAssertEqual(rows.rest, "2 more passed")
    }

    func testManySkippedChecksAreCounted() {
        let rows = PRChecks.rows(checks((1...15).map { ["name": "Job \($0)", "state": "skipped"] }))
        XCTAssertEqual(rows.shown.count, 4)
        XCTAssertEqual(rows.rest, "11 more skipped")
    }

    func testADocumentPreviewSaysWhoEditedItAndItsFirstLine() {
        XCTAssertTrue(DocPreview.isDocument("document"))
        XCTAssertFalse(DocPreview.isDocument("media"))
        XCTAssertEqual(DocPreview.editedLine(editor: "Dana", edited: "2h ago"), "Edited by Dana · 2h ago")
        XCTAssertEqual(DocPreview.editedLine(editor: nil, edited: "2h ago"), "Edited 2h ago")
        XCTAssertNil(DocPreview.editedLine(editor: " ", edited: nil))
        XCTAssertEqual(DocPreview.firstLine("Drafts save offline. Photos come later."), "Drafts save offline.")
        XCTAssertEqual(DocPreview.firstLine("Goals\nShip it"), "Goals")
        XCTAssertEqual(DocPreview.appSymbol("Google Sheets"), "tablecells")
        XCTAssertEqual(DocPreview.appSymbol("Notion"), "note.text")
        XCTAssertNil(DocPreview.appSymbol(nil))
    }
}
