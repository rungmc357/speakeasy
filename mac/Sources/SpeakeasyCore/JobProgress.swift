import Foundation

// MARK: - Job progress (contract §3)

public enum JobProgress {
    /// "2 min", "45 s", "1 h 05 min": how long a task has run (or ran).
    public static func duration(_ seconds: TimeInterval) -> String {
        let t = max(0, Int(seconds.rounded()))
        if t < 60 { return "\(t) s" }
        if t < 3600 { return "\(t / 60) min" }
        return "\(t / 3600) h \(String(format: "%02d", (t % 3600) / 60)) min"
    }

    /// When it started: the server's `started_at`, else the request event, else the first update.
    public static func start(_ info: WorkInfo) -> Date? {
        info.startedAt ?? info.events.first { $0.kind == "request" }?.at ?? info.events.first?.at ?? info.updated
    }

    /// Elapsed time ("4 min"): live while running, start → finish once finished. nil without a start.
    public static func elapsed(_ info: WorkInfo, now: Date) -> String? {
        guard let start = start(info) else { return nil }
        let end = info.isTerminal ? (info.finishedAt ?? info.updatedAt ?? info.updated ?? now) : now
        guard end >= start else { return nil }
        return duration(end.timeIntervalSince(start))
    }

    /// (step, of): the server's numbers when it sent them, else the plan's.
    public static func step(_ info: WorkInfo) -> (index: Int, count: Int)? {
        if let n = info.stepCount, n > 0 {
            let i = min(max(info.stepIndex ?? 1, 1), n)
            return (i, n)
        }
        guard let p = JobBoard.progress(info) else { return nil }
        return (min(p.done + 1, p.total), p.total)
    }

    /// The plan step it's on now ("Run the checkout tests"), while running. The apps show this line
    /// and the status, never a step count or a bar driven by one.
    public static func currentStep(_ info: WorkInfo) -> String? {
        guard !info.isTerminal, !info.plan.isEmpty, let s = step(info) else { return nil }
        let text = info.plan[min(max(s.index - 1, 0), info.plan.count - 1)].trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }

    /// "about 3 min left", only when the server gave an ETA.
    public static func eta(_ info: WorkInfo) -> String? {
        guard !info.isTerminal, let s = info.etaSeconds, s > 0 else { return nil }
        if s < 60 { return "under a minute left" }
        return "about \(duration(TimeInterval(s))) left"
    }

