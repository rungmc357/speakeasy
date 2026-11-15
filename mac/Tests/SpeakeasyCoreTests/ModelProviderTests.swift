import XCTest
@testable import SpeakeasyCore

/// SPK-508: every picker says which provider serves a model, groups models by provider and flags
/// pay-per-use providers. The same model from two providers stays two options.
final class ModelProviderTests: XCTestCase {
    static let json: [String: Any] = [
        "available": true,
        "providers": [
            ["id": "anthropic", "name": "Anthropic", "label": "Claude subscription", "metered": false, "models": [
                ["id": "anthropic/claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "provider": "anthropic",
                 "provider_name": "Anthropic", "provider_label": "Claude subscription", "metered": false,
                 "default": true, "reasoning_levels": ["none", "low", "medium", "high"]],
            ]],
            ["id": "openrouter", "name": "OpenRouter", "label": "OpenRouter — pay per use", "metered": true, "models": [
                ["id": "openrouter/anthropic/claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "provider": "openrouter",
                 "provider_name": "OpenRouter", "provider_label": "OpenRouter — pay per use", "metered": true,
                 "reasoning_levels": ["none", "low", "medium", "high"]],
            ]],
            // An older plugin: no label or flag; the app derives the same words.
            ["id": "openai-codex", "name": "OpenAI Codex", "models": [
                ["id": "openai-codex/gpt-6.1-terra", "name": "GPT-6.1 Terra", "provider": "openai-codex",
                 "provider_name": "OpenAI Codex", "reasoning_levels": ["low", "medium", "high"]],
            ]],
            ["id": "nous", "name": "Nous Portal", "models": [
                ["id": "nous/hermes-5-405b", "name": "Hermes 5 405B", "reasoning_levels": []],
            ]],
            ["id": "studio", "name": "Studio", "local": true, "models": [
                ["id": "studio/qwen3.8-coder", "name": "Qwen3.8 Coder", "reasoning_levels": []],
            ]],
        ],
        "default": ["model": "anthropic/claude-sonnet-5-5", "reasoning": "medium", "source": "hermes"],
        "hermes_default": ["model": "anthropic/claude-sonnet-5-5", "reasoning": "medium"],
    ]

    func testCatalogCarriesProviderLabelAndMeteredFromServerOrDerived() {
        let c = ModelCatalog(json: Self.json)
        XCTAssertEqual(c.providers.map(\.label), ["Claude subscription", "OpenRouter — pay per use", "ChatGPT sign-in",
                                                  "Nous Portal — pay per use", "Studio"])
        XCTAssertEqual(c.providers.map(\.metered), [false, true, false, true, false])
        XCTAssertEqual(c.option("openai-codex/gpt-6.1-terra")?.providerLabel, "ChatGPT sign-in")
        XCTAssertEqual(c.option("nous/hermes-5-405b")?.metered, true)
        XCTAssertEqual(c.option("nous/hermes-5-405b")?.provider, "nous")   // no provider field: from the id
    }

    func testSameModelFromTwoProvidersStaysTwoOptions() {
        let c = ModelCatalog(json: Self.json)
        let sonnets = c.allModels.filter { $0.name == "Claude Sonnet 5.5" }
        XCTAssertEqual(sonnets.map(\.id), ["anthropic/claude-sonnet-5-5", "openrouter/anthropic/claude-sonnet-5-5"])
        XCTAssertEqual(sonnets.map(\.withProvider), ["Claude Sonnet 5.5 · Claude subscription",
                                                     "Claude Sonnet 5.5 · OpenRouter — pay per use"])
        XCTAssertEqual(c.provider(of: "openrouter/anthropic/claude-sonnet-5-5")?.id, "openrouter")
    }

