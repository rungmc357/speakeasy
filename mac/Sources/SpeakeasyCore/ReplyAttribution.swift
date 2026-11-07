import Foundation

/// The evidence for this reply, independent of the composer's next-model choice.
public struct ReplyAttribution: Equatable, Sendable {
    public var model: ModelBadge
    public var fallback: Bool
    public init(model: ModelBadge, fallback: Bool = false) {
        self.model = model; self.fallback = fallback
    }
    public init?(json: Any?) {
        guard let object = json as? [String: Any], let model = ModelBadge(json: object),
              !["unknown", "default", "none", "null"].contains(model.id.lowercased().split(separator: "/").last.map(String.init) ?? "unknown") else { return nil }
        self.init(model: model, fallback: object["fallback"] as? Bool ?? false)
    }
    public var label: String { model.name + (fallback ? " · fallback" : "") }
    public static func details(for attribution: Self?, at: Date?) -> String {
        if let attribution { return attribution.details(at: at) }
        return "Model not recorded" + (at.map { "\nTime: \($0.formatted(date: .abbreviated, time: .standard))" } ?? "")
    }
    /// Effort/provider changes matter too; provenance alone does not create clutter.
    public func differs(from other: Self?) -> Bool {
        guard let other else { return true }
        return model.id != other.model.id || model.providerName != other.model.providerName ||
            model.reasoning != other.model.reasoning || fallback != other.fallback
    }
    public func details(at: Date?) -> String {
        var lines = ["Model: \(model.name)"]
        if !model.providerName.isEmpty { lines.append("Provider: \(model.providerName)") }
        if let reasoning = model.reasoning { lines.append("Reasoning: \(reasoning.label)") }
        if fallback { lines.append("Fallback") }
        switch model.source {
        case "session": lines.append("Source: session record")
        case "run": lines.append(model.served ? "Source: served task model" : "Source: requested task model")
        default: lines.append("Source: reply record")
        }
        if let at { lines.append("Time: \(at.formatted(date: .abbreviated, time: .standard))") }
        return lines.joined(separator: "\n")
    }
}

public extension HistoryConversation {
    /// Computed over the full loaded thread (including appended pages), not per row or fetch.
    var replyModelLabels: Set<String> {
        var previous: ReplyAttribution?
        var shown = Set<String>()
        for entry in entries where entry.kind == .assistant {
            if let attribution = entry.attribution {
                if attribution.differs(from: previous) { shown.insert(entry.id) }
                previous = attribution
            } else {
                previous = nil // unknown breaks continuity, never inherits a known label
            }
        }
        return shown
    }
}
