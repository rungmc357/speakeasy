import Foundation
import UniformTypeIdentifiers

/// Whether a typed-message composer (Home, a conversation, New chat; Mac and iPhone/iPad) offers
/// videos, and what it says when one can't go (SPK-230). Plugins before 0.3.47 refuse a staged video
/// at upload ("files must be PDF or plain text"), which left a chip whose Retry could never work and a
/// Send that stayed off. Pure: the composers ask it what their picker and drop take, and call
/// `refusal(for:)` on every item before making a chip. The picker filter alone isn't enough: a base
/// list with a supertype (`.item`, `.data`, `.content`) still lets a video through, and drops and
/// pastes don't go through the picker at all.
///
/// The call panel's drop isn't a composer: a video dropped there still goes as a plain file through
/// the call's own upload, as it always has (`AttachmentPolicy.classify`).
public struct VideoAttachmentGate: Equatable, Sendable {
    /// What the paired Hermes does with a staged video.
    public enum Hermes: Equatable, Sendable {
        /// Status hasn't loaded, so nothing is known about the plugin yet.
        case unknown
        /// Staging takes MP4 and QuickTime: `video_attachments` is true, or a 0.3.47 plugin (the
        /// first to stage videos, released before the flag) doesn't list it.
        case takesVideos
        /// A plugin before 0.3.47: staging refuses videos and a released update takes them.
        case needsUpdate
        /// The plugin says `video_attachments: false`: updating won't help.
        case noVideos
    }

    public var hermes: Hermes
    /// This app can stage a video (the Mac sends the file as it is).
    public var appSendsVideos: Bool

    public init(hermes: Hermes, appSendsVideos: Bool = true) {
        self.hermes = hermes; self.appSendsVideos = appSendsVideos
    }

    /// From `GET /voice/status`; nil = not loaded yet.
    public init(_ status: ServerStatus?, appSendsVideos: Bool = true) {
        self.init(hermes: status.map { Self.hermes(videoAttachments: $0.capabilities?.videoAttachments, version: $0.version) } ?? .unknown,
                  appSendsVideos: appSendsVideos)
    }

    /// The first plugin whose staging takes videos (SPK-220). It doesn't list `video_attachments`.
    public static let firstVideoPlugin = "0.3.47"

    /// From a loaded status's `capabilities.video_attachments` (nil when absent) and `version`. Apps
    /// that decode their own status type (the iPhone) call this with those two fields.
    public static func hermes(videoAttachments: Bool?, version: String?) -> Hermes {
        if let videoAttachments { return videoAttachments ? .takesVideos : .noVideos }
        guard let version, !UpdateCheck.isNewer(firstVideoPlugin, than: version) else { return .needsUpdate }
        return .takesVideos
    }

    /// An older plugin: a newer one that takes videos is out, so Settings offers it.
    public static let needsUpdate = "Update Speakeasy on your Hermes to send videos."
    /// This app or this Hermes can't send videos, and no update changes that today.
    public static let cantSendHere = "Videos can't be sent here."
    /// A video in a format staging refuses (AVI, WebM, …).
    public static let onlyMP4OrMOV = "Only MP4 and MOV videos can be sent."
    /// Status hasn't loaded: the same words as any other unreachable Hermes.
    public static let notLoaded = "Couldn't reach your Hermes. Check the connection and try again."

    /// The video types a composer's picker and drop add when videos can go.
    public static let videoTypes: [UTType] = [.mpeg4Movie, .quickTimeMovie]

    /// The picker and drop take videos.
    public var offersVideos: Bool { hermes == .takesVideos && appSendsVideos }

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

    /// The line to show instead of making a chip for an item of `type`; nil = go ahead. The app's own
    /// limit comes first: a phone that can't stage a video never says to update Hermes.
    public func refusal(for type: UTType?) -> String? {
        guard Self.isVideo(type), let type else { return nil }
        guard appSendsVideos else { return Self.cantSendHere }
        switch hermes {
        case .unknown: return Self.notLoaded
        case .needsUpdate: return Self.needsUpdate
        case .noVideos: return Self.cantSendHere
        case .takesVideos: return Self.videoTypes.contains(where: type.conforms(to:)) ? nil : Self.onlyMP4OrMOV
        }
    }

    public func refusal(for url: URL, contentType: UTType? = nil) -> String? {
        refusal(for: contentType ?? UTType(filenameExtension: url.pathExtension))
    }
}
