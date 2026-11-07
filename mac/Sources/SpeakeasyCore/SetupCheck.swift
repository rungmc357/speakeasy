import Foundation

/// The Setup check screen (`GET /voice/setup-check`): every requirement a first call needs as a
/// ready / not-ready row with at most one fix. Rows come from the plugin, so new checks and better
/// fixes ship with a plugin update; the app only maps each fix `kind` to a button. A plugin without
/// the route gets rows built here from `/voice/status` (`SetupCheck.fallback`).
public struct SetupCheck: Equatable, Sendable {
    public var ready: Bool
    public var rows: [Row]
    public var version: String

    public init(ready: Bool, rows: [Row], version: String = "") {
        self.ready = ready; self.rows = rows; self.version = version
    }

    public struct Row: Equatable, Sendable, Identifiable {
        public var id: String
        public var ok: Bool
        /// Optional rows (the voice brief) never keep a call from starting.
        public var blocking: Bool
        public var title: String
        public var detail: String
        public var fix: Fix?

        public init(id: String, ok: Bool, blocking: Bool = true, title: String, detail: String = "", fix: Fix? = nil) {
            self.id = id; self.ok = ok; self.blocking = blocking; self.title = title; self.detail = detail
            self.fix = ok ? nil : fix
        }

        /// Red (blocks a call), grey (optional, not done yet) or green.
        public var state: RowState { ok ? .ready : (blocking ? .needsFix : .optional) }
    }

    public enum RowState: Equatable, Sendable { case ready, needsFix, optional }

    public struct Fix: Equatable, Sendable {
        public enum Kind: Equatable, Sendable {
            /// The app's Sign in to ChatGPT sheet (it also installs or updates Codex when needed).
            case codexSignIn
            /// `POST /voice/setup-check/{row id}`: the plugin fixes it by itself.
            case action
            /// Copy `payload` (a /restart command or a request to paste into the user's Hermes chat).
            case copy
            /// The app's own settings page named by `payload` (e.g. `voice`).
            case openSettings
            /// A newer plugin's fix this app doesn't know: show the detail, no button.
            case unknown(String)

            init(_ raw: String) {
                switch raw {
                case "codex_sign_in": self = .codexSignIn
                case "action": self = .action
                case "copy": self = .copy
                case "open_settings": self = .openSettings
                default: self = .unknown(raw)
                }
            }
        }
        public var kind: Kind
        public var label: String
        public var payload: String

        public init(kind: Kind, label: String, payload: String = "") {
            self.kind = kind; self.label = label; self.payload = payload
        }

        /// The app can show a button for it.
        public var actionable: Bool {
            if case .unknown = kind { return false }
            return !label.isEmpty
        }
    }

    /// Rows that still block a call.
    public var blockers: [Row] { rows.filter { $0.blocking && !$0.ok } }

    public init?(json: [String: Any]) {
        guard let raw = json["rows"] as? [[String: Any]] else { return nil }
        rows = raw.compactMap { row in
            guard let id = row["id"] as? String, let ok = row["ok"] as? Bool else { return nil }
            var fix: Fix?
            if let f = row["fix"] as? [String: Any], let kind = f["kind"] as? String {
                fix = Fix(kind: Fix.Kind(kind), label: f["label"] as? String ?? "", payload: f["payload"] as? String ?? "")
            }
            return Row(id: id, ok: ok, blocking: row["blocking"] as? Bool ?? true, title: row["title"] as? String ?? id,
                       detail: row["detail"] as? String ?? "", fix: fix)
        }
        // A row this app couldn't read never turns a not-ready answer into a ready one.
        ready = (json["ready"] as? Bool ?? false) && rows.count == raw.count
        version = json["version"] as? String ?? ""
    }

    public static let restartCommand = "/restart"
    static let finishSetupRequest = "Please finish setting up Speakeasy on this Hermes: run hermes voice setup --yes, then tell me if Hermes needs a restart."
    static let updateRequest = "Please update the Speakeasy plugin on this Hermes (hermes plugins update speakeasy), then tell me if Hermes needs a restart."

    /// Rows for a plugin from before the Setup check route, from what `/voice/status` says. The first
    /// row asks for a plugin update, since only a newer plugin can say exactly what's missing.
    public static func fallback(_ status: ServerStatus) -> SetupCheck {
        var rows = [Row(id: "plugin", ok: false, blocking: false, title: "Older Speakeasy on your Hermes",
                        detail: "A newer Speakeasy plugin checks everything a call needs and can fix more from here. "
                            + "Paste this request into your chat with Hermes.",
                        fix: Fix(kind: .copy, label: "Copy a request for Hermes", payload: updateRequest))]
        let apiOK = status.hermesAPIOK == true
        rows.append(Row(id: "hermes_api", ok: apiOK, title: apiOK ? "Speakeasy can reach Hermes" : "Speakeasy's connection on your Hermes is off",
                        detail: "Send /restart in your chat with Hermes. If that doesn't do it, paste a setup request instead.",
                        fix: Fix(kind: .copy, label: "Copy /restart", payload: restartCommand)))
        let voice = status.checks.first { $0.id == "voice" }
        let voiceOK = (status.voiceReady ?? voice?.ok) == true
        if voiceOK {
            rows.append(Row(id: "voice", ok: true, title: "Voice: \(voice?.title ?? "ready")"))
        } else if status.offersCodexSignIn {
            rows.append(Row(id: "voice", ok: false, title: "Voice: ChatGPT isn't signed in",
                            detail: "Calls use your ChatGPT sign-in on the Hermes computer. Sign in from here with a short code.",
                            fix: Fix(kind: .codexSignIn, label: "Sign in with ChatGPT")))
        } else {
            rows.append(Row(id: "voice", ok: false, title: "Voice isn't set up yet",
                            detail: "Paste this request into your chat with Hermes and it will finish voice setup.",
                            fix: Fix(kind: .copy, label: "Copy a request for Hermes", payload: finishSetupRequest)))
        }
        let blocking = rows.filter(\.blocking)
        return SetupCheck(ready: blocking.allSatisfy(\.ok), rows: rows, version: status.version ?? "")
    }
}

public extension ServerStatus {
    /// Start a call should open the Setup check instead of dialing: this server says a call can't
    /// start yet (voice not ready, which includes "Hermes needs a restart", or no Hermes connection).
    /// nil-safe for older plugins: they fall back to the checklist.
    var needsSetupCheck: Bool {
        if hermesAPIOK == false { return true }
        if agentModelMissing { return true }  // the call would connect, but nothing it hands off could run
        if let voiceReady { return !voiceReady }
        return !readyForCalls
    }
}
