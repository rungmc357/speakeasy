import Foundation

// Server-side settings, brief and status (`/voice/settings`, `/voice/brief`, `/voice/status`).
// Decoding is tolerant: unknown keys are ignored and every field is optional, so a
// newer or older server never breaks the app.

public enum VoiceProvider: String, Codable, CaseIterable, Sendable, Identifiable {
    case codex, openai
    public var id: String { rawValue }
    public var label: String {
        switch self {
        case .codex: return "GPT-Live-1 via Codex OAuth (ChatGPT account)"
        case .openai: return "GPT-Live-1 via the OpenAI API (API key)"
        }
    }
}

public struct ServerSettings: Codable, Equatable, Sendable {
    public struct Voice: Codable, Equatable, Sendable {
        public var provider: String?
        public var voice: String?
        public init(provider: String? = nil, voice: String? = nil) { self.provider = provider; self.voice = voice }
    }
    public struct Brief: Codable, Equatable, Sendable {
        public var autoRefresh: Bool?
        public var includeRecentVoice: Bool?
        enum CodingKeys: String, CodingKey { case autoRefresh = "auto_refresh", includeRecentVoice = "include_recent_voice" }
        public init(autoRefresh: Bool? = nil, includeRecentVoice: Bool? = nil) {
            self.autoRefresh = autoRefresh; self.includeRecentVoice = includeRecentVoice
        }
    }

