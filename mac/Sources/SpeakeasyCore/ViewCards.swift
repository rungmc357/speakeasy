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
        "translation", "recipe", "nutrition", "comparison", "list", "steps", "stats", "chart", "progress", "news",
        "home", "thermostat", "camera", "question", "lanes", "before_after", "flow", "devices",
    ]

    public init?(json: Any?, number: Int) {
        guard let raw = json as? [String: Any], let kind = raw["kind"] as? String, Self.drawn.contains(kind) else { return nil }
        self.number = number; self.kind = kind; self.data = JSONValue(any: raw)
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

    public static func degrees(_ value: Double?) -> String { value.map { "\(Int($0.rounded()))°" } ?? "–" }
}
