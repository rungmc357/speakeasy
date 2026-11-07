import AppKit
import ApplicationServices
import SwiftUI
import SpeakeasyCore

/// Native acceptance through the production session view and accessibility actions. No live server.
@MainActor
enum ReplyWorkCheck {
    static func run(_ controller: HistoryWindowController, backend: DemoHistoryBackend, dir: String) async throws {
        let application = AXUIElementCreateApplication(getpid())
        AXUIElementSetAttributeValue(application, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        AXUIElementSetAttributeValue(application, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
        let payload: [String: Any] = ["session": [
            "id": "work-check", "title": "Check the release report", "source": "speakeasy",
            "source_label": "Speakeasy", "state": "idle", "send_via": "task", "entries": [
                ["id": "u", "kind": "user", "text": "Check the release report."],
                ["id": "n", "kind": "assistant", "text": "Checking the saved report", "narration": true],
                ["id": "t", "kind": "tools", "items": [
                    ["id": "read", "kind": "read", "text": "Read the release report", "tool": "read_file", "status": "done", "args": "reports/release.txt", "result": "10 checks passed"],
                    ["id": "run", "kind": "run", "text": "Ran the release check", "tool": "terminal", "status": "done", "args": "make verify", "result": "10 checks passed"]]],
                ["id": "a", "kind": "assistant", "text": "All **10 checks passed**. The build is ready for review."]]]]
        guard let conversation = HistoryConversation(json: payload) else { throw failure("Invalid check fixture") }
        backend.conversations[conversation.id] = conversation
        controller.store.open(conversation.id)
        try await Task.sleep(nanoseconds: 700_000_000)
        guard let window = controller.window else { throw failure("No session window") }
        func all() -> [NSObject] { descendants(window) }
        func find(_ id: String) -> NSObject? { all().first { string($0, "accessibilityIdentifier") == id } }
        guard all().filter({ string($0, "accessibilityIdentifier") == "reply-work-toggle" }).count == 1,
              let toggle = find("reply-work-toggle"), find("reply-work-steps") == nil else {
            let nodes = all().map { "\(type(of: $0)) | \(string($0, "accessibilityIdentifier") ?? "") | \(string($0, "accessibilityLabel") ?? "")" }
            try nodes.joined(separator: "\n").write(toFile: dir + "/accessibility.txt", atomically: true, encoding: .utf8)
            try HistoryShots.snapshot(controller, to: dir + "/failed-collapsed.png")
            throw failure("Not one collapsed card")
        }
        try HistoryShots.snapshot(controller, to: dir + "/work-collapsed.png")
        guard press(toggle) else { throw failure("Card press not handled") }
        try await Task.sleep(nanoseconds: 400_000_000)
        guard find("reply-work-steps") != nil else { throw failure("Card did not expand") }
        let steps = all().filter { string($0, "accessibilityIdentifier") == "reply-work-step" }
        guard steps.count == 2, steps.first.flatMap({ string($0, "accessibilityLabel") })?.contains("Ran the release check") == true else { throw failure("Wrong steps/order") }
        guard press(steps[0]) else { throw failure("Step press not handled") }
        try await Task.sleep(nanoseconds: 400_000_000)
        guard find("reply-work-evidence") != nil else { throw failure("Evidence did not expand") }
        try HistoryShots.snapshot(controller, to: dir + "/work-evidence.png")
        guard press(steps[0]) else { throw failure("Step collapse not handled") }
        try await Task.sleep(nanoseconds: 300_000_000)
        guard find("reply-work-evidence") == nil else { throw failure("Evidence did not collapse") }
        guard let close = find("reply-work-toggle"), press(close) else { throw failure("Card collapse not handled") }
        try await Task.sleep(nanoseconds: 300_000_000)
        guard find("reply-work-steps") == nil, controller.store.target == .session(conversation.id) else { throw failure("Collapsed card navigated") }
        let receipt: [String: Any] = ["passed": true, "surface": "production Mac session", "one_card": true,
                                    "expands": true, "newest_first": true, "evidence": true, "collapses": true, "no_navigation": true]
        try JSONSerialization.data(withJSONObject: receipt, options: [.prettyPrinted, .sortedKeys])
            .write(to: URL(fileURLWithPath: dir + "/work-check.json"))
    }
    private static func failure(_ message: String) -> NSError { NSError(domain: "ReplyWorkCheck", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    // SwiftUI accessibility proxies expose selectors without declaring NSAccessibilityProtocol.
    private static func string(_ node: NSObject, _ key: String) -> String? {
        guard node.responds(to: NSSelectorFromString(key)) else { return nil }
        return node.value(forKey: key) as? String
    }
    private static func press(_ node: NSObject) -> Bool {
        let selector = NSSelectorFromString("accessibilityPerformPress")
        guard node.responds(to: selector), let implementation = node.method(for: selector) else { return false }
        typealias Action = @convention(c) (AnyObject, Selector) -> Bool
        return unsafeBitCast(implementation, to: Action.self)(node, selector)
    }
    private static func descendants(_ root: Any, depth: Int = 0) -> [NSObject] {
        guard depth < 40, let node = root as? NSObject else { return [] }
        let children = node.responds(to: NSSelectorFromString("accessibilityChildren"))
            ? (node.value(forKey: "accessibilityChildren") as? [Any] ?? []) : []
        return [node] + children.flatMap { descendants($0, depth: depth + 1) }
    }
}
