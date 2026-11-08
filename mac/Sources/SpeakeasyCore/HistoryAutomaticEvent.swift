import Foundation

/// An attributed producer event, never a person's message. Transcript remains unmodified.
public struct HistoryAutomaticEvent: Equatable, Sendable {
    public let title: String
    public let label: String
    public let summary: String
    public let url: URL?

    public init?(json: Any?) {
        guard let value = json as? [String: Any], let title = value["title"] as? String,
              !title.isEmpty else { return nil }
        self.title = String(title.prefix(160))
        let state = value["state"] as? String ?? "unknown"
        self.label = ["completed": "Task completed", "failed": "Task failed", "blocked": "Task needs attention",
                      "ready for review": "Ready for review"][state] ?? "Task update"
        self.summary = String((value["summary"] as? String ?? "A recorded update is available.").prefix(640))
        self.url = (value["url"] as? String).flatMap(URL.init(string:)).flatMap { $0.scheme == "https" && $0.host != nil ? $0 : nil }
    }
}
