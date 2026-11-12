import Foundation

// Which model works a chat and how hard it thinks (servers with `capabilities.model_choice`;
// docs/API.md "Models"). The catalog is Hermes' own (GET /voice/models); a choice is saved per chat
// (PUT /voice/history/{id}/model), rides on a New chat's first message (POST /voice/work), or is
// Speakeasy's default (settings `jobs`). Clients never invent models: they pick from the list.

/// One reasoning level (`none` … `max`), weakest first.
public enum ReasoningLevel: String, CaseIterable, Sendable, Identifiable, Comparable {
    case none, minimal, low, medium, high, xhigh, max
    public var id: String { rawValue }
    /// "Medium", "Max", "Off". A model's strongest level reads "Max" whichever id it has (GPT's
    /// `xhigh`, Claude's `max`), so Home and a chat never name the same top level two ways (SPK-309
    /// D1/D11). Only a list holding both says "Extra high" for `xhigh` (`label(among:)`).
    public var label: String {
        switch self {
        case .none: return "Off"
        case .minimal: return "Minimal"
        case .low: return "Low"
        case .medium: return "Medium"
        case .high: return "High"
        case .xhigh, .max: return "Max"
        }
    }
    /// The label within a list of levels: `xhigh` is "Extra high" only beside a `max`.
    public func label(among levels: [ReasoningLevel]) -> String {
        self == .xhigh && levels.contains(.max) ? "Extra high" : label
    }
    public func short(among levels: [ReasoningLevel]) -> String {
        self == .xhigh && levels.contains(.max) ? "XHigh" : short
    }
    /// For a segmented control with several levels: "Med", "Max".
    public var short: String {
        switch self {
        case .none: return "Off"
        case .minimal: return "Min"
        case .medium: return "Med"
        default: return label
        }
    }
    public static func < (a: ReasoningLevel, b: ReasoningLevel) -> Bool {
        allCases.firstIndex(of: a)! < allCases.firstIndex(of: b)!
    }
    /// The level a model takes: itself, else the nearest weaker one, else its weakest (none stays
    /// unset when the model can't switch reasoning off). Mirrors the plugin's models.clamp.
    public func clamped(to supported: [ReasoningLevel]) -> ReasoningLevel? {
        guard !supported.isEmpty else { return nil }
        if supported.contains(self) { return self }
        if self == .none { return nil }
        let on = supported.filter { $0 != .none }
        return on.last { $0 < self } ?? on.first
    }
}

/// One model Hermes offers.
public struct ModelOption: Equatable, Sendable, Identifiable, Hashable {
    /// "provider/model": what a choice names.
    public var id: String
    public var name: String
    public var provider: String
    public var providerName: String
    /// Empty: it doesn't reason (no segmented control).
    public var levels: [ReasoningLevel]
    /// Speakeasy's default (or Hermes' when Speakeasy has none).
    public var isDefault: Bool

    public init(id: String, name: String, provider: String, providerName: String, levels: [ReasoningLevel],
                isDefault: Bool = false) {
        self.id = id; self.name = name; self.provider = provider; self.providerName = providerName
        self.levels = levels; self.isDefault = isDefault
    }

    init?(json: Any?, providerName fallback: String) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) else { return nil }
        self.init(id: id, name: nonEmpty(o["name"]) ?? id, provider: nonEmpty(o["provider"]) ?? "",
                  providerName: nonEmpty(o["provider_name"]) ?? fallback,
                  levels: (o["reasoning_levels"] as? [String] ?? []).compactMap(ReasoningLevel.init(rawValue:)),
                  isDefault: o["default"] as? Bool ?? false)
    }
}

public struct ModelProvider: Equatable, Sendable, Identifiable {
    public var id: String
    public var name: String
    /// A model server the user runs themselves (Hermes `providers:` entries).
    public var local: Bool
    public var models: [ModelOption]
    public init(id: String, name: String, local: Bool = false, models: [ModelOption]) {
        self.id = id; self.name = name; self.local = local; self.models = models
    }
}

/// A {model, reasoning} pair; nil halves mean "the default".
public struct ModelChoice: Equatable, Sendable, Hashable {
    public var model: String?
    public var reasoning: ReasoningLevel?
    public init(model: String? = nil, reasoning: ReasoningLevel? = nil) { self.model = model; self.reasoning = reasoning }
    public var isEmpty: Bool { model == nil && reasoning == nil }

    init?(json: Any?) {
        guard let o = json as? [String: Any] else { return nil }
        self.init(model: nonEmpty(o["model"]), reasoning: (o["reasoning"] as? String).flatMap(ReasoningLevel.init(rawValue:)))
    }

