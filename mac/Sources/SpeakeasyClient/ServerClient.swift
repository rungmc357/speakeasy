import Foundation
import SpeakeasyCore

extension ClientIdentity {
    /// This app's `X-Speakeasy-Client` value: platform, OS version, app version + build, device class.
    public static let current: String? = {
        let info = Bundle.main.infoDictionary ?? [:]
        let version = info["CFBundleShortVersionString"] as? String ?? "dev"
        let build = info["CFBundleVersion"] as? String ?? "0"
        let os = ProcessInfo.processInfo.operatingSystemVersion
        #if os(visionOS)
        return header(platform: "visionOS", osVersion: os, appVersion: version, build: build, deviceClass: "Apple Vision Pro")
        #elseif os(iOS)
        let model = deviceClass(fromMachine: ProcessInfo.processInfo.environment["SIMULATOR_MODEL_IDENTIFIER"] ?? machine())
        let kind = ["iPhone", "iPad", "iPod"].contains(model) ? model : "iOS device"
        return header(platform: kind == "iPad" ? "iPadOS" : "iOS", osVersion: os, appVersion: version, build: build,
                      deviceClass: kind)
        #else
        return header(platform: "macOS", osVersion: os, appVersion: version, build: build, deviceClass: "Mac")
        #endif
    }()

    /// Sets the header on a request to the plugin (all of them: authenticated routes, pairing, probes).
    public static func tag(_ request: inout URLRequest) {
        if let value = current { request.setValue(value, forHTTPHeaderField: headerName) }
    }

    #if os(iOS)
    private static func machine() -> String {
        var size = 0
        sysctlbyname("hw.machine", nil, &size, nil, 0)
        guard size > 0 else { return "" }
        var bytes = [CChar](repeating: 0, count: size)
        sysctlbyname("hw.machine", &bytes, &size, nil, 0)
        return String(cString: bytes)
    }
    #endif
}

/// Authenticated api HTTP client. The device bearer is attached only to URLs
/// under the trusted api origin re-validated at request time.
public final class ServerClient: @unchecked Sendable {
    public struct HTTPError: LocalizedError {
        public let status: Int
        public let message: String
        /// The api's short reason code when it gives one (`too_many`, `ended`, `closed`, …).
        public let reason: String?
        public init(status: Int, message: String, reason: String? = nil) {
            self.status = status; self.message = message; self.reason = reason
        }
        public var errorDescription: String? { message }

        /// Builds the error from an api error body `{error, reason?}`.
        init(status: Int, body: Data, fallback: String) {
            let object = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
            self.init(status: status, message: object?["error"] as? String ?? fallback, reason: object?["reason"] as? String)
        }
    }

    public let base: URL
    private let token: String
    private let session: URLSession

    public init?(config: AppConfig) {
        guard let token = config.deviceToken, let server = config.serverURL,
              let trusted = trustedServerBaseURL(server.absoluteString) else { return nil }
        self.base = trusted
        self.token = token
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = 30
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        session = URLSession(configuration: configuration)
    }

    /// A long request (Suggest channels waits on a Hermes run) needs its own session: the shared
    /// one gives up after 30 s of silence.
    private func sessionFor(_ timeout: TimeInterval) -> URLSession {
        guard timeout > 30 else { return session }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = timeout
        configuration.requestCachePolicy = .reloadIgnoringLocalCacheData
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        return URLSession(configuration: configuration)
    }

    public func url(_ path: String) -> URL {
        var text = base.absoluteString
        while text.hasSuffix("/") { text.removeLast() }
        return URL(string: text + path)!
    }

