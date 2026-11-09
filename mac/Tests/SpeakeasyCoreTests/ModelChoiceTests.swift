import XCTest
@testable import SpeakeasyCore

/// Per-chat model and reasoning (ModelChoice.swift, ModelPickerState.swift; docs/API.md "Models").
final class ModelChoiceTests: XCTestCase {
    static let json: [String: Any] = [
        "available": true,
        "providers": [
            ["id": "anthropic", "name": "Anthropic", "current": true, "local": false, "models": [
                ["id": "anthropic/claude-opus-5-5", "model": "claude-opus-5-5", "provider": "anthropic",
                 "provider_name": "Anthropic", "name": "Claude Opus 5.5", "default": true,
                 "reasoning_levels": ["none", "low", "medium", "high", "max"]],
                ["id": "anthropic/claude-haiku-5-5", "model": "claude-haiku-5-5", "provider": "anthropic",
                 "provider_name": "Anthropic", "name": "Claude Haiku 5.5", "default": false,
                 "reasoning_levels": ["none", "low", "medium", "high"]],
            ]],
            ["id": "openai-codex", "name": "OpenAI Codex", "models": [
                ["id": "openai-codex/gpt-5.5", "name": "GPT-5.5", "provider": "openai-codex",
                 "reasoning_levels": ["low", "medium", "high", "xhigh"]],
            ]],
            ["id": "studio-qwen", "name": "Studio Qwen", "local": true, "models": [
                ["id": "studio-qwen/qwen3.8-coder", "name": "Qwen3.8 Coder", "reasoning_levels": []],
            ]],
            ["id": "empty", "name": "Empty", "models": []],
        ],
        "default": ["model": "anthropic/claude-opus-5-5", "reasoning": "medium", "source": "hermes"],
        "hermes_default": ["model": "anthropic/claude-opus-5-5", "reasoning": "medium"],
        "fast": ["model": "anthropic/claude-haiku-5-5", "reasoning": "low"],
        "smart": ["model": "anthropic/claude-opus-5-5", "reasoning": "high"],
    ]
    var catalog: ModelCatalog { ModelCatalog(json: Self.json) }

    func testCatalogParsesProvidersDefaultsAndLevels() {
        let c = catalog
        XCTAssertTrue(c.available)
        XCTAssertEqual(c.providers.map(\.id), ["anthropic", "openai-codex", "studio-qwen"], "empty providers dropped")
        XCTAssertTrue(c.providers[2].local)
        XCTAssertEqual(c.option("openai-codex/gpt-5.5")?.levels, [.low, .medium, .high, .xhigh])
        XCTAssertEqual(c.defaultChoice, ModelChoice(model: "anthropic/claude-opus-5-5", reasoning: .medium))
        XCTAssertEqual(c.fast.model, "anthropic/claude-haiku-5-5")
        XCTAssertFalse(ModelCatalog(json: ["available": false, "providers": []]).available)
        XCTAssertFalse(ModelCatalog(json: nil).available)
    }

    func testChipLabels() {
        let c = catalog
        XCTAssertEqual(c.chipLabel(nil), "Claude Opus 5.5 · Medium")
        XCTAssertEqual(c.chipLabel(ModelChoice(model: "openai-codex/gpt-5.5", reasoning: .xhigh)), "GPT-5.5 · Max")
        XCTAssertEqual(c.chipLabel(ModelChoice(reasoning: .high)), "Claude Opus 5.5 · High", "level only keeps the model")
        XCTAssertEqual(c.chipLabel(ModelChoice(model: "studio-qwen/qwen3.8-coder", reasoning: .high)), "Qwen3.8 Coder",
                       "a model that doesn't reason shows no level")
        XCTAssertEqual(c.chipLabel(ModelChoice(model: "openai-codex/gpt-5.5", reasoning: .max)), "GPT-5.5 · Max",
                       "clamped to what the model takes")
    }

    func testLevelClamping() {
        XCTAssertEqual(ReasoningLevel.max.clamped(to: [.none, .low, .medium, .high]), .high)
        XCTAssertEqual(ReasoningLevel.minimal.clamped(to: [.low, .medium]), .low)
        XCTAssertNil(ReasoningLevel.none.clamped(to: [.low, .medium]))
        XCTAssertNil(ReasoningLevel.high.clamped(to: []))
        XCTAssertEqual(ReasoningLevel.xhigh.label, "Max", "the strongest level reads Max on every model")
        XCTAssertEqual(ReasoningLevel.max.label, "Max")
        XCTAssertEqual(ReasoningLevel.xhigh.label(among: [.high, .xhigh]), "Max")
        XCTAssertEqual(ReasoningLevel.xhigh.label(among: [.high, .xhigh, .max]), "Extra high")
        XCTAssertEqual(ReasoningLevel.xhigh.short, "Max")
        XCTAssertEqual(ReasoningLevel.medium.short, "Med")
    }

