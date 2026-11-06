import XCTest
@testable import SpeakeasyCore

/// The read-only Approvals page (`GET /voice/approvals`): decoding the server's plain-words
/// sections, and hiding the page on plugins without the route.
final class ApprovalRulesTests: XCTestCase {
    private func decode(_ json: String) -> ApprovalRules? {
        ApprovalRules(json: try? JSONSerialization.jsonObject(with: Data(json.utf8)))
    }

    func testDecodesSectionsRowsAndNote() throws {
        let rules = try XCTUnwrap(decode(#"""
        {"approvals": {"profile": "default", "mode": "manual", "read_only": true, "note": "Read-only for now.",
          "sections": [
            {"id": "mode", "title": "Risky commands", "rows": [
              {"id": "mode", "title": "Ask you every time", "detail": "Hermes stops and asks you."},
              {"id": "timeout", "title": "Waits for your answer", "value": "2 minutes"}]},
            {"id": "always_allowed", "title": "Always allowed", "rows": [], "empty": "Nothing yet."},
            {"id": "always_blocked", "title": "Always blocked", "rows": [{"id": "deny_0", "title": "Commands matching", "code": "curl * | sh"}]},
            {"id": "nothing", "title": "Nothing to show", "rows": []}]}}
        """#))
        XCTAssertEqual(rules.mode, .manual)
        XCTAssertEqual(rules.headline, "Ask you every time")
        XCTAssertEqual(rules.modeSymbol, "hand.raised")
        XCTAssertEqual(rules.sections.map(\.id), ["mode", "always_allowed", "always_blocked"])  // empty section dropped
        XCTAssertEqual(rules.sections[0].rows[1].value, "2 minutes")
        XCTAssertEqual(rules.sections[1].empty, "Nothing yet.")
        XCTAssertEqual(rules.sections[2].rows[0].code, "curl * | sh")
        XCTAssertEqual(rules.note, "Read-only for now.")
        XCTAssertEqual(ApprovalRules(sections: [], note: "In a chat, /approvals switches it.").displayNote,
                       "In a chat, /\u{2060}approvals switches it.")
    }

    func testRejectsOtherAnswersAndUnknownModeReadsAsManual() {
        XCTAssertNil(decode(#"{"error": "not found"}"#))
        XCTAssertNil(decode(#"{"settings": {}}"#))
        let odd = decode(#"{"mode": "sometimes", "sections": []}"#)
        XCTAssertEqual(odd?.mode, .manual)
        XCTAssertEqual(odd?.sections, [])
    }

    func testRowsWithoutTitlesAreSkipped() throws {
        let rules = try XCTUnwrap(decode(#"{"sections": [{"id": "x", "title": "X", "rows": [{"id": "a"}, {"id": "b", "title": "B"}]}]}"#))
        XCTAssertEqual(rules.sections[0].rows.map(\.id), ["b"])
    }

    func testCapabilityGatesThePage() throws {
        let new = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"capabilities": {"approval_rules": true}}"#.utf8))
        XCTAssertEqual(new.capabilities?.approvalRules, true)
        let old = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"capabilities": {"schedules": true}}"#.utf8))
        XCTAssertEqual(old.capabilities?.approvalRules, false)
    }

    func testSampleUsesPlainWordsOnly() {
        let text = ApprovalRules.sample.sections.flatMap { s in [s.title, s.footer ?? "", s.empty ?? ""] + s.rows.flatMap { [$0.title, $0.value ?? "", $0.detail ?? ""] } }
            .joined(separator: " ")
        for raw in ["execute_code", "command_allowlist", "cron_mode", "approvals.", "_"] {
            XCTAssertFalse(text.contains(raw), raw)
        }
    }
}