    public func request(_ path: String, method: String = "GET", body: Data? = nil,
                 headers: [String: String] = [:], timeout: TimeInterval = 30) -> URLRequest {
        var request = URLRequest(url: url(path), timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        ClientIdentity.tag(&request)
        if body != nil { request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        for (key, value) in headers { request.setValue(value, forHTTPHeaderField: key) }
        request.httpBody = body
        return request
    }

    /// The relay call's audio socket on this server (Grok Voice), with the same bearer as every
    /// other call to it. Nil when the address can't be made into ws/wss.
    public func relaySocketRequest(_ relay: RelayTransport) -> URLRequest? {
        guard let url = relay.socketURL(base: base) else { return nil }
        var request = URLRequest(url: url, timeoutInterval: 30)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        ClientIdentity.tag(&request)
        return request
    }

    /// Raw response for the Speakeasy settings routes (typed decoding lives in the core).
    public func data(_ path: String, method: String = "GET", body: Data? = nil, timeout: TimeInterval = 30) async throws -> Data {
        let (data, response) = try await sessionFor(timeout).data(for: request(path, method: method, body: body, timeout: timeout))
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            throw HTTPError(status: status, body: data, fallback: "Server HTTP \(status)")
        }
        return data
    }

    // MARK: Speakeasy server routes

    public func settings() async throws -> ServerSettings { try ServerSettings.decode(try await data("/voice/settings")) }
    public func saveSettings(_ settings: ServerSettings) async throws -> ServerSettings {
        let out = try await data("/voice/settings", method: "PATCH", body: try settings.patchBody())
        return (try? ServerSettings.decode(out)) ?? settings
    }
    /// Sign in to ChatGPT (CodexLogin.swift): start Codex's device-code login on the Hermes computer.
    public func startCodexLogin(switchFromAPIKey: Bool = false) async throws -> CodexLoginState {
        let body: [String: Any] = switchFromAPIKey ? ["switch_from_api_key": true] : [:]
        let object = try await post("/voice/codex/login", body)
        guard let state = CodexLoginState(json: object) else { throw HTTPError(status: 502, message: "Unexpected sign-in answer") }
        return state
    }
    public func codexLoginState() async throws -> CodexLoginState {
        guard let state = CodexLoginState(json: try await json("/voice/codex/login")) else {
            throw HTTPError(status: 502, message: "Unexpected sign-in answer")
        }
        return state
    }
    /// Add key (OpenAIKey.swift): send the OpenAI API key to `POST /voice/openai-key`. The body bytes
    /// are zeroed after the request; the reply never carries the key. Returns whether a key is now set.
    @discardableResult
    public func saveOpenAIKey(_ value: String) async throws -> Bool {
        var bytes = SecureInputRequest.answerBody(value.trimmingCharacters(in: .whitespacesAndNewlines))
        var body = Data(bytes)
        defer { for i in bytes.indices { bytes[i] = 0 }; body.resetBytes(in: 0..<body.count) }
        let out = try await data("/voice/openai-key", method: "POST", body: body, timeout: 20)
        let object = (try? JSONSerialization.jsonObject(with: out)) as? [String: Any]
        return (object?["api_key_set"] as? Bool) ?? true
    }
    public func cancelCodexLogin() async throws {
        _ = try await json("/voice/codex/login", method: "DELETE")
    }
    public func status() async throws -> ServerStatus { try JSONDecoder().decode(ServerStatus.self, from: try await data("/voice/status")) }
    /// The Setup check rows (SetupCheck.swift); nil from a plugin without the route (404).
    public func setupCheck() async throws -> SetupCheck? {
        do {
            return SetupCheck(json: try await json("/voice/setup-check"))
        } catch let error as HTTPError where error.status == 404 {
            return nil
        }
    }
    /// A Setup check row's plugin-side fix (`kind: action`); answers the fresh rows.
    public func fixSetupCheck(_ rowID: String) async throws -> SetupCheck? {
        let safe = rowID.filter { $0.isLetter || $0 == "_" }
        return SetupCheck(json: try await post("/voice/setup-check/\(safe)", [:]))
    }
    public func brief() async throws -> VoiceBrief { try JSONDecoder().decode(VoiceBrief.self, from: try await data("/voice/brief")) }
    public func saveBrief(_ text: String) async throws -> VoiceBrief? {
        try? JSONDecoder().decode(VoiceBrief.self, from: try await data("/voice/brief", method: "PUT", body: try VoiceBrief.putBody(text: text)))
    }
    public func rewriteBrief() async throws -> VoiceBrief? {
        try? JSONDecoder().decode(VoiceBrief.self, from: try await data("/voice/brief/rewrite", method: "POST", body: Data("{}".utf8)))
    }
    public func tune() async throws -> BriefTune { try JSONDecoder().decode(BriefTune.self, from: try await data("/voice/brief/tune")) }
    public func startTune() async throws -> BriefTune {
        try JSONDecoder().decode(BriefTune.self, from: try await data("/voice/brief/tune", method: "POST", body: Data("{}".utf8)))
    }
    public func applyTune(accept: [String]) async throws -> VoiceBrief? {
        try? JSONDecoder().decode(VoiceBrief.self, from: try await data("/voice/brief/tune/apply", method: "POST",
                                                                      body: try BriefTune.applyBody(accept: accept)))
    }
    public func dismissTune() async throws {
        _ = try await data("/voice/brief/tune/dismiss", method: "POST", body: Data("{}".utf8))
    }

    // MARK: AI thread titles

    /// Past threads still under their stand-in label (empty on an older plugin: no route yet).
    public func pendingThreadTitles() async throws -> [ThreadTitleCandidate] {
        let raw = try await data("/voice/titles", timeout: 15)
        return ThreadTitles.decodePending(raw) ?? []
    }

    /// Persists one on-device generated title; the server refines the thread's job name too.
    public func renameThread(taskID: String, title: String) async throws {
        try await data("/voice/titles", method: "POST",
                       body: try ThreadTitles.renameBody(taskID: taskID, title: title))
    }
    public func destinations() async throws -> [Destination] { Destination.list(try await data("/voice/destinations")) }
    public func destinationsWithSuggestion() async throws -> ([Destination], String?) {
        let raw = try await data("/voice/destinations")
        return (Destination.list(raw), Destination.suggested(raw))
    }
    public func routingChoices() async throws -> RoutingChoices { try RoutingChoices.decode(try await data("/voice/routing")) }
    public func routingModels(_ provider: String) async throws -> [String] {
        let query = provider.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? provider
        return try ProviderModels.decode(try await data("/voice/routing/models?provider=\(query)", timeout: 60)).models
    }
    public func chooseRouting(provider: String, model: String, thinking: Bool?) async throws -> RoutingChoices {
        try RoutingChoices.decode(try await data("/voice/routing", method: "POST",
                                                 body: try RoutingChoices.postBody(provider: provider, model: model, thinking: thinking)))
    }
    public func homeControl() async throws -> HomeControlInfo {
        try HomeControlInfo.decode(try await data("/voice/home", timeout: 30))
    }
    public func setHomeControl(enabled: Bool? = nil, entities: [String]? = nil) async throws -> HomeControlInfo {
        try HomeControlInfo.decode(try await data("/voice/home", method: "PUT",
                                                  body: try HomeControlInfo.putBody(enabled: enabled, entities: entities), timeout: 30))
    }
    public func onboarding() async throws -> OnboardingStatus { OnboardingStatus.parse(try await data("/voice/onboarding")) }
    public func completeOnboarding(assistantName: String, userName: String, target: String?, continuity: Bool?) async throws {
        _ = try await data("/voice/onboarding", method: "POST", body: try OnboardingStatus.postBody(
            assistantName: assistantName, userName: userName, target: target, continuity: continuity))
    }
    /// One read-only Hermes run (up to ~90 s) that proposes delivery channels; nothing is saved.
    public func suggestChannels() async throws -> [ServerSettings.Channel] {
        ChannelSuggestions.parse(try await data("/voice/destinations/suggest", method: "POST", body: Data("{}".utf8), timeout: 120))
    }

    /// Approve / deny / revise an email draft. Throws `DraftActionError.changed` on 409.
    public func draftAction(_ action: DraftAction, draft: EmailDraft, instructions: String?) async throws {
        do {
            _ = try await data("/voice/drafts/\(escape(draft.draftID))", method: "POST",
                               body: try draftActionBody(action, draft: draft, instructions: instructions))
        } catch let error as HTTPError where error.status == 409 {
            throw DraftActionError.changed
        }
    }

    /// Dismiss a finished task's image review card (all its images, or the listed card numbers).
    /// `POST /voice/tasks/{run_id}/seen`: the user opened (or dismissed) this answer. It leaves New
    /// answers on every device and the voice won't read it out later. Older plugins answer 404.
    func markAnswerSeen(runID: String) async throws {
        _ = try await data("/voice/tasks/\(escape(runID))/seen", method: "POST", body: Data("{}".utf8))
    }

    func dismissReview(runID: String, cards: [Int]? = nil) async throws {
        _ = try await data("/voice/reviews/\(escape(runID))/dismiss", method: "POST", body: try reviewDismissBody(cards: cards))
    }

    /// `GET /voice/endpoints`: every address of this Hermes (nil on servers without it).
    public func endpoints() async -> EndpointsResponse? {
        guard let data = try? await data("/voice/endpoints") else { return nil }
        return EndpointsResponse.decode(data)
    }

    /// Is `base` the same Hermes this device paired with? First `POST /voice/prove` (no token sent:
    /// the server answers an HMAC keyed by the token's hash over a fresh nonce and the host), then,
    /// only if that matches, an authenticated `GET /voice/status` with the existing token.
    public static func verifySameHermes(base: URL, deviceID: String, token: String,
                                        session: URLSession = URLSession(configuration: .ephemeral)) async -> Bool {
        guard let trusted = trustedServerBaseURL(base.absoluteString), let host = trusted.host else { return false }
        let nonce = ServerProof.newNonce()
        var prove = URLRequest(url: trusted.appendingPathComponent("voice/prove"), timeoutInterval: 10)
        prove.httpMethod = "POST"
        prove.setValue("application/json", forHTTPHeaderField: "Content-Type")
        prove.httpBody = try? JSONSerialization.data(withJSONObject: ["device_id": deviceID, "nonce": nonce])
        guard let (data, response) = try? await session.data(for: prove),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let proof = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["proof"] as? String,
              ServerProof.matches(proof, ServerProof.expected(token: token, nonce: nonce, host: host)) else { return false }
        var status = URLRequest(url: trusted.appendingPathComponent("voice/status"), timeoutInterval: 10)
        status.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        ClientIdentity.tag(&status)
        guard let (_, statusResponse) = try? await session.data(for: status) else { return false }
        return (statusResponse as? HTTPURLResponse)?.statusCode == 200
    }

    /// Can this device use `base` right now (authenticated `GET /voice/status`, short timeout)?
    /// Only for addresses this Hermes itself reported (or the paired one).
    public static func reachable(base: URL, token: String, timeout: TimeInterval = 8,
                                 session: URLSession = URLSession(configuration: .ephemeral)) async -> Bool {
        guard let trusted = trustedServerBaseURL(base.absoluteString) else { return false }
        var request = URLRequest(url: trusted.appendingPathComponent("voice/status"), timeoutInterval: timeout)
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        ClientIdentity.tag(&request)
        guard let (_, response) = try? await session.data(for: request) else { return false }
        return (response as? HTTPURLResponse)?.statusCode == 200
    }

    /// The newest address note on the relay that is newer than `after` (nil: none, or unreadable).
    public static func latestAddressNote(_ drop: AddressDrop, after lastSeq: Int,
                                         session: URLSession = URLSession(configuration: .ephemeral)) async -> AddressNote? {
        guard let url = AddressNote.pollURL(drop) else { return nil }
        guard let (data, response) = try? await session.data(for: URLRequest(url: url, timeoutInterval: 10)),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return AddressNote.newest(inPoll: data, drop: drop, after: lastSeq)
    }

    /// `POST /voice/pair` (no auth): trade a one-time code for a device token.
    public static func pair(server: URL, code: String, deviceName: String) async throws -> PairResponse {
        guard let base = trustedServerBaseURL(server.absoluteString) else {
            throw HTTPError(status: 0, message: "That server address isn't allowed. Use this Mac (127.0.0.1), an https Tailscale address or Speakeasy's Cloudflare address.")
        }
        var request = URLRequest(url: base.appendingPathComponent("voice/pair"), timeoutInterval: 20)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        ClientIdentity.tag(&request)
        request.httpBody = try pairRequestBody(code: code, deviceName: deviceName)
        let (data, response) = try await URLSession(configuration: .ephemeral).data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            // The server's own words and the status go to the log only; the person sees a plain line.
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            NSLog("Speakeasy pairing: HTTP %d %@", status, (object?["error"] as? String) ?? "")
            throw HTTPError(status: status, message: PlainFailure.pairing(status: status))
        }
        return try JSONDecoder().decode(PairResponse.self, from: data)
    }

