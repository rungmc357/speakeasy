import Foundation
import Combine
import AVFoundation
#if canImport(CoreGraphics)
import CoreGraphics
import ImageIO
#endif

// Files your agent made (servers with `files`; the plugin's docs/API.md "Files"): every file Hermes
// handed over in a reply, across all chats. Shared by the Mac History window and the iPhone/iPad app:
// the list, its type chips, thumbnails, and opening one (downloaded to a private temp copy for Quick
// Look and the share sheet).

public struct AgentFile: Identifiable, Equatable, Hashable, Sendable {
    public enum Kind: String, Sendable, CaseIterable {
        case image, video, audio, document, other
    }

    public var id: String
    public var name: String
    public var kind: Kind
    public var mime: String
    public var bytes: Int
    public var at: Date
    /// The visible chat it came from (open it with the history routes).
    public var sessionID: String
    public var chat: String
    public var source: String

    public init(id: String, name: String, kind: Kind, mime: String = "", bytes: Int, at: Date, sessionID: String,
                chat: String = "", source: String = "") {
        self.id = id; self.name = name; self.kind = kind; self.mime = mime; self.bytes = bytes; self.at = at
        self.sessionID = sessionID; self.chat = chat; self.source = source
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = o["id"] as? String, !id.isEmpty,
              let name = o["name"] as? String, !name.isEmpty else { return nil }
        self.id = id
        self.name = name
        kind = Kind(rawValue: o["kind"] as? String ?? "") ?? .other
        mime = o["mime"] as? String ?? ""
        bytes = (o["bytes"] as? NSNumber)?.intValue ?? 0
        at = Date(timeIntervalSince1970: (o["at"] as? NSNumber)?.doubleValue ?? 0)
        sessionID = o["session_id"] as? String ?? ""
        chat = o["chat"] as? String ?? ""
        source = o["source"] as? String ?? ""
    }

    /// The file name, safe as the last component of a local path.
    public var safeName: String {
        let cleaned = name.replacingOccurrences(of: "/", with: "-").replacingOccurrences(of: ":", with: "-")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let trimmed = cleaned.hasPrefix(".") ? "file" + cleaned : cleaned
        return trimmed.isEmpty ? "file" : String(trimmed.prefix(180))
    }

    public var fileExtension: String { (name as NSString).pathExtension.lowercased() }

    /// The row's second line: where it came from, when, and how big ("Porch repaint · 9:05 PM · 48 KB").
    public func detail(now: Date = Date(), calendar: Calendar = .current) -> String {
        var parts: [String] = []
        let chatName = chat.trimmingCharacters(in: .whitespacesAndNewlines)
        if !chatName.isEmpty { parts.append(chatName) }
        parts.append(AgentFileFormat.when(at, now: now, calendar: calendar))
        if bytes > 0 { parts.append(AgentFileFormat.size(bytes)) }
        return parts.joined(separator: " · ")
    }

    /// The symbol for a file without a picture of its own.
    public var symbol: String {
        switch kind {
        case .image: return "photo"
        case .video: return "film"
        case .audio: return "waveform"
        case .other: return fileExtension == "zip" ? "doc.zipper" : "doc"
        case .document:
            switch fileExtension {
            case "pdf": return "doc.richtext"
            case "csv", "tsv", "xlsx", "xls", "ods": return "tablecells"
            case "pptx", "ppt", "odp": return "rectangle.on.rectangle"
            case "kml", "kmz", "geojson", "gpx": return "map"
            case "html", "htm": return "globe"
            case "md", "txt", "rtf": return "doc.plaintext"
            default: return "doc.text"
            }
        }
    }

    /// A short type word for accessibility and badges ("PDF", "Picture").
    public var typeWord: String {
        switch kind {
        case .image: return "Picture"
        case .video: return "Video"
        case .audio: return "Audio"
        default: return fileExtension.isEmpty ? "File" : fileExtension.uppercased()
        }
    }
}

