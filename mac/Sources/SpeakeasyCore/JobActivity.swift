import Foundation

// Live job activity (servers with `capabilities.job_activity`): what a running job is doing now,
// what it has found, what it touched, and a picture of its kind of work. Every field is optional:
// an older plugin sends none of them and the job page looks as it did. Nothing here is ever made
// up on the client; it only carries what the server reported.

/// One thing the job did, in plain words ("Read checkout.spec.ts").
public struct JobActivityItem: Equatable, Sendable, Identifiable {
    public enum Kind: String, Equatable, Sendable { case read, edit, run, search, browse, think, message, tool }
    public enum Status: String, Equatable, Sendable { case running, done, failed }

    public var id: String
    public var at: Date?
    public var kind: Kind
    public var text: String
    public var detail: String?
    public var status: Status
    // The raw call behind the sentence (newer plugins; all optional): the tool's own name, its
    // arguments (≤ 300 chars) and what came back (≤ 500 chars), and when it started and ended.
    public var tool: String?
    public var args: String?
    public var result: String?
    public var startedAt: Date?
    public var endedAt: Date?

    public init(id: String, at: Date? = nil, kind: Kind, text: String, detail: String? = nil, status: Status = .done,
                tool: String? = nil, args: String? = nil, result: String? = nil, startedAt: Date? = nil, endedAt: Date? = nil) {
        self.id = id; self.at = at; self.kind = kind; self.text = text; self.detail = detail; self.status = status
        self.tool = tool; self.args = args; self.result = result; self.startedAt = startedAt; self.endedAt = endedAt
    }

    public init?(json: Any?, index: Int) {
        guard let o = json as? [String: Any], let text = nonEmpty(o["text"]) else { return nil }
        self.init(id: nonEmpty(o["id"]) ?? "a\(index)", at: decodeDate(o["at"]),
                  kind: (o["kind"] as? String).flatMap(Kind.init(rawValue:)) ?? .tool,
                  text: String(text.prefix(120)), detail: nonEmpty(o["detail"]).map { String($0.prefix(120)) },
                  status: (o["status"] as? String).flatMap(Status.init(rawValue:)) ?? .done,
                  tool: nonEmpty(o["tool"]).map { String($0.prefix(60)) },
                  args: nonEmpty(o["args"]).map { String($0.prefix(300)) },
                  result: nonEmpty(o["result"]).map { String($0.prefix(500)) },
                  startedAt: decodeDate(o["started_at"]), endedAt: decodeDate(o["ended_at"]))
    }

    /// The SF Symbol for the kind of action.
    public var symbol: String {
        switch kind {
        case .read: return "doc.text"
        case .edit: return "pencil"
        case .run: return "play"
        case .search: return "magnifyingglass"
        case .browse: return "globe"
        case .think: return "sparkles"
        case .message: return "bubble.left"
        case .tool: return "wrench.and.screwdriver"
        }
    }

    /// The server keeps at most this many (newest last).
    public static let maxItems = 40

    public static func list(json: Any?) -> [JobActivityItem] {
        let raw = json as? [Any] ?? []
        return Array(raw.enumerated().compactMap { JobActivityItem(json: $0.element, index: $0.offset) }.suffix(maxItems))
    }
}

/// Something the job learned before it finished.
public struct JobFinding: Equatable, Sendable, Identifiable {
    public var id: String
    public var at: Date?
    public var text: String
    public init(id: String, at: Date? = nil, text: String) { self.id = id; self.at = at; self.text = text }

    public static let maxItems = 8

    public static func list(json: Any?) -> [JobFinding] {
        let raw = json as? [Any] ?? []
        return Array(raw.enumerated().compactMap { i, item -> JobFinding? in
            guard let o = item as? [String: Any], let text = nonEmpty(o["text"]) else { return nil }
            return JobFinding(id: nonEmpty(o["id"]) ?? "f\(i)", at: decodeDate(o["at"]), text: String(text.prefix(160)))
        }.suffix(maxItems))
    }
}

/// A file, site or command the job touched.
public struct JobTouched: Equatable, Sendable, Identifiable {
    public enum Kind: String, Equatable, Sendable { case file, url, command }
    public var kind: Kind
    public var label: String
    public var detail: String?
    /// Only https links open.
    public var url: URL?
    public var id: String { "\(kind.rawValue)|\(label)" }

    public init(kind: Kind, label: String, detail: String? = nil, url: URL? = nil) {
        self.kind = kind; self.label = label; self.detail = detail; self.url = url
    }

    public var symbol: String {
        switch kind {
        case .file: return "doc"
        case .url: return "globe"
        case .command: return "terminal"
        }
    }

    public static let maxItems = 20

