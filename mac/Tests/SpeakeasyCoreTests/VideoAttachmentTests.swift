import XCTest
import Foundation
import ImageIO
import SpeakeasyCore


/// Real invented MP4/MOV files generated locally; no external service or model calls.
final class VideoAttachmentTests: XCTestCase {
    private func clip(_ ext: String, rotate: Bool = false) throws -> URL {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("video-test-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: directory) }
        let base = directory.appendingPathComponent("source." + ext)
        try ffmpeg(["-f", "lavfi", "-i", "testsrc2=size=320x180:rate=4:duration=2", "-c:v", "libx264",
                    "-threads", "1", "-pix_fmt", "yuv420p", base.path])
        if !rotate { return base }
        let rotated = directory.appendingPathComponent("rotated." + ext)
        try ffmpeg(["-display_rotation", "90", "-i", base.path, "-c", "copy", rotated.path])
        return rotated
    }

    private func ffmpeg(_ args: [String]) throws {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/opt/homebrew/bin/ffmpeg")
        process.arguments = ["-nostdin", "-v", "error"] + args
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        try process.run(); process.waitUntilExit()
        XCTAssertEqual(process.terminationStatus, 0)
    }

    func testMP4KeepsOriginalBytesAndCarriesDurationAndPoster() throws {
        let url = try clip("mp4")
        let original = try Data(contentsOf: url)
        let preview = try VideoAttachmentPreview.prepare(original, mimeType: "video/mp4")
        XCTAssertEqual(original, try Data(contentsOf: url))
        XCTAssertEqual(preview.duration, 2, accuracy: 0.1)
        XCTAssertNotNil(CGImageSourceCreateWithData(preview.poster as CFData, nil))
        XCTAssertLessThan(preview.poster.count, 200_000)
    }

    func testMOVKeepsQuickTimeMIME() throws {
        let preview = try VideoAttachmentPreview.prepare(Data(contentsOf: clip("mov")), mimeType: "video/quicktime")
        XCTAssertGreaterThan(preview.duration, 0)
        XCTAssertFalse(preview.poster.isEmpty)
    }

    func testRotationMetadataProducesUprightPoster() throws {
        let preview = try VideoAttachmentPreview.prepare(Data(contentsOf: clip("mov", rotate: true)), mimeType: "video/quicktime")
        let source = try XCTUnwrap(CGImageSourceCreateWithData(preview.poster as CFData, nil))
        let poster = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        XCTAssertGreaterThan(poster.height, poster.width)
    }

    func testCorruptAndLargeVideosAreNotAcceptedAsPosters() throws {
        XCTAssertThrowsError(try VideoAttachmentPreview.prepare(Data("invalid".utf8), mimeType: "video/mp4"))
        XCTAssertThrowsError(try VideoAttachmentPreview.prepare(Data(count: AttachmentPolicy.maxFileBytes + 1), mimeType: "video/mp4")) {
            XCTAssertEqual($0 as? AttachmentFailure, .tooLarge)
        }
    }
}
