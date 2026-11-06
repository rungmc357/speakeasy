import Foundation

/// A session page's `usage` (`GET /voice/history/{id}`, plugin 0.3.37+): what the conversation used,
/// added up by the plugin from Hermes' own records (its helpers and a continued chat's Speakeasy
/// follow-ups included). Every figure is an estimate; the apps always say so.
public struct ChatUsage: Equatable, Sendable {
    public enum Basis: String, Equatable, Sendable { case estimated, included, unknown, mixed }
    public struct Line: Equatable, Sendable, Identifiable {
        public var label: String
        public var value: String
        public var id: String { label }
        public init(label: String, value: String) { self.label = label; self.value = value }
    }
    public struct ModelShare: Equatable, Sendable, Identifiable {
        public var name: String
        public var tokens: Int
        public var value: String
        public var id: String { name }
        public init(name: String, tokens: Int, value: String) { self.name = name; self.tokens = tokens; self.value = value }
    }

    public var basis: Basis
    /// One line for the header ("About $2.50 · estimate", "Included in your ChatGPT plan · 42K tokens").
    public var summary: String
    /// nil when nothing was priced (a plan sign-in or a model without a price).
    public var costUSD: Double?
    public var tokens: Int
    public var lines: [Line]
    public var models: [ModelShare]
    /// The estimate caveat shown under the details.
    public var note: String

    public static let defaultNote = "Estimated from the token counts and price list Hermes keeps. Your provider's bill may differ, and plan sign-ins don't bill per use."

    public init(basis: Basis, summary: String, costUSD: Double? = nil, tokens: Int, lines: [Line] = [],
                models: [ModelShare] = [], note: String = ChatUsage.defaultNote) {
        self.basis = basis; self.summary = summary; self.costUSD = costUSD; self.tokens = tokens
        self.lines = lines; self.models = models; self.note = note
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let tokens = (o["tokens"] as? NSNumber)?.intValue, tokens > 0 else { return nil }
        let basis = (o["basis"] as? String).flatMap(Basis.init(rawValue:)) ?? .unknown
        var summary = (o["summary"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let cost = (o["cost_usd"] as? NSNumber)?.doubleValue
        if summary.isEmpty { summary = "\(tokens) tokens" }
        // The label is never lost, whatever an older/newer plugin words: a priced figure says "estimate".
        if cost != nil, !summary.localizedCaseInsensitiveContains("estimate") { summary += " · estimate" }
        let lines = (o["lines"] as? [Any] ?? []).compactMap { any -> Line? in
            guard let l = any as? [String: Any], let label = l["label"] as? String, let value = l["value"] as? String,
                  !label.isEmpty, !value.isEmpty else { return nil }
            return Line(label: label, value: value)
        }
        let models = (o["models"] as? [Any] ?? []).compactMap { any -> ModelShare? in
            guard let m = any as? [String: Any], let name = m["name"] as? String, !name.isEmpty,
                  let value = m["value"] as? String else { return nil }
            return ModelShare(name: name, tokens: (m["tokens"] as? NSNumber)?.intValue ?? 0, value: value)
        }
        let note = (o["note"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? Self.defaultNote
        self.init(basis: basis, summary: summary, costUSD: cost, tokens: tokens, lines: lines, models: models, note: note)
    }

    /// The header line's SF Symbol.
    public var symbol: String {
        switch basis {
        case .estimated, .mixed: return "dollarsign.circle"
        case .included: return "checkmark.seal"
        case .unknown: return "gauge.with.dots.needle.33percent"
        }
    }

    /// Spoken by VoiceOver for the header line.
    public var accessibilityText: String { "Usage: \(summary). Shows details." }

    /// Invented demo usage for screenshots.
    public static let demo = ChatUsage(
        basis: .estimated, summary: "About $1.84 · estimate", costUSD: 1.84, tokens: 2_412_000,
        lines: [Line(label: "Cost", value: "About $1.84 (estimate)"),
                Line(label: "Tokens", value: "2.4M total"),
                Line(label: "Sent to the model", value: "2.4M (2.1M reused from cache)"),
                Line(label: "Written by the model", value: "18.6K (incl. 4.2K thinking)"),
                Line(label: "Model calls", value: "41"),
                Line(label: "Includes", value: "3 helpers")],
        models: [ModelShare(name: "Claude Opus 5.5", tokens: 2_100_000, value: "2.1M tokens · about $1.71"),
                 ModelShare(name: "GPT-5.6 Mini", tokens: 312_000, value: "312K tokens · about 13¢")])
    public static let demoIncluded = ChatUsage(
        basis: .included, summary: "Included in your ChatGPT plan · 486K tokens", tokens: 486_000,
        lines: [Line(label: "Cost", value: "Nothing billed per use"),
                Line(label: "Tokens", value: "486K total"),
                Line(label: "Sent to the model", value: "479K (402K reused from cache)"),
                Line(label: "Written by the model", value: "7.1K"),
                Line(label: "Model calls", value: "12")],
        models: [ModelShare(name: "GPT-5.6", tokens: 486_000, value: "486K tokens · plan")])
}