    /// `{model, reasoning}` for a request body; "default" clears a half (PUT), absent halves are left out.
    public func body(clearing: Bool = false) -> [String: Any] {
        var out: [String: Any] = [:]
        if let model { out["model"] = model } else if clearing { out["model"] = "default" }
        if let reasoning { out["reasoning"] = reasoning.rawValue } else if clearing { out["reasoning"] = "default" }
        return out
    }
}

/// GET /voice/models.
public struct ModelCatalog: Equatable, Sendable {
    public var available: Bool
    public var providers: [ModelProvider]
    /// What a chat with no choice runs on (Speakeasy's default, else Hermes').
    public var defaultChoice: ModelChoice
    /// "speakeasy" or "hermes".
    public var defaultSource: String
    public var hermesDefault: ModelChoice
    public var fast: ModelChoice
    public var smart: ModelChoice

    public init(available: Bool = true, providers: [ModelProvider], defaultChoice: ModelChoice = ModelChoice(),
                defaultSource: String = "hermes", hermesDefault: ModelChoice = ModelChoice(),
                fast: ModelChoice = ModelChoice(), smart: ModelChoice = ModelChoice()) {
        self.available = available; self.providers = providers; self.defaultChoice = defaultChoice
        self.defaultSource = defaultSource; self.hermesDefault = hermesDefault; self.fast = fast; self.smart = smart
    }

    public init(json: Any?) {
        let o = json as? [String: Any] ?? [:]
        let providers: [ModelProvider] = (o["providers"] as? [Any] ?? []).compactMap { raw in
            guard let p = raw as? [String: Any], let id = nonEmpty(p["id"]) else { return nil }
            let name = nonEmpty(p["name"]) ?? id
            let models = (p["models"] as? [Any] ?? []).compactMap { ModelOption(json: $0, providerName: name) }
            return models.isEmpty ? nil : ModelProvider(id: id, name: name, local: p["local"] as? Bool ?? false, models: models)
        }
        let def = o["default"] as? [String: Any]
        self.init(available: (o["available"] as? Bool ?? false) && !providers.isEmpty, providers: providers,
                  defaultChoice: ModelChoice(json: def) ?? ModelChoice(),
                  defaultSource: nonEmpty(def?["source"]) ?? "hermes",
                  hermesDefault: ModelChoice(json: o["hermes_default"]) ?? ModelChoice(),
                  fast: ModelChoice(json: o["fast"]) ?? ModelChoice(), smart: ModelChoice(json: o["smart"]) ?? ModelChoice())
    }

    public static let path = "/voice/models"

    public var allModels: [ModelOption] { providers.flatMap(\.models) }
    public func option(_ id: String?) -> ModelOption? {
        guard let id else { return nil }
        return allModels.first { $0.id == id }
    }

    /// The model + level a chat actually runs on: its own choice over the default, half by half,
    /// the level clamped to what the model takes.
    public func effective(_ chat: ModelChoice?) -> (model: ModelOption?, reasoning: ReasoningLevel?) {
        let model = option(chat?.model) ?? option(defaultChoice.model)
        let wanted = chat?.reasoning ?? defaultChoice.reasoning
        guard let model else { return (nil, wanted) }
        return (model, wanted?.clamped(to: model.levels) ?? (model.levels.isEmpty ? nil : nil))
    }

    /// The chip's words: "GPT-5.5 · Medium", "Claude Opus 5.5" (no level known), "Qwen3.8 Coder"
    /// (doesn't reason).
    public func chipLabel(_ chat: ModelChoice?) -> String {
        let (model, reasoning) = effective(chat)
        let name = model?.name ?? "Default model"
        guard let reasoning, !(model?.levels.isEmpty ?? false) else { return name }
        return "\(name) · \(reasoning.label)"
    }
}

/// A run's model as the server describes it (tasks' `model`, a session page's `model` / `next_model`).
public struct ModelBadge: Equatable, Sendable {
    public var id: String
    public var name: String
    public var providerName: String
    public var reasoning: ReasoningLevel?
    /// "voice", "task", "chat", "default" or "hermes": where the choice came from.
    public var source: String
    /// Hermes reported it ran on this model (not just what was asked).
    public var served: Bool
    public var unavailable = false