    /// One opted-in delivery channel: new tasks about `topic` (or that name `label`) go to `target`.
    public struct Channel: Codable, Equatable, Sendable, Identifiable, Hashable {
        public var target: String
        public var label: String
        public var topic: String
        public var newThread: Bool
        public var id: String { target }
        enum CodingKeys: String, CodingKey { case target, label, topic, newThread = "new_thread" }
        public init(target: String, label: String, topic: String = "", newThread: Bool = false) {
            self.target = target; self.label = label; self.topic = topic; self.newThread = newThread
        }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            target = try c.decode(String.self, forKey: .target)
            label = (try? c.decodeIfPresent(String.self, forKey: .label)) ?? target
            topic = (try? c.decodeIfPresent(String.self, forKey: .topic)) ?? ""
            newThread = (try? c.decodeIfPresent(Bool.self, forKey: .newThread)) ?? false
        }
        var patchObject: [String: Any] { ["target": target, "label": label, "topic": topic, "new_thread": newThread] }
    }

    /// Where finished work goes: the default `target` (nil = nowhere), whether each task opens a new
    /// thread there, and extra opted-in `channels` routed by topic.
    public struct Delivery: Codable, Equatable, Sendable {
        public var target: String?
        public var newThread: Bool?
        public var channels: [Channel]?
        /// "single" (home only), "home" (home + continue approved threads), "topic" (also sort new work).
        public var mode: String?
        enum CodingKeys: String, CodingKey { case target, newThread = "new_thread", legacyNewThread = "new_thread_per_task", channels, mode }
        public init(target: String? = nil, newThread: Bool? = nil, channels: [Channel]? = nil, mode: String? = nil) {
            self.target = target; self.newThread = newThread; self.channels = channels; self.mode = mode
        }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            target = try? c.decodeIfPresent(String.self, forKey: .target)
            newThread = (try? c.decodeIfPresent(Bool.self, forKey: .newThread)) ?? (try? c.decodeIfPresent(Bool.self, forKey: .legacyNewThread))
            channels = try? c.decodeIfPresent([Channel].self, forKey: .channels)
            mode = try? c.decodeIfPresent(String.self, forKey: .mode)
        }
        public func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encodeIfPresent(target, forKey: .target)
            try c.encodeIfPresent(newThread, forKey: .newThread)
            try c.encodeIfPresent(channels, forKey: .channels)
            try c.encodeIfPresent(mode, forKey: .mode)
        }
    }
    public struct Continuity: Codable, Equatable, Sendable {
        public var enabled: Bool?
        public init(enabled: Bool? = nil) { self.enabled = enabled }
    }

    /// A brief spoken update on long tasks.
    public struct Speech: Codable, Equatable, Sendable {
        public var progress: Bool?
        public init(progress: Bool? = nil) { self.progress = progress }
    }

    public var assistantName: String?
    public var userName: String?
    public var voice: Voice?
    public var idlePauseMinutes: Double?
    /// Legacy single-target field; `delivery.target` wins when both are present.
    public var notifyTarget: String?
    public var delivery: Delivery?
    public var continuity: Continuity?
    public var speech: Speech?
    public var maxCallMinutes: Int?
    public var instructionsExtra: String?
    public var brief: Brief?

    enum CodingKeys: String, CodingKey {
        case assistantName = "assistant_name", userName = "user_name", voice, idlePauseMinutes = "idle_pause_minutes"
        case notifyTarget = "notify_target", delivery, continuity, speech, instructionsExtra = "instructions_extra", brief
    }

    public init(assistantName: String? = nil, userName: String? = nil, voice: Voice? = nil, idlePauseMinutes: Double? = nil,
                notifyTarget: String? = nil, delivery: Delivery? = nil, continuity: Continuity? = nil,
                instructionsExtra: String? = nil, brief: Brief? = nil) {
        self.assistantName = assistantName; self.userName = userName; self.voice = voice
        self.idlePauseMinutes = idlePauseMinutes; self.notifyTarget = notifyTarget; self.delivery = delivery
        self.continuity = continuity; self.instructionsExtra = instructionsExtra; self.brief = brief
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        assistantName = try? c.decodeIfPresent(String.self, forKey: .assistantName)
        userName = try? c.decodeIfPresent(String.self, forKey: .userName)
        voice = try? c.decodeIfPresent(Voice.self, forKey: .voice)
        idlePauseMinutes = try? c.decodeIfPresent(Double.self, forKey: .idlePauseMinutes)
        notifyTarget = try? c.decodeIfPresent(String.self, forKey: .notifyTarget)
        delivery = try? c.decodeIfPresent(Delivery.self, forKey: .delivery)
        continuity = try? c.decodeIfPresent(Continuity.self, forKey: .continuity)
        speech = try? c.decodeIfPresent(Speech.self, forKey: .speech)
        if let v = try? c.decodeIfPresent(VoiceLimits.self, forKey: .voice) { maxCallMinutes = v.maxCallMinutes }
        instructionsExtra = try? c.decodeIfPresent(String.self, forKey: .instructionsExtra)
        brief = try? c.decodeIfPresent(Brief.self, forKey: .brief)
    }

    /// The effective delivery target (nil = off).
    public var deliveryTarget: String? {
        let raw = delivery?.target ?? notifyTarget
        let trimmed = raw?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return trimmed.isEmpty || trimmed == "none" ? nil : trimmed
    }

    public static func decode(_ data: Data) throws -> ServerSettings {
        // Accept both a bare object and `{"settings": {...}}`.
        if let wrapped = try? JSONDecoder().decode([String: ServerSettings].self, from: data), let inner = wrapped["settings"] {
            return inner
        }
        return try JSONDecoder().decode(ServerSettings.self, from: data)
    }

    /// Body for `PATCH /voice/settings`: every known field (nested objects whole, so a
    /// shallow merge on the server cannot drop a sibling). Delivery target "none" = off.
    public func patchBody() throws -> Data {
        var object: [String: Any] = [:]
        if let assistantName { object["assistant_name"] = assistantName }
        if let userName { object["user_name"] = userName }
        if let voice {
            var v: [String: Any] = [:]
            if let p = voice.provider { v["provider"] = p }
            if let name = voice.voice { v["voice"] = name }
            if let maxCallMinutes { v["max_call_minutes"] = maxCallMinutes }
            object["voice"] = v
        } else if let maxCallMinutes {
            object["voice"] = ["max_call_minutes": maxCallMinutes]
        }
        if let idlePauseMinutes { object["idle_pause_minutes"] = idlePauseMinutes }
        var d: [String: Any] = ["target": deliveryTarget ?? "none"]
        if let v = delivery?.newThread { d["new_thread"] = deliveryTarget == nil ? false : v }
        if let channels = delivery?.channels { d["channels"] = channels.map(\.patchObject) }
        if let mode = delivery?.mode { d["mode"] = mode }
        object["delivery"] = d
        if let enabled = continuity?.enabled { object["continuity"] = ["enabled": enabled] }
        if let speech {
            var x: [String: Any] = [:]
            if let v = speech.progress { x["progress"] = v }
            object["speech"] = x
        }
        if let instructionsExtra { object["instructions_extra"] = instructionsExtra }
        if let brief {
            var b: [String: Any] = [:]
            if let v = brief.autoRefresh { b["auto_refresh"] = v }
            if let v = brief.includeRecentVoice { b["include_recent_voice"] = v }
            object["brief"] = b
        }
        return try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    /// `voice.max_call_minutes` is read separately so `Voice` keeps its provider/voice shape.
    private struct VoiceLimits: Decodable {
        var maxCallMinutes: Int?
        enum CodingKeys: String, CodingKey { case maxCallMinutes = "max_call_minutes" }
    }

    // MARK: Resolved values with product defaults

    public var resolvedAssistantName: String {
        let name = assistantName?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return name.isEmpty ? VoiceState.defaultAssistantName : name
    }
    public var resolvedProvider: VoiceProvider { voice?.provider.flatMap(VoiceProvider.init(rawValue:)) ?? .codex }
    /// Idle auto-pause in seconds; 0 disables. Default 5 minutes.
    public var idleTimeout: TimeInterval {
        guard let minutes = idlePauseMinutes, minutes.isFinite, minutes >= 0 else { return VoiceState.defaultIdleTimeout }
        return min(minutes, 24 * 60) * 60
    }

}

