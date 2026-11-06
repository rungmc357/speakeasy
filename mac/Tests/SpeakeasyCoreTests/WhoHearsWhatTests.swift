import XCTest
@testable import SpeakeasyCore

/// "Who hears what": the copy follows the voice setting, and the extra lines show only when they apply.
final class WhoHearsWhatTests: XCTestCase {
    func testVoiceFollowsProvider() {
        XCTAssertEqual(WhoHearsWhat.Voice(provider: .codex), .openai)
        XCTAssertEqual(WhoHearsWhat.Voice(provider: .openai), .openai)
        XCTAssertEqual(WhoHearsWhat.Voice(provider: .grok), .xai)
        XCTAssertEqual(WhoHearsWhat.Voice(provider: nil), .openai, "an older server reports no provider: Codex is the default")
    }

    func testChatGPTVoiceCopy() {
        let w = WhoHearsWhat(voice: .openai)
        XCTAssertEqual(w.callLead, "Calls use the ChatGPT voice.")
        XCTAssertTrue(w.callBody.hasPrefix("OpenAI hears the call itself: what you say, and what the voice says back"))
        XCTAssertTrue(w.callBody.contains("a short note about you"))
        XCTAssertTrue(w.callBody.contains("the names of tasks that are running"))
        XCTAssertEqual(w.details.first, "OpenAI doesn't get your sessions, files or tools, or anything you attach or share from your screen. Those go only to your agent.")
        XCTAssertEqual(w.details.count, 3)
        XCTAssertTrue(w.extras.isEmpty)
        XCTAssertEqual(w.settingsSubtitle, "OpenAI hears your calls. Your sessions, files and tools stay on your computer.")
    }

    func testGrokVoiceNamesXAI() {
        let w = WhoHearsWhat(voice: .xai)
        XCTAssertEqual(w.callLead, "Calls use the Grok voice.")
        XCTAssertTrue(w.callBody.hasPrefix("xAI hears the call itself"))
        XCTAssertTrue(w.details[0].hasPrefix("xAI doesn't get"))
        XCTAssertTrue(w.settingsSubtitle.hasPrefix("xAI hears your calls."))
        XCTAssertFalse(w.plainText.contains("OpenAI"))
    }

    func testPrivateVoiceSaysNothingLeaves() {
        let w = WhoHearsWhat(voice: .local, listening: true)
        XCTAssertEqual(w.callLead, "Calls stay on your computer too.")
        XCTAssertEqual(w.callBody, "The voice runs on your own machine, so nothing you say leaves it.")
        XCTAssertNil(w.provider)
        XCTAssertFalse(w.plainText.contains("OpenAI"))
        XCTAssertEqual(w.details.count, 2)
        XCTAssertEqual(w.settingsSubtitle, "Calls stay on your computer.")
        XCTAssertEqual(w.extras, ["Nothing leaves your Mac while it listens."])
    }

    func testExtrasOnlyWhenTheyApply() {
        let w = WhoHearsWhat(voice: .openai, viaCloudflare: true, notifications: true, listening: true, device: "iPhone")
        XCTAssertEqual(w.extras.count, 3)
        XCTAssertTrue(w.extras[0].contains("through Cloudflare"))
        XCTAssertTrue(w.extras[1].hasPrefix("Notifications go through Speakeasy's notification service."))
        XCTAssertEqual(w.extras[2], "Nothing leaves your iPhone while it listens. When you start a call, the voice gets what it picked up, so it can help.")
        XCTAssertEqual(WhoHearsWhat(voice: .openai, notifications: true).extras.count, 1)
    }

    func testCloudflareDetection() {
        let quick = URL(string: "https://bright-river-sample.trycloudflare.com")!
        let tail = URL(string: "https://studio.example-tail.ts.net")!
        let own = URL(string: "https://voice.example.com")!
        XCTAssertTrue(WhoHearsWhat.isCloudflare(server: quick, endpoints: nil))
        XCTAssertFalse(WhoHearsWhat.isCloudflare(server: tail, endpoints: nil))
        XCTAssertFalse(WhoHearsWhat.isCloudflare(server: nil, endpoints: nil))
        let eps = ServerEndpoints(tailnet: tail, cloudflare: own, cloudflarePermanent: true)
        XCTAssertTrue(WhoHearsWhat.isCloudflare(server: own, endpoints: eps))
        XCTAssertFalse(WhoHearsWhat.isCloudflare(server: tail, endpoints: eps))
    }

    func testNoToolNamesOrStepCounts() {
        let all = WhoHearsWhat(voice: .openai, viaCloudflare: true, notifications: true, listening: true).plainText
        for bad in ["realtime", "WebRTC", "API", "relay", "tunnel", "Step "] {
            XCTAssertFalse(all.contains(bad), "plain words only: \(bad)")
        }
    }
}
