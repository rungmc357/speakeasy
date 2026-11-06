import Foundation

/// A task's or a session page's `context` (plugin 0.3.37+): which model answered the latest call and
/// how full its context window was — the "Claude Opus 5.5 · 22%" footer chat platforms can show,
/// recorded by the plugin as the work runs. Always an estimate; the apps say so.
public struct ContextUse: Equatable, Sendable {
    /// The whole line: "Claude Opus 5.5 · about 22% of context used · 3 min".
    public var line: String
    /// The line without the model and duration ("about 22% of context used").
    public var use: String
    /// "About 44K of 200K tokens in the latest model call (peak about 31%). An estimate …"
    public var detail: String
    public var model: String?
    public var percent: Int?
    public var peakPercent: Int?
    public var tokens: Int
    public var window: Int?
    /// How long a finished task took ("3 min"); nil while it runs.
    public var took: String?

    public init(line: String, use: String, detail: String, model: String? = nil, percent: Int? = nil,
                peakPercent: Int? = nil, tokens: Int, window: Int? = nil, took: String? = nil) {
        self.line = line; self.use = use; self.detail = detail; self.model = model; self.percent = percent
        self.peakPercent = peakPercent; self.tokens = tokens; self.window = window; self.took = took
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let line = o["line"] as? String, !line.isEmpty else { return nil }
        self.line = line
        use = (o["use"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? line
        detail = o["detail"] as? String ?? ""
        model = (o["model"] as? String).flatMap { $0.isEmpty ? nil : $0 }
        percent = (o["percent"] as? NSNumber)?.intValue
        peakPercent = (o["peak_percent"] as? NSNumber)?.intValue
        tokens = (o["tokens"] as? NSNumber)?.intValue ?? 0
        window = (o["window"] as? NSNumber)?.intValue
        took = (o["took"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    }

    /// The header line when the header already names the model (or the chip does): no model repeated.
    /// "About 22% of context used" with a capital first letter.
    public var useSentence: String { use.prefix(1).uppercased() + use.dropFirst() }

    /// A session header's line: the model and how full its context got, no duration.
    public var modelAndUse: String { model.map { "\($0) · \(use)" } ?? useSentence }

    /// A gauge that fills with the share used; a plain one when the size isn't known.
    public var symbol: String {
        guard let p = percent else { return "gauge.with.dots.needle.0percent" }
        switch p {
        case ..<34: return "gauge.with.dots.needle.33percent"
        case ..<67: return "gauge.with.dots.needle.50percent"
        default: return "gauge.with.dots.needle.67percent"
        }
    }

    /// Nearly full: the line gets a warmer tint so a chat about to be summarized is noticeable.
    public var isNearlyFull: Bool { (percent ?? 0) >= 80 }

    public var accessibilityText: String { "\(line). Estimate." }

    // Invented demo figures for screenshots and previews.
    public static let demo = ContextUse(
        line: "Claude Opus 5.5 · about 22% of context used", use: "about 22% of context used",
        detail: "About 44K of 200K tokens in the latest model call (peak about 31%). An estimate from the token counts the provider reported for the latest model call.",
        model: "Claude Opus 5.5", percent: 22, peakPercent: 31, tokens: 44_000, window: 200_000)
    public static let demoDone = ContextUse(
        line: "Claude Opus 5.5 · about 38% of context used · 4 min", use: "about 38% of context used",
        detail: "About 76K of 200K tokens in the latest model call. An estimate from the token counts the provider reported for the latest model call.",
        model: "Claude Opus 5.5", percent: 38, tokens: 76_000, window: 200_000, took: "4 min")
    public static let demoFast = ContextUse(
        line: "GPT-6.1 Mini · about 6% of context used · 23 s", use: "about 6% of context used",
        detail: "About 24K of 400K tokens in the latest model call. An estimate from the token counts the provider reported for the latest model call.",
        model: "GPT-6.1 Mini", percent: 6, tokens: 24_000, window: 400_000, took: "23 s")
}