    public static func list(json: Any?) -> [JobTouched] {
        var seen = Set<String>()
        var out: [JobTouched] = []
        for item in json as? [Any] ?? [] {
            guard let o = item as? [String: Any], let label = nonEmpty(o["label"]),
                  let kind = (o["kind"] as? String).flatMap(Kind.init(rawValue:)) else { continue }
            let url = nonEmpty(o["url"]).flatMap(URL.init(string:)).flatMap { $0.scheme == "https" ? $0 : nil }
            let t = JobTouched(kind: kind, label: String(label.prefix(60)), detail: nonEmpty(o["detail"]).map { String($0.prefix(60)) }, url: url)
            if seen.insert(t.id).inserted { out.append(t) }
            if out.count == maxItems { break }
        }
        return out
    }
}

/// The picture at the top of a job page, by the kind of work it is doing.
public enum JobHero: Equatable, Sendable {
    public struct Source: Equatable, Sendable, Identifiable {
        public var title: String
        public var site: String
        public var id: String { site + "|" + title }
        public init(title: String, site: String) { self.title = title; self.site = site }
    }
    public struct Point: Equatable, Sendable {
        public var x: String
        public var y: Double
        public init(x: String, y: Double) { self.x = x; self.y = y }
    }

    /// The same thing run many times ("31 passed · 0 failed of 50").
    case trials(total: Int, passed: Int, failed: Int)
    /// Research: the pages it read.
    case sources([Source])
    /// A code change: files, lines added and removed, a few lines of it.
    case diff(files: Int, added: Int, removed: Int, preview: [String])
    /// Numbers it pulled.
    case series(label: String, unit: String?, points: [Point])
    /// The fallback: the plan's steps.
    case steps

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let type = o["type"] as? String else { return nil }
        func int(_ k: String) -> Int? { (o[k] as? NSNumber)?.intValue }
        switch type {
        case "trials":
            guard let total = int("total"), total > 0, total <= 10_000 else { return nil }
            let passed = min(max(int("passed") ?? 0, 0), total)
            let failed = min(max(int("failed") ?? 0, 0), total - passed)
            self = .trials(total: total, passed: passed, failed: failed)
        case "sources":
            let items: [Source] = (o["items"] as? [Any] ?? []).compactMap { raw in
                guard let s = raw as? [String: Any], let site = nonEmpty(s["site"]) else { return nil }
                return Source(title: String((nonEmpty(s["title"]) ?? site).prefix(100)), site: String(site.prefix(60)))
            }
            guard !items.isEmpty else { return nil }
            self = .sources(Array(items.prefix(12)))
        case "diff":
            guard let files = int("files"), files > 0 else { return nil }
            let preview = (o["preview"] as? String ?? "").split(separator: "\n", omittingEmptySubsequences: false)
                .prefix(6).map { String($0.prefix(80)) }.filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            self = .diff(files: files, added: max(int("added") ?? 0, 0), removed: max(int("removed") ?? 0, 0), preview: preview)
        case "series":
            let points: [Point] = (o["points"] as? [Any] ?? []).compactMap { raw in
                guard let p = raw as? [String: Any], let y = (p["y"] as? NSNumber)?.doubleValue, y.isFinite else { return nil }
                return Point(x: p["x"].map { "\($0)" } ?? "", y: y)
            }
            guard points.count >= 2 else { return nil }
            self = .series(label: nonEmpty(o["label"]) ?? "", unit: nonEmpty(o["unit"]), points: Array(points.suffix(60)))
        case "steps":
            self = .steps
        default:
            return nil
        }
    }

    /// What a job page shows on top: the server's hero when it sent one, else its step path when
    /// it has a plan of two or more steps, else nothing (the page as before).
    public static func select(_ info: WorkInfo) -> JobHero? {
        if let hero = info.hero {
            if hero == .steps { return info.plan.count >= 2 ? .steps : nil }
            return hero
        }
        return info.plan.count >= 2 && !info.isTerminal ? .steps : nil
    }

    /// Short name for tests and accessibility.
    public var kind: String {
        switch self {
        case .trials: return "trials"
        case .sources: return "sources"
        case .diff: return "diff"
        case .series: return "series"
        case .steps: return "steps"
        }
    }
}

extension WorkInfo {
    /// Fills the live-activity fields from a task's JSON (called from `init?(json:)`).
    mutating func decodeActivity(_ object: [String: Any]) {
        activity = JobActivityItem.list(json: object["activity"])
        findings = JobFinding.list(json: object["findings"])
        touched = JobTouched.list(json: object["touched"])
        hero = JobHero(json: object["hero"])
    }

    /// The server sent any live activity for this task.
    public var hasLiveActivity: Bool { !activity.isEmpty || !findings.isEmpty || !touched.isEmpty || hero != nil }
}
