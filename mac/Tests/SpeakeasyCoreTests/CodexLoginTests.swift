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
        XCTAssertEqual(CallProblem.of(s, canSignInCodex: false)?.fix, .retry, "old plugins keep today's text")
        let plan = "Your ChatGPT plan doesn't include voice through Codex (or your workspace turned it off). Use Grok or an OpenAI API key for voice instead: Settings › Voice."
        s.connection = .failed(plan); s.lastError = plan
        XCTAssertEqual(CallProblem.of(s, canSignInCodex: true)?.fix, .chooseVoice)
        XCTAssertEqual(CallProblem.of(s)?.fixLabel, "Choose another voice")
    }
}
