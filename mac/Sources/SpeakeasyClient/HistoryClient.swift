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
    public func startChat(text: String, clientID: String = UUID().uuidString) async throws -> String? {
        guard let clean = TaskMessageRequest.clean(text) else { throw HTTPError(status: 400, message: "The message is empty.") }
        let reply = try await post("/voice/work", ["text": clean, "source": "typed", "client_id": clientID])
        guard reply["ok"] as? Bool == true else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't start that.")
        }
        return (reply["session_id"] as? String) ?? (reply["run_id"] as? String)
    }
}