public enum AgentFileFormat {
    public static func size(_ bytes: Int) -> String {
        let f = ByteCountFormatter()
        f.countStyle = .file
        f.allowedUnits = [.useKB, .useMB, .useGB]
        return f.string(fromByteCount: Int64(max(bytes, 0)))
    }

    /// "9:05 PM" today, "Yesterday", the weekday this week, else "Mar 4" (with the year when it isn't this one).
    public static func when(_ date: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
        if calendar.isDate(date, inSameDayAs: now) {
            let f = DateFormatter(); f.calendar = calendar; f.timeZone = calendar.timeZone
            f.locale = Locale(identifier: "en_US"); f.dateFormat = "h:mm a"
            return f.string(from: date)
        }
        if let y = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: y) { return "Yesterday" }
        let f = DateFormatter(); f.calendar = calendar; f.timeZone = calendar.timeZone; f.locale = Locale(identifier: "en_US")
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: date), to: calendar.startOfDay(for: now)).day ?? 99
        if days < 7 && days > 0 { f.dateFormat = "EEEE"; return f.string(from: date) }
        f.dateFormat = calendar.component(.year, from: date) == calendar.component(.year, from: now) ? "MMM d" : "MMM d, yyyy"
        return f.string(from: date)
    }
}

/// One type chip: `nil` id is All.
public struct AgentFileKindCount: Equatable, Hashable, Sendable {
    public var id: String
    public var label: String
    public var count: Int
    public init(id: String, label: String, count: Int) { self.id = id; self.label = label; self.count = count }
}

/// `GET /voice/files`.
public struct AgentFileList: Equatable, Sendable {
    public var files: [AgentFile]
    public var kinds: [AgentFileKindCount]
    public var total: Int
    public var next: String?
    /// The server's first scan is still running: ask again shortly.
    public var loading: Bool

    public init(files: [AgentFile], kinds: [AgentFileKindCount] = [], total: Int? = nil, next: String? = nil, loading: Bool = false) {
        self.files = files; self.kinds = kinds; self.total = total ?? files.count; self.next = next; self.loading = loading
    }

    public init(json: Any?) {
        let o = json as? [String: Any] ?? [:]
        files = (o["files"] as? [Any] ?? []).compactMap(AgentFile.init(json:))
        kinds = (o["kinds"] as? [Any] ?? []).compactMap { raw in
            guard let k = raw as? [String: Any], let id = k["id"] as? String, let label = k["label"] as? String else { return nil }
            return AgentFileKindCount(id: id, label: label, count: (k["count"] as? NSNumber)?.intValue ?? 0)
        }
        total = (o["total"] as? NSNumber)?.intValue ?? files.count
        next = (o["next"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        loading = o["loading"] as? Bool ?? false
    }

    public static func path(kind: String? = nil, cursor: String? = nil, sessionID: String? = nil, limit: Int? = nil) -> String {
        var items: [URLQueryItem] = []
        if let kind, !kind.isEmpty { items.append(URLQueryItem(name: "kind", value: kind)) }
        if let sessionID, !sessionID.isEmpty { items.append(URLQueryItem(name: "session_id", value: sessionID)) }
        if let cursor { items.append(URLQueryItem(name: "cursor", value: cursor)) }
        if let limit { items.append(URLQueryItem(name: "limit", value: String(limit))) }
        var parts = URLComponents()
        parts.path = "/voice/files"
        parts.queryItems = items.isEmpty ? nil : items
        return parts.string ?? "/voice/files"
    }

    public static func filePath(_ id: String) -> String {
        "/voice/files/" + (id.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? id)
    }

    public static func thumbPath(_ id: String) -> String { filePath(id) + "/thumb" }
}

/// Where files come from: the paired Hermes, or invented demo data.
@MainActor
public protocol AgentFilesBackend: AnyObject {
    func list(kind: String?, cursor: String?) async throws -> AgentFileList
    func thumbnail(_ file: AgentFile) async throws -> Data
    /// A local copy to open, share or drag out (a private temp file with the real name).
    func download(_ file: AgentFile) async throws -> URL
}

public enum AgentFileStore {
    /// Downloads live here, one folder per file id, so two files with one name never collide.
    public static var folder: URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("speakeasy-files", isDirectory: true)
    }

    public static func localURL(for file: AgentFile) -> URL {
        folder.appendingPathComponent(file.id, isDirectory: true).appendingPathComponent(file.safeName)
    }

    /// The local copy when it's already here (same size).
    public static func cached(_ file: AgentFile) -> URL? {
        let url = localURL(for: file)
        guard let size = (try? FileManager.default.attributesOfItem(atPath: url.path))?[.size] as? NSNumber,
              size.intValue == file.bytes else { return nil }
        return url
    }

    public static func save(_ data: Data, for file: AgentFile) throws -> URL {
        let url = localURL(for: file)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        #if os(iOS)
        try data.write(to: url, options: [.atomic, .completeFileProtection])
        #else
        try data.write(to: url, options: [.atomic])
        #endif
        return url
    }

    /// Clears every downloaded copy (they're re-fetched when opened again).
    public static func clear() { try? FileManager.default.removeItem(at: folder) }
}

@MainActor
public final class AgentFilesModel: ObservableObject {
    @Published public private(set) var files: [AgentFile] = []
    @Published public private(set) var kinds: [AgentFileKindCount] = []
    @Published public private(set) var total = 0
    @Published public private(set) var loading = false
    @Published public private(set) var loaded = false
    /// The server is still finding files (its first scan after loading).
    @Published public private(set) var serverScanning = false
    @Published public private(set) var error: String?
    @Published public private(set) var next: String?
    @Published public private(set) var thumbs: [String: Data] = [:]
    /// The file being fetched to open (shows a spinner on its row).
    @Published public private(set) var opening: String?
    @Published public var openError: String?
    /// The chosen type chip (nil: All).
    @Published public private(set) var kind: String?