/// `GET /voice/brief`.
public struct VoiceBrief: Codable, Equatable, Sendable {
    public var text: String
    /// e.g. `missing`, `writing`, `ready`, `failed` (server-defined; shown as-is).
    public var state: String?
    public var updatedAt: Date?
    public var edited: Bool

    /// The server sends and accepts the text as `brief` (docs/API.md); `text` is read for older replies.
    enum CodingKeys: String, CodingKey { case text = "brief", legacyText = "text", state, updatedAt = "updated_at", edited }

    public init(text: String = "", state: String? = nil, updatedAt: Date? = nil, edited: Bool = false) {
        self.text = text; self.state = state; self.updatedAt = updatedAt; self.edited = edited
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = (try? c.decodeIfPresent(String.self, forKey: .text))
            ?? (try? c.decodeIfPresent(String.self, forKey: .legacyText)) ?? ""
        state = try? c.decodeIfPresent(String.self, forKey: .state)
        edited = (try? c.decodeIfPresent(Bool.self, forKey: .edited)) ?? false
        if let seconds = try? c.decodeIfPresent(Double.self, forKey: .updatedAt) {
            updatedAt = Date(timeIntervalSince1970: seconds)
        } else if let text = try? c.decodeIfPresent(String.self, forKey: .updatedAt) {
            updatedAt = ISO8601DateFormatter.flexible(text)
        } else {
            updatedAt = nil
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(text, forKey: .text)
        try c.encodeIfPresent(state, forKey: .state)
        try c.encodeIfPresent(updatedAt?.timeIntervalSince1970, forKey: .updatedAt)
        try c.encode(edited, forKey: .edited)
    }

    public static func putBody(text: String) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["brief": text], options: [])
    }
}

/// `GET /voice/brief/tune`: brief edits Hermes proposes from recent calls, each reviewed by the user.
public struct BriefTune: Codable, Equatable, Sendable {
    public struct Edit: Codable, Equatable, Sendable, Identifiable {
        public var id: String
        public var kind: String          // add | change | remove
        public var section: String?
        public var old: String?
        public var new: String?
        public var why: String
        public var evidence: String?
    }
    public struct Issue: Codable, Equatable, Sendable {
        public var what: String
        public var evidence: String?
    }
    public var state: String             // none | working | ready | failed
    public var calls: Int
    public var error: String?
    public var summary: String
    public var edits: [Edit]
    public var productIssues: [Issue]

    enum CodingKeys: String, CodingKey { case state, calls, error, summary, edits, productIssues = "product_issues" }

