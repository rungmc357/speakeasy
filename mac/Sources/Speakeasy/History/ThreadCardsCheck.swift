import AppKit
import ApplicationServices
import SwiftUI
import SpeakeasyCore

/// SPK-540 acceptance on the production session view, through accessibility: a thread waiting on an
/// approval card offers the card's own button, a press goes to the card-action route with the
/// preview's fingerprint, the settled card has no buttons, and a decision shows its options and
/// still answers its task. Invented content and the demo backend only: nothing here reaches a server.
@MainActor
enum ThreadCardsCheck {
    static let fingerprint = String(repeating: "0a", count: 32)

    static func run(_ controller: HistoryWindowController, backend: DemoHistoryBackend, dir: String) async throws {
        let application = AXUIElementCreateApplication(getpid())
        AXUIElementSetAttributeValue(application, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        AXUIElementSetAttributeValue(application, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
        guard let window = controller.window else { throw failure("No session window") }
        func nodes(_ id: String) -> [NSObject] {
            ReplyWorkCheck.descendants(window).filter { ReplyWorkCheck.string($0, "accessibilityIdentifier") == id }
        }
        // A button says its words as its label; plain text says them as its value.
        func labels(_ id: String) -> [String] {
            nodes(id).map { node in
                ["accessibilityLabel", "accessibilityValue"].lazy.compactMap { ReplyWorkCheck.string(node, $0) }.first { !$0.isEmpty } ?? ""
            }
        }
        var passed: [String] = []
        func check(_ name: String, _ ok: Bool, _ seen: @autoclosure () -> String) throws {
            print("thread-cards check \(name): \(ok ? "PASS" : "FAIL")")
            guard ok else {
                let all = ReplyWorkCheck.descendants(window).map {
                    "\(type(of: $0)) | \(ReplyWorkCheck.string($0, "accessibilityIdentifier") ?? "") | \(ReplyWorkCheck.string($0, "accessibilityLabel") ?? "")"
                }
                try? all.joined(separator: "\n").write(toFile: dir + "/accessibility.txt", atomically: true, encoding: .utf8)
                try? HistoryShots.snapshot(controller, to: dir + "/failed-\(name).png")
                throw failure("\(name): \(seen())")
            }
            passed.append(name)
        }

        let t0 = Date().timeIntervalSince1970 - 600
        let approval = try conversation(id: "cards-check-approval", title: "QA test: approval card in a chat thread", t0: t0,
            asked: "Draft a test note for #qa-sandbox and hold it for my OK.",
            answer: "The test note is ready. Nothing is posted until you approve it on the card.",
            job: ["run_id": "run_cards_check"],
            view: ["kind": "confirm", "type": "post", "title": "Post the test note to #qa-sandbox?",
                   "details": ["account": "qa-sandbox (test)", "body": "QA test note. Nothing here is real and nothing should be posted."],
                   "sha256": fingerprint, "state": "pending",
                   "actions": [["id": "approve", "label": "Post", "style": "primary", "value": fingerprint],
                               ["id": "cancel", "label": "Cancel", "style": "secondary", "value": fingerprint]]])
        backend.conversations[approval.id] = approval
        controller.store.open(approval.id)
        try await Task.sleep(nanoseconds: 900_000_000)
        try check("testThePageOffersTheCardsOwnApproveButton",
                  nodes("history-confirm").count == 1 && labels("history-confirm-approve") == ["Post"]
                      && labels("history-confirm-cancel") == ["Cancel"],
                  "cards \(nodes("history-confirm").count), approve \(labels("history-confirm-approve")), cancel \(labels("history-confirm-cancel"))")
        try HistoryShots.snapshot(controller, to: dir + "/thread-card-pending.png")

        let refusal = "The preview changed. Review it and approve again."
        backend.cardRefusal = refusal
        guard let refused = nodes("history-confirm-approve").first, ReplyWorkCheck.press(refused) else { throw failure("Post press not handled") }
        try await Task.sleep(nanoseconds: 900_000_000)
        try check("testARefusalIsSaidUnderTheCardAndTheButtonsStay",
                  labels("history-confirm-error") == [refusal] && labels("history-confirm-approve") == ["Post"],
                  "error \(labels("history-confirm-error")), approve \(labels("history-confirm-approve"))")
        try HistoryShots.snapshot(controller, to: dir + "/thread-card-refused.png")
        backend.cardRefusal = nil
        backend.cardActions = []

        guard let approve = nodes("history-confirm-approve").first, ReplyWorkCheck.press(approve) else { throw failure("Post press not handled") }
        try await Task.sleep(nanoseconds: 900_000_000)
        let sent = backend.cardActions
        try check("testPostGoesToTheCardActionRouteWithThePreviewFingerprint",
                  sent.count == 1 && sent.first?.runID == "run_cards_check" && sent.first?.view == 0
                      && sent.first?.action == "approve" && sent.first?.value == fingerprint
                      && labels("history-confirm-error").isEmpty,
                  "\(sent), error \(labels("history-confirm-error"))")
        try check("testASettledCardShowsItsOutcomeNotButtons",
                  nodes("history-confirm").count == 1 && nodes("history-confirm-approve").isEmpty && nodes("history-confirm-cancel").isEmpty,
                  "cards \(nodes("history-confirm").count), approve \(labels("history-confirm-approve")), cancel \(labels("history-confirm-cancel"))")
        try HistoryShots.snapshot(controller, to: dir + "/thread-card-approved.png")

        let decision = try conversation(id: "cards-check-decision", title: "QA test: decision card in a chat thread", t0: t0,
            asked: "Ask me which colour to use.", answer: "Which colour should the sample be? I'd go with blue.",
            job: ["run_id": "run_cards_question", "question_open": true, "question_state": "open", "answered": false, "set_aside": false],
            view: ["kind": "question", "question": "QA test: which colour should the sample be?", "recommended": 0,
                   "context": "Test question, nothing depends on it.", "why": "Blue is the control value.", "affects": "Nothing",
                   "options": [["text": "Blue", "detail": "Control value", "risk": "low", "reversible": true],
                               ["text": "Green", "detail": "Alternate value", "risk": "low", "reversible": true]]])
        // A pick is saved until the server confirms it, and the demo backend never does: without this
        // the next run would find its own question already answered and draw no card.
        controller.store.pending.remove("run_cards_question")
        defer { controller.store.pending.remove("run_cards_question") }
        backend.conversations[decision.id] = decision
        controller.store.open(decision.id)
        try await Task.sleep(nanoseconds: 900_000_000)
        // The card's identifier covers everything inside it: its options are the buttons named "Option n: …".
        func options() -> [NSObject] {
            nodes("history-question").filter { ReplyWorkCheck.string($0, "accessibilityLabel")?.hasPrefix("Option ") == true }
        }
        let said = options().map { ReplyWorkCheck.string($0, "accessibilityLabel") ?? "" }
        let details = options().map { ReplyWorkCheck.string($0, "accessibilityValue") ?? "" }
        try check("testADecisionShowsEveryOptionAndTheRecommendedOne",
                  said == ["Option 1: Blue, recommended", "Option 2: Green"],
                  "options \(said)")
        try check("testADecisionOptionReadsItsDetailAndTheWhy",
                  details == ["Control value. Blue is the control value.", "Alternate value"],
                  "details \(details)")
        try HistoryShots.snapshot(controller, to: dir + "/thread-decision.png")
        guard let green = options().last, ReplyWorkCheck.press(green) else { throw failure("Option press not handled") }
        try await Task.sleep(nanoseconds: 600_000_000)
        try check("testADecisionOptionStillAnswersItsTask",
                  backend.sent.last?.text == "Green" && backend.sent.last?.answers == "run_cards_question"
                      && controller.store.pending["run_cards_question"]?.text == "Green" && nodes("history-question").isEmpty,
                  "sent \(backend.sent.map { "\($0.text) -> \($0.answers ?? "nobody")" }), pick \(controller.store.pending["run_cards_question"]?.text ?? "none"), card nodes \(nodes("history-question").count)")

        let receipt: [String: Any] = ["passed": true, "surface": "production Mac session", "backend": "DemoHistoryBackend", "checks": passed]
        try JSONSerialization.data(withJSONObject: receipt, options: [.prettyPrinted, .sortedKeys])
            .write(to: URL(fileURLWithPath: dir + "/thread-cards-check.json"))
    }

    /// One finished task with one card, in the server's `/voice/history/{id}` shape.
    private static func conversation(id: String, title: String, t0: Double, asked: String, answer: String,
                                     job extra: [String: Any], view: [String: Any]) throws -> HistoryConversation {
        var job: [String: Any] = ["status": "completed", "title": title, "started_at": t0, "finished_at": t0 + 60,
                                  "session_id": id, "result": ["spoken": answer, "full": answer, "views": [view]]]
        job.merge(extra) { _, new in new }
        let payload: [String: Any] = ["session": [
            "id": id, "title": title, "source": "speakeasy", "source_label": "Speakeasy", "state": "needs_you", "send_via": "task",
            "entries": [["id": "u", "kind": "user", "at": t0, "text": asked], ["id": "a", "kind": "assistant", "at": t0 + 60, "text": answer]],
            "jobs": [job], "approvals": []]]
        guard let conversation = HistoryConversation(json: payload) else { throw failure("Invalid check fixture") }
        return conversation
    }

    private static func failure(_ message: String) -> NSError {
        NSError(domain: "ThreadCardsCheck", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}
