import Foundation

/// A call is not a Hermes session. Its saved conversation is fetched by interaction ID.
public struct SavedCall: Equatable, Sendable {
    public let interactionID: String
    public let turns: [TranscriptTurn]

    public init(json: [String: Any]) {
        interactionID = json["interaction_id"] as? String ?? ""
        turns = (json["turns"] as? [[String: Any]] ?? []).enumerated().compactMap { i, row in
            guard let role = row["role"] as? String, ["user", "assistant"].contains(role),
                  let text = row["text"] as? String, !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
            if role == "user", row["kind"] as? String == "typed" {
                return TranscriptTurn(typed: text, id: i)
            }
            if role == "assistant", row["kind"] as? String == "text" {
                return TranscriptTurn(textReply: text, id: i, taskID: row["task_id"] as? String ?? "",
                                      taskName: row["task_name"] as? String)
            }
            return TranscriptTurn(id: i, speaker: role == "user" ? .you : .assistant, text: text)
        }
    }

    public static func interactionID(summaryID: String) -> String? {
        guard summaryID.hasPrefix("call:") else { return nil }
        let id = String(summaryID.dropFirst(5))
        return id.isEmpty ? nil : id
    }
}
