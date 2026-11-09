import Foundation
import ImageIO
import SwiftUI

/// Pictures for cards, galleries and bubbles: decoded off the main thread, downsampled to the size
/// they are drawn at, and cached.
///
/// `UIImage(data:)` on a 12-megapixel photo decodes lazily, on the main thread, the first time the
/// picture is drawn, and again whenever the system has dropped the decoded copy: a 30–90 ms frame
/// each time a card with a photo scrolls into view (build 73 perf pass 3). A card draws a picture at
/// most ~400 pt wide, so 1200 px covers a 3× screen; the full-screen viewer asks for more.
public enum PictureDecode {
    /// Long-edge pixels for a picture drawn inside a card or bubble.
    public static let displayPixels = 1200
    /// Long-edge pixels for the full-screen viewer (zoomable).
    public static let viewerPixels = 4096

    private final class Box { let image: CGImage; init(_ image: CGImage) { self.image = image } }
    private static let cache: NSCache<NSString, Box> = {
        let cache = NSCache<NSString, Box>()
        cache.totalCostLimit = 96 << 20
        return cache
    }()

    /// A cache key from the bytes' length and both ends (cheap, unlike hashing a whole photo).
    private static func key(_ data: Data, _ pixels: Int) -> NSString {
        var hasher = Hasher()
        hasher.combine(data.count)
        hasher.combine(data.prefix(4096))
        hasher.combine(data.suffix(4096))
        return "\(hasher.finalize())-\(pixels)" as NSString
    }

    /// The picture at no more than `maxPixels` on its long edge, decoded off the main thread.
    public static func cgImage(_ data: Data, maxPixels: Int = displayPixels) async -> CGImage? {
        let key = key(data, maxPixels)
        if let hit = cache.object(forKey: key) { return hit.image }
        let image = await Task.detached(priority: .userInitiated) { decode(data, maxPixels: maxPixels) }.value
        if let image { cache.setObject(Box(image), forKey: key, cost: image.bytesPerRow * image.height) }
        return image
    }

    /// Synchronous decode (call off the main thread). Orientation applied; never upscales.
    public static func decode(_ data: Data, maxPixels: Int) -> CGImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary),
              CGImageSourceGetCount(source) > 0 else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixels,
        ]
        return CGImageSourceCreateThumbnailAtIndex(source, CGImageSourceGetPrimaryImageIndex(source), options as CFDictionary)
    }

    /// A SwiftUI image of the decoded picture (pixel size; use `.resizable()`).
    public static func image(_ data: Data, maxPixels: Int = displayPixels) async -> Image? {
        guard let cg = await cgImage(data, maxPixels: maxPixels) else { return nil }
        return Image(decorative: cg, scale: 1)
    }
}
