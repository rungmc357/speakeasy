import XCTest
@testable import SpeakeasyCore

/// `--live-call-smoke` types its one line the way the admitted voice provider accepts it: Codex voice
/// refuses `response.item.create` on the data channel, so it goes through the server's typed-turn
/// route; every other provider keeps the data-channel event.
final class SmokeTypedTurnTests: XCTestCase {
    func testCodexVoiceTypesThroughTheServer() {
        XCTAssertEqual(SmokeTypedTurn.plan(voiceProvider: "codex", prompt: "Quick sound check."),
                       .serverRoute(text: "Quick sound check."))
        XCTAssertEqual(SmokeTypedTurn.plan(voiceProvider: "Codex", prompt: "  Two\n lines  "),
                       .serverRoute(text: "Two lines"))
    }

    func testCodexNeverSendsTheRefusedDataChannelEvent() {
        if case .dataChannel = SmokeTypedTurn.plan(voiceProvider: "codex", prompt: "Hello there.") {
            XCTFail("Codex voice refuses response.item.create; the smoke test must not send it")
        }
    }

    func testOtherProvidersKeepTheDataChannelEvent() {
        for provider in ["openai", "grok", ""] {
            XCTAssertEqual(SmokeTypedTurn.plan(voiceProvider: provider, prompt: "Hello there."),
                           .dataChannel(prompt: "Hello there."), provider)
        }
        let event = SmokeTypedTurn.itemCreate("Hello there.")
        XCTAssertTrue(JSONSerialization.isValidJSONObject(event))
        XCTAssertEqual(event["type"] as? String, "response.item.create")
        let item = event["item"] as? [String: Any]
        XCTAssertEqual(item?["role"] as? String, "user")
        let content = item?["content"] as? [[String: Any]]
        XCTAssertEqual(content?.first?["type"] as? String, "input_text")
        XCTAssertEqual(content?.first?["text"] as? String, "Hello there.")
    }

    func testCodexPromptTheServerWouldRefuseIsCaughtBeforeSending() {
        guard case .invalid = SmokeTypedTurn.plan(voiceProvider: "codex", prompt: " \n ") else {
            return XCTFail("an empty prompt must not be sent")
        }
        guard case .invalid = SmokeTypedTurn.plan(voiceProvider: "codex",
                                                  prompt: String(repeating: "a", count: TypedText.maxScalars + 1)) else {
            return XCTFail("an over-long prompt must not be sent")
        }
        XCTAssertEqual(SmokeTypedTurn.plan(voiceProvider: "codex", prompt: String(repeating: "a", count: TypedText.maxScalars)),
                       .serverRoute(text: String(repeating: "a", count: TypedText.maxScalars)))
    }

    func testStreamedCaptionCountsOnceItGoesQuiet() {
        let t = Date(timeIntervalSince1970: 1_000)
        XCTAssertFalse(SmokeTypedTurn.replySettled(heard: "", lastDelta: nil, now: t))
        XCTAssertFalse(SmokeTypedTurn.replySettled(heard: "  ", lastDelta: t, now: t.addingTimeInterval(5)))
        XCTAssertFalse(SmokeTypedTurn.replySettled(heard: "Sounds good", lastDelta: t, now: t.addingTimeInterval(1)))
        XCTAssertTrue(SmokeTypedTurn.replySettled(heard: "Sounds good", lastDelta: t,
                                                  now: t.addingTimeInterval(SmokeTypedTurn.replyQuiet)))
    }
}
