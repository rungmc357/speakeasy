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

// MARK: Profile switching (build 75; servers with `capabilities.profiles`, docs/API.md "Profiles")
//
// The phone can switch which Hermes profile it shows. It only ever sends the profile's NAME in
// `X-Speakeasy-Profile`; the plugin checks the name against the profiles its gateway serves and
// reaches that profile with the profile's own key, on the server. No key comes to the phone.

/// One profile the app can switch to (`GET /voice/profiles`).
public struct ProfileEntry: Equatable, Sendable, Identifiable, Hashable {
    public var id: String
    public var name: String
    public var description: String?
    /// The profile's own default model, read only (nil when its config names none).
    public var model: String?
    /// Speakeasy's own profile.
    public var isOwn: Bool

    public init(id: String, name: String, description: String? = nil, model: String? = nil, isOwn: Bool = false) {
        self.id = id; self.name = name; self.description = description; self.model = model; self.isOwn = isOwn
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) else { return nil }
        self.init(id: id, name: String((nonEmpty(o["name"]) ?? id).prefix(40)),
                  description: nonEmpty(o["description"]).map { String($0.prefix(200)) },
                  model: nonEmpty(o["model"]).map { String($0.prefix(80)) }, isOwn: o["own"] as? Bool ?? false)
    }

    /// The model as a row shows it: the part after the provider ("invented/garden-model-1" → "garden-model-1").
    public var modelLabel: String? { model.map { $0.split(separator: "/").last.map(String.init) ?? $0 } }
}

/// `GET /voice/profiles`: the profiles this Hermes serves, Speakeasy's own first.
public struct ProfileRoster: Equatable, Sendable {
    public static let path = "/voice/profiles"
    /// The header a switching app sends on every request (a profile name, never a key).
    public static let headerName = "X-Speakeasy-Profile"
    /// Where the app keeps the profile it shows (survives launches).
    public static let currentKey = "SpeakeasyCurrentProfile"

    public var profiles: [ProfileEntry]
    public var ownID: String

    public init(profiles: [ProfileEntry], ownID: String = "default") {
        self.profiles = profiles; self.ownID = ownID
    }

    public init(json: Any?) {
        let o = json as? [String: Any] ?? [:]
        let rows = (o["profiles"] as? [Any] ?? []).compactMap(ProfileEntry.init(json:))
        self.init(profiles: rows, ownID: nonEmpty(o["own"]) ?? rows.first(where: \.isOwn)?.id ?? rows.first?.id ?? "default")
    }

    public var own: ProfileEntry? { profiles.first { $0.id == ownID } ?? profiles.first }
    public func profile(_ id: String?) -> ProfileEntry? { profiles.first { $0.id == (id ?? ownID) } }
    /// The profile shown: the saved one while the server still serves it, else the own.
    public func current(saved: String?) -> ProfileEntry? { profile(saved) ?? own }
    public func others(than id: String?) -> [ProfileEntry] {
        let shown = current(saved: id)?.id
        return profiles.filter { $0.id != shown }
    }

    /// Invented profiles for previews, the demo and UI tests.
    public static let sample = ProfileRoster(profiles: [
        ProfileEntry(id: "default", name: "Hermes", model: "anthropic/claude-sonnet-5-5", isOwn: true),
        ProfileEntry(id: "research-desk", name: "Research desk", description: "Reads papers and writes short briefs.",
                     model: "invented/reader-2"),
    ])
}
