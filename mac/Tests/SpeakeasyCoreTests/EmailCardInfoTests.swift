import XCTest
@testable import SpeakeasyCore

/// The email card (b73): recipients with names and addresses, the thread line, unresolved names,
/// the edit payload, and Home's pick. Invented people only.
final class EmailCardInfoTests: XCTestCase {
    private func card(_ extra: [String: Any] = [:]) -> ViewCard {
        var json: [String: Any] = [
            "kind": "draft", "channel": "email", "to": "Pat Doe", "draft_id": "ed_" + String(repeating: "a", count: 24),
            "sha256": String(repeating: "b", count: 64), "status": "pending", "send_as": "sam@example.com",
            "recipients": ["to": [["name": "Pat Doe", "address": "pat@example.org"]],
                           "cc": [["name": "Robin Vale", "address": "robin@example.net"], ["name": "", "address": "kit@example.net"]]],
            "subject": "RE: Spring permit", "body": "Hi Pat,\n\nIt cleared.", "thread": "Spring permit", "thread_count": 4,
            "thread_last": ["from": "Pat Doe <pat@example.org>", "when": "Mon", "text": "Any news?"],
            "unresolved": ["Lark & Finch"], "notes": ["Copied as on your last message"],
        ]
        for (k, v) in extra { json[k] = v }
        return ViewCard(json: json, number: 1)!
    }

    func testReadsEveryRecipientAndTheThread() throws {
        let info = try XCTUnwrap(EmailCardInfo(card()))
        XCTAssertEqual(info.to, [.init(name: "Pat Doe", address: "pat@example.org")])
        XCTAssertEqual(info.cc.map(\.label), ["Robin Vale", "kit@example.net"])
        XCTAssertTrue(info.bcc.isEmpty)
        XCTAssertEqual(info.threadLine, "Reply in thread: Spring permit · 4 messages")
        XCTAssertEqual(info.threadLast?.text, "Any news?")
        XCTAssertEqual(info.unresolved, ["Lark & Finch"])
        XCTAssertTrue(card().isEmailCard)
    }

    func testAPlainDraftCardIsNotAnEmailCard() {
        let plain = ViewCard(json: ["kind": "draft", "channel": "sms", "to": "Pat", "body": "hi"], number: 1)!
        XCTAssertNil(EmailCardInfo(plain))
        let older = ViewCard(json: ["kind": "draft", "channel": "email", "to": "Pat", "body": "hi"], number: 1)!
        XCTAssertNil(EmailCardInfo(older), "an older server's draft view has no draft id: the plain card draws it")
    }

    func testTypedRecipients() {
        XCTAssertEqual(EmailCardInfo.Person(typed: "Lee Park <lee@example.org>"), .init(name: "Lee Park", address: "lee@example.org"))
        XCTAssertEqual(EmailCardInfo.Person(typed: " ana@example.org "), .init(name: "", address: "ana@example.org"))
        XCTAssertNil(EmailCardInfo.Person(typed: "Lark & Finch"))
        XCTAssertNil(EmailCardInfo.Person(typed: "Lark <not an address>"))
        XCTAssertEqual(EmailCardInfo.Person(name: "Lee Park", address: "lee@example.org").wire, "Lee Park <lee@example.org>")
    }

    func testAddingTheUnresolvedNameClearsIt() throws {
        let info = try XCTUnwrap(EmailCardInfo(card(["unresolved": ["Jo Marsh"]])))
        var fields = info.fields
        XCTAssertEqual(info.stillUnresolved(in: fields), ["Jo Marsh"])
        fields.cc.append(.init(name: "Jo Marsh", address: "jo@marsh.example"))
        XCTAssertEqual(info.stillUnresolved(in: fields), [])
    }

    /// "an email to Ben and Wren Mallory", Wren Mallory being Ben's firm: never a missing recipient.
    func testAnOrganisationIsNeverAMissingRecipient() throws {
        let info = try XCTUnwrap(EmailCardInfo(card()))   // an older server still lists "Lark & Finch"
        XCTAssertEqual(info.stillUnresolved(in: info.fields), [])
        let firm = try XCTUnwrap(EmailCardInfo(card([
            "recipients": ["to": [["name": "Ben Tarlow", "address": "ben@wrenmallory.example", "org": "Wren Mallory"]]],
            "unresolved": ["Wren Mallory", "Jo Marsh"]])))
        XCTAssertEqual(firm.to.first?.org, "Wren Mallory")
        XCTAssertEqual(firm.stillUnresolved(in: firm.fields), ["Jo Marsh"], "only a named person warns")
        let byDomain = try XCTUnwrap(EmailCardInfo(card([
            "recipients": ["to": [["name": "Ben Tarlow", "address": "ben@wrenmallory.example"]]],
            "unresolved": ["Wren Mallory", "Pat"]])))
        XCTAssertEqual(byDomain.stillUnresolved(in: byDomain.fields), ["Pat"])
        XCTAssertFalse(EmailCardInfo.domain("pat@patterson.example", matches: "Pat"))
        XCTAssertFalse(EmailCardInfo.domain("lark@icloud.com", matches: "Lark & Finch"))
        XCTAssertTrue(EmailCardInfo.looksLikeOrg("Ostrow Holdings LLP"))
        XCTAssertFalse(EmailCardInfo.looksLikeOrg("Jo Marsh"))
    }

    func testTheFirmIsNeverSent() {
        let p = EmailCardInfo.Person(name: "Pat Doe", address: "pat@larkfinch.example", org: "Lark & Finch")
        XCTAssertEqual(p.wire, "Pat Doe <pat@larkfinch.example>")
        XCTAssertNil(EmailCardInfo.Person(name: "Pat Doe", address: "pat@example.org", org: "").org)
    }

    func testEditValueCarriesEveryFieldAndTheHash() throws {
        let info = try XCTUnwrap(EmailCardInfo(card()))
        let value = info.fields.value(sha256: info.sha256, after: "send")
        XCTAssertEqual(value["sha256"].string, info.sha256)
        XCTAssertEqual(value["after"].string, "send")
        XCTAssertEqual(value["to"].strings, ["Pat Doe <pat@example.org>"])
        XCTAssertEqual(value["cc"].strings, ["Robin Vale <robin@example.net>", "kit@example.net"])
        XCTAssertEqual(value["subject"].string, "RE: Spring permit")
        var empty = info.fields; empty.to = []
        XCTAssertFalse(empty.sendable)
    }

    func testHomePicksTheEmailCardOverEverythingElse() {
        let stats = ViewCard(json: ["kind": "stats", "items": []], number: 1)!
        let list = ViewCard(json: ["kind": "list", "items": []], number: 2)!
        let email = card()
        XCTAssertEqual(ResultCardPick.pick([stats, list, email])?.kind, "draft")
        XCTAssertEqual(ResultCardPick.pick([stats, list])?.kind, "list")
    }
}
