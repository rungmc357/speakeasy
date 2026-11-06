import Foundation

/// Sign in to ChatGPT from the app: the plugin runs Codex's own device-code login on the Hermes
/// computer (`POST/GET/DELETE /voice/codex/login`, capability `codex_login`). The app shows the
/// one-time code and a link to open; it never sees a token.
///
/// The plugin first looks for a working sign-in in every Codex copy on the machine, so the app
/// offers the button only when the server says `codex_sign_in: none` (or `api_key`, as a switch).
public enum CodexSignIn: String, Codable, Sendable, Equatable {
    case chatgpt, apiKey = "api_key", none
}

public struct CodexLoginState: Equatable, Sendable {
    public enum Phase: String, Equatable, Sendable {
        case idle, installing, pending, signedIn = "signed_in", expired, failed, cancelled
    }
    public var phase: Phase
    public var message: String
    /// Only https on openai.com / chatgpt.com; anything else is dropped here as well as on the server.
    public var verificationURL: URL?
    public var userCode: String?
    public var expiresAt: Date?
    public var signIn: CodexSignIn?

    public init(phase: Phase, message: String = "", verificationURL: URL? = nil, userCode: String? = nil,
                expiresAt: Date? = nil, signIn: CodexSignIn? = nil) {
        self.phase = phase; self.message = message; self.verificationURL = verificationURL
        self.userCode = userCode; self.expiresAt = expiresAt; self.signIn = signIn
    }

    public init?(json: [String: Any]) {
        guard let raw = json["state"] as? String, let phase = Phase(rawValue: raw) else { return nil }
        self.phase = phase
        message = (json["message"] as? String) ?? ""
        signIn = (json["sign_in"] as? String).flatMap(CodexSignIn.init(rawValue:))
        verificationURL = (json["verification_url"] as? String).flatMap(Self.safeURL)
        userCode = (json["user_code"] as? String).flatMap(Self.safeCode)
        expiresAt = (json["expires_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
        if phase == .pending && (verificationURL == nil || userCode == nil) {
            self.phase = .failed
            message = "The Hermes computer sent a sign-in page Speakeasy doesn't recognize, so it isn't shown."
        }
    }

    /// The polling sheet keeps going only while Codex is installing or the code waits.
    public var isWaiting: Bool { phase == .pending || phase == .installing }

    public static func safeURL(_ text: String) -> URL? {
        guard let url = URL(string: text), url.scheme == "https", url.user == nil, url.password == nil,
              url.port == nil || url.port == 443, let host = url.host?.lowercased() else { return nil }
        let ok = ["openai.com", "chatgpt.com"].contains { host == $0 || host.hasSuffix("." + $0) }
        return ok ? url : nil
    }

    public static func safeCode(_ text: String) -> String? {
        let code = text.trimmingCharacters(in: .whitespaces).uppercased()
        let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")
        guard (3...40).contains(code.count), code.unicodeScalars.allSatisfy(allowed.contains) else { return nil }
        return code
    }

    /// "Code expires in 14 min" / "in 40 s" for the sheet's caption.
    public func expiryLine(now: Date = Date()) -> String? {
        guard phase == .pending, let expiresAt else { return nil }
        let left = Int(expiresAt.timeIntervalSince(now))
        if left <= 0 { return "This code has expired." }
        return left >= 90 ? "Code expires in \(Int((Double(left) / 60).rounded())) min" : "Code expires in \(left) s"
    }

    /// The sheet's status line.
    public var statusLine: String {
        switch phase {
        case .idle: return "Getting a code…"
        case .installing: return message.isEmpty ? "Installing Codex on the Hermes computer…" : message
        case .pending: return "Waiting for you to approve the code…"
        case .signedIn: return "Signed in ✓"
        case .expired: return message.isEmpty ? "The code expired. Get a new one." : message
        case .failed: return message.isEmpty ? "Codex couldn't finish signing in. Try again." : message
        case .cancelled: return "Sign-in cancelled."
        }
    }
}

public extension ServerStatus {
    /// The server can sign Codex in from the app (plugin with `capabilities.codex_login`).
    var canSignInCodex: Bool { capabilities?.codexLogin == true }

    /// Offer "Sign in to ChatGPT": Codex voice, the server can do it, and no copy on the machine is
    /// signed in with ChatGPT. Never while a working sign-in exists anywhere on the machine.
    var offersCodexSignIn: Bool {
        guard canSignInCodex, resolvedProvider == .codex else { return false }
        switch codexSignIn {
        case .some(.chatgpt): return false
        case .some(.apiKey): return true
        case .some(.none): return true
        case nil: return codexSignedIn == false
        }
    }

    /// Codex works but is signed in with an OpenAI API key: the button offers a switch.
    var codexOnAPIKey: Bool { codexSignIn == .apiKey }
}

public extension CodexLoginState {
    /// A call or status error that means Codex on the Hermes computer isn't signed in to ChatGPT
    /// (the plugin's "Codex is not signed in… Run `codex login`" and Codex's own 401 wording).
    static func errorMeansNotSignedIn(_ text: String) -> Bool {
        let lower = text.lowercased()
        guard lower.contains("codex") || lower.contains("chatgpt") else { return false }
        if errorMeansPlanHasNoVoice(text) { return false }
        return ["not signed in", "isn't signed in", "codex login", "not logged in", "sign in again", "signed out",
                "rejected codex's saved sign-in"].contains { lower.contains($0) }
    }

    /// The plugin's "Your ChatGPT plan doesn't include voice through Codex" (reason codex_plan_no_voice):
    /// offer Grok or an API key, never a redial into the same error.
    static func errorMeansPlanHasNoVoice(_ text: String) -> Bool {
        let lower = text.lowercased()
        return lower.contains("plan doesn't include voice") || lower.contains("plan doesn\u{2019}t include voice")
    }
}

/// Words shared by the Mac and iPhone sheets.
public enum CodexLoginCopy {
    public static let button = "Sign in to ChatGPT"
    public static let switchButton = "Switch to ChatGPT sign-in"
    public static let title = "Sign in to ChatGPT"
    public static let intro = "Voice runs on your ChatGPT plan through Codex on your Hermes computer. Open the sign-in page on this device, sign in, and enter this code."
    public static let done = "Codex on your Hermes computer is signed in to your ChatGPT account. Calls use your plan from now on."
    public static let open = "Open sign-in page"
    public static let copy = "Copy code"
    public static let alternative = "Use Grok or an API key instead"
    public static let apiKeyNote = "Codex on your Hermes computer is signed in with an OpenAI API key, so calls bill that key. Sign in to ChatGPT to use your plan instead."
    public static let planNoVoice = "Your ChatGPT plan doesn't include voice through Codex. Use Grok or an OpenAI API key for voice instead."
    public static let chooseVoice = "Choose another voice"
    public static let notSignedIn = "Codex on your Hermes computer isn't signed in to ChatGPT yet."
}