    public var backend: AgentFilesBackend?
    private var thumbLoads: Set<String> = []
    private var generation = 0
    private var firstLoad: Task<Void, Never>?

    public init(backend: AgentFilesBackend? = nil) { self.backend = backend }

    /// The chips row: All plus each kind the person has, hidden entirely when there's one kind or none.
    public var showsChips: Bool { kinds.count > 1 }
    public var isEmpty: Bool { loaded && files.isEmpty && !serverScanning }

    public func choose(kind: String?) async {
        guard kind != self.kind else { return }
        self.kind = kind
        files = []; next = nil; loaded = false
        await reload()
    }

    /// The page's first load, on a task the model owns: a page rebuilt while it opens (iPad's
    /// sidebar closing as Files opens) cancels its own `.task`, and that cancelled request used to
    /// show "Couldn't reach your Hermes" over a server that was answering. One load at a time.
    public func loadIfNeeded() {
        guard !loaded, firstLoad == nil, backend != nil else { return }
        firstLoad = Task { [weak self] in
            await self?.reload()
            self?.firstLoad = nil
        }
    }

    /// Waits for a load `loadIfNeeded` started (tests and pull-to-refresh).
    public func waitForLoad() async { await firstLoad?.value }

    /// Seconds before the one automatic retry of a failed list call (tests shorten it).
    public var retryDelay: TimeInterval = 1.0

    public func reload() async {
        guard let backend else { return }
        generation &+= 1
        let mine = generation
        loading = !loaded
        defer { if mine == generation { loading = false } }
        var tries = 0
        var retried = false
        while true {
            do {
                let page = try await backend.list(kind: kind, cursor: nil)
                guard mine == generation else { return }
                apply(page, replacing: true)
                error = nil
                if page.loading, tries < 20 {
                    tries += 1
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                    guard mine == generation, !Task.isCancelled else { return }
                    continue
                }
                return
            } catch {
                guard mine == generation else { return }
                // Nobody is waiting for the answer any more (the page went away): not a failure, and
                // the next open loads again.
                if WorkPoll.isCancellation(error) { return }
                if !retried, AgentFilesModel.worthRetrying(error) {
                    retried = true
                    try? await Task.sleep(nanoseconds: UInt64(retryDelay * 1_000_000_000))
                    guard mine == generation, !Task.isCancelled else { return }
                    continue
                }
                if files.isEmpty { self.error = AgentFilesModel.message(error) }
                loaded = true
                return
            }
        }
    }

