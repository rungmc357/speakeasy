import Foundation

// Secure input (servers with `capabilities.secret_input`): a key a skill needs, or the Mac password
// for one sudo command. The plugin lists open requests at GET /voice/secrets (and as `secure_inputs`
// on a call's event feed); the app answers one with POST /voice/secrets/{id}. A request never
// carries a value, and nothing the app keeps does either.

public struct SecureInputRequest: Equatable, Sendable, Identifiable {
    public enum Kind: String, Equatable, Sendable { case secret, sudo, login, code }

    public var id: String
    public var kind: Kind
    /// The job (task title) that asks.
    public var job: String
    public var runID: String?
    public var sessionID: String?
    /// secret: the env var Hermes saves it as (`OPENWEATHER_API_KEY`).
    public var envVar: String
    /// secret: "OpenWeather API key"; sudo: "Your Mac's password".
    public var label: String
    /// secret: the skill's own prompt and help line.
    public var prompt: String
    public var help: String
    public var skill: String
    /// sudo: the exact command, shown in full.
    public var command: String
    /// login: the page origin the login is saved for; login / code: the site's host.
    public var origin: String
    public var site: String
    /// Where it goes, in the plugin's words.
    public var storedWhere: String
    public var expiresAt: Date
    /// Opened by the user from the app (Settings › Voice › Add key), not asked for by a task: no
    /// countdown, no "needs you", and the title says what it adds.
    public var userStarted: Bool

    public init(id: String, kind: Kind, job: String, runID: String? = nil, sessionID: String? = nil, envVar: String = "",
                label: String = "", prompt: String = "", help: String = "", skill: String = "", command: String = "",
                origin: String = "", site: String = "", storedWhere: String = "", expiresAt: Date,
                userStarted: Bool = false) {
        self.id = id; self.kind = kind; self.job = job; self.runID = runID; self.sessionID = sessionID
        self.envVar = envVar; self.label = label; self.prompt = prompt; self.help = help; self.skill = skill
        self.command = command; self.origin = origin; self.site = site
        self.storedWhere = storedWhere; self.expiresAt = expiresAt; self.userStarted = userStarted
    }

    /// One entry of `requests` / `secure_inputs`; nil when it isn't a valid request.
    public init?(_ object: [String: Any]) {
        guard let id = object["id"] as? String, id.hasPrefix("sec_"),
              let kind = Kind(rawValue: object["kind"] as? String ?? "") else { return nil }
        func s(_ key: String) -> String { (object[key] as? String) ?? "" }
        let expires = (object["expires_at"] as? Double).map { Date(timeIntervalSince1970: $0) }
            ?? Date().addingTimeInterval(Double((object["seconds_left"] as? Int) ?? 120))
        self.init(id: id, kind: kind, job: s("job"), runID: object["run_id"] as? String,
                  sessionID: object["session_id"] as? String, envVar: s("env_var"), label: s("label"),
                  prompt: s("prompt"), help: s("help"), skill: s("skill"), command: s("command"),
                  origin: s("origin"), site: s("site"), storedWhere: s("stored_where"), expiresAt: expires)
        if kind == .secret && envVar.isEmpty { return nil }
        if kind == .login && site.isEmpty { return nil }
    }

    /// GET /voice/secrets → its requests, oldest first.
    public static func list(_ data: Data) -> [SecureInputRequest] {
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let items = object["requests"] as? [[String: Any]] else { return [] }
        return items.compactMap(SecureInputRequest.init)
    }

    /// A `secure_inputs` event payload (an array of requests).
    public static func list(event payload: Any?) -> [SecureInputRequest] {
        ((payload as? [[String: Any]]) ?? []).compactMap(SecureInputRequest.init)
    }

    public func secondsLeft(now: Date = Date()) -> Int { max(0, Int(expiresAt.timeIntervalSince(now).rounded(.down))) }

    // MARK: copy

    /// The card's headline.
    public var title: String {
        switch kind {
        case .secret: return userStarted ? "Add your \(label.isEmpty ? "key" : label)"
                                         : "Your agent needs your \(label.isEmpty ? "key" : label)"
        case .sudo: return "Your agent needs your Mac's password to run:"
        case .login: return "Add a login for \(site)"
        case .code: return "Enter the code \(site) sent you"
        }
    }

    /// Who asks: "Weather skill · Weather for the trip".
    public var askedBy: String {
        let skillName = skill.isEmpty ? "" : "\(skill.prefix(1).uppercased() + skill.dropFirst()) skill"
        return [skillName, job].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// Where it goes, always said plainly.
    public var whereItGoes: String {
        if !storedWhere.isEmpty { return storedWhere }
        switch kind {
        case .secret: return "Saved to your Hermes's private settings as \(envVar). The AI never sees it."
        case .sudo: return "Used once for this command, then forgotten. Speakeasy never stores it."
        case .login: return "Saved in your Hermes's encrypted vault for \(site). The AI can fill it in on that site but never sees the password."
        case .code: return "Typed into the page once. Never saved."
        }
    }

    public var primaryButton: String {
        switch kind { case .secret, .login: return "Save securely"; case .sudo: return "Allow once"; case .code: return "Send code" }
    }
    public var skipButton: String { userStarted ? "Cancel" : kind == .sudo ? "Don't allow" : "Skip" }
    public var fieldPlaceholder: String {
        switch kind {
        case .secret: return label.isEmpty ? "Key" : label
        case .sudo: return "Mac password"
        case .login: return "Password"
        case .code: return "Code"
        }
    }

    /// The notification line (never a value): "Needs you: Your agent needs your OpenWeather API key".
    public var notificationLine: String {
        switch kind {
        case .secret: return "Needs you: \(title)"
        case .sudo: return "Needs you: Your agent needs your Mac's password to run a command"
        case .login: return "Needs you: Your agent needs a login for \(site)"
        case .code: return "Needs you: Your agent needs the code \(site) sent you"
        }
    }

    /// Body bytes for POST /voice/secrets/{id}. The caller zeroes them after the request is sent.
    public static func answerBody(_ value: String) -> [UInt8] {
        Array("{\"value\":".utf8) + quoted(value) + Array("}".utf8)
    }

    /// A login: `{"identifier":…,"password":…,"code":…?}`.
    public static func loginBody(identifier: String, password: String, code: String) -> [UInt8] {
        var out = Array("{\"identifier\":".utf8) + quoted(identifier) + Array(",\"password\":".utf8) + quoted(password)
        let trimmed = code.trimmingCharacters(in: .whitespaces)
        if !trimmed.isEmpty { out += Array(",\"code\":".utf8) + quoted(trimmed) }
        return out + Array("}".utf8)
    }

    static func quoted(_ value: String) -> [UInt8] {
        var out = Array("\"".utf8)
        for scalar in value.unicodeScalars {
            switch scalar {
            case "\"": out += Array("\\\"".utf8)
            case "\\": out += Array("\\\\".utf8)
            case _ where scalar.value < 0x20: out += Array(String(format: "\\u%04x", scalar.value).utf8)
            default: out += Array(String(scalar).utf8)
            }
        }
        out += Array("\"".utf8)
        return out
    }

    public static let skipBody: [UInt8] = Array("{\"action\":\"skip\"}".utf8)
}
