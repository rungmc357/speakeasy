import Foundation

/// The model chip and its sheet, as state (the iPhone/iPad and Mac views draw it). Picking a model
/// keeps the level when the model takes it, else the nearest weaker one; "Use for this chat" saves
/// the pick for the chat (or, on New chat, for its first job), "Make default" saves it as Speakeasy's
/// default for every chat without its own.
public struct ModelPickerState: Equatable, Sendable {
    public enum Scope: String, Sendable { case chat, makeDefault }

    public var catalog: ModelCatalog
    /// The chat's own pick (nil = it follows the default).
    public var chatChoice: ModelChoice?
    /// The sheet's working selection.
    public var model: ModelOption?
    public var reasoning: ReasoningLevel?

    private var authoritative: ModelBadge?
    private var existingConversation: Bool
    private var initialModel: String?
    private var initialReasoning: ReasoningLevel?

    public init(catalog: ModelCatalog, chatChoice: ModelChoice?, nextModel: ModelBadge? = nil,
                existingConversation: Bool = false) {
        self.catalog = catalog
        self.chatChoice = chatChoice.flatMap { $0.isEmpty ? nil : $0 }
        self.authoritative = nextModel
        self.existingConversation = existingConversation
        if let nextModel {
            self.model = catalog.option(nextModel.id) ?? ModelOption(id: nextModel.id, name: nextModel.name,
                provider: "", providerName: nextModel.providerName, levels: [])
            self.reasoning = nextModel.reasoning
        } else if existingConversation && self.chatChoice == nil {
            self.model = nil; self.reasoning = nil
        } else {
            let (model, reasoning) = catalog.effective(self.chatChoice)
            self.model = model; self.reasoning = reasoning
        }
        initialModel = self.model?.id; initialReasoning = self.reasoning
    }

    /// The server's next-send target, not the current app default or historical served model.
    public var chipLabel: String {
        if let authoritative { return authoritative.label + (authoritative.unavailable ? " · Unavailable" : "") }
        if existingConversation && chatChoice == nil { return "Model unknown" }
        return catalog.chipLabel(chatChoice)
    }
    /// Only new chats/default-resolved conversations follow the app default.
    public var followsDefault: Bool { authoritative.map { ["default", "hermes"].contains($0.source) } ?? (!existingConversation && chatChoice == nil) }
    /// The segmented control's levels (none: hidden, the model doesn't reason).
    public var levels: [ReasoningLevel] { model?.levels ?? [] }

    public mutating func select(_ option: ModelOption) {
        model = option
        let wanted = reasoning ?? catalog.defaultChoice.reasoning
        reasoning = wanted?.clamped(to: option.levels)
        if reasoning == nil, !option.levels.isEmpty {
            reasoning = option.levels.contains(.medium) ? .medium : option.levels.first { $0 != .none }
        }
    }

    public mutating func select(_ level: ReasoningLevel) {
        guard levels.contains(level) else { return }
        reasoning = level
    }

    /// The session header's "worked on by" line is shown only when the chat ran on something other
    /// than what the composer's chip says now (the chip is the one place the model shows otherwise).
    public func headerShows(_ badge: ModelBadge) -> Bool {
        guard let id = initialModel else { return true }
        if id != badge.id { return true }
        if let ran = badge.reasoning, let now = initialReasoning { return ran != now }
        return false
    }

    /// The selection as a choice to save.
    public var selection: ModelChoice {
        ModelChoice(model: model?.id, reasoning: levels.isEmpty ? nil : reasoning)
    }

    /// The sheet's selection differs from what the chat runs on now.
    public var changed: Bool {
        return initialModel != model?.id || (levels.isEmpty ? false : initialReasoning != reasoning)
    }

    /// Apply: the chat's new pick (also for "Make default", so the chat shows it at once: a chat that
    /// followed the default keeps following it).
    public mutating func apply(_ scope: Scope) {
        authoritative = nil
        initialModel = model?.id; initialReasoning = reasoning
        switch scope {
        case .chat:
            chatChoice = selection
        case .makeDefault:
            catalog.defaultChoice = selection
            catalog.defaultSource = "speakeasy"
            for p in catalog.providers.indices {
                for m in catalog.providers[p].models.indices {
                    catalog.providers[p].models[m].isDefault = catalog.providers[p].models[m].id == selection.model
                }
            }
            if chatChoice != nil { chatChoice = selection }
        }
    }

    /// Back to the default for this chat.
    public mutating func useDefault() {
        chatChoice = nil
        authoritative = nil; existingConversation = false
        (model, reasoning) = catalog.effective(nil)
        initialModel = model?.id; initialReasoning = reasoning
    }
}
