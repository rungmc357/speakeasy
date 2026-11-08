import XCTest
@testable import SpeakeasyCore

/// SPK-238: build 70 on plugin 0.3.46 said a saved call was "no longer saved" and left Recent empty.
final class PluginFeatureGateTests: XCTestCase {
    private func caps(_ json: String) throws -> ServerCapabilities {
        try JSONDecoder().decode(ServerCapabilities.self, from: Data(json.utf8))
    }

    func testFlagsDecodeAndDefaultOff() throws {
        let both = try caps(#"{"saved_calls": true, "now_recent": true}"#)
        XCTAssertTrue(both.savedCalls)
        XCTAssertTrue(both.nowRecent)
        let old = try caps(#"{"now": true, "now_dismiss": true, "voice_notes": true}"#)   // 0.3.46
        XCTAssertFalse(old.savedCalls)
        XCTAssertFalse(old.nowRecent)
    }

    func testAccessFromCapabilities() throws {
        let old = try caps(#"{"now": true}"#)
        XCTAssertEqual(PluginFeatureGate.access(.savedCall, capabilities: old), .needsUpdate)
        XCTAssertEqual(PluginFeatureGate.access(.recent, capabilities: old), .needsUpdate)
        let new = try caps(#"{"saved_calls": true, "now_recent": true}"#)
        XCTAssertEqual(PluginFeatureGate.access(.savedCall, capabilities: new), .available)
        XCTAssertEqual(PluginFeatureGate.access(.recent, capabilities: new), .available)
        XCTAssertEqual(PluginFeatureGate.access(.savedCall, capabilities: nil), .unknown)
        XCTAssertEqual(PluginFeatureGate.access(advertised: nil), .unknown)
    }

    func testOlderPluginIsNeverAskedAndSaysUpdate() {
        XCTAssertFalse(PluginFeatureGate.shouldAsk(.needsUpdate))
        XCTAssertTrue(PluginFeatureGate.shouldAsk(.available))
        XCTAssertTrue(PluginFeatureGate.shouldAsk(.unknown))
        for feature in [PluginFeatureGate.Feature.savedCall, .recent] {
            let outcome = PluginFeatureGate.outcome(feature, access: .needsUpdate, status: nil)
            XCTAssertEqual(outcome, .needsUpdate)
            XCTAssertEqual(PluginFeatureGate.line(outcome), "Update Speakeasy on your Hermes to see this.")
        }
    }

    /// The Geo case: the old plugin's unknown-route 404 never reads as a call that's gone.
    func testA404WithoutTheFlagIsNotAGoneCall() {
        XCTAssertEqual(PluginFeatureGate.outcome(.savedCall, access: .unknown, status: 404), .needsUpdate)
        XCTAssertEqual(PluginFeatureGate.outcome(.recent, access: .unknown, status: 404), .needsUpdate)
    }

    func testA404FromAPluginWithSavedCallsIsAGoneCall() {
        let outcome = PluginFeatureGate.outcome(.savedCall, access: .available, status: 404)
        XCTAssertEqual(outcome, .gone)
        XCTAssertEqual(PluginFeatureGate.line(outcome), "This call is no longer saved on your Hermes.")
        // Recent has nothing to be "gone": its 404 is an ordinary failure.
        XCTAssertEqual(PluginFeatureGate.outcome(.recent, access: .available, status: 404), .failed)
    }

    func testOtherAnswers() {
        for access in [PluginFeatureGate.Access.available, .unknown] {
            XCTAssertEqual(PluginFeatureGate.outcome(.savedCall, access: access, status: 200), .loaded)
            XCTAssertEqual(PluginFeatureGate.outcome(.recent, access: access, status: 200), .loaded)
            XCTAssertEqual(PluginFeatureGate.outcome(.savedCall, access: access, status: 500), .failed)
            XCTAssertEqual(PluginFeatureGate.outcome(.savedCall, access: access, status: nil), .failed)
        }
        XCTAssertNil(PluginFeatureGate.line(.loaded))
        XCTAssertNil(PluginFeatureGate.line(.failed))
    }

    /// SPK-118/158 tone: plain words, no error codes or jargon.
    func testCopyIsPlain() {
        for line in [PluginFeatureGate.updateLine, PluginFeatureGate.callGoneLine] {
            XCTAssertNil(line.range(of: #"\d{3}|error|plugin|route|capabilit"#, options: [.regularExpression, .caseInsensitive]), line)
            XCTAssertTrue(line.hasSuffix("."))
        }
    }
}
