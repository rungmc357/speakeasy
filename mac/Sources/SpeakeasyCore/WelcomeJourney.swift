import Foundation

/// First-launch navigation, independent of connection and permission setup.
public struct WelcomeJourney: Equatable, Sendable {
    public enum Route: String, Sendable { case intro, newcomer, setup }
    public static let seenKey = "speakeasy.macWelcomeSeen"
    public static let installCommand = "curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash"
    public static let headlines = ["Talk to your agent.", "It works while you talk.", "It's yours.", "Connect your agent."]
    public static let lines = ["Call it and say what you need.", "Each thing you ask becomes a task you can watch.",
                               "Hermes is an agent that runs on your own computer.", "Pair the one you have, or set one up on a Mac."]
    public var page: Int
    public var route: Route
    public init(seen: Bool = false, paired: Bool = false) {
        page = seen ? 3 : 0
        route = paired ? .setup : .intro
    }
    public mutating func forward() {
        guard route == .intro else { return }
        if page < 3 { page += 1 } else { route = .setup }
    }
    public mutating func back() {
        if route != .intro { route = .intro; page = 3 }
        else { page = max(0, page - 1) }
    }
    public mutating func skip() { page = 3; route = .intro }
}
