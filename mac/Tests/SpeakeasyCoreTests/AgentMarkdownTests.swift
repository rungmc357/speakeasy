import XCTest
@testable import SpeakeasyCore

final class AgentMarkdownTests: XCTestCase {
    private func bold(_ a: AttributedString) -> [String] {
        a.runs.compactMap { run in
            run.inlinePresentationIntent?.contains(.stronglyEmphasized) == true ? String(a[run.range].characters) : nil
        }
    }
    private func text(_ a: AttributedString) -> String { String(a.characters) }

    func testBoldRendersAndNoAsterisksReachTheScreen() {
        let a = AgentMarkdown.attributed("**Done and verified:** the fix is live, and **$360k to you**.")
        XCTAssertEqual(text(a), "Done and verified: the fix is live, and $360k to you.")
        XCTAssertEqual(bold(a), ["Done and verified:", "$360k to you"])
    }

    func testBoldTheStrictParserRefusesStillRenders() {
        let spaced = AgentMarkdown.attributed("** spaced bold ** here")
        XCTAssertEqual(text(spaced), "spaced bold here")
        XCTAssertEqual(bold(spaced), ["spaced bold"])
        let glued = AgentMarkdown.attributed("**Status:**ready")
        XCTAssertEqual(text(glued), "Status: ready")
        XCTAssertEqual(bold(glued), ["Status:"])
    }

    func testHeadingsBulletsQuotesAndRules() {
        let a = AgentMarkdown.attributed("## What changed\n- **Mac:** fixed\n* iPhone too\n  - nested\n> quoted\n---\n1. first")
        XCTAssertEqual(text(a), "What changed\n• Mac: fixed\n• iPhone too\n  • nested\nquoted\n\n1. first")
        XCTAssertEqual(bold(a), ["What changed", "Mac:"])
    }

    func testItalicCodeAndLinks() {
        let a = AgentMarkdown.attributed("*soft* and `code **x**` and [the repo](https://github.com/example/repo)")
        XCTAssertEqual(text(a), "soft and code **x** and the repo")  // marks inside code stay literal
        XCTAssertTrue(a.runs.contains { $0.link?.absoluteString == "https://github.com/example/repo" })
    }

    func testBareURLsLinkAndUnsafeSchemesDont() {
        let a = AgentMarkdown.attributed("See https://example.com/page. Or [this](file:///etc/passwd).")
        XCTAssertTrue(a.runs.contains { $0.link?.absoluteString == "https://example.com/page" })
        XCTAssertFalse(a.runs.contains { $0.link?.scheme == "file" })
        XCTAssertEqual(text(a), "See https://example.com/page. Or this.")
    }

    func testStreamingNeverFlashesAnOpenMark() {
        XCTAssertEqual(text(AgentMarkdown.attributed("Checked it: **Done and ver", streaming: true)), "Checked it: Done and ver")
        XCTAssertEqual(text(AgentMarkdown.attributed("Run `swift te", streaming: true)), "Run swift te")
        XCTAssertEqual(text(AgentMarkdown.attributed("Open [the repo](https://git", streaming: true)), "Open the repo")
        // Once the close arrives it is bold.
        XCTAssertEqual(bold(AgentMarkdown.attributed("**Done and verified:** yes", streaming: true)), ["Done and verified:"])
    }

    func testPlainForPreviewsAndNotifications() {
        XCTAssertEqual(AgentMarkdown.plain("## Result\n**Shipped:** v2 — see [notes](https://example.com)"),
                       "Result\nShipped: v2 — see notes")
    }

    func testOrdinaryTextIsUntouched() {
        let s = "Rate is 2*3 = 6 and a_b_c stays, 4.5★ rating."
        XCTAssertEqual(text(AgentMarkdown.attributed(s)), s)
    }
}