    /// One line for a running tile: "4 min · about 2 min left" (no step count).
    public static func line(_ info: WorkInfo, now: Date) -> String? {
        var parts: [String] = []
        if let e = elapsed(info, now: now) { parts.append(e) }
        if let eta = eta(info) { parts.append(eta) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// Why a failed task failed, in one plain sentence: `failure_reason`, else the failure text.
    public static func failureReason(_ info: WorkInfo) -> String? {
        guard info.status == "failed" else { return nil }
        if let r = info.failureReason, r.lowercased() != "unknown" { return r }
        return info.failure?.text
    }

    /// A step's time, relative to now ("just now", "3 min ago"), so steps logged in the same minute
    /// don't all read "6:19".
    public static func relative(_ at: Date?, now: Date) -> String? {
        guard let at else { return nil }
        let s = now.timeIntervalSince(at)
        if s < 45 { return "just now" }
        if s < 3600 { return "\(Int((s / 60).rounded())) min ago" }
        if s < 86400 { return "\(Int(s / 3600)) h ago" }
        return nil
    }
}

// MARK: - Card actions (contract §2)

/// A button a card offers (`actions` on any view). The server decides what an id means; unknown
/// ids still render.
public struct CardAction: Equatable, Sendable, Identifiable {
    public enum Style: String, Sendable { case primary, secondary, destructive }
    public var id: String
    public var label: String
    public var style: Style
    /// Ask before running it.
    public var confirm: Bool
    /// What a device action sends with it (e.g. brightness), when the card set one.
    public var value: JSONValue

    public init(id: String, label: String, style: Style = .secondary, confirm: Bool = false, value: JSONValue = .null) {
        self.id = id; self.label = label; self.style = style; self.confirm = confirm; self.value = value
    }

    public init?(json: JSONValue) {
        guard let id = json["id"].string, let label = json["label"].string else { return nil }
        self.init(id: String(id.prefix(60)), label: String(label.prefix(40)),
                  style: Style(rawValue: json["style"].string ?? "") ?? .secondary,
                  confirm: json["confirm"].bool ?? false, value: json["value"])
    }
}

public extension ViewCard {
    /// The card's buttons (at most four).
    var actions: [CardAction] { Array(self["actions"].array.compactMap(CardAction.init(json:)).prefix(4)) }
    /// Its index in `result.views`, which the card-action route addresses.
    var viewIndex: Int { number - 1 }
}

/// Plain-text form of a card for Copy.
public enum CardText {
    public static func plain(_ c: ViewCard) -> String {
        var lines: [String] = []
        if let t = c["title"].string ?? c["name"].string ?? c["place"].string { lines.append(t) }
        switch c.kind {
        case "chart":
            // Title, what's plotted and its order, every value in drawn order, then freshness.
            return ChartFacts(c).plainText
        case "stats", "nutrition":
            for i in c["items"].array { lines.append("\(i["label"].string ?? ""): \(i["value"].string ?? "")") }
        case "draft":
            return [c["subject"].string, c["body"].string].compactMap { $0 }.joined(separator: "\n\n")
        case "timeline":
            for d in c["days"].array {
                lines.append(d["label"].string ?? "")
                for i in d["items"].array { lines.append("  " + [i["when"].string, i["what"].string, i["where"].string].compactMap { $0 }.joined(separator: " · ")) }
            }
        case "rows":
            for i in c["items"].array { lines.append("• " + [i["ref"].string, i["name"].string, i["state"].string, i["amount"].string].compactMap { $0 }.joined(separator: " ")) }
        case "metric":
            return [c["label"].string, [c["value"].string, c["unit"].string, c["delta"].string].compactMap { $0 }.joined(separator: " ")]
                .compactMap { $0 }.joined(separator: ": ")
        case "preview":
            if let s = c["creator"].string { lines.append(s) }
            if let s = c["summary"].string { lines.append(s) }
            for (n, l) in c["list"].strings.enumerated() { lines.append("\(n + 1). \(l)") }
            for f in c["flags"].strings { lines.append("! " + f) }
            if let u = c["url"].string { lines.append(u) }
        case "list", "steps":
            for i in c["items"].array { lines.append("• " + (i.string ?? i["text"].string ?? i["title"].string ?? "")) }
        default:
            if lines.isEmpty || lines == [c.summary] { return c.summary }
            lines.append(c.summary)
        }
        return lines.filter { !$0.isEmpty }.joined(separator: "\n")
    }

    public static func number(_ v: Double) -> String { v.rounded() == v ? String(Int(v)) : String(format: "%.2f", v) }
}

/// Maps for a place or route card: its own https `url` when it's a maps link, else an Apple Maps
/// search for the address or name, else nil.
public enum MapsLink {
    public static func url(_ c: ViewCard) -> URL? {
        if let u = c["maps_url"].url { return u }
        if let lat = c["lat"].double, let lon = c["lon"].double {
            let q = (c["name"].string ?? c["title"].string ?? "Place").addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? ""
            return URL(string: "https://maps.apple.com/?ll=\(lat),\(lon)&q=\(q)")
        }
        let query = c["address"].string ?? c["to"].string ?? c["destination"].string ?? c["name"].string
        guard let query, let q = query.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) else { return nil }
        if c.kind == "route" { return URL(string: "https://maps.apple.com/?daddr=\(q)") }
        return URL(string: "https://maps.apple.com/?q=\(q)")
    }
}

// MARK: - Saying it once (contract §5)

public enum TextEcho {
    static func norm(_ s: String) -> String { s.lowercased().filter { $0.isLetter || $0.isNumber } }

    /// `line` is already shown by `shown` (equal, or contained in it).
    public static func repeats(_ line: String?, in shown: String?) -> Bool {
        guard let line, let shown else { return false }
        let a = norm(line), b = norm(shown)
        return !a.isEmpty && b.contains(a)
    }

    /// Two short lines that say nearly the same thing ("Run it 50 times" / "Running it 50 times to
    /// be sure"): one contains the other, or most of the shorter one's words (ignoring little words,
    /// matching "run"/"running") are in the longer one.
    public static func nearDuplicate(_ a: String?, _ b: String?) -> Bool {
        guard let a, let b else { return false }
        if repeats(a, in: b) || repeats(b, in: a) { return true }
        let little: Set<String> = ["a", "an", "the", "it", "its", "to", "be", "of", "and", "or", "for", "in", "on",
                                   "at", "is", "are", "so", "that", "this", "your", "you", "my", "i", "we", "with", "now", "up"]
        func words(_ s: String) -> [String] {
            s.lowercased().split { !$0.isLetter && !$0.isNumber }.map(String.init).filter { !little.contains($0) }
        }
        let wa = words(a), wb = words(b)
        let (short, long) = wa.count <= wb.count ? (wa, wb) : (wb, wa)
        guard !short.isEmpty else { return false }
        func same(_ x: String, _ y: String) -> Bool {
            x == y || (min(x.count, y.count) >= 3 && (x.hasPrefix(y) || y.hasPrefix(x)))
        }
        let hits = short.filter { w in long.contains { same(w, $0) } }.count
        return hits >= min(2, short.count) && Double(hits) / Double(short.count) >= 0.75
    }

    /// `text` with a leading `headline` taken off, so a page that leads with the headline doesn't
    /// say it again underneath. nil when nothing is left.
    public static func remainder(_ text: String, after headline: String?) -> String? {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let headline, !norm(headline).isEmpty else { return t.isEmpty ? nil : t }
        let h = headline.trimmingCharacters(in: .whitespacesAndNewlines)
        if norm(t) == norm(h) { return nil }
        if t.hasPrefix(h) {
            let rest = t.dropFirst(h.count).trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
            return rest.isEmpty ? nil : String(t.dropFirst(h.count)).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return t
    }
}
