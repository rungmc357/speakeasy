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

    /// SPK-187: an answer with prose, a long shell one-liner and a second block. Copy takes exactly
    /// each block's contents (no fences, no language tag, no prose), and the folded preview is prose only.
    func testCopyTakesExactlyEachBlock() {
        let line = "mkdir -p ~/.ssh && echo 'ssh-ed25519 AAAAexampleexample helper@build-box' >> ~/.ssh/authorized_keys"
        let text = "Paste this on the build box:\n\n```bash\n\(line)\n```\n\nThen restart it:\n```\nsudo launchctl kickstart -k system/com.example.sync\n```"
        XCTAssertEqual(MessageBlock.code(in: text), [line, "sudo launchctl kickstart -k system/com.example.sync"])
        XCTAssertEqual(MessageBlock.prose(in: text), "Paste this on the build box:\n\nThen restart it:")
        XCTAssertEqual(MessageBlock.code(in: "No code here."), [])
    }
}
