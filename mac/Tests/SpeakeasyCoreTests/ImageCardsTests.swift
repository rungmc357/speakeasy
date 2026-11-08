import XCTest
import CoreGraphics
@testable import SpeakeasyCore

final class ImageCardsTests: XCTestCase {
    func testImageCardsDecodeWithServerCardNumbers() {
        let info = WorkInfo(json: ["run_id": "run_1", "status": "completed", "result": [
            "full": "Here is concept 6.",
            "cards": [
                ["kind": "product", "name": "KEF", "url": "https://example.com/kef"],
                ["kind": "image", "name": "voxel-flame-concept-06.png"],
                ["kind": "image", "name": "  "],
            ]]])
        XCTAssertEqual(info?.products.map(\.number), [1])
        // A file's name is never the words shown for a picture.
        XCTAssertEqual(info?.images, [ImageCard(number: 2, name: "Image 2"),
                                      ImageCard(number: 3, name: "Image 3")])
    }

    func testCaptionsAreWordsAndAMissingFileDrawsNoTile() {
        let info = WorkInfo(json: ["run_id": "run_1", "status": "completed", "result": [
            "full": "Three looks for the bakery page.",
            "cards": [
                ["kind": "image", "name": "bakery-landing-v1.png", "caption": "Warm landing page"],
                ["kind": "image", "name": "IMG_2231.HEIC", "available": false],
                ["kind": "image", "name": "Menu board, chalk style"],
                ["kind": "image", "caption": "/tmp/shots/sheet.png"],
            ]]])
        // Card 2's file is gone: no tile, and every other picture keeps its server number.
        XCTAssertEqual(info?.images, [ImageCard(number: 1, name: "Warm landing page"),
                                      ImageCard(number: 3, name: "Menu board, chalk style"),
                                      ImageCard(number: 4, name: "Image 4")])
        XCTAssertTrue(ImageCard.looksLikeFileName("contact-sheet_v3.JPG"))
        XCTAssertFalse(ImageCard.looksLikeFileName("Sunset over the pier"))
        XCTAssertFalse(ImageCard.looksLikeFileName("v2.5 of the logo"))
    }

    func testResultWithOnlyImagesIsKeptAndNeverBecomesAProduct() {
        let info = WorkInfo(json: ["status": "completed", "result": ["cards": [
            ["kind": "image", "name": "a.png", "url": "https://example.com/a", "path": "/Users/x/a.png"]]]])
        XCTAssertNotNil(info?.result)
        XCTAssertEqual(info?.images.count, 1)
        XCTAssertTrue(info?.products.isEmpty ?? false)
    }

    func testIgnoresCardsBeyondTheRouteLimitAndOtherKinds() {
        let cards: [[String: Any]] = (1...10).map { ["kind": "image", "name": "\($0).png"] } + [["kind": "video", "name": "v"]]
        let info = WorkInfo(json: ["status": "completed", "result": ["full": "x", "cards": cards]])
        XCTAssertEqual(info?.images.map(\.number), Array(1...8))
        XCTAssertNil(ImageCard(json: ["kind": "image", "name": "x"], number: 9))
        XCTAssertNil(ImageCard(json: ["kind": "product", "name": "x"], number: 1))
    }

    func testPreviewSizeFitsScreenAndKeepsAspect() {
        XCTAssertEqual(imagePreviewSize(for: CGSize(width: 800, height: 600), screen: CGSize(width: 1920, height: 1080)),
                       CGSize(width: 800, height: 600))
        let big = imagePreviewSize(for: CGSize(width: 4000, height: 2000), screen: CGSize(width: 2000, height: 1000))
        XCTAssertEqual(big, CGSize(width: 1700, height: 850))
        XCTAssertEqual(imagePreviewSize(for: CGSize(width: 32, height: 32), screen: nil), CGSize(width: 240, height: 240))
        XCTAssertEqual(imagePreviewSize(for: .zero, screen: nil), CGSize(width: 480, height: 360))
    }
}
