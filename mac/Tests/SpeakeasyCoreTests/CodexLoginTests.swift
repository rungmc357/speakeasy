import XCTest
@testable import SpeakeasyCore

/// Sign in to ChatGPT from the app (`/voice/codex/login`): what the server says, what is shown, and
/// that the button never appears while a working sign-in exists or on plugins without the route.
final class CodexLoginTests: XCTestCase {
    private func status(_ json: String) throws -> ServerStatus {
        try JSONDecoder().decode(ServerStatus.self, from: Data(json.utf8))
    }

    func testOfferedOnlyWhenNoCopyIsSignedIn() throws {
        let none = try status(#"{"provider": "codex", "codex_signed_in": false, "codex_sign_in": "none", "capabilities": {"codex_login": true}}"#)
        XCTAssertTrue(none.offersCodexSignIn)
        XCTAssertFalse(none.codexOnAPIKey)
        let signed = try status(#"{"provider": "codex", "codex_signed_in": true, "codex_sign_in": "chatgpt", "capabilities": {"codex_login": true}}"#)
        XCTAssertFalse(signed.offersCodexSignIn)
        let apiKey = try status(#"{"provider": "codex", "codex_signed_in": true, "codex_sign_in": "api_key", "capabilities": {"codex_login": true}}"#)
        XCTAssertTrue(apiKey.offersCodexSignIn)
        XCTAssertTrue(apiKey.codexOnAPIKey)
    }

    func testOldPluginsKeepTodaysText() throws {
        let old = try status(#"{"provider": "codex", "codex_signed_in": false, "capabilities": {"card_actions": true}}"#)
        XCTAssertFalse(old.canSignInCodex)
        XCTAssertFalse(old.offersCodexSignIn)
        let older = try status(#"{"provider": "codex", "codex_signed_in": false}"#)
        XCTAssertFalse(older.offersCodexSignIn)
    }

    func testNotOfferedForOtherProviders() throws {
        let openai = try status(#"{"provider": "openai", "codex_sign_in": "none", "capabilities": {"codex_login": true}}"#)
        XCTAssertFalse(openai.offersCodexSignIn)
    }

    func testPendingStateParses() {
        let state = CodexLoginState(json: ["state": "pending", "verification_url": "https://auth.openai.com/codex/device",
                                           "user_code": "abcd-12345", "expires_at": 2_000_000_000, "sign_in": "none"])
        XCTAssertEqual(state?.phase, .pending)
        XCTAssertEqual(state?.userCode, "ABCD-12345")
        XCTAssertEqual(state?.verificationURL?.host, "auth.openai.com")
        XCTAssertEqual(state?.signIn, CodexSignIn.none)
        XCTAssertTrue(state?.isWaiting ?? false)
        XCTAssertEqual(state?.expiryLine(now: Date(timeIntervalSince1970: 2_000_000_000 - 600)), "Code expires in 10 min")
        XCTAssertEqual(state?.expiryLine(now: Date(timeIntervalSince1970: 2_000_000_000 - 30)), "Code expires in 30 s")
    }

    func testAnUntrustedURLIsNeverShown() {
        for url in ["http://auth.openai.com/codex/device", "https://openai.com.evil.example/x", "https://evilopenai.com/",
                    "https://me@auth.openai.com/", "https://auth.openai.com:8443/", "javascript:alert(1)"] {
            let state = CodexLoginState(json: ["state": "pending", "verification_url": url, "user_code": "ABCD-1234"])
            XCTAssertEqual(state?.phase, .failed, url)
            XCTAssertNil(state?.verificationURL, url)
        }
        XCTAssertNotNil(CodexLoginState.safeURL("https://chatgpt.com/device"))
        XCTAssertNil(CodexLoginState.safeCode("<script>"))
    }

    func testStatusLines() {
        XCTAssertEqual(CodexLoginState(phase: .signedIn).statusLine, "Signed in ✓")
        XCTAssertFalse(CodexLoginState(phase: .signedIn).isWaiting)
        XCTAssertTrue(CodexLoginState(phase: .installing).isWaiting)
        XCTAssertNil(CodexLoginState(json: ["state": "bogus"]))
    }

    func testCallErrorsThatMeanNotSignedIn() {
        XCTAssertTrue(CodexLoginState.errorMeansNotSignedIn(
            "Codex is not signed in on this computer. Run `codex login` here."))
        XCTAssertTrue(CodexLoginState.errorMeansNotSignedIn("ChatGPT signed out on the Hermes computer; sign in again"))
        XCTAssertFalse(CodexLoginState.errorMeansNotSignedIn("Server HTTP 502"))
        XCTAssertFalse(CodexLoginState.errorMeansNotSignedIn("Tailscale is not signed in"))
        XCTAssertTrue(CodexLoginState.errorMeansNotSignedIn(
            "Codex isn't signed in to ChatGPT on the Hermes computer. Tap Sign in to ChatGPT in the app, or run codex login there."))
    }

    func testPlanWithoutVoiceIsNotASignInProblem() {
        let plan = "Your ChatGPT plan doesn't include voice through Codex (or your workspace turned it off). Use Grok or an OpenAI API key for voice instead: Settings › Voice."
        XCTAssertTrue(CodexLoginState.errorMeansPlanHasNoVoice(plan))
        XCTAssertFalse(CodexLoginState.errorMeansNotSignedIn(plan))
    }

    func testFailedCallOffersSignInNotARedial() {
        var s = VoiceState()
        let error = "Codex isn't signed in to ChatGPT on the Hermes computer. Tap Sign in to ChatGPT in the app, or run codex login there."
        s.connection = .failed(error); s.lastError = error
        XCTAssertEqual(CallProblem.of(s, canSignInCodex: true)?.fix, .signInCodex)
        XCTAssertEqual(CallProblem.of(s, canSignInCodex: true)?.fixLabel, "Sign in to ChatGPT")
        // Older plugins (no sign-in from the app): the same plain line, never "run codex login here".
        XCTAssertEqual(CallProblem.of(s, canSignInCodex: false)?.fix, .chooseVoice, "no redial into the same error")
        XCTAssertEqual(CallProblem.of(s, canSignInCodex: false)?.detail, CodexLoginCopy.notSignedInUpdate)
        let plan = "Your ChatGPT plan doesn't include voice through Codex (or your workspace turned it off). Use Grok or an OpenAI API key for voice instead: Settings › Voice."
        s.connection = .failed(plan); s.lastError = plan
        XCTAssertEqual(CallProblem.of(s, canSignInCodex: true)?.fix, .chooseVoice)
        XCTAssertEqual(CallProblem.of(s)?.fixLabel, "Choose another voice")
    }

    /// SPK-156: the plugin's own call-start text ("Run `codex login` here") becomes the same plain
    /// line as Settings, with Update when an update is offered; nothing tells the user to type a command.
    func testOldPluginNotSignedInOffersUpdateNotACommand() {
        var s = VoiceState()
        let error = "Codex is not signed in on this computer. Run `codex login` here. Hermes' own ChatGPT/Codex sign-in is separate and doesn't carry over."
        s.connection = .failed(error); s.lastError = error
        let withUpdate = CallProblem.of(s, canSignInCodex: false, canUpdatePlugin: true)
        XCTAssertEqual(withUpdate?.fix, .updatePlugin)
        XCTAssertEqual(withUpdate?.fixLabel, "Update")
        XCTAssertEqual(withUpdate?.detail, "Not signed in. Update Speakeasy on your Hermes to sign in from here.")
        XCTAssertEqual(CallProblem.of(s, canSignInCodex: false)?.fix, .chooseVoice)
        XCTAssertEqual(CallProblem.of(s, canSignInCodex: true, canUpdatePlugin: true)?.fix, .signInCodex,
                       "a plugin that can sign in from here never asks for an update")
        for problem in [withUpdate, CallProblem.of(s)] {
            let words = (problem?.title ?? "") + " " + (problem?.detail ?? "")
            XCTAssertFalse(words.contains("codex login"), words)
            XCTAssertFalse(words.contains("`"), words)
            XCTAssertFalse(words.contains("on this computer"), "the Hermes computer is not this device")
        }
    }

    func testVoiceChecksNeverNameATerminalCommand() throws {
        func status(_ json: String) throws -> ServerStatus { try JSONDecoder().decode(ServerStatus.self, from: Data(json.utf8)) }
        let old = try status(#"{"provider":"codex","codex_signed_in":false}"#)
        XCTAssertEqual(old.checks.first { $0.id == "voice" }?.fix, CodexLoginCopy.notSignedInUpdate)
        let new = try status(#"{"provider":"codex","codex_signed_in":false,"capabilities":{"codex_login":true}}"#)
        XCTAssertEqual(new.checks.first { $0.id == "voice" }?.fix, "Sign in to ChatGPT in Settings › Voice")
        let grok = try status(#"{"provider":"grok","grok_signed_in":false,"grok_ready":false,"grok_message":"Grok voice needs your xAI sign-in: run `hermes auth add xai-oauth`"}"#)
        XCTAssertEqual(grok.grokVoiceReady, false)
        XCTAssertEqual(grok.checks.first { $0.id == "voice" }?.fix, "Not signed in to Grok. Ask your Hermes to sign in to X.")
        for s in [old, new, grok] {
            let fix = s.checks.first { $0.id == "voice" }?.fix ?? ""
            XCTAssertFalse(fix.contains("run "), fix)
            XCTAssertFalse(fix.contains("hermes "), fix)
        }
    }

    func testGrokReadyThroughTheKeyCountsAsReady() throws {
        let key = try JSONDecoder().decode(ServerStatus.self, from: Data(
            #"{"provider":"grok","grok_signed_in":false,"grok_api_key_set":true,"grok_ready":true}"#.utf8))
        XCTAssertEqual(key.grokVoiceReady, true)
        XCTAssertTrue(key.grokOnAPIKey)
        XCTAssertNil(key.checks.first { $0.id == "voice" }?.fix)
        let signed = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"grok_signed_in":true}"#.utf8))
        XCTAssertEqual(signed.grokVoiceReady, true)
        XCTAssertFalse(signed.grokOnAPIKey)
        let unknown = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{}"#.utf8))
        XCTAssertNil(unknown.grokVoiceReady)
    }

    func testGrokRequestIsForTheAgentAndNamesTheDevice() {
        let text = GrokSignInCopy.request(device: "iPad")
        XCTAssertTrue(text.contains("hermes auth add xai-oauth"), "the agent gets the exact command")
        XCTAssertTrue(text.contains("my iPad"))
        XCTAssertFalse(text.contains("\\"), "no stray line-continuation backslash")
        XCTAssertFalse(text.contains("\n"), "one paragraph")
    }
}
