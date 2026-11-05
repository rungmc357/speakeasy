import Foundation
import SpeakeasyCore

// Session / task history (servers with `capabilities.history`). See SpeakeasyCore/SessionHistory.swift.

extension ServerClient {
    /// One page of `GET /voice/history`.
    public func history(_ query: HistoryQuery) async throws -> HistoryPage {
        let object = try await json(query.path)
        guard let page = HistoryPage(json: object) else {
            throw HTTPError(status: 502, message: "Your Hermes sent a history page Speakeasy couldn't read.")
        }
        return page
    }

    /// `GET /voice/history/{session_id}`: its messages, tool calls, helpers, approvals and questions.
    public func historyDetail(_ sessionID: String) async throws -> HistoryDetail {
        let allowed = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/"))
        let object = try await json("/voice/history/\(sessionID.addingPercentEncoding(withAllowedCharacters: allowed) ?? sessionID)")
        guard let detail = HistoryDetail(json: object, id: sessionID) else {
            throw HTTPError(status: 502, message: "Your Hermes sent a session Speakeasy couldn't read.")
        }
        return detail
    }

    /// A message to one session, by id: never through the voice's router. Idempotent on `clientID`.
    public func sendHistoryMessage(_ request: HistoryMessageRequest) async throws -> HistoryMessageReply {
        let reply = try await post(request.path, request.body)
        guard let parsed = HistoryMessageReply(json: reply) else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't take that message.")
        }
        return parsed
    }

    /// Answer the approval / question a message sent through Hermes waits on.
    public func answerHistorySend(_ answer: HistorySendAnswer) async throws {
        let reply = try await post(answer.path, answer.body)
        guard reply["ok"] as? Bool == true else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't take that answer.")
        }
    }

    /// New chat: `POST /voice/work` with the first message (its own job, its own session). The
    /// route takes `source: "typed"` only, so a voice message's transcript goes as typed text.
    /// Returns the new task's run id; its session shows up in the history with that id in `run_ids`.
    public func startChat(text: String, clientID: String = UUID().uuidString) async throws -> String? {
        guard let clean = TaskMessageRequest.clean(text) else { throw HTTPError(status: 400, message: "The message is empty.") }
        let reply = try await post("/voice/work", ["text": clean, "source": "typed", "client_id": clientID])
        guard reply["ok"] as? Bool == true else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't start that.")
        }
        return (reply["session_id"] as? String) ?? (reply["run_id"] as? String)
    }
}