    /// A GET whose body isn't JSON (files, thumbnails): the bytes and the response, any status.
    public func raw(_ path: String, timeout: TimeInterval = 30) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request(path, timeout: timeout))
        guard let http = response as? HTTPURLResponse else { throw HTTPError(status: 502, message: "No reply") }
        return (data, http)
    }

    public func json(_ path: String, method: String = "GET", body: Data? = nil,
              headers: [String: String] = [:]) async throws -> [String: Any] {
        let (data, response) = try await session.data(for: request(path, method: method, body: body, headers: headers))
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        guard (200..<300).contains(status) else {
            throw HTTPError(status: status, message: object["error"] as? String ?? "Server HTTP \(status)",
                            reason: object["reason"] as? String)
        }
        return object
    }

    public func post(_ path: String, _ object: [String: Any], headers: [String: String] = [:]) async throws -> [String: Any] {
        try await json(path, method: "POST", body: try JSONSerialization.data(withJSONObject: object), headers: headers)
    }

    /// POST the SDP offer byte-for-byte (the body encoder never trims it). `screen` is this client's
    /// sharing declaration, sent only to plugins that advertise attachments.
    public func admitSession(sdp: String, idempotencyKey: String, resumeFrom: String? = nil,
                      tour: [String: String]? = nil, screen: ScreenDeclaration? = nil,
                      room: String? = nil) async throws -> SessionAdmission {
        let body = try sessionRequestBody(sdp: sdp, resumeFrom: resumeFrom, tour: tour, screen: screen, room: room)
        let object = try await json("/voice/sessions", method: "POST", body: body,
                                    headers: ["Idempotency-Key": idempotencyKey])
        guard let admission = SessionAdmission(json: object) else {
            throw HTTPError(status: 502, message: "Server returned an invalid session answer")
        }
        return admission
    }

    public func interaction(_ id: String) async throws -> InteractionSnapshot? {
        InteractionSnapshot(json: try await json("/voice/interactions/\(escape(id))"))
    }

    /// The Codex data channel closes before its app-server thread stops.
    public func finishCodexTransport(interactionID: String) async throws {
        _ = try await post("/voice/interactions/\(escape(interactionID))/end", [:])
    }

    /// Close a session this device gave up on before it went live (a stalled connection attempt):
    /// the plugin stops its voice session so it isn't left open beside the retried one.
    public func abandonSession(interactionID: String) async throws {
        _ = try await post("/voice/interactions/\(escape(interactionID))/end", ["abandon": true])
    }

    public func work(runID: String?) async throws -> WorkInfo? {
        let object = try await json(runID.map { "/voice/work/\(escape($0))" } ?? "/voice/work/latest")
        return WorkInfo(json: object["work"])
    }

    /// *hero*: for a wide contact sheet, its readable crop (plugin 0.3.47+; an older one ignores it).
    public func image(runID: String, index: Int, hero: Bool = false) async throws -> Data {
        guard (1...8).contains(index) else { throw HTTPError(status: 400, message: "Invalid card") }
        let path = "/voice/card-image/\(escape(runID))/\(index)" + (hero ? "?v=hero" : "")
        let (data, response) = try await session.data(for: request(path))
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              http.mimeType?.hasPrefix("image/") == true, !data.isEmpty, data.count <= 8_000_000 else {
            throw HTTPError(status: (response as? HTTPURLResponse)?.statusCode ?? 502, message: "Image unavailable")
        }
        return data
    }

    /// The latest image a running task produced or is looking at (authenticated, vetted server-side).
    func liveImage(runID: String) async throws -> Data {
        let (data, response) = try await session.data(for: request("/voice/live-image/\(escape(runID))"))
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              http.mimeType?.hasPrefix("image/") == true, !data.isEmpty, data.count <= 8_000_000 else {
            throw HTTPError(status: (response as? HTTPURLResponse)?.statusCode ?? 502, message: "Image unavailable")
        }
        return data
    }

    /// Latest work plus every task of the most recent call (after-call Work view).
    public func latestWork() async throws -> (WorkInfo?, [TaskItem]) {
        let object = try await json("/voice/work/latest")
        return (WorkInfo(json: object["work"]), TaskItem.list(json: object["tasks"]) ?? [])
    }

    /// "Not needed": set aside a task's waiting question, decision or approval without answering it.
    public func setAside(runID: String) async throws {
        _ = try await post("/voice/tasks/\(escape(runID))/set-aside", [:])
    }

    /// ✕ on a decision card (servers with `question_actions`): close the task's question on every
    /// device without telling the agent anything. `undo` (the toast's Undo) brings it back.
    public func dismissQuestion(runID: String, undo: Bool = false) async throws {
        _ = try await post("/voice/tasks/\(escape(runID))/question/\(undo ? "undo" : "dismiss")", [:])
    }

    /// A tapped option or typed answer to one task's question, by run id alone (servers with
    /// `question_state`): it reaches the task with or without a call. Idempotent on `clientID`.
    public func answerQuestion(runID: String, text: String, clientID: String) async throws {
        _ = try await post("/voice/tasks/\(escape(runID))/question/answer", ["text": text, "client_id": clientID])
    }

    /// Clear finished tasks from the task list (running tasks are never cleared).
    public func dismissTasks(runIDs: [String]) async throws {
        _ = try await post("/voice/tasks/dismiss", ["run_ids": runIDs])
    }

    /// Hold the call's conversation and tasks for Resume before the session closes.
    public func pause(interactionID: String) async throws {
        _ = try await json("/voice/interactions/\(escape(interactionID))/pause", method: "POST", body: Data("{}".utf8))
    }

    /// Skip button during the first-call tour: the voice drops it.
    public func skipTour(interactionID: String) async throws {
        _ = try await json("/voice/interactions/\(escape(interactionID))/skip-tour", method: "POST", body: Data("{}".utf8))
    }

    /// A tapped answer on a task's question card; it reaches the task as a follow-up.
    /// Runs a card's action (contract §2): in a call through the interaction, else on the run.
    /// Returns the server's short message for a toast.
    public func cardAction(interactionID: String?, runID: String, view: Int, action: String, value: Any?) async throws -> String? {
        var body: [String: Any] = ["view": view, "action": action]
        if let value { body["value"] = value }
        let reply: [String: Any]
        if let interactionID {
            body["run_id"] = runID
            reply = try await post("/voice/interactions/\(escape(interactionID))/card-action", body)
        } else {
            reply = try await post("/voice/work/\(escape(runID))/card-action", body)
        }
        return reply["message"] as? String
    }

    public func answer(interactionID: String, taskID: String, text: String) async throws {
        _ = try await post("/voice/interactions/\(escape(interactionID))/answer", ["task_id": taskID, "text": text])
    }

    /// A new job from typed text, no call needed (servers with `start_jobs`); idempotent on `clientID`.
    /// Returns the job's run id (openable on the board) when Hermes has one yet.
    /// `attachments`: ids staged with `stageAttachment` (servers with `attachments`); with any, `text` may be empty.
    /// `choice`: servers with `model_choice`: the model / level for the job (and the chat it becomes).
    public func startJob(text: String, attachments: [String] = [], clientID: String = UUID().uuidString,
                         choice: ModelChoice? = nil) async throws -> String? {
        let clean = TaskMessageRequest.clean(text)
        guard clean != nil || !attachments.isEmpty else { throw HTTPError(status: 400, message: "The job is empty.") }
        var body: [String: Any] = ["text": clean ?? "", "source": "typed", "client_id": clientID]
        if !attachments.isEmpty { body["attachments"] = attachments }
        if let choice, !choice.isEmpty { body.merge(choice.body()) { _, new in new } }
        let reply = try await post("/voice/work", body)
        guard reply["ok"] as? Bool == true else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't start that job.")
        }
        return reply["run_id"] as? String
    }

    // MARK: Agents (servers with `agents`; docs/API.md "Agents")

    /// The agent picker's list, the saved allowlist and every profile that could be allowed.
    public func agents() async throws -> AgentRoster {
        AgentRoster(json: try await json(AgentRoster.path))
    }

    /// Settings › Agents: which Hermes profiles new chats may use.
    public func setAllowedAgents(_ ids: [String]) async throws {
        _ = try await json("/voice/settings", method: "PATCH",
                           body: try JSONSerialization.data(withJSONObject: AgentRoster.allowedPatch(ids)))
    }

    // MARK: Skills (servers with `slash_skills`; docs/API.md "Skills")

    /// The skills a "/" in the message box offers.
    public func slashSkills() async throws -> [SlashSkill] {
        let reply = try await json("/voice/skills")
        return ((reply["skills"] as? [[String: Any]]) ?? []).compactMap(SlashSkill.init(json:))
    }

    // MARK: Models (servers with `model_choice`; docs/API.md "Models")

    /// The models Hermes offers, the default and the fast/smart presets.
    public func models(refresh: Bool = false) async throws -> ModelCatalog {
        ModelCatalog(json: try await json(ModelCatalog.path + (refresh ? "?refresh=1" : "")))
    }

    /// Use for this chat. Returns the chat's next model as the server now sees it.
    @discardableResult
    public func setSessionModel(_ request: SessionModelRequest) async throws -> ModelBadge? {
        let reply = try await json(request.path, method: "PUT", body: try JSONSerialization.data(withJSONObject: request.body))
        return ModelBadge(json: reply["next_model"])
    }

    /// Settings › Jobs: Speakeasy's default model and reasoning.
    /// This Hermes' approval rules in plain words, read-only (servers with `approval_rules`).
    public func approvalRules() async throws -> ApprovalRules {
        guard let rules = ApprovalRules(json: try await json(ApprovalRules.path)) else {
            throw HTTPError(status: 0, message: "Speakeasy couldn't read the approval rules.")
        }
        return rules
    }

    public func jobModelDefaults() async throws -> JobModelDefaults {
        JobModelDefaults(settingsJSON: try await json("/voice/settings"))
    }

    public func setJobModelDefaults(_ defaults: JobModelDefaults) async throws {
        _ = try await json("/voice/settings", method: "PATCH", body: try JSONSerialization.data(withJSONObject: defaults.patchBody))
    }

    /// A New chat: `startJob`, plus the Hermes session it runs in (plugins from 0.3.34 reply with
    /// `session_id` at once; nil from older ones, which the caller then looks up by run id).
    public func startChatSession(text: String, attachments: [String] = [], clientID: String = UUID().uuidString,
                                 choice: ModelChoice? = nil, agent: String? = nil) async throws -> (runID: String?, sessionID: String?) {
        let clean = TaskMessageRequest.clean(text)
        guard clean != nil || !attachments.isEmpty else { throw HTTPError(status: 400, message: "The job is empty.") }
        var body: [String: Any] = ["text": clean ?? "", "source": "typed", "client_id": clientID]
        if !attachments.isEmpty { body["attachments"] = attachments }
        if let agent {
            body["agent"] = agent   // another agent runs on its own model: no model fields
        } else if let choice, !choice.isEmpty { body.merge(choice.body()) { _, new in new } }
        let reply = try await post("/voice/work", body)
        guard reply["ok"] as? Bool == true else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't start that job.")
        }
        let session = (reply["session_id"] as? String) ?? ((reply["task"] as? [String: Any])?["session_id"] as? String)
        return (reply["run_id"] as? String, session)
    }

    // MARK: History (servers with `history`; docs/API.md "History")

    /// Home's chat (servers with `home_chat`): the coordinator replies, and starts or follows up a job
    /// only for real work. Idempotent on *clientID*.
    public func homeMessage(text: String, attachments: [String] = [], clientID: String = UUID().uuidString,
                            choice: ModelChoice? = nil, replyTo: String? = nil) async throws -> HomeChatReply {
        var body: [String: Any] = ["text": text.trimmingCharacters(in: .whitespacesAndNewlines), "client_id": clientID]
        if let replyTo { body["reply_to"] = replyTo }
        if !attachments.isEmpty { body["attachments"] = attachments }
        if let choice, !choice.isEmpty { body.merge(choice.body()) { _, new in new } }
        let reply = try await post("/voice/home/message", body)
        guard let parsed = HomeChatReply(json: reply) else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't take that message.")
        }
        return parsed
    }

    /// Home chat's model (servers with `home_chat`).
    public func homeModel() async throws -> HomeModelStatus {
        HomeModelStatus(json: try await json("/voice/home/model"))
    }

    /// The coordinator's model (the secondary row in Home's model menu); nil goes back to Auto.
    public func setHomeModel(_ id: String?) async throws -> HomeModelStatus {
        HomeModelStatus(json: try await post("/voice/home/model", ["model": id.map { $0 as Any } ?? NSNull()]))
    }

    /// Home's chip: the model jobs started from Home run on; nil goes back to the Hermes default.
    public func setHomeWorkModel(_ id: String?) async throws -> HomeModelStatus {
        HomeModelStatus(json: try await post("/voice/home/model", ["work_model": id.map { $0 as Any } ?? NSNull()]))
    }

    /// One page of every Hermes session, newest first.
    public func history(query: String = "", source: String? = nil, cursor: String? = nil) async throws -> HistoryPage {
        HistoryPage(json: try await json(HistoryPage.path(query: query, source: source, cursor: cursor)))
    }

    /// One session's conversation, its Speakeasy jobs and the approvals waiting on the user.
    public func historySession(_ sessionID: String, before: Int? = nil) async throws -> HistoryConversation {
        guard let parsed = HistoryConversation(json: try await json(HistoryConversation.path(sessionID, before: before))) else {
            throw HTTPError(status: 502, message: "Your Hermes sent a conversation this app can't read.")
        }
        return parsed
    }

    /// Text (typed, or a voice message transcribed on this device) to one session, by id.
    public func sendToSession(_ message: HistoryMessageRequest) async throws -> HistoryMessageReply {
        let reply = try await post(message.path, message.body)
        guard let parsed = HistoryMessageReply(json: reply) else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't take that message.")
        }
        return parsed
    }

    /// Cancel a message the server holds for when the session's task finishes (`message_modes`).
    public func cancelQueued(sessionID: String, clientID: String) async throws {
        _ = try await json(QueuedMessage.path(session: sessionID, clientID: clientID), method: "DELETE")
    }

    /// Send a held message now, as a steer (`message_modes`).
    public func steerQueued(sessionID: String, clientID: String) async throws -> TaskMessageReply? {
        TaskMessageReply(json: try await post(QueuedMessage.path(session: sessionID, clientID: clientID) + "/steer", [:]))
    }

    /// Answer the approval one of the session's Speakeasy tasks waits on (`once`, `session`, `deny`).
    public func answerSessionApproval(sessionID: String, runID: String, choice: String) async throws {
        _ = try await post(HistoryConversation.path(sessionID) + "/approval", ["run_id": runID, "choice": choice])
    }

    /// Text to one task by run id (servers with `task_messages`): a steer while it runs, the answer
    /// while it waits, a linked follow-up once it finished. Idempotent on `client_id`.
    public func sendMessage(_ message: TaskMessageRequest) async throws -> TaskMessageReply {
        let reply = try await post(message.path, message.body)
        guard let parsed = TaskMessageReply(json: reply) else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't take that message.")
        }
        return parsed
    }

    public func sendMessage(runID: String, text: String, source: TaskMessageSource = .typed, clientID: String,
                            interactionID: String? = nil, choiceTaskID: String? = nil,
                            attachments: [String] = []) async throws -> TaskMessageReply {
        try await sendMessage(TaskMessageRequest(runID: runID, text: text, source: source, clientID: clientID,
                                                 interactionID: interactionID, choiceTaskID: choiceTaskID,
                                                 attachments: attachments))
    }

    /// A turn typed on the call screen (plugins with `input_text: true`). `text` must already be
    /// `TypedText.check(...)`'s `.ok` value. True only when the server says it took it (`sent: true`);
    /// refusals throw `HTTPError` with the plugin's reason (400 invalid_body / empty_text /
    /// text_too_long, 404 unknown_interaction (no reason = a plugin without the route), 409
    /// call_ended / call_paused / not_connected, 502 transport_error, 504 timeout).
    public func inputText(interactionID: String, text: String) async throws -> Bool {
        TypedText.taken(try await post(TypedText.path(interactionID: interactionID), TypedText.body(text)))
    }

    /// Stop one task by id, with or without a call (plugins with `task_stop`): a panel row's Stop.
    public func stopTask(runID: String) async throws {
        _ = try await post("/voice/work/\(escape(runID))/stop", [String: Any]())
    }

    /// Stop the newest of a session's tasks that is still running (409 = that one wasn't; try the next).
    public func stopSession(runIDs: [String]) async throws {
        var last: Error?
        for run in runIDs.reversed() {
            do { try await stopTask(runID: run); return } catch let e as HTTPError where e.status == 409 { last = e }
        }
        if let last { throw last }
    }

    public func cancelBackend(interactionID: String, runID: String) async throws {
        _ = try await post("/voice/interactions/\(escape(interactionID))/cancel-backend", ["run_id": runID])
    }

    public func resolveApproval(interactionID: String, approval: ApprovalInfo, choice: String) async throws {
        precondition(choice == "once" || choice == "deny")
        _ = try await post("/voice/interactions/\(escape(interactionID))/approval",
                           ["run_id": approval.runID, "request_id": approval.requestID, "choice": choice])
    }

    // MARK: Look at this: screen sharing, pictures and files

    /// The screen button: `{on, seq}` to the call. Returns the plugin's state in force (its seq can be
    /// newer than the one sent; an older one changes nothing). 409 reasons: ended, permission, not_declared.
    public func setScreen(interactionID: String, _ toggle: ScreenToggle) async throws -> ScreenToggle {
        let object = try await json(SharingRoute.screen(interactionID), method: "POST", body: try toggle.body())
        guard let state = ScreenToggle(json: object) else { throw HTTPError(status: 502, message: "Server returned an invalid screen state") }
        return state
    }

    /// Answers a capture request: `capturing` first (410 = closed: don't capture), `uploading` while a
    /// capture is on its way, or `failed` with the reason. Throws `HTTPError` 410 once it has closed.
    public func reportCapture(interactionID: String, captureID: String, _ report: CaptureReport) async throws {
        _ = try await json(SharingRoute.capture(interactionID, captureID), method: "POST", body: try report.body())
    }

    /// Uploads a screen capture (`captureID` and `app`), a picture or a file as the raw body; returns
    /// the plugin's attachment id (the same one again for a repeated capture upload). Refusals are
    /// `HTTPError`s with the plugin's reason: 409 too_many / too_large / ended, 410 closed,
    /// 413 too_large, 415 unsupported.
    public func uploadAttachment(interactionID: String, _ attachment: EncodedAttachment, as kind: AttachmentUploadKind,
                                 captureID: String? = nil, app: String? = nil,
                                 progress: (@Sendable (Double) -> Void)? = nil) async throws -> String {
        let headers = attachmentUploadHeaders(kind: kind, mimeType: attachment.mimeType, filename: attachment.filename,
                                              captureID: captureID, app: app)
        // Up to 10 MB over Tailscale: give a file longer than the usual 30 s.
        let timeout: TimeInterval = attachment.mimeType.hasPrefix("video/") ? 180 : (attachment.data.count > 1_000_000 ? 120 : 30)
        let req = request(SharingRoute.attachments(interactionID), method: "POST", headers: headers, timeout: timeout)
        let (data, response) = try await sessionFor(timeout).upload(for: req, from: attachment.data, delegate: UploadProgress(progress))
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else { throw HTTPError(status: status, body: data, fallback: "Upload HTTP \(status)") }
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let id = object["id"] as? String, !id.isEmpty else {
            throw HTTPError(status: 502, message: "Server returned an invalid attachment")
        }
        return id
    }

    /// Stages a picture (JPEG, already re-encoded) or a file (PDF or text) for a typed message or new
    /// job (servers with `attachments`); returns its id for `TaskMessageRequest.attachments` /
    /// `startJob(attachments:)`. `progress` gets 0...1 as the body goes out. Refusals are `HTTPError`s
    /// with the plugin's reason: 409 too_many, 413 too_large, 415 unsupported.
    public func stageAttachment(_ attachment: EncodedAttachment, as kind: AttachmentUploadKind,
                                progress: (@Sendable (Double) -> Void)? = nil) async throws -> StagedAttachmentReply {
        precondition(kind != .screen, "screen captures go to a call")
        let headers = attachmentUploadHeaders(kind: kind, mimeType: attachment.mimeType, filename: attachment.filename)
        let timeout: TimeInterval = attachment.mimeType.hasPrefix("video/") ? 180 : (attachment.data.count > 1_000_000 ? 120 : 30)
        let req = request(StagedAttachmentRoute.stage, method: "POST", headers: headers, timeout: timeout)
        let delegate = UploadProgress(progress)
        let (data, response) = try await sessionFor(timeout).upload(for: req, from: attachment.data, delegate: delegate)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else { throw HTTPError(status: status, body: data, fallback: "Upload HTTP \(status)") }
        guard let reply = StagedAttachmentReply(json: try? JSONSerialization.jsonObject(with: data)) else {
            throw HTTPError(status: 502, message: "Server returned an invalid attachment")
        }
        progress?(1)
        return reply
    }

    /// The x on a staged chip. 404 = already gone (sent, removed or expired).
    public func removeStagedAttachment(id: String) async throws {
        _ = try await json(StagedAttachmentRoute.remove(id), method: "DELETE")
    }

    /// Recover a staged thumbnail after Share-to-app handoff; bytes stay on the paired path.
    public func stagedAttachmentPreview(id: String) async throws -> Data {
        let req = request(StagedAttachmentRoute.remove(id) + "/preview", method: "GET")
        let (data, response) = try await sessionFor(30).data(for: req)
        guard let response = response as? HTTPURLResponse, response.statusCode == 200,
              data.count <= AttachmentPolicy.maxImageBytes else { throw URLError(.badServerResponse) }
        return data
    }

    /// A recorded voice message's audio, transcribed by Hermes the way a Telegram voice note is
    /// (servers with `transcribe`, plugin 0.3.37+). Returns the text. Errors are `HTTPError`s with
    /// the plugin's reason: 413 too_large, 415 unsupported, 502 failed, 503 unavailable/busy.
    public func transcribeVoice(_ audio: Data, mimeType: String = "audio/mp4") async throws -> String {
        let timeout: TimeInterval = audio.count > 2_000_000 ? 180 : 90
        let req = request(VoiceTranscribeRoute.path, method: "POST", headers: ["Content-Type": mimeType], timeout: timeout)
        let (data, response) = try await sessionFor(timeout).upload(for: req, from: audio)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else { throw HTTPError(status: status, body: data, fallback: "Transcribe HTTP \(status)") }
        guard let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let text = body["text"] as? String else {
            throw HTTPError(status: 502, message: "Server returned no transcript")
        }
        return text
    }

    /// A voice note (servers with `voice_notes`): the recording itself is the message; Hermes
    /// transcribes it on arrival. Idempotent on `clientID`, so Retry never sends it twice.
    public func sendVoiceNote(_ note: VoiceNoteRequest) async throws -> VoiceNoteReply {
        // Hermes transcribes before the message goes: allow for a slow local model.
        let timeout: TimeInterval = note.audio.count > 2_000_000 ? 240 : 150
        let req = request(note.path, method: "POST", headers: note.headers, timeout: timeout)
        let (data, response) = try await sessionFor(timeout).upload(for: req, from: note.audio)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else { throw HTTPError(status: status, body: data, fallback: "Voice message HTTP \(status)") }
        guard let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let reply = VoiceNoteReply(json: body) else {
            throw HTTPError(status: 502, message: "Your Hermes didn't take that voice message.")
        }
        return reply
    }

    /// The ✕ on a pending picture or file. 404 = already gone; 409 reason `sent` = it went with a request.
    public func removeAttachment(interactionID: String, attachmentID: String) async throws {
        _ = try await json(SharingRoute.attachment(interactionID, attachmentID), method: "DELETE")
    }

    /// A screen capture or picture a task carried (`number` from `SharedItem`), through the authenticated api.
    public func sharedImage(runID: String, number: Int) async throws -> Data {
        guard (1...SharedItem.maxItems).contains(number) else { throw HTTPError(status: 400, message: "Invalid picture") }
        let (data, response) = try await session.data(for: request(SharingRoute.sharedImage(runID, number)))
        guard let http = response as? HTTPURLResponse, http.statusCode == 200,
              http.mimeType?.hasPrefix("image/") == true, !data.isEmpty, data.count <= 8_000_000 else {
            throw HTTPError(status: (response as? HTTPURLResponse)?.statusCode ?? 502, message: "Image unavailable")
        }
        return data
    }

    /// Reports an upload's sent fraction (URLSession task delegate).
    private final class UploadProgress: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
        let report: (@Sendable (Double) -> Void)?
        init(_ report: (@Sendable (Double) -> Void)?) { self.report = report }
        func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                        totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
            guard totalBytesExpectedToSend > 0 else { return }
            report?(min(0.99, Double(totalBytesSent) / Double(totalBytesExpectedToSend)))
        }
    }

    private func escape(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-_"))) ?? id
    }

    // MARK: - Event stream

    // MARK: History actions and live changes (plugins from 0.3.34)

    /// Rename a conversation (Hermes' own title: its other apps show it too).
    public func renameSession(_ sessionID: String, title: String) async throws {
        _ = try await post(HistoryConversation.path(sessionID) + "/rename", ["title": title])
    }
    /// Pin or unpin (shared by every device).
    public func pinSession(_ sessionID: String, pinned: Bool) async throws {
        _ = try await post(HistoryConversation.path(sessionID) + "/pin", ["pinned": pinned])
    }
    /// Archive or unarchive (Hermes' own archive: hidden in its other apps too).
    public func archiveSession(_ sessionID: String, archived: Bool) async throws {
        _ = try await post(HistoryConversation.path(sessionID) + "/archive", ["archived": archived])
    }
    /// Mark as read on every device.
    public func markSessionRead(_ sessionID: String) async throws {
        _ = try await post(HistoryConversation.path(sessionID) + "/read", [:])
    }

    /// Reads `GET /voice/history/events` (session_updated, session_archived, decision_resolved,
    /// history_changed, reset) until it ends. `.unsupported` on older plugins: keep polling.
    public func streamHistoryEvents(parser: inout SSEParser, onEvent: (SSEEvent) async -> Void) async -> StreamOutcome {
        var headers = ["Accept": "text/event-stream"]
        if let last = parser.lastEventID { headers["Last-Event-ID"] = last }
        // `now=1`: Home's `now` {rev} events too (plugins without Home ignore the query).
        var req = request("/voice/history/events?now=1", headers: headers, timeout: 3600)
        req.timeoutInterval = 3600
        do {
            let (bytes, response) = try await session.bytes(for: req)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 404 || status == 405 || status == 501 { return .unsupported }
            guard (200..<300).contains(status) else { return .failed(HTTPError(status: status, message: "History stream HTTP \(status)")) }
            var line = Data()
            for try await byte in bytes {
                line.append(byte)
                if byte == 0x0A {
                    let events = parser.append(String(decoding: line, as: UTF8.self))
                    line.removeAll(keepingCapacity: true)
                    for event in events { await onEvent(event) }
                }
                if Task.isCancelled { return .ended }
            }
            return .ended
        } catch {
            if Task.isCancelled { return .ended }
            return .failed(error)
        }
    }

    public enum StreamOutcome { case ended, unsupported, failed(Error) }

    /// Reads `GET /voice/interactions/<id>/events` until the server ends it.
    /// Returns `.unsupported` on 404 so the caller falls back to polling.
    public func streamEvents(interactionID: String, parser: inout SSEParser,
                      onEvent: (SSEEvent) async -> Void) async -> StreamOutcome {
        var headers = ["Accept": "text/event-stream"]
        if let last = parser.lastEventID { headers["Last-Event-ID"] = last }
        var req = request("/voice/interactions/\(escape(interactionID))/events", headers: headers, timeout: 3600)
        req.timeoutInterval = 3600
        do {
            let (bytes, response) = try await session.bytes(for: req)
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 404 || status == 405 || status == 501 { return .unsupported }
            guard (200..<300).contains(status) else { return .failed(HTTPError(status: status, message: "Event stream HTTP \(status)")) }
            var line = Data()
            for try await byte in bytes {
                line.append(byte)
                if byte == 0x0A {
                    let events = parser.append(String(decoding: line, as: UTF8.self))
                    line.removeAll(keepingCapacity: true)
                    for event in events { await onEvent(event) }
                }
                if Task.isCancelled { return .ended }
            }
            return .ended
        } catch {
            if Task.isCancelled { return .ended }
            return .failed(error)
        }
    }
}
