import XCTest
@testable import SpeakeasyCore

final class MessageBlockTests: XCTestCase {
    func testPlainTextIsOneBlock() {
        XCTAssertEqual(MessageBlock.split("Three strips fit the 6 ft bench."), [.text("Three strips fit the 6 ft bench.")])
        XCTAssertEqual(MessageBlock.split(""), [])
    }

    func testFencedCodeIsSplitOutWithItsLanguage() {
        let text = "Run this:\n\n```bash\nhermes plugins update speakeasy\n  --yes\n```\nThen restart."
        XCTAssertEqual(MessageBlock.split(text), [
            .text("Run this:"),
            .code("hermes plugins update speakeasy\n  --yes", language: "bash"),
            .text("Then restart."),
        ])
    }

    func testAnUnclosedFenceStaysCode() {
        XCTAssertEqual(MessageBlock.split("Here:\n```\nlet x = 1"), [.text("Here:"), .code("let x = 1", language: nil)])
    }
}
