import Foundation

/// One Hermes skill a message can invoke with `/command` (`GET /voice/skills`, plugin 0.3.37+).
public struct SlashSkill: Equatable, Hashable, Sendable, Identifiable {
    /// What goes after the slash ("soup-scaler").
    public var command: String
    public var name: String
    /// One line, at most ~160 characters.
    public var description: String
    /// The skill's folder ("cooking"), or "".
    public var category: String
    public var id: String { command }

    public init(command: String, name: String? = nil, description: String = "", category: String = "") {
        self.command = command; self.name = name ?? command; self.description = description; self.category = category
    }

    public init?(json: [String: Any]) {
        guard let command = json["command"] as? String, !command.isEmpty else { return nil }
        self.init(command: command, name: json["name"] as? String, description: json["description"] as? String ?? "",
                  category: json["category"] as? String ?? "")
    }

    /// "Soup scaler": the command as words, for the list's title line.
    public var title: String {
        let words = command.replacingOccurrences(of: "-", with: " ").replacingOccurrences(of: "_", with: " ")
        return words.prefix(1).uppercased() + words.dropFirst()
    }
}

/// The "/" pop-up over a message box, as state (the iPhone/iPad and Mac composers draw it).
///
///     var slash = SlashMenu(skills: catalog)
///     slash.update(text: "/sou")        // matches: soup-scaler…
///     text = slash.pick(slash.matches[0], in: text)   // "/soup-scaler "
///
/// It opens only while the box's text is a single leading `/word` being typed (no space yet), so a
/// path or a sentence with a slash in it never pops anything up; Escape/dismiss hides it until the
/// command word changes.
public struct SlashMenu: Equatable, Sendable {
    public var skills: [SlashSkill]
    public private(set) var query: String?
    public private(set) var matches: [SlashSkill] = []
    /// The highlighted row (arrow keys on the Mac, the first row's return key everywhere).
    public var highlighted = 0
    private var dismissedQuery: String?
    public static let limit = 8

    public init(skills: [SlashSkill] = []) { self.skills = skills }

    public var isOpen: Bool { query != nil && !matches.isEmpty }

    /// The word being typed after a leading "/" ("" right after the slash), or nil when the text isn't
    /// a lone slash command in progress.
    public static func commandQuery(_ text: String) -> String? {
        guard text.first == "/" else { return nil }
        let word = text.dropFirst()
        guard !word.contains(where: { $0.isWhitespace || $0 == "/" }) else { return nil }
        return String(word)
    }

    public mutating func update(text: String) {
        let q = Self.commandQuery(text)
        if q != dismissedQuery { dismissedQuery = nil }
        query = (q != nil && q == dismissedQuery) ? nil : q
        let old = matches
        matches = query.map { Self.rank(skills, $0) } ?? []
        if matches != old { highlighted = 0 }
    }

    public mutating func dismiss() {
        dismissedQuery = query
        query = nil
        matches = []
    }

    public mutating func move(_ delta: Int) {
        guard !matches.isEmpty else { return }
        highlighted = (highlighted + delta + matches.count) % matches.count
    }

    public var highlightedSkill: SlashSkill? { matches.indices.contains(highlighted) ? matches[highlighted] : nil }

    /// The known skill a message starts with ("/soup-scaler for twelve" → soup-scaler), or nil.
    public func leadingSkill(in text: String) -> SlashSkill? {
        guard text.first == "/" else { return nil }
        let word = text.dropFirst().prefix { !$0.isWhitespace }.lowercased().replacingOccurrences(of: "_", with: "-")
        guard text.dropFirst().count > word.count else { return nil }  // still typing the command
        return skills.first { $0.command.lowercased() == word }
    }

    /// The box's text after picking *skill*: "/command " with the cursor ready for the request.
    public func pick(_ skill: SlashSkill, in text: String) -> String { "/\(skill.command) " }

    /// Prefix matches first, then word-start matches ("scaler" finds soup-scaler), then anywhere in the
    /// command or description; at most `limit`.
    public static func rank(_ skills: [SlashSkill], _ query: String) -> [SlashSkill] {
        let q = query.lowercased().replacingOccurrences(of: "_", with: "-")
        guard !q.isEmpty else { return Array(skills.prefix(limit)) }
        var scored: [(Int, SlashSkill)] = []
        for skill in skills {
            let c = skill.command.lowercased()
            let score: Int
            if c.hasPrefix(q) { score = 0 }
            else if c.split(whereSeparator: { $0 == "-" || $0 == "_" || $0 == "." }).contains(where: { $0.hasPrefix(q) }) { score = 1 }
            else if c.contains(q) { score = 2 }
            else if skill.description.lowercased().contains(query.lowercased()) { score = 3 }
            else { continue }
            scored.append((score, skill))
        }
        return scored.enumerated().sorted { a, b in
            a.element.0 != b.element.0 ? a.element.0 < b.element.0 : a.offset < b.offset
        }.prefix(limit).map(\.element.1)
    }
}

/// Invented skills for demo screenshots and previews.
public enum DemoSlashSkills {
    public static let all: [SlashSkill] = [
        SlashSkill(command: "bike-fit", description: "Size a bike frame from your height and inseam.", category: "outdoors"),
        SlashSkill(command: "garden-plan", description: "Plan raised beds, spacing and a planting calendar.", category: "outdoors"),
        SlashSkill(command: "meal-prep", description: "Turn a week of meals into one grocery list and a prep order.", category: "cooking"),
        SlashSkill(command: "pdf-summary", description: "Summarize a long PDF into the parts that need you.", category: "documents"),
        SlashSkill(command: "soup-scaler", description: "Scale a soup recipe for more people, in cups and ounces.", category: "cooking"),
        SlashSkill(command: "trip-packing", description: "A packing list for the weather where you're going.", category: "travel"),
    ]
}
