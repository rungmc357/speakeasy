import XCTest
@testable import SpeakeasyCore

/// Settings › Voice › Add key: the OpenAI API key goes from the app's secure box to the Hermes
/// computer; nobody is told to open a terminal.
final class OpenAIKeyTests: XCTestCase {
    private func status(_ json: String) throws -> ServerStatus {
        try JSONDecoder().decode(ServerStatus.self, from: Data(json.utf8))
    }

    func testCapabilityAndSetupLineNeverMentionATerminal() throws {
        let new = try status(#"{"provider":"openai","api_key_set":false,"hermes_api_ok":true,"capabilities":{"openai_key":true}}"#)
        XCTAssertTrue(new.canAddOpenAIKey)
        XCTAssertEqual(new.checks.first { $0.id == "voice" }?.fix, "Add your OpenAI API key in Settings › Voice")

        let old = try status(#"{"provider":"openai","api_key_set":false,"hermes_api_ok":true,"capabilities":{"codex_login":true}}"#)
        XCTAssertFalse(old.canAddOpenAIKey)
        let oldFix = try XCTUnwrap(old.checks.first { $0.id == "voice" }?.fix)
        XCTAssertFalse(oldFix.contains("run "), oldFix)
        XCTAssertFalse(OpenAIKeyCopy.oldPluginNote.contains("hermes voice setup"))

        let set = try status(#"{"provider":"openai","api_key_set":true,"hermes_api_ok":true,"capabilities":{"openai_key":true}}"#)
        XCTAssertNil(set.checks.first { $0.id == "voice" }?.fix)
    }

    func testTheBoxIsTheUsersOwnNotATaskAsking() {
        let add = SecureInputRequest.openAIKey()
        XCTAssertTrue(add.userStarted)
        XCTAssertEqual(add.kind, .secret)
        XCTAssertEqual(add.envVar, "SPEAKEASY_OPENAI_API_KEY")
        XCTAssertEqual(add.title, "Add your OpenAI API key")
        XCTAssertEqual(add.skipButton, "Cancel")
        XCTAssertEqual(add.primaryButton, "Save securely")
        XCTAssertGreaterThan(add.secondsLeft(), 3600, "no countdown on a box the user opened")
        XCTAssertTrue(add.whereItGoes.contains("Hermes computer"))
        XCTAssertFalse(add.help.contains("hermes voice setup"))
        XCTAssertTrue(SecureInputRequest.openAIKey(replacing: true).help.contains("replace"))

        // A task's key request keeps its own wording.
        let asked = SecureInputRequest(id: "sec_fixture_weather_key_000", kind: .secret, job: "Weather", envVar: "X_KEY",
                                       label: "Weather key", expiresAt: Date().addingTimeInterval(60))
        XCTAssertFalse(asked.userStarted)
        XCTAssertEqual(asked.title, "Your agent needs your Weather key")
        XCTAssertEqual(asked.skipButton, "Skip")
    }

    func testRoughKeyCheckBeforeSending() {
        let fake = "sk" + "-" + "proj-fake-test-key-0123456789"
        XCTAssertTrue(SecureInputRequest.looksLikeOpenAIKey(fake))
        XCTAssertTrue(SecureInputRequest.looksLikeOpenAIKey("  \(fake)\n"))
        XCTAssertFalse(SecureInputRequest.looksLikeOpenAIKey("not-a-key-at-all-0123456789"))
        XCTAssertFalse(SecureInputRequest.looksLikeOpenAIKey("sk-short"))
        XCTAssertFalse(SecureInputRequest.looksLikeOpenAIKey("sk-has a space 0123456789abc"))
        // The body the plugin parses: {"value": …}, exactly.
        let body = SecureInputRequest.answerBody(fake)
        let object = try? JSONSerialization.jsonObject(with: Data(body)) as? [String: String]
        XCTAssertEqual(object, ["value": fake])
    }
}
