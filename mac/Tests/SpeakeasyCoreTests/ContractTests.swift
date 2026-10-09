import XCTest
@testable import SpeakeasyCore

/// Replies recorded from the real plugin server (plugin/tests/e2e_local.py with
/// SPEAKEASY_CONTRACT_DIR set). If the server's JSON changes, re-record them; these tests
/// then show exactly where the app stops understanding it.
final class ContractTests: XCTestCase {
    private func fixture(_ name: String) throws -> Data {
        let url = try XCTUnwrap(Bundle.module.url(forResource: name, withExtension: "json", subdirectory: "Contract"))
        return try Data(contentsOf: url)
    }

    func testStatus() throws {
        let s = try JSONDecoder().decode(ServerStatus.self, from: fixture("status"))
        XCTAssertEqual(s.assistantName, "Nova")
        XCTAssertEqual(s.provider, "codex")
        XCTAssertEqual(s.codexSignedIn, true)
        XCTAssertEqual(s.hermesAPIOK, true)
        XCTAssertEqual(s.threadsSupported, true)
        XCTAssertNotNil(s.voiceReady, "the server says whether a call can start")
    }

    func testSettings() throws {
        let s = try ServerSettings.decode(fixture("settings"))
        XCTAssertEqual(s.assistantName, "Nova")
        XCTAssertEqual(s.userName, "Sam")
        XCTAssertNil(s.deliveryTarget, "\"none\" means no delivery")
    }

    func testOnboarding() throws {
        XCTAssertFalse(OnboardingStatus.parse(try fixture("onboarding")).isComplete)
        let after = OnboardingStatus.parse(try fixture("onboarding_post"))
        XCTAssertTrue(after.isComplete)
        XCTAssertTrue(after.done.isSuperset(of: ["paired", "names_set", "delivery_set"]))
    }

    /// The bodies the app sends, written where the plugin's e2e test replays them against the real
    /// server (`app request accepted: ...`). Keeps the request side of the contract honest too.
    func testRecordAppRequests() throws {
        let dir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("Contract/requests", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        func record(_ name: String, _ method: String, _ path: String, _ body: Data) throws {
            let object = try JSONSerialization.jsonObject(with: body)
            let spec: [String: Any] = ["method": method, "path": path, "body": object]
            try JSONSerialization.data(withJSONObject: spec, options: [.sortedKeys, .prettyPrinted])
                .write(to: dir.appendingPathComponent(name + ".json"))
        }
        var full = try ServerSettings.decode(fixture("settings"))
        try record("settings_patch", "PATCH", "/voice/settings", full.patchBody())
        full.delivery = .init(target: nil, newThread: false)
        try record("settings_patch_no_delivery", "PATCH", "/voice/settings", full.patchBody())
        full.delivery = .init(target: "telegram:555000111", newThread: false, channels: [
            .init(target: "discord:9000000000001", label: "#general", topic: "everyday questions and errands", newThread: true)])
        try record("settings_patch_channels", "PATCH", "/voice/settings", full.patchBody())
        full.continuity = .init(enabled: false)
        try record("settings_patch_continuity_off", "PATCH", "/voice/settings", full.patchBody())
        try record("onboarding_post", "POST", "/voice/onboarding",
                   OnboardingStatus.postBody(assistantName: "Nova", userName: "Sam", target: nil, writeBrief: false,
                                             continuity: true))
        let brief = """
        ## User
        Sam, a product designer in Lisbon. Call him Sam.
        ## Assistant persona
        Nova: calm, direct, a little dry. First person, no filler.
        ## Capability map
        Can check the calendar, draft email for approval, search the web and message Sam on Telegram.
        ## Answer preferences
        Short spoken answers, the key number first, details on screen.
        ## Current context
        Preparing a client pitch this week.
        """
        try record("brief_put", "PUT", "/voice/brief", VoiceBrief.putBody(text: brief))
        let home = try HomeControlInfo.decode(fixture("home_put"))
        try record("home_put", "PUT", "/voice/home", HomeControlInfo.putBody(enabled: true, entities: Array(home.includedIDs.prefix(2))))
        // A call started by turning listening mode off: the e2e test replays it (with an
        // Idempotency-Key, against the faked OpenAI negotiation) and expects 201.
        var room = RoomTranscript()
        let start = Date(timeIntervalSince1970: 1_760_000_000)
        room.appendFinal("Sam says the deadline is Friday the 14th, and we can't move it.", at: start)
        room.appendFinal("Priya promises to send the deck to everyone by Wednesday.", at: start.addingTimeInterval(40))
        var utc = Calendar(identifier: .gregorian)
        utc.timeZone = TimeZone(identifier: "UTC")!
        let sdp = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
        try record("session-with-room", "POST", "/voice/sessions",
                   sessionRequestBody(sdp: sdp, room: room.snapshot(now: start.addingTimeInterval(60), calendar: utc).text))
    }

    func testHomeControl() throws {
        let found = try HomeControlInfo.decode(fixture("home"))
        XCTAssertTrue(found.available)
        XCTAssertFalse(found.enabled, "off until the user turns it on")
        XCTAssertFalse(found.devices.isEmpty)
        XCTAssertFalse(found.explainer.isEmpty)
        XCTAssertEqual(found.groups.first?.kind, "light", "lights first")
        let on = try HomeControlInfo.decode(fixture("home_put"))
        XCTAssertTrue(on.enabled)
        XCTAssertTrue(on.configured)
        XCTAssertFalse(on.includedIDs.isEmpty, "turning on saves the default picks")
        XCTAssertFalse(on.devices.contains { $0.kind == "lock" && $0.included }, "locks are never ticked by default")
    }

    func testDestinationsAndSuggestion() throws {
        let raw = try fixture("destinations")
        let list = Destination.list(raw)
        XCTAssertEqual(list.map(\.target), ["discord:9000000000001", "discord:9000000000002", "telegram:555000111"])
        XCTAssertEqual(list.first?.label, "Discord · Home / general")
        XCTAssertEqual(Destination.suggested(raw), "telegram:555000111")
    }

    func testBriefRoundTrip() throws {
        let b = try JSONDecoder().decode(VoiceBrief.self, from: fixture("brief"))
        XCTAssertEqual(b.state, "none")
        XCTAssertEqual(b.text, "")
        let body = try JSONSerialization.jsonObject(with: VoiceBrief.putBody(text: "About Sam")) as? [String: Any]
        XCTAssertEqual(Set(body?.keys ?? [:].keys), ["brief"], "PUT /voice/brief takes exactly {\"brief\": ...}")
        let withText = try JSONDecoder().decode(VoiceBrief.self, from: Data(#"{"brief":"About Sam","state":"edited","edited":true}"#.utf8))
        XCTAssertEqual(withText.text, "About Sam")
    }
}
