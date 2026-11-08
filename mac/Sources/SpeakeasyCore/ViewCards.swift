import Foundation

/// A visual card the api sent with an answer (`result.views`): a stock quote, the weather, a game,
/// a place, a day's schedule… The plugin validates every field against its catalog (views.py);
/// here we keep the tree as typed JSON and give each card kind a small read-only accessor. A kind
/// the app doesn't draw yet is skipped, so new server kinds never break an older app.
public enum JSONValue: Equatable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case array([JSONValue])
    case object([String: JSONValue])
    case null

    public init(any value: Any?) {
        switch value {
        case let v as String: self = .string(v)
        case let v as NSNumber:
            // NSNumber bridges JSON booleans too; CFBoolean is the only reliable tell.
            if CFGetTypeID(v) == CFBooleanGetTypeID() { self = .bool(v.boolValue) } else { self = .number(v.doubleValue) }
        case let v as Bool: self = .bool(v)
        case let v as [Any]: self = .array(v.map { JSONValue(any: $0) })
        case let v as [String: Any]: self = .object(v.mapValues { JSONValue(any: $0) })
        default: self = .null
        }
    }

    public subscript(key: String) -> JSONValue { if case .object(let o) = self { return o[key] ?? .null }; return .null }
    public var string: String? {
        switch self {
        case .string(let s): return s.isEmpty ? nil : s
        case .number(let n): return n.rounded() == n ? String(Int(n)) : String(n)
        default: return nil
        }
    }
    public var double: Double? { if case .number(let n) = self { return n }; return nil }
    public var bool: Bool? { if case .bool(let b) = self { return b }; return nil }
    public var array: [JSONValue] { if case .array(let a) = self { return a }; return [] }
    public var url: URL? {
        guard let s = string, s.lowercased().hasPrefix("https://"), let u = URL(string: s), u.host != nil else { return nil }
        return u
    }
    public var strings: [String] { array.compactMap(\.string) }
    /// Back to Foundation types for a JSON request body.
    public var foundation: Any {
        switch self {
        case .string(let s): return s
        case .number(let n): return n
        case .bool(let b): return b
        case .array(let a): return a.map(\.foundation)
        case .object(let o): return o.mapValues(\.foundation)
        case .null: return NSNull()
        }
    }
    public var doubles: [Double] { array.compactMap(\.double) }
}

public struct ViewCard: Equatable, Sendable, Identifiable {
    public let number: Int
    public let kind: String
    public let data: JSONValue
    public var id: Int { number }

    /// Kinds the shared card views draw. Anything else is dropped at decode time.
    public static let drawn: Set<String> = [
        "quote", "weather", "game", "games", "standings", "clock", "timer", "countdown", "calendar_day", "agenda",
        "reminder", "place", "places", "route", "transit", "flight", "package", "contact", "message_draft",
        "message", "inbox", "media", "now_playing", "fact", "entity", "definition", "conversion", "math",
        "translation", "recipe", "nutrition", "comparison", "list", "steps", "stats", "chart", "news",
        "home", "thermostat", "camera", "question", "lanes", "before_after", "flow", "devices",
        "explore", "compare", "status", "draft", "timeline", "rows", "metric", "preview", "confirm",
        "issue", "pr", "jobs", "site", "mockups",
    ]

    /// Cards are a task's answer, so the task has finished: a "progress" card (older servers sent
    /// counts like "5 of 13 crashes had reports" as one) is drawn as a plain fact, never a bar.
    public init?(json: Any?, number: Int) {
        guard var raw = json as? [String: Any], var kind = raw["kind"] as? String else { return nil }
        if kind == "progress" { raw = Self.settled(progress: raw); kind = "fact" }
        guard Self.drawn.contains(kind) else { return nil }
        self.number = number; self.kind = kind; self.data = JSONValue(any: raw)
    }

    /// A finished task's progress card as a fact: the label is the title, the numbers read "5 of 13"
    /// with the unit once ("5 of 13 crashes", not "5 crashes of 13 crashes"), no bar, no percent.
    static func settled(progress raw: [String: Any]) -> [String: Any] {
        func number(_ v: Any?) -> Double? {
            guard let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
            return n.doubleValue.isFinite ? n.doubleValue : nil
        }
        func text(_ v: Double) -> String { JSONValue.number(v).string ?? "\(v)" }
        var fact: [String: Any] = ["kind": "fact"]
        if let label = raw["label"] as? String, !label.isEmpty { fact["title"] = label }
        if let value = number(raw["value"]) {
            if let total = number(raw["total"]), total > 0 { fact["value"] = "\(text(value)) of \(text(total))" } else { fact["value"] = text(value) }
            if let unit = raw["unit"] as? String, !unit.isEmpty { fact["unit"] = unit }
        }
        if let detail = raw["detail"] as? String, !detail.isEmpty { fact["subtitle"] = detail }
        return fact
    }

