import XCTest

/// "Needs you" has one look on every surface (`NeedsYouMark`: brass raised hand + "Needs you"),
/// and no status in the shared views is drawn in system orange (off the brand's palette). The
/// views live in SpeakeasyClient, which this test target doesn't link, so this reads their source.
final class NeedsYouLookTests: XCTestCase {
    private let sources = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .appendingPathComponent("../../Sources").standardized

    private func swiftFiles(_ sub: String) throws -> [URL] {
        let dir = sources.appendingPathComponent(sub)
        let all = FileManager.default.enumerator(at: dir, includingPropertiesForKeys: nil)?.allObjects as? [URL] ?? []
        return all.filter { $0.pathExtension == "swift" }
    }

    /// Lines with code (comments dropped), so an explanatory comment never trips the check.
    private func codeLines(_ url: URL) throws -> [(Int, String)] {
        try String(contentsOf: url, encoding: .utf8).components(separatedBy: "\n").enumerated()
            .compactMap { i, line in
                let code = line.components(separatedBy: "//").first ?? ""
                return code.trimmingCharacters(in: .whitespaces).isEmpty ? nil : (i + 1, code)
            }
    }

    func testQuestionAndDecisionCardsUseTheSharedMarkNotOrange() throws {
        for name in ["Cards/QuestionCard.swift", "Jobs/JobViews.swift"] {
            let url = sources.appendingPathComponent("SpeakeasyClient/\(name)")
            for (n, code) in try codeLines(url) {
                XCTAssertFalse(code.contains(".orange"), "\(name):\(n) draws a status in system orange: \(code)")
                XCTAssertFalse(code.contains("Text(answer == nil ? \"Needs you\""),
                               "\(name):\(n) draws its own Needs you label instead of NeedsYouMark")
            }
            let text = try String(contentsOf: url, encoding: .utf8)
            XCTAssertTrue(text.contains("NeedsYouMark()"), "\(name) shows the shared Needs you mark")
            XCTAssertTrue(text.contains("needsYouShownAbove"), "\(name) leaves its mark out when the page already says it")
        }
    }

    func testTheMarkIsTheBrassHandAndCaptionWords() throws {
        let url = sources.appendingPathComponent("SpeakeasyClient/Jobs/NeedsYouMark.swift")
        let text = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(text.contains("\"hand.raised.fill\""))
        XCTAssertTrue(text.contains("\"Needs you\""))
        XCTAssertTrue(text.contains(".caption.weight(.semibold)"))
        XCTAssertTrue(text.contains("BrandPalette.amber"))
        XCTAssertFalse(text.contains("orange"))
    }

    /// The Mac board and session list carry the same mark: no white-on-red "Needs you" capsule
    /// (a red capsule is only for a count badge).
    func testMacBoardAndListUseTheMark() throws {
        for url in try swiftFiles("Speakeasy/History") {
            for (n, code) in try codeLines(url) where code.contains("Text(\"Needs you\")") {
                XCTFail("\(url.lastPathComponent):\(n) draws its own Needs you label: \(code)")
            }
        }
        let board = try String(contentsOf: sources.appendingPathComponent("Speakeasy/History/BoardViews.swift"), encoding: .utf8)
        XCTAssertTrue(board.contains("if card.needsYou { NeedsYouMark() }"))
    }
}
