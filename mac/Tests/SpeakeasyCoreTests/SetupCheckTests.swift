import XCTest
@testable import SpeakeasyCore

final class SetupCheckTests: XCTestCase {
    private func json(_ text: String) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any] ?? [:]
    }

    func testDecodesPluginRows() throws {
        let check = try XCTUnwrap(SetupCheck(json: json("""
        {"ready": false, "version": "0.3.37", "rows": [
          {"id": "plugin", "ok": true, "blocking": true, "title": "Speakeasy 0.3.37 is on your Hermes", "detail": "", "fix": null},
          {"id": "restart", "ok": false, "blocking": true, "title": "Hermes needs a restart", "detail": "Send /restart.",
           "fix": {"kind": "copy", "label": "Copy /restart", "payload": "/restart"}},
          {"id": "voice", "ok": false, "blocking": true, "title": "Voice", "detail": "Sign in.",
           "fix": {"kind": "codex_sign_in", "label": "Sign in with ChatGPT", "payload": ""}},
          {"id": "brief", "ok": false, "blocking": false, "title": "No voice brief yet", "detail": "Optional", "fix": null}
        ]}
        """)))
        XCTAssertFalse(check.ready)
        XCTAssertEqual(check.version, "0.3.37")
        XCTAssertEqual(check.rows.map(\.id), ["plugin", "restart", "voice", "brief"])
        XCTAssertEqual(check.rows[1].fix, .init(kind: .copy, label: "Copy /restart", payload: "/restart"))
        XCTAssertEqual(check.rows[2].fix?.kind, .codexSignIn)
        XCTAssertEqual(check.rows[3].state, .optional)
        XCTAssertEqual(check.blockers.map(\.id), ["restart", "voice"])
    }

    func testUnknownFixKindShowsNoButton() throws {
        let check = try XCTUnwrap(SetupCheck(json: json("""
        {"ready": false, "rows": [{"id": "x", "ok": false, "title": "New check", "detail": "d",
          "fix": {"kind": "teleport", "label": "Beam it"}}]}
        """)))
        XCTAssertEqual(check.rows[0].fix?.kind, .unknown("teleport"))
        XCTAssertFalse(check.rows[0].fix!.actionable)
        XCTAssertTrue(check.rows[0].blocking)
    }

    func testUnreadableRowNeverMeansReady() throws {
        let check = try XCTUnwrap(SetupCheck(json: json("""
        {"ready": true, "rows": [{"id": "plugin", "ok": true, "title": "ok"}, {"broken": 1}]}
        """)))
        XCTAssertFalse(check.ready)
        XCTAssertNil(SetupCheck(json: [:]))
    }

    func testFallbackForAnOlderPlugin() {
        var status = ServerStatus(provider: "codex", codexSignedIn: false, hermesAPIOK: true, version: "0.3.30")
        status.voiceReady = false
        status.capabilities = ServerCapabilities(codexLogin: true)
        let check = SetupCheck.fallback(status)
        XCTAssertFalse(check.ready)
        XCTAssertEqual(check.rows.first?.id, "plugin")
        XCTAssertFalse(check.rows.first!.blocking)
        XCTAssertEqual(check.rows.first { $0.id == "voice" }?.fix?.kind, .codexSignIn)
        status.capabilities = nil
        XCTAssertEqual(SetupCheck.fallback(status).rows.first { $0.id == "voice" }?.fix?.kind, .copy)
        status.voiceReady = true; status.codexSignedIn = true
        XCTAssertTrue(SetupCheck.fallback(status).ready)
        for row in SetupCheck.fallback(ServerStatus(hermesAPIOK: false)).rows where !row.ok {
            XCTAssertFalse((row.detail + row.title).lowercased().contains("terminal"))
            XCTAssertNotNil(row.fix)
        }
    }

    func testStartACallOpensTheCheckOnlyWhenACallCantStart() {
        var status = ServerStatus(provider: "codex", codexSignedIn: true, hermesAPIOK: true)
        status.voiceReady = true
        XCTAssertFalse(status.needsSetupCheck)
        status.voiceReady = false   // includes "Hermes needs a restart"
        XCTAssertTrue(status.needsSetupCheck)
        status.voiceReady = true; status.hermesAPIOK = false
        XCTAssertTrue(status.needsSetupCheck)
        // Older plugin without voice_ready: the checklist decides, and the brief never blocks.
        let old = ServerStatus(provider: "codex", codexSignedIn: true, briefState: "writing", hermesAPIOK: true)
        XCTAssertFalse(old.needsSetupCheck)
        XCTAssertTrue(ServerStatus(provider: "codex", codexSignedIn: false, hermesAPIOK: true).needsSetupCheck)
    }
}
