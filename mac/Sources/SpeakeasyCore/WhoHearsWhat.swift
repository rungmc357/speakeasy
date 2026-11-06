import Foundation

/// "Who hears what": the plain privacy screen shown in onboarding and from Settings (Mac and
/// iPhone/iPad share this copy). It changes with the voice setting, and a few short lines show
/// only when they apply (Cloudflare connection, notifications, listening mode).
///
/// What is true today (keep the copy matching the code):
/// - The realtime voice provider hears the call audio both ways, including the short result
///   summaries the voice reads out, plus the call's opening instructions: a short note about the
///   user (name, answer style, what the agent can do) and the titles of running tasks.
/// - Sessions, files, tools, attachments and shared screens go only to the user's Hermes.
/// - Typed and recorded messages outside a call go straight to Hermes (transcribed there).
public struct WhoHearsWhat: Equatable, Sendable {
    /// Who runs the call voice.
    public enum Voice: Equatable, Sendable {
        /// The ChatGPT voice (ChatGPT sign-in or an OpenAI API key).
        case openai
        /// Grok voice (beta).
        case xai
        /// A private voice that runs on the user's own computer (not shipped yet).
        case local

        public init(provider: VoiceProvider?) {
            switch provider ?? .codex {
            case .codex, .openai: self = .openai
            case .grok: self = .xai
            }
        }
    }

    public var voice: Voice
    /// The app reaches Hermes through a Cloudflare tunnel.
    public var viaCloudflare: Bool
    /// Notifications are on (they go through the push relay).
    public var notifications: Bool
    /// Listening mode (beta) is switched on.
    public var listening: Bool
    /// "Mac", "iPhone", "iPad" — for the listening line.
    public var device: String

    public init(voice: Voice, viaCloudflare: Bool = false, notifications: Bool = false,
                listening: Bool = false, device: String = "Mac") {
        self.voice = voice; self.viaCloudflare = viaCloudflare; self.notifications = notifications
        self.listening = listening; self.device = device
    }

    public static let title = "Who hears what"

    public static let intro = "Speakeasy runs on your own computer. Your agent, sessions, files and tools stay there."

    /// The bold lead of the call paragraph.
    public var callLead: String {
        switch voice {
        case .openai: return "Calls use the ChatGPT voice."
        case .xai: return "Calls use the Grok voice."
        case .local: return "Calls stay on your computer too."
        }
    }

    /// The rest of the call paragraph, after the bold lead.
    public var callBody: String {
        switch voice {
        case .openai, .xai:
            let who = voice == .openai ? "OpenAI" : "xAI"
            return "\(who) hears the call itself: what you say, and what the voice says back, including the short summaries it reads to you. So it can keep up, it also gets a short note about you (your name, how you like answers, what your agent can do) and the names of tasks that are running."
        case .local:
            return "The voice runs on your own machine, so nothing you say leaves it."
        }
    }

    /// Who the voice provider is, or nil when the voice runs locally.
    public var provider: String? {
        switch voice {
        case .openai: return "OpenAI"
        case .xai: return "xAI"
        case .local: return nil
        }
    }

    /// The paragraphs after the call paragraph, in order.
    public var details: [String] {
        var out: [String] = []
        if let who = provider {
            out.append("\(who) doesn't get your sessions, files or tools, or anything you attach or share from your screen. Those go only to your agent.")
        }
        out.append("Messages you type or record outside a call go straight to your agent.")
        out.append("Your agent still uses whatever AI model you set it up with. Speakeasy doesn't change that.")
        return out
    }

    /// Short lines that show only when they apply.
    public var extras: [String] {
        var out: [String] = []
        if viaCloudflare {
            out.append("Your app reaches your computer through Cloudflare, so your messages pass through Cloudflare on the way. Tailscale keeps them between your own devices.")
        }
        if notifications {
            out.append("Notifications go through Speakeasy's notification service. It only learns that something changed, never what. Your phone gets the text from your own computer.")
        }
        if listening {
            let nothing = voice == .local ? "" : " When you start a call, the voice gets what it picked up, so it can help."
            out.append("Nothing leaves your \(device) while it listens.\(nothing)")
        }
        return out
    }

    public static let button = "Got it"
    public static let footnote = "You can change the voice any time in Settings."

    /// The Settings row's second line.
    public var settingsSubtitle: String {
        switch voice {
        case .openai: return "OpenAI hears your calls. Your sessions, files and tools stay on your computer."
        case .xai: return "xAI hears your calls. Your sessions, files and tools stay on your computer."
        case .local: return "Calls stay on your computer."
        }
    }

    /// The whole screen as plain text (VoiceOver summary, copy, tests).
    public var plainText: String {
        ([Self.title, Self.intro, "\(callLead) \(callBody)"] + details + extras).joined(separator: "\n\n")
    }

    /// True when the app reaches its Hermes through a Cloudflare tunnel (quick tunnel or the
    /// user's own domain), not Tailscale or this computer.
    public static func isCloudflare(server: URL?, endpoints: ServerEndpoints?) -> Bool {
        guard let server, let host = server.host?.lowercased() else { return false }
        if isQuickTunnelURL(server) { return true }
        if let cf = endpoints?.cloudflare?.host?.lowercased(), cf == host { return true }
        return false
    }
}
