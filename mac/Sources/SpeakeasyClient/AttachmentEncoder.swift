import CoreGraphics
import Foundation
import ImageIO

import SpeakeasyCore
import UniformTypeIdentifiers

/// A screen capture, picture or file ready to upload.
public struct EncodedAttachment: Equatable, Sendable {
    public enum Kind: String, Sendable {
        /// JPEG, re-encoded by `AttachmentEncoder`.
        case image
        /// The file's own bytes.
        case file
    }
    public let kind: Kind
    public let data: Data
    /// "image/jpeg" for images; the file's own type (or application/octet-stream) otherwise.
    public let mimeType: String
    /// The original name; images get a .jpg extension.
    public let filename: String
    public var videoDuration: Double? = nil
    public var videoPoster: Data? = nil

    public init(kind: Kind, data: Data, mimeType: String, filename: String) {
        self.kind = kind; self.data = data; self.mimeType = mimeType; self.filename = filename
    }
}

/// Turns a capture, a pasted picture or a dropped file into an upload, by `AttachmentPolicy`.
/// Images are decoded through ImageIO's thumbnail API (orientation applied, long edge ≤ 1568 px) and
/// re-encoded as sRGB JPEG with nothing else copied over, so EXIF and GPS metadata never leave the
/// device; quality steps down toward about 300 KB. Other regular files up to 10 MB go as they are.
/// Errors are `AttachmentFailure`s. ImageIO only, so it builds for iPhone and Vision Pro too.
public enum AttachmentEncoder {
    /// Image types ImageIO can read on this system.
    public static let imageTypes: Set<String> = Set(CGImageSourceCopyTypeIdentifiers() as? [String] ?? [])

    /// A screen capture, or any decoded picture, as JPEG named `name`.jpg.
    public static func encode(_ image: CGImage, name: String,
                              longEdge: Int = AttachmentPolicy.maxLongEdge) throws -> EncodedAttachment {
        let size = AttachmentPolicy.fittedSize(CGSize(width: image.width, height: image.height), longEdge: longEdge)
        guard let prepared = opaque(image, size: size) else {
            throw AttachmentFailure.unsupported
        }
        let data = try AttachmentPolicy.fitToBudget { jpeg(prepared, quality: $0) }
        return EncodedAttachment(kind: .image, data: data, mimeType: "image/jpeg", filename: name + ".jpg")
    }