    /// A slow answer, a dropped connection or a server hiccup gets one quiet second try; a refusal
    /// (401, 404, a bad request) would only fail again.
    static func worthRetrying(_ error: Error) -> Bool {
        if WorkPoll.isCancellation(error) { return false }
        if let status = (error as? StatusCarrying)?.status { return status == 0 || status >= 500 || status == 429 }
        return (error as NSError).domain == NSURLErrorDomain
    }

    public func loadMore() async {
        guard let backend, let cursor = next, !loading else { return }
        let mine = generation
        loading = true
        defer { loading = false }
        do {
            let page = try await backend.list(kind: kind, cursor: cursor)
            guard mine == generation else { return }
            apply(page, replacing: false)
        } catch {
            // the list stays; the next scroll to the end tries again
        }
    }

    private func apply(_ page: AgentFileList, replacing: Bool) {
        if replacing {
            files = page.files
        } else {
            let have = Set(files.map(\.id))
            files += page.files.filter { !have.contains($0.id) }
        }
        next = page.next
        serverScanning = page.loading
        total = page.total
        // The chips come from the All list's counts; a filtered page still reports every kind.
        if !page.kinds.isEmpty || kind == nil { kinds = page.kinds }
        loaded = true
    }

    public func thumbnail(for file: AgentFile) async {
        guard file.kind == .image || file.kind == .video, thumbs[file.id] == nil,
              !thumbLoads.contains(file.id), let backend else { return }
        thumbLoads.insert(file.id)
        defer { thumbLoads.remove(file.id) }
        if file.kind == .video {
            // Only authenticated, privately cached files; no remote URL or server path to AVFoundation.
            guard let url = try? await backend.download(file),
                  let data = await AgentVideoPoster.make(url) else { return }
            thumbs[file.id] = data
            return
        }
        if let data = try? await backend.thumbnail(file), !data.isEmpty { thumbs[file.id] = data }
    }

    /// Fetches the file to a private local copy; nil (with `openError`) when it can't.
    public func localCopy(_ file: AgentFile) async -> URL? {
        if let url = AgentFileStore.cached(file) { return url }
        guard let backend else { return nil }
        opening = file.id
        defer { if opening == file.id { opening = nil } }
        do {
            let url = try await backend.download(file)
            openError = nil
            return url
        } catch {
            if (error as? StatusCarrying)?.status == 404 {
                openError = "That file isn't on your Hermes computer anymore."
                files.removeAll { $0.id == file.id }
            } else {
                openError = AgentFilesModel.message(error)
            }
            return nil
        }
    }

    /// What actually happened, in plain words: "Couldn't reach" only when the request never got
    /// there; a slow answer says it's slow, and an answer that was an error says what Hermes said.
    public static func message(_ error: Error) -> String {
        let ns = error as NSError
        if ns.domain == NSURLErrorDomain {
            if ConnectionTrouble.unreachable.contains(ns.code) { return ConnectionTrouble.message(for: error, server: nil) }
            return "Couldn't load your files. Try again."
        }
        if let status = (error as? StatusCarrying)?.status, status >= 500 {
            let said = (error as? LocalizedError)?.errorDescription ?? ""
            let detail = said.isEmpty || said.hasPrefix("Server HTTP") ? "" : " (\(said))"
            return "Your Hermes answered with an error while listing your files\(detail). Try again in a moment."
        }
        if let e = error as? LocalizedError, let text = e.errorDescription { return text }
        return error.localizedDescription
    }

