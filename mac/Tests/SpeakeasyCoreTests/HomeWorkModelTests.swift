import XCTest
@testable import SpeakeasyCore

/// Home's chip is the work model (the model jobs from Home run on); the coordinator's model is a
/// separate, secondary choice. Invented model ids.
final class HomeWorkModelTests: XCTestCase {
    func testStatusReadsWorkAndCoordinatorSeparately() {
        let json: [String: Any] = [
            "model": ["id": "acme/fast-1", "name": "Fast 1"], "source": "auto",
            "coordinator": ["model": ["id": "acme/fast-1", "name": "Fast 1"], "source": "auto",
                            "auto": ["id": "acme/fast-1", "name": "Fast 1"], "options": [["id": "acme/fast-1", "name": "Fast 1"]]],
            "work": ["model": ["id": "acme/big-3", "name": "Big 3"], "source": "default",
                     "default": ["id": "acme/big-3", "name": "Big 3"],
                     "options": [["id": "acme/big-3", "name": "Big 3"], ["id": "other/huge-2", "name": "Huge 2"]]],
        ]
        let status = HomeModelStatus(json: json)
        XCTAssertEqual(status.model?.id, "acme/fast-1")
        XCTAssertFalse(status.chosen)
        let work = try? XCTUnwrap(status.work)
        XCTAssertEqual(work?.model?.name, "Big 3")
        XCTAssertEqual(work?.defaultModel?.id, "acme/big-3")
        XCTAssertEqual(work?.options.map(\.id), ["acme/big-3", "other/huge-2"])
        XCTAssertEqual(work?.chosen, false)
    }

    func testOlderPluginHasNoWorkModel() {
        let status = HomeModelStatus(json: ["model": ["id": "acme/fast-1", "name": "Fast 1"], "source": "chosen"])
        XCTAssertNil(status.work)
        XCTAssertTrue(status.chosen)
    }

    func testJobCardCarriesTheModelThatRan() throws {
        let item = try XCTUnwrap(NowItem(json: ["id": "r1", "kind": "working", "title": "Compare two desks",
                                                "model": ["id": "acme/big-3", "name": "Big 3", "provider": "acme"]]))
        XCTAssertEqual(item.workModel?.label, "Big 3")
        let bare = try XCTUnwrap(NowItem(json: ["id": "r2", "kind": "working", "title": "Compare two desks"]))
        XCTAssertNil(bare.workModel)
    }

    func testPickerReadsMoreModelsAndOlderPluginsHaveNone() {
        let json: [String: Any] = ["work": ["model": ["id": "acme/big-3", "name": "Big 3"], "source": "default",
                                            "options": [["id": "acme/big-3", "name": "Big 3"]],
                                            "more": [["id": "acme/old-1", "name": "Old 1"]]]]
        XCTAssertEqual(HomeModelStatus(json: json).work?.more.map(\.id), ["acme/old-1"])
        let older: [String: Any] = ["work": ["model": ["id": "acme/big-3", "name": "Big 3"], "source": "default", "options": []]]
        XCTAssertEqual(HomeModelStatus(json: older).work?.more, [])
    }

    /// The picker's one plain line and provider glyph (SF Symbols only), read from id and provider.
    func testPickerDescribesModelsPlainly() {
        typealias O = HomeModelStatus.Option
        let opus = O(id: "anthropic/claude-opus-5-5", name: "Claude Opus 5.5", providerName: "Anthropic")
        let sonnet = O(id: "anthropic/claude-sonnet-5-5", name: "Claude Sonnet 5.5", providerName: "Anthropic")
        let haiku = O(id: "anthropic/claude-haiku-5-5", name: "Claude Haiku 5.5", providerName: "Anthropic")
        let mini = O(id: "openai-codex/gpt-5.4-mini", name: "GPT-5.4 Mini", providerName: "OpenAI Codex")
        let local = O(id: "studio-qwen/qwen3.8-coder", name: "Qwen3.8 Coder", providerName: "Studio Qwen")
        let odd = O(id: "acme/big-3", name: "Big 3", providerName: "Acme")
        XCTAssertEqual(opus.blurb, "Most capable. Best for long, hard jobs.")
        XCTAssertEqual(sonnet.blurb, "Fast and capable. Good for most jobs.")
        XCTAssertEqual(haiku.blurb, "Quickest. Best for light, simple jobs.")
        XCTAssertEqual(mini.blurb, "Small and quick. Best for light jobs.")
        XCTAssertEqual(local.blurb, "Runs on your own computer.")
        XCTAssertEqual(odd.blurb, "From Acme.")
        XCTAssertEqual(opus.glyph, "asterisk")
        XCTAssertEqual(mini.glyph, "circle.hexagonpath")
        XCTAssertEqual(local.glyph, "desktopcomputer")
        XCTAssertEqual(odd.glyph, "cpu")
    }
}