    func testPickerSelectionAlwaysNamesItsProvider() {
        var p = ModelPickerState(catalog: ModelCatalog(json: Self.json), chatChoice: nil)
        XCTAssertEqual(p.selectionLine, "Claude Sonnet 5.5 · Claude subscription")
        XCTAssertEqual(p.selectedProviderID, "anthropic")
        XCTAssertEqual(p.chipLabelWithProvider, "Claude Sonnet 5.5 · Medium · Claude subscription")
        p.select(p.catalog.option("openrouter/anthropic/claude-sonnet-5-5")!)
        XCTAssertEqual(p.selectionLine, "Claude Sonnet 5.5 · OpenRouter — pay per use")
        XCTAssertEqual(p.selectedProviderID, "openrouter")
        p.apply(.chat)
        XCTAssertEqual(p.chipProvider, "OpenRouter — pay per use")
    }

    func testServerBadgeNamesItsProvider() {
        let badge = ModelBadge(json: ["id": "openrouter/anthropic/claude-sonnet-5-5", "name": "Claude Sonnet 5.5",
                                      "provider_name": "OpenRouter", "provider_label": "OpenRouter — pay per use",
                                      "metered": true, "source": "chat"])!
        XCTAssertEqual(badge.providerLabel, "OpenRouter — pay per use")
        XCTAssertTrue(badge.metered)
        let old = ModelBadge(json: ["id": "openai-codex/gpt-5.5", "name": "GPT-5.5", "provider_name": "OpenAI Codex"])!
        XCTAssertEqual(old.providerLabel, "ChatGPT sign-in")
        let p = ModelPickerState(catalog: ModelCatalog(json: Self.json), chatChoice: nil, nextModel: badge,
                                 existingConversation: true)
        XCTAssertEqual(p.chipProvider, "OpenRouter — pay per use")
        XCTAssertEqual(p.selectedProviderID, "openrouter")
    }

    func testHomeOptionsGroupByProviderAndAutoNamesItsProvider() {
        let s = HomeModelStatus(json: [
            "coordinator": [
                "source": "auto",
                "auto": ["id": "anthropic/claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "provider_name": "Anthropic",
                         "provider": "anthropic", "provider_label": "Claude subscription", "metered": false],
                "options": [
                    ["id": "anthropic/claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "provider_name": "Anthropic"],
                    ["id": "openai-codex/gpt-6.1-terra", "name": "GPT-6.1 Terra", "provider_name": "OpenAI Codex"],
                    ["id": "anthropic/claude-haiku-5-5", "name": "Claude Haiku 5.5", "provider_name": "Anthropic"],
                    ["id": "openrouter/anthropic/claude-sonnet-5-5", "name": "Claude Sonnet 5.5", "provider_name": "OpenRouter",
                     "provider_label": "OpenRouter — pay per use", "metered": true],
                ],
            ],
        ])
        XCTAssertEqual(s.autoLabel, "Auto — Claude Sonnet 5.5 · Claude subscription")
        let groups = HomeModelStatus.groups(s.options)
        XCTAssertEqual(groups.map(\.label), ["Claude subscription", "ChatGPT sign-in", "OpenRouter — pay per use"])
        XCTAssertEqual(groups[0].options.map(\.name), ["Claude Sonnet 5.5", "Claude Haiku 5.5"])
        XCTAssertEqual(groups.map(\.metered), [false, false, true])
    }

    func testDerivedLabels() {
        XCTAssertEqual(ProviderLabel.describe(id: "openrouter", name: "OpenRouter").label, "OpenRouter — pay per use")
        XCTAssertEqual(ProviderLabel.describe(id: "venice", name: "Venice").label, "Venice")
        XCTAssertFalse(ProviderLabel.describe(id: "venice", name: "Venice").metered)
        XCTAssertEqual(ProviderLabel.describe(id: "custom:proxy", name: "My proxy", local: true).label, "My proxy")
        XCTAssertEqual(ProviderLabel.line("Qwen3.8 Coder", "Studio"), "Qwen3.8 Coder · Studio")
    }
}
