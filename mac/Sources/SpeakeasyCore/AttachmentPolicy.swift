import CoreGraphics
import Foundation
import UniformTypeIdentifiers

/// Why a screen capture or a dropped item can't go to Hermes. Raw values are the reason strings
/// the plugin knows (`capture-status: failed` and upload refusals); they pick the spoken line.
public enum AttachmentFailure: String, Error, Equatable, Sendable, CaseIterable {
    /// Screen Recording isn't allowed for Speakeasy.
    case permission
    /// The frontmost app has no window worth capturing.
    case noWindow = "no_window"
    /// One of Speakeasy's own regular windows (Settings, image preview, onboarding) is active.
    case speakeasyWindow = "speakeasy_window"
    /// A password manager is frontmost.
    case passwordManager = "password_manager"
    /// The frontmost app holds secure keyboard input (a password field, Secure Keyboard Entry).
    case secureInput = "secure_input"
    /// The capture came back near-uniform (protected content, a locked screen).
    case blank
    /// Over the size caps even after re-encoding.
    case tooLarge = "too_large"
    /// The capture didn't finish in time.
    case timeout
    /// Not something that can be attached (a folder, an app bundle, an alias, a link).
    case unsupported
}

/// What a dropped or pasted item is sent as.
public enum AttachmentItemKind: Equatable, Sendable {
    /// Re-encoded and sent to Hermes as an image.
    case image
    /// Sent as-is and saved on the Hermes machine, which opens it with its own tools.
    case file
}

/// An on-screen window as the window server lists it (`CGWindowListCopyWindowInfo`, front to back).
public struct ScreenWindow: Equatable, Sendable {
    public var id: UInt32
    public var pid: Int32
    public var layer: Int
    public var alpha: Double
    /// Global coordinates in points, origin at the top left of the main display.
    public var bounds: CGRect

    public init(id: UInt32, pid: Int32, layer: Int, alpha: Double, bounds: CGRect) {
        self.id = id; self.pid = pid; self.layer = layer; self.alpha = alpha; self.bounds = bounds
    }
}

/// The window to capture and whatever sits on it (sheets, the open/save panel).
public struct WindowPick: Equatable, Sendable {
    /// The app's window.
    public var main: UInt32
    /// Every window in the picture, `main` first. One entry means the plain window.
    public var windows: [UInt32]
    /// The union of their bounds, which the capture is cropped to.
    public var area: CGRect

    public init(main: UInt32, windows: [UInt32], area: CGRect) {
        self.main = main; self.windows = windows; self.area = area
    }
}

/// The rules for "Look at this": sizes, caps, what may be captured and how dropped items are sorted.
/// Pure logic; the Mac app captures and the client encodes by these rules.
public enum AttachmentPolicy {
    /// Pending attachments per call (pictures and files; the screen capture is extra).
    public static let maxAttachments = 3
    /// Longest image edge in pixels. Hermes keeps session images at this size, and every later
    /// model call in the session sends them again.
    public static let maxLongEdge = 1568
    /// Longest edge for pictures from the iPhone/iPad composer (photos, camera, screenshots): a bit
    /// more than the Mac's so a phone photo's small print stays legible.
    public static let phoneLongEdge = 2048
    /// Files the composer may send as files (the plugin's `POST /voice/attachments` takes these; pictures
    /// are re-encoded to JPEG instead).
    public static let stagedFileTypes: Set<String> = ["application/pdf", "text/plain", "text/markdown", "text/csv",
                                                      "application/json", "video/mp4", "video/quicktime"]
    /// Aim for about this many bytes per image.
    public static let targetImageBytes = 300_000
    /// Never send an image bigger than this.
    public static let maxImageBytes = 2_500_000
    /// All images in one request together; stays under Hermes' 10 MB limit after base64.
    public static let maxRequestImageBytes = 6_500_000
    /// Other files pass through up to this size and are never inlined.
    public static let maxFileBytes = 10_000_000
    /// JPEG qualities tried in order; the last is the floor.
    public static let qualityLadder: [Double] = [0.82, 0.72, 0.62, 0.52]

