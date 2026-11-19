import Foundation

/// A long message in Home's thread (SPK-536): shown whole up to `collapseOver` characters; past
/// that, a preview that ends on a whole word with an ellipsis, and a click shows the rest. The
/// words are never cut mid-word and never clipped without a sign that more follows.
public enum LongMessage {
    /// Messages up to this length show in full (about ten lines in Home's column).
    public static let collapseOver = 600
    /// How much of a longer message the collapsed bubble shows.
    public static let previewChars = 420

    /// The collapsed preview, or nil when the message is short enough to show whole.
    public static func preview(_ text: String, collapseOver: Int = collapseOver, previewChars: Int = previewChars) -> String? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count > collapseOver, previewChars > 0 else { return nil }
        let head = trimmed.prefix(previewChars)
        var cut = head.endIndex
        // The next character is a space or line break: the head already ends on a whole word.
        let next = trimmed[head.endIndex...].first
        if let next, !next.isWhitespace,
           let space = head.lastIndex(where: { $0.isWhitespace }), space > head.startIndex {
            cut = space
        }
        var shown = String(head[..<cut])
        // No dangling comma, dash or colon before the ellipsis.
        while let last = shown.last, last.isWhitespace || ",;:-\u{2013}\u{2014}".contains(last) { shown.removeLast() }
        return shown.isEmpty ? String(head) + "\u{2026}" : shown + "\u{2026}"
    }
}
