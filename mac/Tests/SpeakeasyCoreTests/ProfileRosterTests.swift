import XCTest
@testable import SpeakeasyCore

/// Profile switching (build 75; docs/API.md "Profiles"): the roster the phone switches between.
final class ProfileRosterTests: XCTestCase {
    func testParsesTheServersShape() {
        let r = ProfileRoster(json: [
            "profiles": [["id": "default", "name": "Hermes", "model": "invented/own-model", "own": true, "current": false],
                         ["id": "garden", "name": "Garden helper", "model": "invented/garden-model-1", "own": false,
                          "current": true]],
            "current": "garden", "own": "default",
        ])
        XCTAssertEqual(r.profiles.map(\.id), ["default", "garden"])
        XCTAssertEqual(r.ownID, "default")
        XCTAssertEqual(r.own?.name, "Hermes")
        XCTAssertEqual(r.profile("garden")?.modelLabel, "garden-model-1")
    }

    func testTheSavedProfileWinsOnlyWhileTheServerServesIt() {
        let r = ProfileRoster.sample
        XCTAssertEqual(r.current(saved: "research-desk")?.id, "research-desk")
        XCTAssertEqual(r.current(saved: "gone-away")?.id, "default")   // removed on the Mac: back to the own
        XCTAssertEqual(r.current(saved: nil)?.id, "default")
        XCTAssertEqual(r.others(than: "research-desk").map(\.id), ["default"])
        XCTAssertEqual(r.others(than: nil).map(\.id), ["research-desk"])
    }

    func testAnEmptyOrOldReplyIsHarmless() {
        let r = ProfileRoster(json: ["error": "not found"])
        XCTAssertTrue(r.profiles.isEmpty)
        XCTAssertNil(r.current(saved: "garden"))
    }

    func testCapabilityIsReadAndDefaultsOff() throws {
        let on = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"profiles": true}"#.utf8))
        let off = try JSONDecoder().decode(ServerCapabilities.self, from: Data("{}".utf8))
        XCTAssertTrue(on.profiles)
        XCTAssertFalse(off.profiles)
    }
}
