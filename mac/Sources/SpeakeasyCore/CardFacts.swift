import Foundation

/// What a pull request card lists for its checks (SPK-231 D5): every failure by its own name, however
/// many; identical checks still waiting (or running) as ONE line ("6 checks waiting"); then what
/// passed or skipped up to the cap, the rest counted ("10 more skipped"). A row the server already
/// collapsed carries ``count`` and counts that many.
public enum PRChecks {
    static let order = ["failed": 0, "running": 1, "pending": 2, "passed": 3, "skipped": 4]

    /// How many checks one row stands for.
    public static func weight(_ check: JSONValue) -> Int { max(1, Int(check["count"].double ?? 1)) }

    /// *total*: how many the PR has when the server sent only some (``checks_total``); *restState*:
    /// the one state all the unsent ones share.
    public static func rows(_ checks: [JSONValue], cap: Int = 4, total: Int? = nil,
                            restState: String? = nil) -> (shown: [JSONValue], rest: String?) {
        let sorted = checks.enumerated().sorted {
            (order[$0.element["state"].string ?? ""] ?? 5, $0.offset) < (order[$1.element["state"].string ?? ""] ?? 5, $1.offset)
        }.map(\.element)
        func state(_ c: JSONValue) -> String { c["state"].string ?? "" }
        var shown = sorted.filter { state($0) == "failed" }
        for (waiting, word) in [("running", "running"), ("pending", "waiting")] {
            let same = sorted.filter { state($0) == waiting }
            let plain = same.filter { $0["note"].string == nil && $0["count"].double == nil }
            if plain.count >= 2 {
                shown += same.filter { $0["note"].string != nil || $0["count"].double != nil }
                shown.append(.object(["name": .string("\(plain.count) checks \(word)"), "state": .string(waiting),
                                      "count": .number(Double(plain.count))]))
            } else {
                shown += same
            }
        }
        let others = sorted.filter { !["failed", "running", "pending"].contains(state($0)) }
        let sent = sorted.reduce(0) { $0 + weight($1) }
        let all = max(total ?? 0, sent)
        let room = max(0, cap - shown.count)
        if others.count <= room + 1 && all == sent {
            return (shown + others, nil)
        }
        let extra = Array(others.prefix(room))
        shown += extra
        var states = Set(others.dropFirst(room).map(state))
        if all > sent { states.insert(restState ?? "") }
        let counted = shown.reduce(0) { $0 + weight($1) }
        guard all > counted else { return (shown, nil) }
        let word = states.count == 1 && !(states.first ?? "").isEmpty ? " " + (states.first ?? "") : ""
        return (shown, "\(all - counted) more\(word)")
    }
}

/// A document preview (SPK-231 D3): no page thumbnail; the title, its first line, who edited it last
/// and when ("Edited by Dana · 2h ago") and the app it lives in, with that app's SF Symbol.
public enum DocPreview {
    public static let entities: Set<String> = ["document", "page", "file", "contract"]

    public static func isDocument(_ entity: String?) -> Bool { entities.contains((entity ?? "").lowercased()) }

    /// "Edited by Dana · 2h ago", "Edited by Dana", "Edited 2h ago", or nil.
    public static func editedLine(editor: String?, edited: String?) -> String? {
        let who = editor?.trimmingCharacters(in: .whitespaces).nilIfEmpty
        let when = edited?.trimmingCharacters(in: .whitespaces).nilIfEmpty
        switch (who, when) {
        case let (w?, t?): return "Edited by \(w) · \(t)"
        case let (w?, nil): return "Edited by \(w)"
        case let (nil, t?): return "Edited \(t)"
        default: return nil
        }
    }

    /// The first line of a summary: up to the first line break or sentence end.
    public static func firstLine(_ summary: String?) -> String? {
        guard let text = summary?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else { return nil }
        let line = text.split(separator: "\n", maxSplits: 1).first.map(String.init) ?? text
        if let end = line.range(of: #"(?<=[.!?])\s"#, options: .regularExpression) {
            return String(line[..<end.lowerBound])
        }
        return line
    }

    /// The app's SF Symbol, by its name; nil = the entity's own symbol.
    public static func appSymbol(_ app: String?) -> String? {
        let a = (app ?? "").lowercased()
        guard !a.isEmpty else { return nil }
        if a.contains("sheet") || a.contains("excel") || a.contains("numbers") || a.contains("airtable") { return "tablecells" }
        if a.contains("slide") || a.contains("keynote") || a.contains("powerpoint") { return "rectangle.on.rectangle" }
        if a.contains("notion") || a.contains("notes") || a.contains("obsidian") || a.contains("bear") { return "note.text" }
        if a.contains("figma") { return "paintbrush.pointed" }
        if a.contains("pdf") || a.contains("acrobat") || a.contains("docusign") { return "doc.richtext" }
        if a.contains("drive") || a.contains("dropbox") || a.contains("box") || a.contains("finder") { return "folder" }
        if a.contains("github") || a.contains("gitlab") { return "chevron.left.forwardslash.chevron.right" }
        return "doc.text"
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}