    public init(state: String = "none", calls: Int = 0, error: String? = nil, summary: String = "",
                edits: [Edit] = [], productIssues: [Issue] = []) {
        self.state = state; self.calls = calls; self.error = error; self.summary = summary
        self.edits = edits; self.productIssues = productIssues
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = (try? c.decodeIfPresent(String.self, forKey: .state)) ?? "none"
        calls = (try? c.decodeIfPresent(Int.self, forKey: .calls)) ?? 0
        error = try? c.decodeIfPresent(String.self, forKey: .error)
        summary = (try? c.decodeIfPresent(String.self, forKey: .summary)) ?? ""
        edits = (try? c.decodeIfPresent([Edit].self, forKey: .edits)) ?? []
        productIssues = (try? c.decodeIfPresent([Issue].self, forKey: .productIssues)) ?? []
    }

    public static func applyBody(accept: [String]) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["accept": accept], options: [])
    }
}

/// `GET /voice/status`.
public struct ServerStatus: Codable, Equatable, Sendable {
    public var assistantName: String?
    public var provider: String?
    public var codexSignedIn: Bool?
    public var apiKeySet: Bool?
    public var briefState: String?
    public var hermesAPIOK: Bool?
    public var version: String?
    /// The delivery platform can open a thread per task.
    public var threadsSupported: Bool?
    /// The server's own answer to "can a call start": the chosen provider is signed in / has a key.
    public var voiceReady: Bool?
    /// Why new threads aren't available (empty when they are).
    public var threadsReason: String?
    /// Chat platforms where a task can open a new thread (e.g. discord, telegram).
    public var threadPlatforms: [String]?
    public var continuityEnabled: Bool?
    /// The model task routing uses (Hermes auxiliary task speakeasy_router), and how to change it.
    public var routingModel: String?
    public var routingHint: String?
    /// What task routing is, in plain words (servers from plugin 0.2.20 on).
    public var routingExplainer: String?
    /// The routing models the user can switch between (servers from plugin 0.2.19 on).
    public var routingChoice: RoutingChoices?
    /// The address `hermes voice setup` advertised for other devices (empty = local only).
    public var advertisedURL: String?
    public var tailscaleName: String?
    /// The server accepts what listening mode heard with a new call (`room` on POST /voice/sessions).
    /// Missing on older plugins, which reject unknown session fields: never send `room` then.
    public var roomListening: Bool?
    /// The server takes `room` with `resume_from` too (listening mode during a call pauses it, then
    /// resumes it with what was heard). Missing on plugins from before that.
    public var roomOnResume: Bool?

    enum CodingKeys: String, CodingKey {
        case assistantName = "assistant_name", provider, codexSignedIn = "codex_signed_in"
        case apiKeySet = "api_key_set", briefState = "brief_state", hermesAPIOK = "hermes_api_ok", version
        case threadsSupported = "threads_supported", voiceReady = "voice_ready"
        case threadsReason = "threads_reason", threadPlatforms = "thread_platforms", continuityEnabled = "continuity_enabled"
        case routingModel = "routing_model", routingHint = "routing_hint", routingChoice = "routing_choice"
        case routingExplainer = "routing_explainer"
        case advertisedURL = "advertised_url", tailscaleName = "tailscale_name"
        case roomListening = "room_listening", roomOnResume = "room_on_resume"
    }