    /// Screenshots and tests: state without a server.
    public func preview(_ list: AgentFileList, thumbs: [String: Data] = [:]) {
        files = list.files; kinds = list.kinds; total = list.total; next = list.next
        serverScanning = list.loading; loaded = true; loading = false; error = nil
        self.thumbs = thumbs
    }
}

public enum AgentVideoPoster {
    public static func make(_ url: URL) async -> Data? {
        guard url.isFileURL else { return nil }
        let asset = AVURLAsset(url: url)
        let generator = AVAssetImageGenerator(asset: asset)
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 960, height: 960)
        generator.requestedTimeToleranceBefore = .zero
        generator.requestedTimeToleranceAfter = .zero
        // Recorded clips often begin with a blank transition. Sample just inside the clip,
        // keeping very short videos in bounds, and fall back to the opening frame.
        let duration = (try? await asset.load(.duration)).map(CMTimeGetSeconds) ?? 0
        let seconds = duration.isFinite && duration > 0 ? min(0.5, duration / 2) : 0
        let sample = CMTime(seconds: seconds, preferredTimescale: 600)
        var frame = try? await generator.image(at: sample)
        if frame == nil { frame = try? await generator.image(at: .zero) }
        guard let frame else { return nil }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, "public.png" as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, frame.image, nil)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return out as Data
    }
}

// MARK: - Demo

/// Invented files for screenshots and demo builds: no real names, chats or content.
@MainActor
public final class DemoAgentFilesBackend: AgentFilesBackend {
    public let files: [AgentFile]

    public init(now: Date = Date()) {
        let h: TimeInterval = 3600
        func file(_ id: String, _ name: String, _ kind: AgentFile.Kind, _ bytes: Int, _ ago: TimeInterval,
                  _ chat: String, _ source: String) -> AgentFile {
            AgentFile(id: id, name: name, kind: kind, bytes: bytes, at: now.addingTimeInterval(-ago),
                      sessionID: "demo-" + id, chat: chat, source: source)
        }
        files = [
            file("porchchart", "porch-paint-colors.png", .image, 182_400, 0.4 * h, "Porch repaint", "Discord · #build"),
            file("seedplan", "spring-seed-plan.pdf", .document, 412_000, 1.2 * h, "Garden beds for spring", "Speakeasy"),
            file("shelfsketch", "walnut-shelf-sketch.png", .image, 960_000, 3 * h, "Walnut shelf brackets", "Discord · #build"),
            file("budget", "kitchen-budget.xlsx", .document, 38_200, 5 * h, "Kitchen refresh budget", "Telegram"),
            file("trailmap", "ridge-loop-trail.kml", .document, 14_800, 26 * h, "Saturday hike", "Speakeasy"),
            file("sunrise", "lake-sunrise.jpg", .image, 2_400_000, 27 * h, "Lake cabin weekend", "Telegram"),
            file("memo", "standup-notes.m4a", .audio, 1_180_000, 50 * h, "Morning check-in", "Speakeasy"),
            file("tour", "lantern-build-demo.mp4", .video, 18_600_000, 74 * h, "Lantern build", "Terminal"),
            file("bars", "weekly-miles-chart.png", .image, 120_500, 98 * h, "Running log", "Speakeasy"),
            file("menu", "dinner-party-menu.md", .document, 3_900, 140 * h, "Dinner party Saturday", "Discord · #home"),
        ]
    }

    public func list(kind: String?, cursor: String?) async throws -> AgentFileList {
        var counts: [AgentFile.Kind: Int] = [:]
        for f in files { counts[f.kind, default: 0] += 1 }
        let labels: [(AgentFile.Kind, String)] = [(.image, "Images"), (.video, "Videos"), (.audio, "Audio"),
                                                  (.document, "Docs"), (.other, "Other")]
        let kinds = labels.compactMap { k, label in counts[k].map { AgentFileKindCount(id: k.rawValue, label: label, count: $0) } }
        let shown = files.filter { kind == nil || $0.kind.rawValue == kind }
        return AgentFileList(files: shown, kinds: kinds, total: shown.count)
    }

