import XCTest
import UniformTypeIdentifiers
@testable import SpeakeasyCore

/// SPK-230: the composers offer videos only to a Hermes whose staging takes them.
final class VideoAttachmentGateTests: XCTestCase {
    private let composerTypes: [UTType] = [.pdf, .plainText, .image, .mpeg4Movie, .quickTimeMovie]
    private func caps(_ json: String) throws -> ServerCapabilities {
        try JSONDecoder().decode(ServerCapabilities.self, from: Data(json.utf8))
    }
    private func status(_ json: String) throws -> ServerStatus {
        try JSONDecoder().decode(ServerStatus.self, from: Data(json.utf8))
    }
    private let mov = URL(fileURLWithPath: "/tmp/porch.mov")

    func testTheCapabilityFlagHasThreeStates() throws {
        XCTAssertEqual(try caps(#"{"attachments": true, "video_attachments": true}"#).videoAttachments, true)
        XCTAssertEqual(try caps(#"{"attachments": true, "video_attachments": false}"#).videoAttachments, false)
        XCTAssertNil(try caps(#"{"attachments": true}"#).videoAttachments, "plugins 0.3.46 and 0.3.47 say nothing")
        XCTAssertNil(try caps(#"{"video_attachments": "yes"}"#).videoAttachments, "only a real Bool counts")
    }

    func testWhatEachPluginDoesWithAVideo() throws {
        XCTAssertEqual(VideoAttachmentGate(nil).hermes, .unknown, "status not loaded yet")
        XCTAssertEqual(VideoAttachmentGate(try status(#"{"version": "0.3.48", "capabilities": {"video_attachments": true}}"#)).hermes,
                       .takesVideos)
        XCTAssertEqual(VideoAttachmentGate(try status(#"{"version": "0.3.47", "capabilities": {"attachments": true}}"#)).hermes,
                       .takesVideos, "0.3.47 stages MP4 and MOV but was released before the flag")
        XCTAssertEqual(VideoAttachmentGate(try status(#"{"version": "0.3.46", "capabilities": {"attachments": true}}"#)).hermes,
                       .needsUpdate)
        XCTAssertEqual(VideoAttachmentGate(try status(#"{"capabilities": {"attachments": true}}"#)).hermes, .needsUpdate,
                       "a plugin too old to say its version")
        XCTAssertEqual(VideoAttachmentGate(try status(#"{"version": "0.3.46"}"#)).hermes, .needsUpdate)
        XCTAssertEqual(VideoAttachmentGate(try status(#"{"version": "0.3.48", "capabilities": {"video_attachments": false}}"#)).hermes,
                       .noVideos, "a plugin that says no: updating won't help")
    }

    func testTheCurrentPluginsStatusKeepsHomeAndVoiceNoteFlagsBesideVideos() throws {
        let current = try caps(#"{"attachments": true, "new_voice_notes": true, "home_chat": true, "video_attachments": true}"#)
        XCTAssertEqual(current.videoAttachments, true); XCTAssertTrue(current.newVoiceNotes); XCTAssertTrue(current.homeChat)
    }

    func testEachAdmissionReadsTheStatusItIsGiven() throws {
        // The Mac composer builds a gate per admission, so re-pairing to an older Hermes takes effect at once.
        let newer = try status(#"{"version": "0.3.47"}"#), older = try status(#"{"version": "0.3.45"}"#)
        XCTAssertNil(VideoAttachmentGate(newer).refusal(for: mov))
        XCTAssertEqual(VideoAttachmentGate(older).refusal(for: mov), VideoAttachmentGate.needsUpdate)
        XCTAssertNil(VideoAttachmentGate(older).refusal(for: URL(fileURLWithPath: "/tmp/notes.txt")),
                     "a text file beside a refused video still goes")
    }

    func testAnOlderPluginGetsNoVideoInThePickerOrDrop() {
        let gate = VideoAttachmentGate(hermes: .needsUpdate)
        XCTAssertFalse(gate.offersVideos)
        XCTAssertEqual(gate.offered(composerTypes), [.pdf, .plainText, .image])
        XCTAssertFalse(VideoAttachmentGate(nil).offersVideos, "status not loaded yet: no videos")
        XCTAssertFalse(VideoAttachmentGate(hermes: .noVideos).offersVideos)
    }

    func testAPluginWithVideosGetsThemOnce() {
        let gate = VideoAttachmentGate(hermes: .takesVideos)
        XCTAssertTrue(gate.offersVideos)
        XCTAssertEqual(gate.offered(composerTypes), [.pdf, .plainText, .image, .mpeg4Movie, .quickTimeMovie])
        XCTAssertEqual(gate.offered([.pdf]), [.pdf, .mpeg4Movie, .quickTimeMovie])
    }

    func testAnAppThatCantSendVideosSaysSoWhateverHermesTakes() {
        for hermes in [VideoAttachmentGate.Hermes.unknown, .takesVideos, .needsUpdate, .noVideos] {
            let phone = VideoAttachmentGate(hermes: hermes, appSendsVideos: false)
            XCTAssertFalse(phone.offersVideos, "\(hermes)")
            XCTAssertEqual(phone.offered(composerTypes), [.pdf, .plainText, .image], "\(hermes)")
            XCTAssertEqual(phone.refusal(for: .mpeg4Movie), VideoAttachmentGate.cantSendHere,
                           "\(hermes): no update line, and no chip the encoder would fail")
            XCTAssertEqual(phone.refusal(for: mov), VideoAttachmentGate.cantSendHere, "\(hermes)")
            XCTAssertNil(phone.refusal(for: .pdf), "\(hermes)")
        }
    }

    func testAVideoForAnOlderPluginSaysToUpdateBeforeAnyChip() {
        let gate = VideoAttachmentGate(hermes: .needsUpdate)
        let line = "Update Speakeasy on your Hermes to send videos."
        XCTAssertEqual(gate.refusal(for: .mpeg4Movie), line)
        XCTAssertEqual(gate.refusal(for: .quickTimeMovie), line)
        XCTAssertEqual(gate.refusal(for: URL(fileURLWithPath: "/Users/me/Movies/porch-4s.mp4")), line)
        XCTAssertEqual(gate.refusal(for: URL(fileURLWithPath: "/Users/me/Movies/IMG_0042.MOV")), line)
        XCTAssertEqual(gate.refusal(for: URL(fileURLWithPath: "/tmp/clip"), contentType: .mpeg4Movie), line,
                       "the file's own type wins over a missing extension")
        for url in ["lease.pdf", "notes.txt", "porch.jpg", "voice.m4a"].map({ URL(fileURLWithPath: "/tmp/" + $0) }) {
            XCTAssertNil(gate.refusal(for: url), url.lastPathComponent)
        }
    }

    func testOtherStatesSayWhatIsTrue() {
        XCTAssertEqual(VideoAttachmentGate(nil).refusal(for: mov), VideoAttachmentGate.notLoaded, "never the update line")
        XCTAssertEqual(VideoAttachmentGate(hermes: .noVideos).refusal(for: mov), VideoAttachmentGate.cantSendHere)
    }

    func testAPluginWithVideosTakesOnlyTheFormatsStagingTakes() {
        let gate = VideoAttachmentGate(hermes: .takesVideos)
        for name in ["porch.mp4", "IMG_0042.MOV", "clip.m4v"] {
            XCTAssertNil(gate.refusal(for: URL(fileURLWithPath: "/tmp/" + name)), name)
        }
        for name in ["clip.avi", "clip.webm"] {
            XCTAssertEqual(gate.refusal(for: URL(fileURLWithPath: "/tmp/" + name)), VideoAttachmentGate.onlyMP4OrMOV, name)
        }
    }

    func testTheLinesArePlainWords() {
        for line in [VideoAttachmentGate.needsUpdate, VideoAttachmentGate.cantSendHere, VideoAttachmentGate.onlyMP4OrMOV] {
            XCTAssertFalse(line.contains("415")); XCTAssertFalse(line.lowercased().contains("error"))
            XCTAssertFalse(line.contains("video_attachments")); XCTAssertFalse(line.contains("PDF"))
        }
        XCTAssertTrue(VideoAttachmentGate.needsUpdate.hasPrefix("Update Speakeasy on your Hermes to "),
                      "the same wording as the other update lines (ScreenSharing, CodexLogin)")
    }

    func testTheCallPanelDropStillTakesAVideoAsAPlainFile() {
        // Not gated: the call's own upload takes any regular file, the same as build 69.
        let imageTypes: Set<String> = [UTType.png.identifier, UTType.jpeg.identifier, UTType.heic.identifier]
        XCTAssertEqual(AttachmentPolicy.classify(URL(fileURLWithPath: "/Users/me/Movies/porch-4s.mp4"),
                                                 byteCount: 1_800_000, imageTypes: imageTypes), .success(.file))
    }
}
