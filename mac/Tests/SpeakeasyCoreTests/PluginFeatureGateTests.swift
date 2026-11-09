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
        XCTAssertEqual(PluginFeatureGate.access(.savedCall, capabilities: old), .hidden)
        XCTAssertEqual(PluginFeatureGate.access(.recent, capabilities: old), .hidden)
        let new = try caps(#"{"saved_calls": true, "now_recent": true}"#)
        XCTAssertEqual(PluginFeatureGate.access(.savedCall, capabilities: new), .available)
        XCTAssertEqual(PluginFeatureGate.access(.recent, capabilities: new), .available)
        XCTAssertEqual(PluginFeatureGate.access(.savedCall, capabilities: nil), .unknown)
        XCTAssertEqual(PluginFeatureGate.access(advertised: nil), .unknown)
    }

    /// No plugin release serves either route yet, so no plugin, however new, is told to update.
    func testNoReleaseBringsTheRouteSoNothingSaysUpdate() throws {
        XCTAssertNil(PluginFeatureGate.firstPluginVersion(.savedCall))
        XCTAssertNil(PluginFeatureGate.firstPluginVersion(.recent))
        let old = try caps(#"{"now": true, "now_dismiss": true}"#)
        for feature in [PluginFeatureGate.Feature.savedCall, .recent] {
            let access = PluginFeatureGate.access(feature, capabilities: old, runningVersion: "0.3.46", latestVersion: "0.3.47")
            XCTAssertEqual(access, .hidden)
            XCTAssertFalse(PluginFeatureGate.shouldAsk(access))
            let outcome = PluginFeatureGate.outcome(feature, access: access, status: nil)
            XCTAssertEqual(outcome, .hidden)
            XCTAssertNil(PluginFeatureGate.line(outcome))
        }
    }

    /// "Update" only when the release the user can install is the one that brings the route.
    func testUpdateLineOnlyWhenAnInstallableReleaseHasTheRoute() {
        func access(_ running: String?, _ latest: String?, first: String? = "0.3.50") -> PluginFeatureGate.Access {
            PluginFeatureGate.access(advertised: false, firstVersion: first, runningVersion: running, latestVersion: latest)
        }
        XCTAssertEqual(access("0.3.46", "0.3.50"), .needsUpdate)
        XCTAssertEqual(access("0.3.46", "0.3.51"), .needsUpdate)
        XCTAssertEqual(access("0.3.46", "0.3.49"), .hidden)      // the newer release doesn't have it yet
        XCTAssertEqual(access("0.3.50", "0.3.50"), .hidden)      // up to date: an update brings nothing
        XCTAssertEqual(access("0.3.46", nil), .hidden)           // published version not known
        XCTAssertEqual(access(nil, "0.3.50"), .hidden)
        XCTAssertEqual(access("0.3.46", "0.3.50", first: nil), .hidden)
        XCTAssertEqual(PluginFeatureGate.access(advertised: false), .hidden)

        XCTAssertFalse(PluginFeatureGate.shouldAsk(.needsUpdate))
        XCTAssertTrue(PluginFeatureGate.shouldAsk(.available))
        XCTAssertTrue(PluginFeatureGate.shouldAsk(.unknown))
        for feature in [PluginFeatureGate.Feature.savedCall, .recent] {
            let outcome = PluginFeatureGate.outcome(feature, access: .needsUpdate, status: nil)
            XCTAssertEqual(outcome, .needsUpdate)
            XCTAssertEqual(PluginFeatureGate.line(outcome), "Update Speakeasy on your Hermes to see this.")
        }
    }

    /// The Geo case: the old plugin's unknown-route 404 never reads as a call that's gone, or as "Update".
    func testA404WithoutTheFlagIsNotAGoneCall() {
        let unknownRoute = Data(#"{"error": "not found"}"#.utf8)
        for feature in [PluginFeatureGate.Feature.savedCall, .recent] {
            XCTAssertEqual(PluginFeatureGate.outcome(feature, access: .unknown, status: 404), .hidden)
            XCTAssertEqual(PluginFeatureGate.outcome(feature, access: .unknown, status: 404, body: unknownRoute), .hidden)
        }
        XCTAssertTrue(PluginFeatureGate.isUnknownRoute(unknownRoute))
    }

    /// Before capabilities arrive, the route's own 404 still means the call is gone.
    func testTheRoutesOwn404IsAGoneCallEvenBeforeCapabilities() {
        let own = Data(#"{"error": "That call isn't saved anymore."}"#.utf8)
        XCTAssertFalse(PluginFeatureGate.isUnknownRoute(own))
        XCTAssertFalse(PluginFeatureGate.isUnknownRoute(Data(#"{"error": "not found", "reason": "call"}"#.utf8)))
        XCTAssertFalse(PluginFeatureGate.isUnknownRoute(Data("not json".utf8)))
        XCTAssertEqual(PluginFeatureGate.outcome(.savedCall, access: .unknown, status: 404, body: own), .gone)
        XCTAssertEqual(PluginFeatureGate.outcome(.recent, access: .unknown, status: 404, body: own), .failed)
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
        XCTAssertNil(PluginFeatureGate.line(.hidden))
    }

    /// SPK-118/158 tone: plain words, no error codes or jargon.
    func testCopyIsPlain() {
        for line in [PluginFeatureGate.updateLine, PluginFeatureGate.callGoneLine] {
            XCTAssertNil(line.range(of: #"\d{3}|error|plugin|route|capabilit"#, options: [.regularExpression, .caseInsensitive]), line)
            XCTAssertTrue(line.hasSuffix("."))
        }
    }
}
