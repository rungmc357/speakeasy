import Foundation
import SpeakeasyCore

// Home (SPK-124): the calls behind the Now screen. Plugins with capability `now`.

extension ServerClient {
    private static func esc(_ s: String) -> String {
        s.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-_.~"))) ?? s
    }

    /// `GET /voice/now?tz=`: the list, in the plugin's order, for the phone's zone.
    public func now(timeZone: TimeZone = .current) async throws -> NowHome {
        NowHome(json: try await json("/voice/now?tz=" + Self.esc(timeZone.identifier)))
    }

    /// "Later": off Home until *until*, or until it changes.
    public func nowLater(_ itemID: String, until: Date?, onChange: Bool) async throws {
        var body: [String: Any] = [:]
        if let until { body["until"] = until.timeIntervalSince1970 }
        if onChange { body["on_change"] = true }
        _ = try await post("/voice/now/\(Self.esc(itemID))/later", body)
    }

    /// Undo a Later.
    public func nowLaterUndo(_ itemID: String) async throws {
        _ = try await json("/voice/now/\(Self.esc(itemID))/later", method: "DELETE")
    }

    /// One way to answer a question card, on a call or not.
    public func nowAnswer(_ itemID: String, optionID: String? = nil, text: String? = nil) async throws {
        let body: [String: Any] = optionID.map { ["option_id": $0] } ?? ["text": text ?? ""]
        _ = try await post("/voice/now/\(Self.esc(itemID))/answer", body)
    }

    public func nowApproval(_ itemID: String, choice: String) async throws {
        _ = try await post("/voice/now/\(Self.esc(itemID))/approval", ["choice": choice])
    }

    @discardableResult
    public func nowClearSeen() async throws -> Int {
        (try await post("/voice/now/clear-seen", [:])["moved"] as? NSNumber)?.intValue ?? 0
    }

    /// Undo the last Clear (plugin 0.3.43+, capability `now_dismiss`).
    public func nowClearSeenUndo() async throws {
        _ = try await json("/voice/now/clear-seen", method: "DELETE")
    }

    /// Swipe-dismiss: the card moves to Earlier on every device (plugin 0.3.43+, `now_dismiss`).
    public func nowDismiss(_ itemID: String) async throws {
        _ = try await post("/voice/now/\(Self.esc(itemID))/dismiss", [:])
    }

    public func nowDismissUndo(_ itemID: String) async throws {
        _ = try await json("/voice/now/\(Self.esc(itemID))/dismiss", method: "DELETE")
    }

    /// Earlier: one page of conversations for the checked kinds, never repeating Home, with the menu.
    public func earlier(kinds: Set<String>, query: String = "", cursor: String? = nil) async throws -> (HistoryPage, [EarlierKind]) {
        var path = "/voice/history?exclude_now=1&kinds=" + EarlierKind.query(kinds)
        if !query.isEmpty { path += "&q=" + Self.esc(query) }
        if let cursor { path += "&cursor=" + Self.esc(cursor) }
        let reply = try await json(path)
        return (HistoryPage(json: reply), EarlierKind.parse(reply["kinds"]))
    }
}
