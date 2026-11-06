import Foundation

/// The one way agent-written text is drawn, on every surface (chat bubbles, answers, job pages,
/// task details, cards) and in every state (finished or still streaming in).
///
/// Agents write Markdown. Before this, each view did its own thing: some parsed inline Markdown,
/// some showed the raw string (the user saw `**Done:**` with the asterisks), some stripped the
/// marks (no asterisks, but no bold either), and streaming text was always raw until it finished.
/// `attributed` renders it; `plain` gives clean text for places that can't style (previews,
/// notifications, Live Activities, copy-as-text).
///
/// What it handles beyond Apple's inline parser:
/// - `# Heading` lines → a bold line (inline-only parsing would show the `#`s).
/// - `- item` / `* item` / `+ item` → `• item`; `> quote` → the quote's text; `---` rules dropped.
/// - Bold the parser refuses (CommonMark flanking rules): `** spaced **`, `**Label:**word`.
/// - Bare `https://` URLs become links; only http(s) links survive (no `file:`/custom schemes).
/// - Streaming: an unclosed `**`, `__` or backtick at the end is hidden until its close arrives,
///   so text never flashes raw marks and then jumps.
/// - Any `**` still left over is removed outside code, so asterisks never reach the screen.
/// Fenced code blocks are not handled here: split them out first with `MessageBlock.split`.
public enum AgentMarkdown {
    public static func attributed(_ text: String, streaming: Bool = false) -> AttributedString {
        var source = prepare(text)
        if streaming { source = hideUnclosed(source) }
        source = linkBareURLs(source)
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace,
                                                              failurePolicy: .returnPartiallyParsedIfPossible)
        guard var out = try? AttributedString(markdown: source, options: options) else {
            return AttributedString(plainFallback(source))
        }
        for run in out.runs where run.link != nil {
            if let url = run.link, let scheme = url.scheme?.lowercased(), scheme == "https" || scheme == "http" { continue }
            out[run.range].link = nil
        }
        return removeLeftoverMarks(out)
    }

    /// The same text with every Markdown mark removed (bullets kept as `•`).
    public static func plain(_ text: String) -> String {
        String(attributed(text).characters)
    }

    // MARK: Lines

    static func prepare(_ text: String) -> String {
        var lines: [String] = []
        var inFence = false
        for raw in text.components(separatedBy: "\n") {
            let trimmed = raw.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") { inFence.toggle(); lines.append(raw); continue }
            if inFence { lines.append(raw); continue }
            lines.append(line(raw))
        }
        return fixBold(lines.joined(separator: "\n"))
    }

    private static func line(_ raw: String) -> String {
        let indent = String(raw.prefix { $0 == " " || $0 == "\t" })
        let body = String(raw.dropFirst(indent.count))
        if let m = groups(#"^#{1,6}\s+(.+?)\s*#*\s*$"#, body) {
            let title = m[0].replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "__", with: "")
            return title.isEmpty ? "" : "**\(title)**"
        }
        if groups(#"^(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})$"#, body) != nil { return "" }
        if let m = groups(#"^[-*+]\s+(.*)$"#, body) { return indent + "• " + m[0] }
        if let m = groups(#"^>\s?(.*)$"#, body) { return indent + m[0] }
        return raw
    }

    /// Bold the CommonMark parser leaves raw: `** spaced **` and `**Label:**word`.
    static func fixBold(_ s: String) -> String {
        var out = replace(#"(?m)(?:^|(?<=[\s(]))\*\*[ \t]+([^*\n]+?)[ \t]+\*\*(?=[\s.,;:!?)]|$)"#, s, "**$1**")
        out = replace(#"\*\*([^*\n]+?[:.,;!?)\]"'”’])\*\*(?=[\p{L}\p{N}])"#, out, "**$1** ")
        return out
    }

    /// The capture groups of the first match (empty strings for groups that didn't take part).
    static func groups(_ pattern: String, _ s: String) -> [String]? {
        guard let re = try? NSRegularExpression(pattern: pattern),
              let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) else { return nil }
        return (1..<max(m.numberOfRanges, 1)).map { i in Range(m.range(at: i), in: s).map { String(s[$0]) } ?? "" }
    }

    static func replace(_ pattern: String, _ s: String, _ template: String) -> String {
        guard let re = try? NSRegularExpression(pattern: pattern) else { return s }
        return re.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: template)
    }

    // MARK: Streaming

    /// Hide an opening mark whose close hasn't streamed in yet (the last one of an odd count).
    static func hideUnclosed(_ s: String) -> String {
        var out = s
        let ticks = out.filter { $0 == "`" }.count
        if ticks % 2 == 1, let i = out.lastIndex(of: "`") { out.remove(at: i) }
        for mark in ["**", "__"] {
            let count = out.components(separatedBy: mark).count - 1
            if count % 2 == 1, let r = out.range(of: mark, options: .backwards) { out.removeSubrange(r) }
        }
        // A link still arriving: "[text](https://exa" shows as its text.
        out = replace(#"\[([^\]\n]*)\]\([^)\n]*$"#, out, "$1")
        out = replace(#"\[([^\]\n]*)$"#, out, "$1")
        return out
    }

    // MARK: Links

    static func linkBareURLs(_ s: String) -> String {
        guard s.contains("https://") || s.contains("http://") else { return s }
        guard let regex = try? NSRegularExpression(pattern: #"(?<![\[(<`/])\bhttps?://[^\s<>()\[\]`]+"#) else { return s }
        var source = s
        let ns = s as NSString
        for match in regex.matches(in: s, range: NSRange(location: 0, length: ns.length)).reversed() {
            var raw = ns.substring(with: match.range)
            var tail = ""
            while let last = raw.last, ".,;:!?*_".contains(last) { tail = String(last) + tail; raw.removeLast() }
            source = (source as NSString).replacingCharacters(in: match.range, with: "[\(raw)](\(raw))" + tail)
        }
        return source
    }

    // MARK: Leftovers

    private static func removeLeftoverMarks(_ input: AttributedString) -> AttributedString {
        var out = input
        while true {
            var found: Range<AttributedString.Index>?
            for run in out.runs where !(run.inlinePresentationIntent?.contains(.code) ?? false) {
                if let r = out[run.range].range(of: "**") { found = r; break }
            }
            guard let r = found else { return out }
            out.removeSubrange(r)
        }
    }

    private static func plainFallback(_ s: String) -> String {
        s.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "__", with: "")
    }
}
