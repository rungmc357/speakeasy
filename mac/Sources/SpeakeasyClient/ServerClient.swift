import Foundation
import SpeakeasyCore

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
    public func status() async throws -> ServerStatus { try JSONDecoder().decode(ServerStatus.self, from: try await data("/voice/status")) }
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
    func dismissReview(runID: String, cards: [Int]? = nil) async throws {
        _ = try await data("/voice/reviews/\(escape(runID))/dismiss", method: "POST", body: try reviewDismissBody(cards: cards))
    }

    /// `POST /voice/pair` (no auth): trade a one-time code for a device token.
    public static func pair(server: URL, code: String, deviceName: String) async throws -> PairResponse {
        guard let base = trustedServerBaseURL(server.absoluteString) else {
            throw HTTPError(status: 0, message: "That server address isn't allowed. Use this Mac (127.0.0.1) or an https Tailscale address.")
        }
        var request = URLRequest(url: base.appendingPathComponent("voice/pair"), timeoutInterval: 20)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try pairRequestBody(code: code, deviceName: deviceName)
        let (data, response) = try await URLSession(configuration: .ephemeral).data(for: request)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let fallback = status == 403 || status == 400 ? "That code is wrong or expired. Run hermes voice pair for a new one."
                : "Pairing failed (HTTP \(status))"
            throw HTTPError(status: status, message: object?["error"] as? String ?? fallback)
        }
        return try JSONDecoder().decode(PairResponse.self, from: data)
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

    public func work(runID: String?) async throws -> WorkInfo? {
        let object = try await json(runID.map { "/voice/work/\(escape($0))" } ?? "/voice/work/latest")
        return WorkInfo(json: object["work"])
    }

    public func image(runID: String, index: Int) async throws -> Data {
        guard (1...8).contains(index) else { throw HTTPError(status: 400, message: "Invalid card") }
        let (data, response) = try await session.data(for: request("/voice/card-image/\(escape(runID))/\(index)"))
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
    public func startJob(text: String, clientID: String = UUID().uuidString) async throws -> String? {
        guard let clean = TaskMessageRequest.clean(text) else { throw HTTPError(status: 400, message: "The job is empty.") }
        let reply = try await post("/voice/work", ["text": clean, "source": "typed", "client_id": clientID])
        guard reply["ok"] as? Bool == true else {
            throw HTTPError(status: 502, message: reply["error"] as? String ?? "Your Hermes didn't start that job.")
        }
        return reply["run_id"] as? String
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
                            interactionID: String? = nil, choiceTaskID: String? = nil) async throws -> TaskMessageReply {
        try await sendMessage(TaskMessageRequest(runID: runID, text: text, source: source, clientID: clientID,
                                                 interactionID: interactionID, choiceTaskID: choiceTaskID))
    }

    /// A turn typed on the call screen (plugins with `input_text: true`). `text` must already be
    /// `TypedText.check(...)`'s `.ok` value. True only when the server says it took it (`sent: true`);
    /// refusals throw `HTTPError` with the plugin's reason (400 invalid_body / empty_text /
    /// text_too_long, 404 unknown_interaction (no reason = a plugin without the route), 409
    /// call_ended / call_paused / not_connected, 502 transport_error, 504 timeout).
    public func inputText(interactionID: String, text: String) async throws -> Bool {
        TypedText.taken(try await post(TypedText.path(interactionID: interactionID), TypedText.body(text)))
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
                                 captureID: String? = nil, app: String? = nil) async throws -> String {
        let headers = attachmentUploadHeaders(kind: kind, mimeType: attachment.mimeType, filename: attachment.filename,
                                              captureID: captureID, app: app)
        // Up to 10 MB over Tailscale: give a file longer than the usual 30 s.
        let timeout: TimeInterval = attachment.data.count > 1_000_000 ? 120 : 30
        let req = request(SharingRoute.attachments(interactionID), method: "POST", headers: headers, timeout: timeout)
        let (data, response) = try await sessionFor(timeout).upload(for: req, from: attachment.data)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else { throw HTTPError(status: status, body: data, fallback: "Upload HTTP \(status)") }
        guard let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let id = object["id"] as? String, !id.isEmpty else {
            throw HTTPError(status: 502, message: "Server returned an invalid attachment")
        }
        return id
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

    private func escape(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-_"))) ?? id
    }

    // MARK: - Event stream

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
