import XCTest
@testable import SpeakeasyCore

/// SPK-536: a long message you sent in Home never stops mid-word without a sign that more follows.
final class LongMessageTests: XCTestCase {
    private let long = "Please go through the checkout tests in the shop project and find out why the payment step fails on every second run. Look at the recent changes to the cart service, compare the logs from the last three nightly runs, and write down what is different between a passing and a failing run. After that, suggest a fix, apply it on a separate branch, and verify correct testing procedure before you tell me it is done."

    func testAFourHundredCharacterMessageShowsWhole() {
        XCTAssertGreaterThan(long.count, 400)
        XCTAssertNil(LongMessage.preview(long))
    }

    func testAVeryLongMessageEndsOnAWholeWordWithAnEllipsis() throws {
        let text = long + " " + long
        let preview = try XCTUnwrap(LongMessage.preview(text))
        XCTAssertTrue(preview.hasSuffix("\u{2026}"))
        let shown = String(preview.dropLast())
        XCTAssertTrue(text.hasPrefix(shown))
        XCTAssertLessThanOrEqual(shown.count, LongMessage.previewChars)
        // The cut lands between words: what follows in the full text is a space.
        let after = text[text.index(text.startIndex, offsetBy: shown.count)]
        XCTAssertTrue(after.isWhitespace, "cut mid-word: \(shown.suffix(20))")
        XCTAssertFalse(shown.last.map { $0.isWhitespace || $0 == "," } ?? true)
    }

    func testCutsBeforeAWordThatWouldBeSplit() throws {
        // "procedure" straddles the preview limit: the preview stops before it, never at "proced".
        let preview = try XCTUnwrap(LongMessage.preview("aaaa verify correct testing procedure now", collapseOver: 10, previewChars: 34))
        XCTAssertEqual(preview, "aaaa verify correct testing\u{2026}")
    }

    func testOneEndlessWordStillGetsAnEllipsis() throws {
        let preview = try XCTUnwrap(LongMessage.preview(String(repeating: "x", count: 900)))
        XCTAssertEqual(preview.count, LongMessage.previewChars + 1)
        XCTAssertTrue(preview.hasSuffix("\u{2026}"))
    }
}