    public init(assistantName: String? = nil, provider: String? = nil, codexSignedIn: Bool? = nil, apiKeySet: Bool? = nil,
                briefState: String? = nil, hermesAPIOK: Bool? = nil, version: String? = nil) {
        self.assistantName = assistantName; self.provider = provider; self.codexSignedIn = codexSignedIn
        self.apiKeySet = apiKeySet; self.briefState = briefState; self.hermesAPIOK = hermesAPIOK; self.version = version
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        assistantName = try? c.decodeIfPresent(String.self, forKey: .assistantName)
        provider = try? c.decodeIfPresent(String.self, forKey: .provider)
        codexSignedIn = try? c.decodeIfPresent(Bool.self, forKey: .codexSignedIn)
        apiKeySet = try? c.decodeIfPresent(Bool.self, forKey: .apiKeySet)
        briefState = try? c.decodeIfPresent(String.self, forKey: .briefState)
        hermesAPIOK = try? c.decodeIfPresent(Bool.self, forKey: .hermesAPIOK)
        version = try? c.decodeIfPresent(String.self, forKey: .version)
        threadsSupported = try? c.decodeIfPresent(Bool.self, forKey: .threadsSupported)
        voiceReady = try? c.decodeIfPresent(Bool.self, forKey: .voiceReady)
        threadsReason = try? c.decodeIfPresent(String.self, forKey: .threadsReason)
        threadPlatforms = try? c.decodeIfPresent([String].self, forKey: .threadPlatforms)
        continuityEnabled = try? c.decodeIfPresent(Bool.self, forKey: .continuityEnabled)
        routingModel = try? c.decodeIfPresent(String.self, forKey: .routingModel)
        routingHint = try? c.decodeIfPresent(String.self, forKey: .routingHint)
        routingChoice = try? c.decodeIfPresent(RoutingChoices.self, forKey: .routingChoice)
        routingExplainer = try? c.decodeIfPresent(String.self, forKey: .routingExplainer)
        advertisedURL = try? c.decodeIfPresent(String.self, forKey: .advertisedURL)
        tailscaleName = try? c.decodeIfPresent(String.self, forKey: .tailscaleName)
        roomListening = try? c.decodeIfPresent(Bool.self, forKey: .roomListening)
        roomOnResume = try? c.decodeIfPresent(Bool.self, forKey: .roomOnResume)
    }

    /// A new thread can be opened for tasks sent to `target` (the server supports it and the
    /// target's platform has threads).
    public func canOpenThread(in target: String?) -> Bool {
        guard threadsSupported == true, let target, !target.isEmpty, target != "none" else { return false }
        let platform = String(target.split(separator: ":").first ?? "")
        return (threadPlatforms ?? []).contains(platform)
    }

    /// "Connected over Tailscale: <name>" or "Local only", for onboarding and Settings.
    public var reachability: String {
        if let name = tailscaleName, !name.isEmpty { return "Connected over Tailscale: \(name)" }
        if let url = advertisedURL, !url.isEmpty { return "Reachable at \(url)" }
        return "Local only"
    }

    public var resolvedAssistantName: String {
        let name = assistantName?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return name.isEmpty ? VoiceState.defaultAssistantName : name
    }

    public struct Check: Equatable, Sendable, Identifiable {
        public var id: String
        public var ok: Bool
        public var title: String
        /// What to do about it, when not ok.
        public var fix: String?
    }

    public var resolvedProvider: VoiceProvider { provider.flatMap(VoiceProvider.init(rawValue:)) ?? .codex }

    /// The checklist the first-run screen and Settings › Connection show.
    public var checks: [Check] {
        var out: [Check] = []
        out.append(Check(id: "hermes", ok: hermesAPIOK == true, title: "Hermes API server",
                         fix: hermesAPIOK == true ? nil : "Turn on the Hermes API server: run hermes voice setup"))
        switch provider.flatMap(VoiceProvider.init(rawValue:)) ?? .codex {
        case .codex:
            out.append(Check(id: "voice", ok: codexSignedIn == true, title: "ChatGPT sign-in",
                             fix: codexSignedIn == true ? nil : "On the Hermes machine, run hermes voice setup (it signs you in to ChatGPT)"))
        case .openai:
            out.append(Check(id: "voice", ok: apiKeySet == true, title: "OpenAI API key",
                             fix: apiKeySet == true ? nil : "On the Hermes machine, run hermes voice setup --api-key"))
        }
        let briefReady = briefState == "ready" || briefState == "edited"
        out.append(Check(id: "brief", ok: briefReady, title: "Voice brief",
                         fix: briefReady ? nil : (briefState == "writing"
                            ? "Hermes is writing the voice brief; calls work meanwhile"
                            : "No voice brief yet; calls work, or write one in Settings › Voice brief")))
        return out
    }