    /// Picture bytes, such as a paste. `.unsupported` when ImageIO can't read them.
    public static func encodeImage(_ data: Data, name: String,
                                   longEdge: Int = AttachmentPolicy.maxLongEdge) throws -> EncodedAttachment {
        guard let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary) else {
            throw AttachmentFailure.unsupported
        }
        return try encode(try decode(source, longEdge: longEdge), name: name, longEdge: longEdge)
    }

    /// A dropped item. Pictures are re-encoded; other regular files pass through with their name and
    /// type. Links, folders, packages and aliases are `.unsupported`; files over 10 MB `.tooLarge`.
    public static func encode(fileAt url: URL, longEdge: Int = AttachmentPolicy.maxLongEdge) throws -> EncodedAttachment {
        // Drops from other apps can be security-scoped on iPhone; a no-op for the unsandboxed Mac app.
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        guard url.isFileURL,
              let values = try? url.resourceValues(forKeys: [.isRegularFileKey, .isAliasFileKey, .fileSizeKey, .contentTypeKey])
        else { throw AttachmentFailure.unsupported }
        let kind = AttachmentPolicy.classify(url, contentType: values.contentType, isRegularFile: values.isRegularFile ?? false,
                                             isAlias: values.isAliasFile ?? false, byteCount: values.fileSize, imageTypes: imageTypes)
        switch kind {
        case .failure(let failure):
            throw failure
        case .success(.image):
            guard let source = CGImageSourceCreateWithURL(url as CFURL, [kCGImageSourceShouldCache: false] as CFDictionary) else {
                throw AttachmentFailure.unsupported
            }
            return try encode(try decode(source, longEdge: longEdge), name: url.deletingPathExtension().lastPathComponent,
                              longEdge: longEdge)
        case .success(.file):
            guard let data = try? Data(contentsOf: url) else { throw AttachmentFailure.unsupported }
            // The size was checked before reading; the file may have grown since.
            guard data.count <= AttachmentPolicy.maxFileBytes else { throw AttachmentFailure.tooLarge }
            if let type = values.contentType ?? UTType(filenameExtension: url.pathExtension), type.conforms(to: .movie) {
                return try encodeVideo(data, filename: url.lastPathComponent,
                    mimeType: type.conforms(to: .quickTimeMovie) ? "video/quicktime" : "video/mp4")
            }
            return EncodedAttachment(kind: .file, data: data,
                                     mimeType: values.contentType?.preferredMIMEType ?? "application/octet-stream",
                                     filename: url.lastPathComponent)
        }
    }

    /// Original MP4/MOV bytes with a small orientation-correct poster. No transcoding or external storage.
    public static func encodeVideo(_ data: Data, filename: String, mimeType: String) throws -> EncodedAttachment {
        let preview = try VideoAttachmentPreview.prepare(data, mimeType: mimeType)
        var result = EncodedAttachment(kind: .file, data: data, mimeType: mimeType, filename: filename)
        result.videoDuration = preview.duration
        result.videoPoster = preview.poster
        return result
    }

    /// A small preview of an encoded picture for the panel's pending row (`maxPixelSize` on the long
    /// edge; 144 covers a 36 pt thumbnail at 2× twice over). Nil for files or unreadable bytes.
    public static func thumbnail(of attachment: EncodedAttachment, maxPixelSize: Int = 144) -> CGImage? {
        if let poster = attachment.videoPoster {
            return thumbnail(of: EncodedAttachment(kind: .image, data: poster, mimeType: "image/jpeg", filename: "poster.jpg"), maxPixelSize: maxPixelSize)
        }
        guard attachment.kind == .image,
              let source = CGImageSourceCreateWithData(attachment.data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary),
              CGImageSourceGetCount(source) > 0 else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
        ]
        return CGImageSourceCreateThumbnailAtIndex(source, CGImageSourceGetPrimaryImageIndex(source), options as CFDictionary)
    }

    /// The primary image (the first frame of a GIF), oriented upright and no larger than the cap.
    private static func decode(_ source: CGImageSource, longEdge cap: Int = AttachmentPolicy.maxLongEdge) throws -> CGImage {
        guard CGImageSourceGetCount(source) > 0 else { throw AttachmentFailure.unsupported }
        let index = CGImageSourceGetPrimaryImageIndex(source)
        let properties = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [CFString: Any]
        let width = properties?[kCGImagePropertyPixelWidth] as? Int ?? 0
        let height = properties?[kCGImagePropertyPixelHeight] as? Int ?? 0
        // Ask for the image's own long edge when it's under the cap, so nothing is scaled up.
        let fitted = AttachmentPolicy.fittedSize(CGSize(width: width, height: height), longEdge: cap)
        let longEdge = fitted == .zero ? cap : Int(max(fitted.width, fitted.height))
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: longEdge,
            kCGImageSourceShouldCacheImmediately: true,
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, index, options as CFDictionary) else {
            throw AttachmentFailure.unsupported
        }
        return image
    }

    /// `image` at `size` with transparency flattened onto white (JPEG has no alpha; transparent areas
    /// would turn black). Returned unchanged when it's already opaque and the right size.
    private static func opaque(_ image: CGImage, size: CGSize) -> CGImage? {
        let hasAlpha: Bool
        switch image.alphaInfo {
        case .none, .noneSkipFirst, .noneSkipLast: hasAlpha = false
        default: hasAlpha = true
        }
        let width = Int(size.width), height = Int(size.height)
        guard hasAlpha || width != image.width || height != image.height else { return image }
        guard let space = CGColorSpace(name: CGColorSpace.sRGB),
              let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                                      space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
        let rect = CGRect(x: 0, y: 0, width: width, height: height)
        context.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
        context.fill(rect)
        context.interpolationQuality = .high
        context.draw(image, in: rect)
        return context.makeImage()
    }

    /// JPEG at `quality`, colors converted for sharing (sRGB), no metadata.
    private static func jpeg(_ image: CGImage, quality: Double) -> Data? {
        let data = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageDestinationLossyCompressionQuality: quality,
            kCGImageDestinationOptimizeColorForSharing: true,
        ]
        CGImageDestinationAddImage(destination, image, options as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { return nil }
        return data as Data
    }
}
