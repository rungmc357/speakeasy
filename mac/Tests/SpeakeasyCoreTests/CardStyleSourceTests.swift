import XCTest

/// The card look lives in one place (`Cards/CardStyle.swift`): the card views read `\.cardStyle` /
/// `CardStyle.active` and never name the brand's brass themselves, so swapping the look (Clean,
/// classic, a later theme) is one line. The views live in SpeakeasyClient, which this target
/// doesn't link, so this reads their source.
final class CardStyleSourceTests: XCTestCase {
    private let cards = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        .appendingPathComponent("../../Sources/SpeakeasyClient/Cards").standardized

    private func code(_ name: String) throws -> [(Int, String)] {
        try String(contentsOf: cards.appendingPathComponent(name), encoding: .utf8).components(separatedBy: "\n").enumerated()
            .compactMap { i, line in
                let code = line.components(separatedBy: "//").first ?? ""
                return code.trimmingCharacters(in: .whitespaces).isEmpty ? nil : (i + 1, code)
            }
    }

    func testCardViewsTakeTheirAccentFromTheCardStyle() throws {
        let files = try FileManager.default.contentsOfDirectory(atPath: cards.path).filter { $0.hasSuffix(".swift") }
        XCTAssertTrue(files.contains("CardStyle.swift"))
        for name in files where name != "CardStyle.swift" {
            for (n, line) in try code(name) {
                // The classic confirm box keeps its brass edge, drawn only when the look is flat.
                if name == "ConfirmCard.swift", line.contains("strokeBorder(JobLook.brass.opacity(0.28))") { continue }
                XCTAssertFalse(line.contains("JobLook.brass") || line.contains("BrandPalette.brass"),
                               "\(name):\(n) names brass directly instead of the card style: \(line)")
            }
        }
    }

    func testTouchUsesCleanAndTheLookIsOneLine() throws {
        let text = try String(contentsOf: cards.appendingPathComponent("CardStyle.swift"), encoding: .utf8)
        XCTAssertTrue(text.contains("static let touchDefault: CardStyle = .clean"))
        XCTAssertTrue(text.contains("static let macDefault: CardStyle = .classic"))
    }

    func testWorkCardsNeverDrawASpinnerOrPercent() throws {
        for (n, line) in try code("WorkCards.swift") {
            XCTAssertFalse(line.contains("ProgressView"), "WorkCards.swift:\(n) draws a spinner/ring: \(line)")
            XCTAssertFalse(line.contains("%"), "WorkCards.swift:\(n) draws a percent: \(line)")
        }
    }
}
