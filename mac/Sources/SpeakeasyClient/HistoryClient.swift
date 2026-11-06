import Foundation
import SpeakeasyCore

// The Mac history window's calls beyond the shared ones in ServerClient (history, historySession,
// sendToSession, answerSessionApproval).

extension ServerClient {
    /// One page for the sidebar list (with its page size).
    public func historyPage(_ query: HistoryQuery) async throws -> HistoryPage {
        HistoryPage(json: try await json(query.path))
    }

    /// New chat: `POST /voice/work` with the first message (its own job and Hermes session, never
    /// routed). The route takes `source: "typed"`; a voice message's reviewed transcript goes as that.
    /// Returns the new task's run id (its session lists it in `run_ids`).
    public func startChat(text: String, clientID: String = UUID().uuidString, choice: ModelChoice? = nil,
                          agent: String? = nil) async throws -> String? {
        guard let clean = TaskMessageRequest.clean(text) else { throw HTTPError(status: 400, message: "The message is empty.") }
        var body: [String: Any] = ["text": clean, "source": "typed", "client_id": clientID]
        if let agent {
            body["agent"] = agent   // another agent runs on its own model: no model fields
        } else if let choice, !choice.isEmpty { body.merge(choice.body()) { _, new in new } }
        let reply = try await post("/voice/work", body)
        guard reply["ok"] as? Bool == true else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't start that.")
        }
        return (reply["session_id"] as? String) ?? (reply["run_id"] as? String)
    }
}

/// The device-wide history feed (`GET /voice/history/events`, plugins from 0.3.34): every list,
/// session page, board and widget refreshes within ~1-2 s of a change made anywhere (another device,
/// Hermes' other apps, a task finishing). Reconnects with backoff; on older plugins it stops and the
/// caller keeps polling. `onChange` gets the event name and its session id (nil: reload everything).
@MainActor
public final class HistoryLiveFeed {
    public private(set) var supported = true
    public private(set) var connected = false
    private var task: Task<Void, Never>?
    private let api: () -> ServerClient?
    private let onChange: @MainActor (String, String?) -> Void
    /// Every event with its whole payload (`run_id` on `decision_resolved`): the phone clears the
    /// answered question's notification and refreshes its Live Activity and widgets from it.
    public var onEvent: (@MainActor (String, [String: Any]) -> Void)?

    public init(api: @escaping () -> ServerClient?, onChange: @escaping @MainActor (String, String?) -> Void) {
        self.api = api; self.onChange = onChange
    }

    public func start() {
        guard task == nil, supported else { return }
        task = Task { [weak self] in
            var parser = SSEParser()
            var backoff: UInt64 = 1
            while !Task.isCancelled {
                guard let self, let api = self.api() else { try? await Task.sleep(nanoseconds: 5_000_000_000); continue }
                let outcome = await api.streamHistoryEvents(parser: &parser) { [weak self] event in
                    let payload = ((try? JSONSerialization.jsonObject(with: Data(event.data.utf8))) as? [String: Any]) ?? [:]
                    await self?.deliver(event.name, payload)
                }
                if self.connected { backoff = 1 }
                self.connected = false
                if case .unsupported = outcome { self.supported = false; return }
                try? await Task.sleep(nanoseconds: backoff * 1_000_000_000)
                backoff = min(backoff * 2, 30)
            }
        }
    }

    private func deliver(_ name: String, _ payload: [String: Any]) {
        connected = true
        onEvent?(name, payload)
        onChange(name, payload["session_id"] as? String)
    }

    /// Events after which every surface showing task state (board, Live Activity, widgets,
    /// delivered notifications) should re-read it: a question answered anywhere, a session changed.
    public static func touchesTasks(_ name: String) -> Bool {
        name == "decision_resolved" || name == "session_updated" || name == "history_changed"
    }

    public func stop() { task?.cancel(); task = nil; connected = false }
}
