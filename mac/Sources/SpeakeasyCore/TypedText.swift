import Foundation

/// A turn typed on the call screen instead of spoken (`session.input_text`), for plugins whose
/// `GET /voice/status` says `input_text: true`. Sent as `POST /voice/interactions/{id}/input-text`.
///
/// The text rule is the plugin's own (`calls.typed_text` plus the 1-1000 check in
/// `VoiceService.input_text`), so the app never sends a draft the server would refuse and never
/// shows a turn the server would have changed:
/// - U+0000-U+001F and U+007F become a space; then every run of whitespace (Python `str.split()`:
///   the Unicode White_Space set) becomes one space, and the ends are trimmed.
/// - Nothing else changes: zero-width joiners and non-joiners, bidi marks, emoji modifiers and
///   other format characters stay exactly as typed.
/// - The limit counts Unicode code points (Python `len`), not Characters: one flag is 2, a family
///   emoji 5.
/// Both sides run the same table, `mac/Tests/SpeakeasyCoreTests/Contract/typed_text.json`
/// (`TypedTextTests` here, `plugin/tests/test_input_text.py` on the server).
public enum TypedText {
    public static let messageType = "session.input_text"
    /// The plugin's `MAX_INPUT_TEXT`, in code points after normalizing.
    public static let maxScalars = 1000

    public enum Check: Equatable, Sendable {
        /// Ready to send, normalized.
        case ok(String)
        /// Nothing left after normalizing (the server's `empty_text`).
        case empty
        /// Over the limit (the server's `text_too_long`); `scalars` is the normalized length.
        case tooLong(scalars: Int)
    }

    /// The server's separators: what it turns into (one) space.
    public static func isSeparator(_ scalar: Unicode.Scalar) -> Bool {
        switch scalar.value {
        case 0x00...0x20, 0x7F, 0x85, 0xA0, 0x1680, 0x2000...0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000:
            return true
        default:
            return false
        }
    }

    /// The draft as the server will take it: one line, single spaces, trimmed. Works on code points
    /// (not Characters), as the server does, so a space followed by a combining mark is still a space.
    public static func normalized(_ text: String) -> String {
        var out = String.UnicodeScalarView()
        var pendingSpace = false
        for scalar in text.unicodeScalars {
            if isSeparator(scalar) {
                pendingSpace = !out.isEmpty
                continue
            }
            if pendingSpace { out.append(" "); pendingSpace = false }
            out.append(scalar)
        }
        return String(out)
    }

    /// Code points, as the server counts them.
    public static func scalarCount(_ text: String) -> Int { text.unicodeScalars.count }

    public static func check(_ draft: String) -> Check {
        let text = normalized(draft)
        if text.isEmpty { return .empty }
        let count = scalarCount(text)
        return count > maxScalars ? .tooLong(scalars: count) : .ok(text)
    }

    public static func path(interactionID: String) -> String {
        "/voice/interactions/\(SharingRoute.pathID(interactionID))/input-text"
    }

    /// Exactly `{"type", "text"}`: the server refuses any other key.
    public static func body(_ text: String) -> [String: Any] { ["type": messageType, "text": text] }

    /// The server took the turn: `200 {"interaction_id", "type", "sent": true}`. Anything else (no
    /// `sent`, `sent: false`, another type) is not a delivery.
    public static func taken(_ reply: [String: Any]) -> Bool {
        reply["sent"] as? Bool == true && (reply["type"] as? String ?? messageType) == messageType
    }
}

/// Where a typed turn shown in the transcript came from: this device's own send (`local`, after the
/// server's 200) or the call's event stream (`stream`, SSE `transcript`, which every client of the
/// call gets, the sender included). The reducer shows each turn once.
public enum TypedTurnOrigin: Equatable, Sendable {
    case local
    /// `id` is the SSE event id when the server sent one (a replayed event is dropped).
    case stream(id: String?)

    var isLocal: Bool { self == .local }
}

/// A typed turn already on screen whose other copy (local echo or stream event) may still come.
public struct TypedTurnEcho: Equatable, Sendable {
    public var text: String
    public var local: Bool
    public var at: Date
}