    public init(kind: String, number: Int = 1, data: [String: Any]) {
        var raw = data; raw["kind"] = kind
        self.number = number; self.kind = kind; self.data = JSONValue(any: raw)
    }

    public static func list(json: Any?) -> [ViewCard] {
        Array((json as? [Any] ?? []).prefix(6)).enumerated().compactMap { ViewCard(json: $0.element, number: $0.offset + 1) }
    }

    public subscript(key: String) -> JSONValue { data[key] }

    /// One line for VoiceOver and the task list ("Apple, $333.02, up 1.1%").
    public var summary: String {
        switch kind {
        case "quote":
            let name = self["name"].string ?? self["symbol"].string ?? "Price"
            var parts = [name]
            if let p = self["price"].double { parts.append(Self.money(p, currency: self["currency"].string)) }
            if let c = self["change_pct"].double { parts.append("\(c >= 0 ? "up" : "down") \(Self.percent(abs(c)))") }
            return parts.joined(separator: ", ")
        case "weather":
            let temp = self["temp"].double.map { "\(Int($0.rounded()))\(self["unit"].string ?? "°")" }
            return [self["place"].string, temp, self["condition"].string].compactMap { $0 }.joined(separator: ", ")
        case "game":
            let teams = self["teams"].array.map { [$0["abbr"].string ?? $0["name"].string, $0["score"].string].compactMap { $0 }.joined(separator: " ") }
            return ([self["league"].string] + teams + [self["detail"].string]).compactMap { $0 }.joined(separator: " · ")
        case "fact":
            let value = [self["value"].string, self["unit"].string].compactMap { $0 }.joined(separator: " ")
            return [self["title"].string, value.isEmpty ? nil : value].compactMap { $0 }.joined(separator: ", ")
        case "explore":
            let names = self["sections"].array.flatMap { $0["items"].array.compactMap { $0["name"].string } }
            return ([self["title"].string].compactMap { $0 } + names.prefix(4)).joined(separator: ", ")
        case "compare":
            let options = self["options"].array, w = Int(self["winner"].double ?? 0)
            let pick = options.indices.contains(w) ? options[w]["name"].string : nil
            return [self["title"].string, pick.map { "My pick: \($0)" }, self["why"].string].compactMap { $0 }.joined(separator: ", ")
        case "status":
            let items = self["items"].array.map { [$0["name"].string, $0["value"].string ?? $0["state"].string].compactMap { $0 }.joined(separator: " ") }
            return ([self["scope"].string].compactMap { $0 } + items.prefix(6)).joined(separator: ", ")
        case "draft":
            return ["Draft to " + (self["to"].string ?? ""), self["subject"].string].compactMap { $0 }.joined(separator: ", ")
        case "timeline":
            let days = self["days"].array.compactMap { d in d["label"].string.map { "\($0): " + d["items"].array.compactMap { [$0["when"].string, $0["what"].string].compactMap { $0 }.joined(separator: " ") }.prefix(3).joined(separator: "; ") } }
            return ([self["title"].string].compactMap { $0 } + days.prefix(3)).joined(separator: ", ")
        case "rows":
            let items = self["items"].array.map { [$0["ref"].string, $0["name"].string, $0["state"].string].compactMap { $0 }.joined(separator: " ") }
            return ([self["title"].string].compactMap { $0 } + items.prefix(5)).joined(separator: ", ")
        case "metric":
            return [self["label"].string, [self["value"].string, self["unit"].string].compactMap { $0 }.joined(separator: " "),
                    self["delta"].string].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: ", ")
        case "preview":
            return [self["title"].string, self["creator"].string.map { "by \($0)" }, self["sub"].string].compactMap { $0 }.joined(separator: ", ")
        case "issue":
            return [self["ref"].string, self["title"].string, self["state"].string, self["assignee"].string]
                .compactMap { $0 }.joined(separator: ", ")
        case "pr":
            let ref = self["number"].string.map { "Pull request #\($0)" } ?? "Pull request"
            return [ref, self["title"].string, PRState.label(self["state"].string), self["reason"].string]
                .compactMap { $0 }.joined(separator: ", ")
        case "jobs":
            let items = self["items"].array.map { [$0["name"].string, $0["status"].string].compactMap { $0 }.joined(separator: ": ") }
            return ([self["title"].string ?? "Running"] + items.prefix(6)).joined(separator: ", ")
        case "site":
            return [self["title"].string ?? self["url"].url?.host, self["sub"].string].compactMap { $0 }.joined(separator: ", ")
        case "mockups":
            let n = self["items"].array.count
            return [self["title"].string, "\(n) screen\(n == 1 ? "" : "s")"].compactMap { $0 }.joined(separator: ", ")
        case "calendar_day":
            let n = DayOverlaps.pairs(self["events"].array).count
            return ([self["title"].string ?? self["date"].string ?? "Today", "\(self["events"].array.count) events"]
                    + (n > 0 ? ["\(n) overlap\(n == 1 ? "" : "s")"] : [])).compactMap { $0 }.joined(separator: ", ")
        default:
            return self["title"].string ?? self["name"].string ?? self["word"].string ?? kind.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    // MARK: - Formatting shared by both apps

    public static func money(_ value: Double, currency: String? = "USD") -> String {
        let f = NumberFormatter()
        f.numberStyle = .currency
        f.currencyCode = currency ?? "USD"
        f.maximumFractionDigits = value >= 1000 ? 0 : (value < 1 ? 4 : 2)
        f.minimumFractionDigits = value >= 1000 ? 0 : 2
        return f.string(from: NSNumber(value: value)) ?? String(format: "%.2f", value)
    }

    public static func percent(_ value: Double) -> String { String(format: abs(value) < 10 ? "%.2f%%" : "%.1f%%", value) }

    /// "+$3.62", "−0.24%". Changes always show two decimals ("−$0.80", not "−$0.799").
    public static func signed(_ value: Double, money: Bool = false, currency: String? = "USD") -> String {
        let sign = value > 0 ? "+" : value < 0 ? "−" : ""
        guard money else { return sign + Self.percent(abs(value)) }
        let f = NumberFormatter()
        f.numberStyle = .currency
        f.currencyCode = currency ?? "USD"
        f.minimumFractionDigits = 2; f.maximumFractionDigits = abs(value) < 0.01 ? 4 : 2
        return sign + (f.string(from: NSNumber(value: abs(value))) ?? String(format: "%.2f", abs(value)))
    }

    /// A weather card's big number: the temperature now, or for a forecast with no "now" (tomorrow,
    /// Monday) its high and low ("55° / 35°"), never a bare dash.
    public var weatherHeadline: String {
        if let now = self["temp"].double { return Self.degrees(now) }
        switch (self["high"].double, self["low"].double) {
        case let (high?, low?): return "\(Self.degrees(high)) / \(Self.degrees(low))"
        case let (high?, nil): return Self.degrees(high)
        case let (nil, low?): return Self.degrees(low)
        default: return Self.degrees(nil)
        }
    }

    public static func degrees(_ value: Double?) -> String { value.map { "\(Int($0.rounded()))°" } ?? "–" }
}

/// A pull request's state in plain words, and its tone.
public enum PRState {
    public static func label(_ state: String?) -> String? {
        switch state {
        case "open": return "Open"
        case "draft": return "Draft"
        case "conflicts": return "Conflicts"
        case "failing": return "Checks failing"
        case "passing": return "Checks passed"
        case "approved": return "Approved"
        case "merged": return "Merged"
        case "closed": return "Closed"
        default: return nil
        }
    }
    /// good, warn, bad or neutral.
    public static func tone(_ state: String?) -> String {
        switch state {
        case "conflicts": return "warn"
        case "failing": return "bad"
        case "passing", "approved", "merged": return "good"
        default: return "neutral"
        }
    }
}

/// Timed events on one day that overlap (index of the earlier, index of the later), from their
/// machine times; the plugin finds the same pairs (views.day_overlaps). Word times never overlap.
public enum DayOverlaps {
    public static func pairs(_ events: [JSONValue]) -> [(Int, Int)] {
        let timed: [(Date, Date, Int)] = events.enumerated().compactMap { i, e in
            guard e["all_day"].bool != true, let s = e["start"].string.flatMap(EventTimes.date),
                  let f = e["end"].string.flatMap(EventTimes.date), f > s else { return nil }
            return (s, f, i)
        }.sorted { $0.0 != $1.0 ? $0.0 < $1.0 : $0.2 < $1.2 }
        var out: [(Int, Int)] = []
        for a in timed.indices { for b in timed.indices where b > a && timed[b].0 < timed[a].1 { out.append((timed[a].2, timed[b].2)) } }
        return out
    }
    /// Indexes of events that overlap another.
    public static func clashing(_ events: [JSONValue]) -> Set<Int> {
        Set(pairs(events).flatMap { [$0.0, $0.1] })
    }
}