    /// Pixel size within `maxLongEdge`, keeping the aspect ratio. Never scales up.
    public static func fittedSize(_ size: CGSize, longEdge: Int = maxLongEdge) -> CGSize {
        let long = max(size.width, size.height)
        guard long > 0 else { return .zero }
        let scale = min(1, CGFloat(longEdge) / long)
        return CGSize(width: max(1, (size.width * scale).rounded()), height: max(1, (size.height * scale).rounded()))
    }

    /// Walks the quality ladder until `encode` returns about `targetImageBytes` or less. At the floor
    /// the result is kept when it's within `maxImageBytes`; otherwise `.tooLarge`. `encode` returning
    /// nil means the image can't be encoded (`.unsupported`).
    public static func fitToBudget(_ encode: (Double) -> Data?) throws -> Data {
        var floor: Data?
        for quality in qualityLadder {
            guard let data = encode(quality) else { throw AttachmentFailure.unsupported }
            if data.count <= targetImageBytes { return data }
            floor = data
        }
        guard let floor, floor.count <= maxImageBytes else { throw AttachmentFailure.tooLarge }
        return floor
    }

    // MARK: Password managers

    /// Apps never captured while frontmost: 1Password (8 and 7), Bitwarden, Dashlane, LastPass,
    /// KeePassXC, Strongbox, Proton Pass, Enpass, Keeper, NordPass, RoboForm, Keychain Access and
    /// Passwords. Browser password-manager extensions aren't covered. Entries are lowercase.
    public static let passwordManagerBundleIDs: Set<String> = [
        "com.1password.1password", "com.agilebits.onepassword7",
        "com.bitwarden.desktop",
        "com.dashlane.dashlanephonefinal", "com.dashlane.dashlane",
        "com.lastpass.lastpass",
        "org.keepassxc.keepassxc", "org.keepassx.keepassxc",
        "com.markmcguill.strongbox", "com.markmcguill.strongbox.mac", "com.markmcguill.strongbox.mac.pro",
        "me.proton.pass.electron",
        "in.sinew.enpass-desktop",
        "com.keepersecurity.passwordmanager",
        "com.nordsec.nordpass",
        "com.sibersystems.roboform",
        "com.apple.keychainaccess",
        "com.apple.passwords",
    ]

    /// Bundle ids compare case-insensitively, as Launch Services does.
    public static func isPasswordManager(bundleID: String?) -> Bool {
        guard let bundleID else { return false }
        return passwordManagerBundleIDs.contains(bundleID.lowercased())
    }

    // MARK: Dropped items

    /// Sorts a dropped or pasted item. Pictures ImageIO can read (`imageTypes`, from
    /// `CGImageSourceCopyTypeIdentifiers`) become images; other regular files up to `maxFileBytes`
    /// pass through. Links, folders, packages and aliases are refused. `contentType` defaults to the
    /// type of the file name's extension.
    public static func classify(_ url: URL, contentType: UTType? = nil, isRegularFile: Bool = true, isAlias: Bool = false,
                                byteCount: Int? = nil, imageTypes: Set<String>) -> Result<AttachmentItemKind, AttachmentFailure> {
        guard url.isFileURL, !url.hasDirectoryPath, isRegularFile, !isAlias else { return .failure(.unsupported) }
        if let type = contentType ?? UTType(filenameExtension: url.pathExtension),
           imageTypes.contains(where: { UTType($0).map(type.conforms(to:)) ?? false }) {
            return .success(.image)
        }
        if let byteCount, byteCount > maxFileBytes { return .failure(.tooLarge) }
        return .success(.file)
    }

    // MARK: Window choice