    /// Calls can start (the brief is optional).
    public var readyForCalls: Bool { checks.filter { $0.id != "brief" }.allSatisfy(\.ok) }
}

/// One entry of `GET /voice/destinations`: a connected Hermes platform finished work can go to.
public struct Destination: Codable, Equatable, Sendable, Identifiable, Hashable {
    public var target: String
    public var label: String
    public var threadsSupported: Bool
    public var id: String { target }
    enum CodingKeys: String, CodingKey { case target, label, name, threadsSupported = "threads_supported" }

    public init(target: String, label: String, threadsSupported: Bool = false) {
        self.target = target; self.label = label; self.threadsSupported = threadsSupported
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        target = try c.decode(String.self, forKey: .target)
        label = (try? c.decodeIfPresent(String.self, forKey: .label)) ?? (try? c.decodeIfPresent(String.self, forKey: .name))
            ?? target.capitalized
        threadsSupported = (try? c.decodeIfPresent(Bool.self, forKey: .threadsSupported)) ?? false
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(target, forKey: .target); try c.encode(label, forKey: .label)
        try c.encode(threadsSupported, forKey: .threadsSupported)
    }

    /// Server shape (docs/API.md): `{"destinations": [{"platform", "connected", "target",
    /// "home_channel": {"name", "target"}, "chats": [{"name", "target"}]}], "threads_supported"}`.
    /// Flattens each platform into its home channel and chats. Also accepts plain
    /// `{"target", "label"}` entries and bare strings. Drops "none" and duplicates.
    /// `suggested` from `GET /voice/destinations`: the connected home channel to preselect, or nil.
    public static func suggested(_ data: Data) -> String? {
        guard let top = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let s = top["suggested"] as? String, !s.isEmpty, s != "none" else { return nil }
        return s
    }

    public static func list(_ data: Data) -> [Destination] {
        let json = try? JSONSerialization.jsonObject(with: data)
        let top = json as? [String: Any]
        let threads = top?["threads_supported"] as? Bool ?? false
        let array = (json as? [Any]) ?? (top?["destinations"] as? [Any]) ?? []
        var out: [Destination] = []
        for item in array {
            if let name = item as? String { out.append(Destination(target: name, label: name.capitalized)); continue }
            guard let object = item as? [String: Any] else { continue }
            if let platform = object["platform"] as? String {
                if object["connected"] as? Bool == false { continue }
                let title = platform.capitalized
                let entryThreads = object["threads_supported"] as? Bool ?? threads
                if let home = object["home_channel"] as? [String: Any], let t = home["target"] as? String {
                    let n = (home["name"] as? String).map { " · \($0)" } ?? ""
                    out.append(Destination(target: t, label: title + n, threadsSupported: entryThreads))
                } else if object["chats"] == nil, let t = object["target"] as? String {
                    // Older servers list a bare platform; with a chat list, a bare target would mean
                    // "the home channel", which this platform doesn't have, so it isn't offered.
                    out.append(Destination(target: t, label: title, threadsSupported: entryThreads))
                }
                for chat in object["chats"] as? [[String: Any]] ?? [] {
                    guard let t = chat["target"] as? String else { continue }
                    out.append(Destination(target: t, label: "\(title) · \(chat["name"] as? String ?? t)",
                                           threadsSupported: entryThreads))
                }
                continue
            }
            guard let d = try? JSONDecoder().decode(Destination.self, from: JSONSerialization.data(withJSONObject: object)) else { continue }
            out.append(d)
        }
        var seen = Set<String>()
        return out.filter { !$0.target.isEmpty && $0.target != "none" && seen.insert($0.target).inserted }
    }
}

/// `GET /voice/onboarding` (which steps the server considers done) and the `POST` body.
public struct OnboardingStatus: Equatable, Sendable {
    public var done: Set<String>
    public init(done: Set<String> = []) { self.done = done }

