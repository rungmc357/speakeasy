import Foundation

// Which Hermes profile ("agent") a chat talks to (servers with `capabilities.agents`; docs/API.md
// "Agents"). The Mac sets which profiles may be used (settings `agents.allowed`); New chat then shows
// an agent chip, and a chat stays with its agent. Nothing allowed = no chip, everything as before.

/// One agent: a Hermes profile on the user's install.
public struct AgentInfo: Equatable, Sendable, Identifiable, Hashable {
    /// The profile id (`default`, `research-desk`): what a request names.
    public var id: String
    /// Ready to show: the profile's display name, or its id in words ("Research desk"); "Hermes" for the main one.
    public var name: String
    public var description: String?
    /// The agent Speakeasy itself lives in (what a chat with no pick runs on).
    public var isDefault: Bool

    public init(id: String, name: String, description: String? = nil, isDefault: Bool = false) {
        self.id = id; self.name = name; self.description = description; self.isDefault = isDefault
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) else { return nil }
        self.init(id: id, name: String((nonEmpty(o["name"]) ?? id).prefix(40)),
                  description: nonEmpty(o["description"]).map { String($0.prefix(200)) },
                  isDefault: o["default"] as? Bool ?? false)
    }
}

/// `GET /voice/agents`.
public struct AgentRoster: Equatable, Sendable {
    public static let path = "/voice/agents"
    /// What the picker offers: Speakeasy's own agent first, then the allowed ones, in the user's order.
    public var agents: [AgentInfo]
    /// The own agent's id.
    public var defaultID: String
    /// The saved allowlist (settings `agents.allowed`).
    public var allowed: [String]
    /// Every agent that could be allowed (the Mac's allowlist editor), own first.
    public var available: [AgentInfo]

    public init(agents: [AgentInfo], defaultID: String = "default", allowed: [String] = [], available: [AgentInfo] = []) {
        self.agents = agents; self.defaultID = defaultID; self.allowed = allowed; self.available = available
    }

    public init(json: Any?) {
        let o = json as? [String: Any] ?? [:]
        let agents = (o["agents"] as? [Any] ?? []).compactMap(AgentInfo.init(json:))
        self.init(agents: agents, defaultID: nonEmpty(o["default"]) ?? agents.first?.id ?? "default",
                  allowed: (o["allowed"] as? [Any] ?? []).compactMap { nonEmpty($0) },
                  available: (o["available"] as? [Any] ?? []).compactMap(AgentInfo.init(json:)))
    }

    /// The chip shows only when there's a choice to make.
    public var showsPicker: Bool { agents.count > 1 }
    public var ownAgent: AgentInfo? { agents.first { $0.id == defaultID } ?? agents.first }
    public func agent(_ id: String?) -> AgentInfo? { agents.first { $0.id == (id ?? defaultID) } }
    /// Agents besides the own one that could be allowed (the Mac's settings list).
    public var others: [AgentInfo] { available.filter { $0.id != defaultID } }

    /// `PATCH /voice/settings` body for a new allowlist.
    public static func allowedPatch(_ ids: [String]) -> [String: Any] { ["agents": ["allowed": ids]] }

    /// Invented profiles for previews and screenshots.
    public static let sample = AgentRoster(
        agents: [AgentInfo(id: "default", name: "Hermes", isDefault: true),
                 AgentInfo(id: "research-desk", name: "Research desk", description: "Reads papers and writes short briefs."),
                 AgentInfo(id: "garden", name: "Garden helper", description: "Plans the vegetable beds.")],
        allowed: ["research-desk", "garden"],
        available: [AgentInfo(id: "default", name: "Hermes", isDefault: true),
                    AgentInfo(id: "research-desk", name: "Research desk", description: "Reads papers and writes short briefs."),
                    AgentInfo(id: "garden", name: "Garden helper", description: "Plans the vegetable beds."),
                    AgentInfo(id: "bookkeeping", name: "Bookkeeping")])
}
