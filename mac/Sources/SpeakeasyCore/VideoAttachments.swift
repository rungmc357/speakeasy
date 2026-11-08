import Foundation
import UniformTypeIdentifiers

/// Whether a typed-message composer (Home, a conversation, New chat; Mac and iPhone/iPad) offers
/// videos, and what it says when one can't go (SPK-230). Videos go only to a Hermes whose plugin says
/// `video_attachments`: older ones refuse a staged video at upload ("files must be PDF or plain
/// text"), which left a chip whose Retry could never work and a Send that stayed off. Pure: the
/// composers ask it what their picker and drop take, and check each item before making a chip.
///
/// The call panel's drop isn't a composer: a video dropped there still goes as a plain file through
/// the call's own upload, as it always has (`AttachmentPolicy.classify`).
public struct VideoAttachmentGate: Equatable, Sendable {
    /// The paired Hermes takes staged videos (capability `video_attachments`).
    public var hermesTakesVideos: Bool
    /// This app can stage a video (the Mac sends the file as it is; the iPhone can't yet).
    public var appSendsVideos: Bool

    public init(hermesTakesVideos: Bool, appSendsVideos: Bool = true) {
        self.hermesTakesVideos = hermesTakesVideos; self.appSendsVideos = appSendsVideos
    }

    /// From `GET /voice/status` `capabilities`; nil (not loaded yet, or an older plugin) offers no videos.
    /// The iPhone app decodes its own capabilities type, so it uses `init(hermesTakesVideos:appSendsVideos:)`.
    public init(_ capabilities: ServerCapabilities?, appSendsVideos: Bool = true) {
        self.init(hermesTakesVideos: capabilities?.videoAttachments == true, appSendsVideos: appSendsVideos)
    }

    /// What the composer says instead of adding a video this Hermes can't take.
    public static let needsUpdate = "Sending videos needs a newer Speakeasy on your Hermes. Update it from Settings."

    /// The video types a composer's picker and drop add when videos can go.
    public static let videoTypes: [UTType] = [.mpeg4Movie, .quickTimeMovie]

    /// The picker and drop take videos.
    public var offersVideos: Bool { hermesTakesVideos && appSendsVideos }

    /// `base` with the video types when videos can go, and with no video type at all otherwise.
    public func offered(_ base: [UTType]) -> [UTType] {
        let others = base.filter { !Self.isVideo($0) }
        return offersVideos ? others + Self.videoTypes : others
    }

    public static func isVideo(_ type: UTType?) -> Bool { type?.conforms(to: .movie) == true }

    /// By the file's own type, else its extension.
    public static func isVideo(_ url: URL, contentType: UTType? = nil) -> Bool {
        isVideo(contentType ?? UTType(filenameExtension: url.pathExtension))
    }

    public static func isVideo(mimeType: String) -> Bool { isVideo(UTType(mimeType: mimeType)) }

    /// The line to show instead of making a chip for an item of `type`; nil = go ahead. Only a Hermes
    /// without videos gets the update line: when it takes them but this app can't send one, the
    /// composer's own encoder refuses the item with its usual line.
    public func refusal(for type: UTType?) -> String? {
        Self.isVideo(type) && !hermesTakesVideos ? Self.needsUpdate : nil
    }

    public func refusal(for url: URL, contentType: UTType? = nil) -> String? {
        Self.isVideo(url, contentType: contentType) && !hermesTakesVideos ? Self.needsUpdate : nil
    }

    /// A staged video the plugin refused as `unsupported` (one that got past the gate before status
    /// loaded): the update line instead of the plugin's words. Retrying can't help, so the chip
    /// should offer only removal. Nil for anything else.
    public func uploadRefusal(mimeType: String, reason: String?) -> String? {
        reason == "unsupported" && Self.isVideo(mimeType: mimeType) && !hermesTakesVideos ? Self.needsUpdate : nil
    }
}