    /// Server shape (docs/API.md): `{"steps": {"names_set": true, ...}, "complete": false, ...}`.
    /// Also tolerates `{"done": [...]}` and `{"completed": true}`.
    public static func parse(_ data: Data) -> OnboardingStatus {
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return OnboardingStatus() }
        var done = Set<String>()
        if let steps = object["steps"] as? [String: Any] {
            for (k, v) in steps where (v as? Bool) == true || ((v as? [String: Any])?["done"] as? Bool) == true { done.insert(k) }
        }
        if let list = object["done"] as? [String] { done.formUnion(list) }
        if object["complete"] as? Bool == true || object["completed"] as? Bool == true { done.insert("complete") }
        return OnboardingStatus(done: done)
    }

    public var isComplete: Bool { done.contains("complete") }

    /// `POST /voice/onboarding` accepts assistant_name, user_name, delivery_target, continuity_enabled
    /// and write_brief. Extra channels and threads are set later in Settings › Delivery.
    public static func postBody(assistantName: String, userName: String, target: String?, writeBrief: Bool = true,
                                continuity: Bool? = nil) throws -> Data {
        let name = assistantName.trimmingCharacters(in: .whitespacesAndNewlines)
        let user = userName.trimmingCharacters(in: .whitespacesAndNewlines)
        var body: [String: Any] = [
            "assistant_name": name.isEmpty ? VoiceState.defaultAssistantName : name,
            "user_name": user,
            "delivery_target": target ?? "none",
            "write_brief": writeBrief,
        ]
        if let continuity { body["continuity_enabled"] = continuity }
        return try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
    }
}

extension ISO8601DateFormatter {
    static func flexible(_ text: String) -> Date? {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: text) { return d }
        f.formatOptions = [.withInternetDateTime]
        return f.date(from: text)
    }
}

/// `POST /voice/destinations/suggest`: channels the user's own Hermes proposes (never saved by the
/// server; the user picks which to add).
public enum ChannelSuggestions {
    public static func parse(_ data: Data) -> [ServerSettings.Channel] {
        guard let top = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let items = top["suggestions"] as? [[String: Any]] else { return [] }
        var seen = Set<String>()
        return items.compactMap { item -> ServerSettings.Channel? in
            guard let target = item["target"] as? String, !target.isEmpty, target != "none",
                  let label = item["label"] as? String, !label.isEmpty, seen.insert(target).inserted else { return nil }
            return ServerSettings.Channel(target: target, label: label, topic: item["topic"] as? String ?? "",
                                          newThread: item["new_thread"] as? Bool ?? false)
        }
    }

    /// Adds picked suggestions to existing channels: an already-opted-in target is kept as is.
    public static func merge(_ picked: [ServerSettings.Channel], into existing: [ServerSettings.Channel],
                             max: Int = 8) -> [ServerSettings.Channel] {
        var out = existing
        for channel in picked where !out.contains(where: { $0.target == channel.target || $0.label.lowercased() == channel.label.lowercased() }) {
            guard out.count < max else { break }
            out.append(channel)
        }
        return out
    }
}


/// `GET /voice/routing`: the model routing uses now, and the providers (with their models) this
/// Hermes is signed in to. The list comes from Hermes itself, never from the app.
public struct RoutingChoices: Codable, Equatable, Sendable {
    public struct Current: Codable, Equatable, Sendable {
        public var provider: String
        public var model: String
        public var thinking: Bool
        public var isDefault: Bool
        enum CodingKeys: String, CodingKey { case provider, model, thinking, isDefault = "is_default" }
        public init(provider: String = "auto", model: String = "", thinking: Bool = true, isDefault: Bool = true) {
            self.provider = provider; self.model = model; self.thinking = thinking; self.isDefault = isDefault
        }
    }
    public struct Provider: Codable, Equatable, Sendable, Identifiable {
        public var id: String
        public var name: String
        public var models: [String]
        public init(id: String, name: String, models: [String]) { self.id = id; self.name = name; self.models = models }
    }
    public var current: Current
    public var providers: [Provider]
    public var label: String
    public init(current: Current, providers: [Provider], label: String = "") {
        self.current = current; self.providers = providers; self.label = label
    }
    public static let defaultID = "auto"
    public static func decode(_ data: Data) throws -> RoutingChoices { try JSONDecoder().decode(RoutingChoices.self, from: data) }
    /// `POST /voice/routing`. `provider` "auto" goes back to Hermes' default (the main model).
    public static func postBody(provider: String, model: String, thinking: Bool?) throws -> Data {
        var body: [String: Any] = ["provider": provider, "model": model]
        if let thinking { body["thinking"] = thinking }
        return try JSONSerialization.data(withJSONObject: body)
    }
}