    /// Windows smaller than this are popovers and badges, not the thing the user is looking at.
    public static let minWindowSize = CGSize(width: 100, height: 60)

    /// The window to capture for the app `appPID`, from the window server's front-to-back list.
    /// Takes the app's frontmost normal-layer, visible window above `minWindowSize`; other
    /// processes' windows (Stage Manager's strip lives on the same layer) never count. When that
    /// window sits inside a larger one of the same app, it's a sheet: the larger window is the target
    /// and every same-app window in front of it and inside it comes along. Windows of the open/save
    /// panel service (`panelServicePIDs`) in front of the target and overlapping it come along too.
    public static func pickWindow(_ windows: [ScreenWindow], appPID: Int32, panelServicePIDs: Set<Int32> = []) -> WindowPick? {
        func isAppWindow(_ w: ScreenWindow) -> Bool {
            w.pid == appPID && w.layer == 0 && w.alpha > 0
                && w.bounds.width >= minWindowSize.width && w.bounds.height >= minWindowSize.height
        }
        func holds(_ outer: CGRect, _ inner: CGRect) -> Bool { outer.insetBy(dx: -2, dy: -2).contains(inner) }
        func area(_ r: CGRect) -> CGFloat { r.width * r.height }

        guard let frontIndex = windows.firstIndex(where: isAppWindow) else { return nil }
        let front = windows[frontIndex]
        // The largest same-app window behind it that holds it; chains of sheets end at the document window.
        let parentIndex = windows.indices.dropFirst(frontIndex + 1)
            .filter { isAppWindow(windows[$0]) && area(windows[$0].bounds) > area(front.bounds) && holds(windows[$0].bounds, front.bounds) }
            .max { area(windows[$0].bounds) < area(windows[$1].bounds) }
        let mainIndex = parentIndex ?? frontIndex
        let main = windows[mainIndex]
        let extras = windows[..<mainIndex].filter { w in
            (isAppWindow(w) && holds(main.bounds, w.bounds))
                || (panelServicePIDs.contains(w.pid) && w.alpha > 0 && w.bounds.intersects(main.bounds))
        }
        let union = extras.reduce(main.bounds) { $0.union($1.bounds) }
        return WindowPick(main: main.id, windows: [main.id] + extras.map(\.id), area: union)
    }

    // MARK: Blank frames

    /// Standard deviation (0–255 gray levels) at or under which a frame counts as blank. Protected
    /// content and a locked screen come back as one flat color (0); an empty white window whose only
    /// marks are its three window buttons still measures about 1.4.
    public static let blankSpread = 0.5

    /// A small grayscale copy of `image` (`side` × `side`, averaged down), for the blank check.
    public static func graySample(_ image: CGImage, side: Int = 64) -> [UInt8] {
        var pixels = [UInt8](repeating: 0, count: side * side)
        pixels.withUnsafeMutableBytes { buffer in
            guard let context = CGContext(data: buffer.baseAddress, width: side, height: side, bitsPerComponent: 8,
                                          bytesPerRow: side, space: CGColorSpaceCreateDeviceGray(),
                                          bitmapInfo: CGImageAlphaInfo.none.rawValue) else { return }
            context.interpolationQuality = .high
            context.draw(image, in: CGRect(x: 0, y: 0, width: side, height: side))
        }
        return pixels
    }

    /// True when the samples are (nearly) all the same level. An empty sample counts as blank.
    public static func isNearlyUniform(_ samples: [UInt8]) -> Bool {
        guard !samples.isEmpty else { return true }
        let n = Double(samples.count)
        let mean = samples.reduce(0.0) { $0 + Double($1) } / n
        let variance = samples.reduce(0.0) { $0 + (Double($1) - mean) * (Double($1) - mean) } / n
        return variance.squareRoot() <= blankSpread
    }

    /// True when a capture shows nothing usable.
    public static func isBlank(_ image: CGImage) -> Bool { isNearlyUniform(graySample(image)) }
}