    public func thumbnail(_ file: AgentFile) async throws -> Data {
        guard let data = Self.picture(for: file.id) else { throw ScheduleProblem(status: 404, message: "No preview") }
        return data
    }

    public func download(_ file: AgentFile) async throws -> URL {
        let data: Data
        if file.kind == .image, let png = Self.picture(for: file.id, size: 1200) {
            data = png
        } else if file.fileExtension == "md" {
            data = Data("# Dinner party menu\n\n- Tomato and basil salad\n- Roast chicken with lemon\n- Olive oil cake\n".utf8)
        } else {
            data = Data("Sample file made for the demo.\n".utf8)
        }
        return try AgentFileStore.save(data, for: file)
    }

    /// A simple invented picture per demo file (a palette or a bar chart), drawn on the device.
    public static func picture(for id: String, size: Int = 360) -> Data? {
        #if canImport(CoreGraphics)
        let w = size, hgt = size * 3 / 4
        let space = CGColorSpaceCreateDeviceRGB()
        guard let ctx = CGContext(data: nil, width: w, height: hgt, bitsPerComponent: 8, bytesPerRow: 0, space: space,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
        func color(_ r: Double, _ g: Double, _ b: Double) -> CGColor { CGColor(red: r, green: g, blue: b, alpha: 1) }
        let W = Double(w), H = Double(hgt)
        switch id {
        case "porchchart":
            let swatches = [color(0.36, 0.47, 0.42), color(0.83, 0.78, 0.67), color(0.62, 0.32, 0.24), color(0.93, 0.91, 0.86)]
            ctx.setFillColor(color(0.97, 0.96, 0.93)); ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
            for (i, c) in swatches.enumerated() {
                ctx.setFillColor(c)
                ctx.fill(CGRect(x: W * 0.06 + Double(i) * W * 0.23, y: H * 0.16, width: W * 0.2, height: H * 0.68))
            }
        case "bars":
            ctx.setFillColor(color(0.10, 0.12, 0.16)); ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
            let heights = [0.35, 0.5, 0.42, 0.7, 0.58, 0.82, 0.64]
            for (i, v) in heights.enumerated() {
                ctx.setFillColor(color(0.98, 0.62, 0.25))
                ctx.fill(CGRect(x: W * 0.08 + Double(i) * W * 0.125, y: H * 0.12, width: W * 0.08, height: H * 0.76 * v))
            }
        case "sunrise":
            for i in 0..<hgt {
                let t = Double(i) / H
                ctx.setFillColor(color(0.98 - 0.5 * t, 0.62 - 0.2 * t, 0.38 + 0.4 * t))
                ctx.fill(CGRect(x: 0, y: Double(i), width: W, height: 1))
            }
            ctx.setFillColor(color(1.0, 0.86, 0.55))
            ctx.fillEllipse(in: CGRect(x: W * 0.4, y: H * 0.22, width: W * 0.2, height: W * 0.2))
            ctx.setFillColor(color(0.16, 0.24, 0.32)); ctx.fill(CGRect(x: 0, y: 0, width: W, height: H * 0.28))
        default:
            ctx.setFillColor(color(0.90, 0.86, 0.78)); ctx.fill(CGRect(x: 0, y: 0, width: W, height: H))
            ctx.setStrokeColor(color(0.35, 0.25, 0.18)); ctx.setLineWidth(W / 60)
            ctx.stroke(CGRect(x: W * 0.15, y: H * 0.2, width: W * 0.7, height: H * 0.12))
            ctx.stroke(CGRect(x: W * 0.25, y: H * 0.32, width: W * 0.08, height: H * 0.45))
            ctx.stroke(CGRect(x: W * 0.67, y: H * 0.32, width: W * 0.08, height: H * 0.45))
        }
        guard let image = ctx.makeImage() else { return nil }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, "public.png" as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, image, nil)
        guard CGImageDestinationFinalize(dest) else { return nil }
        return out as Data
        #else
        return nil
        #endif
    }
}