/// `GET /voice/routing/models?provider=`: every model Hermes knows for one provider.
public struct ProviderModels: Codable, Equatable, Sendable {
    public var provider: String
    public var models: [String]
    public static func decode(_ data: Data) throws -> ProviderModels { try JSONDecoder().decode(ProviderModels.self, from: data) }
}

/// `GET /voice/home` (and the `PUT` reply): instant home control through Home Assistant.
public struct HomeControlInfo: Codable, Equatable, Sendable {
    public struct Device: Codable, Equatable, Sendable, Identifiable {
        public var entityID: String
        public var name: String
        public var kind: String
        public var state: String
        public var included: Bool
        public var id: String { entityID }
        enum CodingKeys: String, CodingKey { case entityID = "entity_id", name, kind, state, included }
        public init(entityID: String, name: String, kind: String, state: String, included: Bool) {
            self.entityID = entityID; self.name = name; self.kind = kind; self.state = state; self.included = included
        }
        /// A plain-words label for the kind ("Lights", "Thermostats").
        public var kindLabel: String { HomeControlInfo.kindLabels[kind] ?? kind.capitalized }
    }
    public var enabled: Bool
    public var configured: Bool
    public var available: Bool
    public var reason: String
    public var devices: [Device]
    public var explainer: String
    public var unit: String?

    enum CodingKeys: String, CodingKey { case enabled, configured, available, reason, devices, explainer, unit }
    public init(enabled: Bool = false, configured: Bool = false, available: Bool = false, reason: String = "",
                devices: [Device] = [], explainer: String = "", unit: String? = nil) {
        self.enabled = enabled; self.configured = configured; self.available = available; self.reason = reason
        self.devices = devices; self.explainer = explainer; self.unit = unit
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        enabled = (try? c.decode(Bool.self, forKey: .enabled)) ?? false
        configured = (try? c.decode(Bool.self, forKey: .configured)) ?? false
        available = (try? c.decode(Bool.self, forKey: .available)) ?? false
        reason = (try? c.decode(String.self, forKey: .reason)) ?? ""
        devices = (try? c.decode([Device].self, forKey: .devices)) ?? []
        explainer = (try? c.decode(String.self, forKey: .explainer)) ?? ""
        unit = try? c.decode(String.self, forKey: .unit)
    }
    public static func decode(_ data: Data) throws -> HomeControlInfo { try JSONDecoder().decode(HomeControlInfo.self, from: data) }

    /// Kinds in display order, with plain labels.
    public static let kindOrder = ["light", "climate", "fan", "switch", "cover", "media_player", "scene", "script", "lock"]
    public static let kindLabels = ["light": "Lights", "climate": "Thermostats", "fan": "Fans", "switch": "Switches & plugs",
                                    "cover": "Blinds & shades", "media_player": "TVs & speakers", "scene": "Scenes",
                                    "script": "Scripts", "lock": "Locks"]
    /// Devices grouped by kind, in display order.
    public var groups: [(kind: String, devices: [Device])] {
        let byKind = Dictionary(grouping: devices, by: \.kind)
        let known = HomeControlInfo.kindOrder.compactMap { k in byKind[k].map { (k, $0) } }
        let rest = byKind.keys.filter { !HomeControlInfo.kindOrder.contains($0) }.sorted().map { ($0, byKind[$0]!) }
        return known + rest
    }
    public var includedIDs: [String] { devices.filter(\.included).map(\.entityID).sorted() }

    /// `PUT /voice/home` body: on/off and/or exactly which devices.
    public static func putBody(enabled: Bool?, entities: [String]?) throws -> Data {
        var body: [String: Any] = [:]
        if let enabled { body["enabled"] = enabled }
        if let entities { body["entities"] = entities.sorted() }
        return try JSONSerialization.data(withJSONObject: body)
    }
}
