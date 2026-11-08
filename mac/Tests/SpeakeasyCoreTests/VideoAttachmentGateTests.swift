import XCTest
import UniformTypeIdentifiers
@testable import SpeakeasyCore

/// SPK-230: the composers offer videos only to a Hermes whose plugin says `video_attachments`.
final class VideoAttachmentGateTests: XCTestCase {
    private let composerTypes: [UTType] = [.pdf, .plainText, .image, .mpeg4Movie, .quickTimeMovie]
    private func caps(_ json: String) throws -> ServerCapabilities {
        try JSONDecoder().decode(ServerCapabilities.self, from: Data(json.utf8))
    }

    func testTheCapabilityFlag() throws {
        XCTAssertTrue(try caps(#"{"attachments": true, "video_attachments": true}"#).videoAttachments)
        XCTAssertFalse(try caps(#"{"attachments": true, "video_attachments": false}"#).videoAttachments)
        XCTAssertFalse(try caps(#"{"attachments": true}"#).videoAttachments, "plugin 0.3.46 (build 69's) says nothing")
        XCTAssertFalse(try caps(#"{"video_attachments": "yes"}"#).videoAttachments, "only a real true counts")
    }

    func testAnOlderPluginGetsNoVideoInThePickerOrDrop() throws {
        let gate = VideoAttachmentGate(try caps(#"{"attachments": true}"#))
        XCTAssertFalse(gate.offersVideos)
        XCTAssertEqual(gate.offered(composerTypes), [.pdf, .plainText, .image])
        XCTAssertFalse(VideoAttachmentGate(nil).offersVideos, "status not loaded yet: no videos")
    }

    func testAPluginWithVideosGetsThemOnce() throws {
        let gate = VideoAttachmentGate(try caps(#"{"video_attachments": true}"#))
        XCTAssertTrue(gate.offersVideos)
        XCTAssertEqual(gate.offered(composerTypes), [.pdf, .plainText, .image, .mpeg4Movie, .quickTimeMovie])
        XCTAssertEqual(gate.offered([.pdf]), [.pdf, .mpeg4Movie, .quickTimeMovie])
    }

    func testAnAppThatCantSendVideosOffersNoneEvenToANewPlugin() {
        let phone = VideoAttachmentGate(hermesTakesVideos: true, appSendsVideos: false)
        XCTAssertFalse(phone.offersVideos)
        XCTAssertEqual(phone.offered(composerTypes), [.pdf, .plainText, .image])
        XCTAssertNil(phone.refusal(for: .mpeg4Movie), "its own encoder says what it sends; no update line")
    }

    func testAVideoForAnOlderPluginSaysToUpdateBeforeAnyChip() {
        let gate = VideoAttachmentGate(hermesTakesVideos: false)
        let line = "Sending videos needs a newer Speakeasy on your Hermes. Update it from Settings."
        XCTAssertEqual(gate.refusal(for: .mpeg4Movie), line)
        XCTAssertEqual(gate.refusal(for: .quickTimeMovie), line)
        XCTAssertEqual(gate.refusal(for: URL(fileURLWithPath: "/Users/me/Movies/porch-4s.mp4")), line)
        XCTAssertEqual(gate.refusal(for: URL(fileURLWithPath: "/Users/me/Movies/IMG_0042.MOV")), line)
        XCTAssertEqual(gate.refusal(for: URL(fileURLWithPath: "/tmp/clip"), contentType: .mpeg4Movie), line,
                       "the file's own type wins over a missing extension")
        for url in ["lease.pdf", "notes.txt", "porch.jpg", "voice.m4a"].map({ URL(fileURLWithPath: "/tmp/" + $0) }) {
            XCTAssertNil(gate.refusal(for: url), url.lastPathComponent)
        }
        XCTAssertNil(VideoAttachmentGate(hermesTakesVideos: true).refusal(for: .mpeg4Movie))
    }

    func testTheLineIsPlainWords() {
        let line = VideoAttachmentGate.needsUpdate
        XCTAssertFalse(line.contains("415")); XCTAssertFalse(line.lowercased().contains("error"))
        XCTAssertFalse(line.contains("video_attachments")); XCTAssertFalse(line.contains("PDF"))
    }

    func testARefusedVideoUploadReadsAsTheUpdateLine() {
        let old = VideoAttachmentGate(hermesTakesVideos: false)
        XCTAssertEqual(old.uploadRefusal(mimeType: "video/mp4", reason: "unsupported"), VideoAttachmentGate.needsUpdate)
        XCTAssertEqual(old.uploadRefusal(mimeType: "video/quicktime", reason: "unsupported"), VideoAttachmentGate.needsUpdate)
        XCTAssertNil(old.uploadRefusal(mimeType: "video/mp4", reason: "too_large"), "a size refusal keeps its own line")
        XCTAssertNil(old.uploadRefusal(mimeType: "application/zip", reason: "unsupported"))
        XCTAssertNil(VideoAttachmentGate(hermesTakesVideos: true).uploadRefusal(mimeType: "video/mp4", reason: "unsupported"))
    }

    func testTheCallPanelDropStillTakesAVideoAsAPlainFile() {
        // Not gated: the call's own upload takes any regular file, the same as build 69.
        let imageTypes: Set<String> = [UTType.png.identifier, UTType.jpeg.identifier, UTType.heic.identifier]
        XCTAssertEqual(AttachmentPolicy.classify(URL(fileURLWithPath: "/Users/me/Movies/porch-4s.mp4"),
                                                 byteCount: 1_800_000, imageTypes: imageTypes), .success(.file))
    }
}