    func testPickerKeepsOrClampsTheLevelWhenTheModelChanges() {
        var s = ModelPickerState(catalog: catalog, chatChoice: nil)
        XCTAssertTrue(s.followsDefault)
        XCTAssertEqual(s.model?.id, "anthropic/claude-opus-5-5")
        s.select(.max)
        XCTAssertEqual(s.reasoning, .max)
        s.select(catalog.option("anthropic/claude-haiku-5-5")!)
        XCTAssertEqual(s.reasoning, .high, "Haiku has no Max: nearest weaker")
        s.select(catalog.option("studio-qwen/qwen3.8-coder")!)
        XCTAssertTrue(s.levels.isEmpty)
        XCTAssertEqual(s.selection, ModelChoice(model: "studio-qwen/qwen3.8-coder", reasoning: nil))
        s.select(.high)  // ignored: no levels
        XCTAssertEqual(s.selection.reasoning, nil)
    }

    func testHeaderShowsTheModelOnlyWhenItDiffersFromTheChip() {
        let s = ModelPickerState(catalog: catalog, chatChoice: nil)  // chip: Claude Opus 5.5 · Medium
        XCTAssertFalse(s.headerShows(ModelBadge(id: "anthropic/claude-opus-5-5", name: "Claude Opus 5.5", reasoning: .medium)),
                       "same as the chip: shown once, above the composer")
        XCTAssertFalse(s.headerShows(ModelBadge(id: "anthropic/claude-opus-5-5", name: "Claude Opus 5.5")), "no level reported")
        XCTAssertTrue(s.headerShows(ModelBadge(id: "anthropic/claude-opus-5-5", name: "Claude Opus 5.5", reasoning: .high)))
        XCTAssertTrue(s.headerShows(ModelBadge(id: "openai-codex/gpt-5.5", name: "GPT-5.5", reasoning: .medium)))
    }

    func testUseForThisChatAndMakeDefault() {
        var s = ModelPickerState(catalog: catalog, chatChoice: nil)
        s.select(catalog.option("openai-codex/gpt-5.5")!)
        XCTAssertTrue(s.changed)
        s.apply(.chat)
        XCTAssertEqual(s.chatChoice, ModelChoice(model: "openai-codex/gpt-5.5", reasoning: .medium))
        XCTAssertEqual(s.chipLabel, "GPT-5.5 · Medium")
        XCTAssertFalse(s.changed)

        var d = ModelPickerState(catalog: catalog, chatChoice: nil)
        d.select(catalog.option("anthropic/claude-haiku-5-5")!)
        d.select(.low)
        d.apply(.makeDefault)
        XCTAssertNil(d.chatChoice, "a chat following the default keeps following it")
        XCTAssertEqual(d.catalog.defaultChoice, ModelChoice(model: "anthropic/claude-haiku-5-5", reasoning: .low))
        XCTAssertEqual(d.catalog.allModels.filter(\.isDefault).map(\.id), ["anthropic/claude-haiku-5-5"])
        XCTAssertEqual(d.chipLabel, "Claude Haiku 5.5 · Low")
        d.useDefault()
        XCTAssertEqual(d.model?.id, "anthropic/claude-haiku-5-5")
    }

    func testRequestEncoding() throws {
        let put = SessionModelRequest(sessionID: "api_other", choice: ModelChoice(model: "openai-codex/gpt-5.5"))
        XCTAssertEqual(put.path, "/voice/history/api_other/model")
        XCTAssertEqual(put.body as NSDictionary, ["model": "openai-codex/gpt-5.5", "reasoning": "default"])
        let msg = try XCTUnwrap(HistoryMessageRequest(sessionID: "d1", text: "Order them", clientID: "c1",
                                                      choice: ModelChoice(reasoning: .high)))
        XCTAssertEqual(msg.body["reasoning"] as? String, "high")
        XCTAssertNil(msg.body["model"], "an unset half is left as it is")
        let plain = try XCTUnwrap(HistoryMessageRequest(sessionID: "d1", text: "Order them", clientID: "c1"))
        XCTAssertNil(plain.body["model"]); XCTAssertNil(plain.body["reasoning"])
        XCTAssertEqual(JobModelDefaults(model: nil, reasoning: .high).patchBody as NSDictionary,
                       ["jobs": ["model": "", "reasoning": "high"]])
        XCTAssertEqual(JobModelDefaults(settingsJSON: ["jobs": ["model": "a/b", "reasoning": ""]]),
                       JobModelDefaults(model: "a/b", reasoning: nil))
    }

