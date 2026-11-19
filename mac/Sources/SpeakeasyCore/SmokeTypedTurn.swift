import Foundation

/// How `--live-call-smoke` gets its one typed line into a call, chosen by the admitted voice provider.
///
/// A Codex (ChatGPT sign-in) voice session has no Responses delegation, so the realtime side refuses a
/// `response.item.create` sent down the data channel ("requires a session with Responses delegation")
/// and the smoke test never hears a reply. On Codex the line goes through the server's typed-turn
/// route instead (`POST /voice/interactions/{id}/input-text`, `session.input_text`), the same path the
/// call screen's composer uses, which hands it to the voice as the user's turn. Other providers keep
/// the data-channel event the smoke test has always sent. `SPEAKEASY_SMOKE_EVENTS` still overrides
/// both.
public enum SmokeTypedTurn: Equatable {
    /// Send through the server's typed-turn route; `text` is already `TypedText`-normalized.
    case serverRoute(text: String)
    /// Send `itemCreate(prompt)` down the call's data channel.
    case dataChannel(prompt: String)
    /// The line can't be sent (empty or over the server's limit once normalized).
    case invalid(String)

    /// The event the smoke test sends on providers whose data channel takes a typed item.
    public static func itemCreate(_ prompt: String) -> [String: Any] {
        ["type": "response.item.create",
         "item": ["type": "message", "role": "user",
                  "content": [["type": "input_text", "text": prompt]]] as [String: Any]]
    }

    /// Providers whose data channel refuses a typed item, so the line must go through the server.
    public static func needsServerRoute(voiceProvider: String) -> Bool {
        voiceProvider.lowercased() == "codex"
    }

    public static func plan(voiceProvider: String, prompt: String) -> SmokeTypedTurn {
        guard needsServerRoute(voiceProvider: voiceProvider) else {
            return .dataChannel(prompt: prompt)
        }
        switch TypedText.check(prompt) {
        case .ok(let text): return .serverRoute(text: text)
        case .empty: return .invalid("the smoke prompt is empty")
        case .tooLong(let scalars): return .invalid("the smoke prompt is \(scalars) characters; the limit is \(TypedText.maxScalars)")
        }
    }

    /// How long streamed caption text must stay unchanged before the smoke test counts it as the reply.
    public static let replyQuiet: TimeInterval = 2

    /// A reply streamed as caption deltas with no closing event: some text, and nothing new for
    /// `replyQuiet` seconds.
    public static func replySettled(heard: String, lastDelta: Date?, now: Date) -> Bool {
        guard let lastDelta, !heard.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return false }
        return now.timeIntervalSince(lastDelta) >= replyQuiet
    }
}
