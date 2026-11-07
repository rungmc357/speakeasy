import Foundation
import AVFoundation
import ImageIO
import UniformTypeIdentifiers

/// Bounded, local video metadata shared with the lightweight Share extension (no WebRTC).
public enum VideoAttachmentPreview {
    public static func prepare(_ data: Data, mimeType: String) throws -> (duration: Double, poster: Data) {
        guard ["video/mp4", "video/quicktime"].contains(mimeType), !data.isEmpty else { throw AttachmentFailure.unsupported }
        guard data.count <= AttachmentPolicy.maxFileBytes else { throw AttachmentFailure.tooLarge }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("video-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let url = directory.appendingPathComponent(mimeType == "video/quicktime" ? "clip.mov" : "clip.mp4")
        try data.write(to: url, options: .atomic)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
        let asset = AVURLAsset(url: url)
        let duration = CMTimeGetSeconds(asset.duration)
        guard duration.isFinite, duration > 0 else { throw AttachmentFailure.unsupported }
        guard duration <= 120 else { throw AttachmentFailure.tooLarge }
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 480, height: 480)
        let poster = try generator.copyCGImage(at: .zero, actualTime: nil)
        let output = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(output, UTType.jpeg.identifier as CFString, 1, nil) else {
            throw AttachmentFailure.unsupported
        }
        CGImageDestinationAddImage(destination, poster, [kCGImageDestinationLossyCompressionQuality: 0.75] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { throw AttachmentFailure.unsupported }
        return (duration, output as Data)
    }
}