    public init(id: String, name: String, providerName: String = "", reasoning: ReasoningLevel? = nil,
                source: String = "hermes", served: Bool = false) {
        self.id = id; self.name = name; self.providerName = providerName; self.reasoning = reasoning
        self.source = source; self.served = served
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) else { return nil }
        self.init(id: id, name: nonEmpty(o["name"]) ?? id, providerName: nonEmpty(o["provider_name"]) ?? "",
                  reasoning: (o["reasoning"] as? String).flatMap(ReasoningLevel.init(rawValue:)),
                  source: nonEmpty(o["source"]) ?? "hermes", served: o["served"] as? Bool ?? false)
        unavailable = o["unavailable"] as? Bool ?? false
    }

    /// "GPT-5.5 · Medium".
    public var label: String { reasoning.map { "\(name) · \($0.label)" } ?? name }
}

/// PUT /voice/history/{session_id}/model: Use for this chat.
public struct SessionModelRequest: Equatable, Sendable {
    public var sessionID: String
    public var choice: ModelChoice
    public init(sessionID: String, choice: ModelChoice) { self.sessionID = sessionID; self.choice = choice }
    public var path: String { HistoryConversation.path(sessionID) + "/model" }
    /// Both halves always sent: a cleared half goes back to the default.
    public var body: [String: Any] { choice.body(clearing: true) }
}

/// Settings › Jobs: Speakeasy's default model and reasoning (settings `jobs`; "" = Hermes' own).
public struct JobModelDefaults: Equatable, Sendable {
    public var model: String?
    public var reasoning: ReasoningLevel?
    public init(model: String? = nil, reasoning: ReasoningLevel? = nil) { self.model = model; self.reasoning = reasoning }
    /// The `GET`/`PATCH /voice/settings` reply. The server wraps it as `{"settings": {...}}`; a bare object
    /// is accepted too, as `ServerSettings.decode` does. nil when there is no `jobs` section: that is an
    /// unreadable reply, not "nothing saved" (SPK-404: reading it as nothing saved erased the pick).
    public init?(settingsJSON: Any?) {
        let reply = settingsJSON as? [String: Any]
        let settings = (reply?["settings"] as? [String: Any]) ?? reply
        guard let jobs = settings?["jobs"] as? [String: Any] else { return nil }
        self.init(model: nonEmpty(jobs["model"]),
                  reasoning: (jobs["reasoning"] as? String).flatMap(ReasoningLevel.init(rawValue:)))
    }
    /// PATCH /voice/settings body.
    public var patchBody: [String: Any] {
        ["jobs": ["model": model ?? "", "reasoning": reasoning?.rawValue ?? ""]]
    }
}

extension ModelCatalog {
    /// Demo and screenshot data (an invented setup in the server's shape).
    public static let sample = ModelCatalog(json: [
        "available": true,
        "providers": [
            ["id": "openai-codex", "name": "OpenAI", "models": [
                ["id": "openai-codex/gpt-5.5", "name": "GPT-5.5", "provider": "openai-codex", "provider_name": "OpenAI",
                 "default": true, "reasoning_levels": ["low", "medium", "high", "xhigh"]],
                ["id": "openai-codex/gpt-5.5-mini", "name": "GPT-5.5 Mini", "provider": "openai-codex", "provider_name": "OpenAI",
                 "reasoning_levels": ["low", "medium", "high"]],
            ]],
            ["id": "anthropic", "name": "Anthropic", "models": [
                ["id": "anthropic/claude-opus-5-5", "name": "Claude Opus 5.5", "provider": "anthropic", "provider_name": "Anthropic",
                 "reasoning_levels": ["none", "low", "medium", "high", "max"]],
                ["id": "anthropic/claude-sonnet-5", "name": "Claude Sonnet 5", "provider": "anthropic", "provider_name": "Anthropic",
                 "reasoning_levels": ["none", "low", "medium", "high"]],
                ["id": "anthropic/claude-haiku-5-5", "name": "Claude Haiku 5.5", "provider": "anthropic", "provider_name": "Anthropic",
                 "reasoning_levels": ["none", "low", "medium", "high"]],
            ]],
            ["id": "studio", "name": "Studio", "local": true, "models": [
                ["id": "studio/qwen3.8-coder", "name": "Qwen3.8 Coder", "provider": "studio", "provider_name": "Studio",
                 "reasoning_levels": []],
            ]],
        ],
        "default": ["model": "openai-codex/gpt-5.5", "reasoning": "medium", "source": "hermes"],
        "hermes_default": ["model": "openai-codex/gpt-5.5", "reasoning": "medium"],
        "fast": ["model": "openai-codex/gpt-5.5-mini", "reasoning": "low"],
        "smart": ["model": "openai-codex/gpt-5.5", "reasoning": "high"],
    ])
}