    func testSessionPageAndTaskCarryTheModel() {
        let page = HistoryConversation(json: ["session": [
            "id": "s1", "title": "Garden", "source": "speakeasy", "send_via": "task", "entries": [], "jobs": [],
            "model": ["id": "openai-codex/gpt-5.5", "name": "GPT-5.5", "provider_name": "OpenAI Codex",
                      "reasoning": "high", "source": "chat", "served": true],
            "next_model": ["id": "openai-codex/gpt-5.5", "name": "GPT-5.5", "reasoning": "high"],
            "model_choice": ["model": "openai-codex/gpt-5.5", "reasoning": "high"]]])
        XCTAssertEqual(page?.model?.label, "GPT-5.5 · High")
        XCTAssertTrue(page?.model?.served ?? false)
        XCTAssertEqual(page?.modelChoice, ModelChoice(model: "openai-codex/gpt-5.5", reasoning: .high))
        let old = HistoryConversation(json: ["session": ["id": "s1"]])
        XCTAssertNil(old?.model); XCTAssertNil(old?.modelChoice)
        let work = WorkInfo(json: ["run_id": "r1", "status": "completed",
                                   "model": ["id": "anthropic/claude-opus-5-5", "name": "Claude Opus 5.5"]])
        XCTAssertEqual(work?.model?.name, "Claude Opus 5.5")
    }

    func testAuthoritativeNextTargetDoesNotFallBackToAppDefault() {
        let next = ModelBadge(id: "openai-codex/gpt-5.5", name: "GPT-5.5", reasoning: .high, source: "chat")
        let picker = ModelPickerState(catalog: catalog, chatChoice: nil, nextModel: next, existingConversation: true)
        XCTAssertEqual(picker.chipLabel, "GPT-5.5 · High")
        XCTAssertEqual(picker.selection, ModelChoice(model: next.id, reasoning: .high))
        XCTAssertFalse(picker.followsDefault)
        XCTAssertFalse(picker.changed)
        var changedDefault = catalog
        changedDefault.defaultChoice = ModelChoice(model: "anthropic/claude-haiku-5-5", reasoning: .low)
        XCTAssertEqual(ModelPickerState(catalog: changedDefault, chatChoice: nil, nextModel: next,
            existingConversation: true).chipLabel, picker.chipLabel)
    }

    func testExistingUnknownAndUnavailableStayHonest() {
        let unknown = ModelPickerState(catalog: catalog, chatChoice: nil, existingConversation: true)
        XCTAssertEqual(unknown.chipLabel, "Model unknown")
        XCTAssertNil(unknown.model)
        var next = ModelBadge(id: "missing/model", name: "Missing model", source: "existing")
        next.unavailable = true
        let unavailable = ModelPickerState(catalog: catalog, chatChoice: nil, nextModel: next, existingConversation: true)
        XCTAssertEqual(unavailable.chipLabel, "Missing model · Unavailable")
        XCTAssertEqual(unavailable.model?.id, "missing/model")
        XCTAssertNil(unavailable.reasoning)
        XCTAssertEqual(ModelPickerState(catalog: catalog, chatChoice: nil).chipLabel, "Claude Opus 5.5 · Medium")
    }

    func testAuthoritativeOffAndUnknownEffortDoNotBorrowDefault() {
        var next = ModelBadge(id: "anthropic/claude-opus-5-5", name: "Claude Opus 5.5", reasoning: ReasoningLevel.none, source: "existing")
        XCTAssertEqual(ModelPickerState(catalog: catalog, chatChoice: nil, nextModel: next).chipLabel, "Claude Opus 5.5 · Off")
        next.reasoning = nil
        let picker = ModelPickerState(catalog: catalog, chatChoice: nil, nextModel: next)
        XCTAssertEqual(picker.chipLabel, "Claude Opus 5.5")
        XCTAssertNil(picker.selection.reasoning)
    }

    func testDistinctConversationSwitchDoesNotRewriteEitherChoice() {
        let a = ModelBadge(id: "openai-codex/gpt-5.5", name: "GPT-5.5", reasoning: .high, source: "chat")
        let b = ModelBadge(id: "anthropic/claude-opus-5-5", name: "Claude Opus 5.5", reasoning: .low, source: "chat")
        let identities = ["garden", "garden-linked", "garden", "shelves", "garden"]
        let expected = [a, a, a, b, a]
        for (id, badge) in zip(identities, expected) {
            let actual = id == "shelves" ? b : a
            XCTAssertEqual(ModelPickerState(catalog: catalog, chatChoice: nil, nextModel: actual,
                existingConversation: true).chipLabel, badge.label)
        }
    }

    func testCapabilityFlag() throws {
        let caps = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"model_choice": true}"#.utf8))
        XCTAssertTrue(caps.modelChoice)
        XCTAssertFalse(try JSONDecoder().decode(ServerCapabilities.self, from: Data("{}".utf8)).modelChoice)
    }
}
