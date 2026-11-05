import Foundation
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// Where history comes from: the paired Hermes, or (screenshots, tests) a demo.
@MainActor
protocol HistoryBackend: AnyObject {
    func page(_ query: HistoryQuery) async throws -> HistoryPage
    func detail(_ sessionID: String) async throws -> HistoryDetail
    func send(_ request: HistoryMessageRequest) async throws -> HistoryMessageReply
    /// New chat; returns the new task's run id (or a session id).
    func startChat(text: String, clientID: String) async throws -> String?
    func answer(_ answer: HistorySendAnswer) async throws
}

@MainActor
final class LiveHistoryBackend: HistoryBackend {
    let api: ServerClient
    init(api: ServerClient) { self.api = api }
    func page(_ query: HistoryQuery) async throws -> HistoryPage { try await api.history(query) }
    func detail(_ sessionID: String) async throws -> HistoryDetail { try await api.historyDetail(sessionID) }
    func send(_ request: HistoryMessageRequest) async throws -> HistoryMessageReply { try await api.sendHistoryMessage(request) }
    func startChat(text: String, clientID: String) async throws -> String? {
        try await api.startChat(text: text, clientID: clientID)
    }
    func answer(_ answer: HistorySendAnswer) async throws { try await api.answerHistorySend(answer) }
}

/// The history window's state: the list, the open session, the composer.
@MainActor
final class HistoryStore: ObservableObject {
    @Published private(set) var list = HistoryList()
    /// nil: nothing open yet.
    @Published private(set) var target: HistoryTarget?
    @Published private(set) var details: [String: HistoryDetail] = [:]
    @Published private(set) var detailErrors: [String: String] = [:]
    @Published private(set) var loadingDetail: Set<String> = []
    @Published private(set) var outbox = HistoryOutbox()
    @Published var searchText = "" { didSet { scheduleSearch() } }
    /// What's typed, per target ("new" for New chat).
    @Published var drafts: [String: String] = [:]
    @Published private(set) var voice = VoiceMessageFlow()
    @Published private(set) var level: Float = 0
    @Published var openSections: Set<String> = []
    @Published private(set) var newChatError: String?
    @Published private(set) var startingChat = false
    @Published private(set) var answering: Set<String> = []
    @Published var answerError: String?
    /// Bumped to move focus (⌘F, ⌘N).
    @Published var focusSearch = 0
    @Published var focusComposer = 0
    /// The server has `capabilities.history`; false shows the update note.
    @Published var supported = true
    @Published var now = Date()

    var backend: HistoryBackend?
    var recorder: VoiceMessageRecording = VoiceMessageRecorder()
    private var searchTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var clockTimer: Timer?

    init() {}

    // MARK: List

    func start() {
        guard supported, backend != nil else { return }
        if list.sessions.isEmpty && !list.loading { run(list.reset()) }
        if clockTimer == nil {
            clockTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.tick() }
            }
        }
    }

    func stop() {
        clockTimer?.invalidate(); clockTimer = nil
        pollTask?.cancel(); pollTask = nil
        cancelVoice()
    }

    func refresh() { run(list.reset()) }
    func loadMore() { if let q = list.loadMore() { run(q) } }
    func retryList() { if let q = list.retry() { run(q) } }

    func setSource(_ id: String?) { if let q = list.setSource(id) { run(q) } }

    private func scheduleSearch() {
        searchTask?.cancel()
        let text = searchText
        searchTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 250_000_000)
            guard !Task.isCancelled, let self else { return }
            if let q = self.list.setQuery(text) { self.run(q) }
        }
    }

    /// Run the search now (tests, screenshots).
    func searchNow(_ text: String) {
        searchTask?.cancel()
        searchText = text
        searchTask?.cancel()
        if let q = list.setQuery(text) { run(q) }
    }

    private func run(_ query: HistoryQuery) {
        guard let backend else { return }
        Task { [weak self] in
            do {
                let page = try await backend.page(query)
                self?.list.apply(page, for: query)
            } catch {
                self?.list.fail(Self.message(error), for: query)
            }
        }
    }

    // MARK: Opening

    func open(_ sessionID: String) {
        guard target != .session(sessionID) else { return }
        cancelVoice()
        target = .session(sessionID)
        newChatError = nil; answerError = nil
        load(sessionID)
        poll()
    }

    func newChat() {
        cancelVoice()
        target = .newChat
        newChatError = nil
        focusComposer += 1
    }

    func load(_ sessionID: String) {
        guard let backend else { return }
        loadingDetail.insert(sessionID)
        Task { [weak self] in
            do {
                let detail = try await backend.detail(sessionID)
                guard let self else { return }
                self.details[sessionID] = detail
                self.detailErrors[sessionID] = nil
                self.outbox.prune(detail.messages, session: sessionID)
            } catch {
                self?.detailErrors[sessionID] = Self.message(error)
            }
            self?.loadingDetail.remove(sessionID)
        }
    }

    /// While the open session is working or waiting on you, keep it fresh.
    private func poll() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                guard let self, !Task.isCancelled, let id = self.target?.sessionID else { return }
                let status = self.details[id]?.session.status
                let pending = self.outbox.items.contains { $0.request.sessionID == id }
                if status == .running || status == .waiting || pending { self.load(id) }
            }
        }
    }

    var openDetail: HistoryDetail? { target?.sessionID.flatMap { details[$0] } }

    func thread(_ detail: HistoryDetail) -> [HistoryMessage] {
        outbox.merged(into: detail.messages, session: detail.session.id)
    }

    // MARK: Composer

    var draftKey: String { target?.sessionID ?? "new" }
    var draft: String {
        get { drafts[draftKey] ?? "" }
        set { drafts[draftKey] = newValue }
    }

    /// ↩ / the send button: the typed words.
    func sendTyped() {
        let text = draft
        guard deliver(text, source: .typed) else { return }
        drafts[draftKey] = nil
    }

    /// Send the reviewed voice message.
    func sendVoice() {
        guard let text = voice.send() else { return }
        if deliver(text, source: .voice, onFail: { [weak self] in self?.voice.sendFailed() }) { voice.sent() }
    }

    func editVoice(_ text: String) { voice.edit(text) }

    /// Send by id (a session) or start one (New chat). False when there was nothing to send.
    @discardableResult
    private func deliver(_ text: String, source: HistoryMessageSource, onFail: (() -> Void)? = nil) -> Bool {
        guard let target, let outgoing = target.outgoing(text, source: source), let backend else { return false }
        switch outgoing {
        case .message(let request):
            outbox.add(request, at: Date())
            transmit(request, backend)
        case .newChat(let text, _, let clientID):
            startingChat = true; newChatError = nil
            Task { [weak self] in
                do {
                    let ref = try await backend.startChat(text: text, clientID: clientID)
                    guard let self else { return }
                    await self.openStarted(ref, text: text)
                } catch {
                    self?.startingChat = false
                    self?.newChatError = Self.message(error)
                    self?.drafts["new"] = text
                    onFail?()
                }
            }
        }
        return true
    }

    /// A new chat started: find its session (the row whose `run_ids` has the new run, or the id
    /// itself) and open it. The session can take a moment to appear in Hermes's history.
    private func openStarted(_ ref: String?, text: String) async {
        defer { startingChat = false }
        guard let backend, let ref else { refresh(); return }
        for attempt in 0..<8 {
            if attempt > 0 { try? await Task.sleep(nanoseconds: 1_000_000_000) }
            guard target == .newChat else { refresh(); return }   // the user moved on
            let first = HistoryQuery(cursor: nil, q: nil, source: nil, limit: 20, generation: -1)
            if let page = try? await backend.page(first),
               let s = page.sessions.first(where: { $0.id == ref || $0.runID == ref }) {
                list.upsert(s)
                open(s.id)
                refresh()
                return
            }
        }
        refresh()
        newChatError = "Started. It shows up in the list once Hermes has it."
    }

    func retry(clientID: String) {
        guard let item = outbox.item(clientID), let backend else { return }
        outbox.mark(clientID, .sending)
        transmit(item.request, backend)
    }

    private func transmit(_ request: HistoryMessageRequest, _ backend: HistoryBackend) {
        Task { [weak self] in
            do {
                _ = try await backend.send(request)
                self?.outbox.mark(request.clientID, .sent)
                self?.bump(request.sessionID, text: request.text)
                self?.load(request.sessionID)
            } catch {
                self?.outbox.mark(request.clientID, .failed(Self.message(error)))
            }
        }
    }

    /// The session just got a message: it moves to the top of the list.
    private func bump(_ sessionID: String, text: String) {
        guard var s = list.sessions.first(where: { $0.id == sessionID }) ?? details[sessionID]?.session else { return }
        s.updatedAt = Date(); s.preview = text
        if s.status == .done || s.status == .failed || s.status == .idle { s.status = .running }
        list.upsert(s)
    }

    /// Approval / question buttons. A message sent through Hermes that waits on it is answered with
    /// its send's answer route (`choice`); a task's is answered by a message to the same session
    /// ("yes" / "no" / the option), which the server hands to that task as the answer.
    func answer(_ text: String, sendID: String? = nil, choice: String? = nil) {
        guard let sendID, let sessionID = target?.sessionID, let backend else { deliver(text, source: .typed); return }
        answering.insert(sendID)
        let request = HistorySendAnswer(sessionID: sessionID, sendID: sendID, choice: choice ?? text)
        Task { [weak self] in
            do { try await backend.answer(request) }
            catch { self?.detailErrors[sessionID] = nil; self?.answerError = Self.message(error) }
            self?.answering.remove(sendID)
            self?.load(sessionID)
        }
    }

    // MARK: Voice messages

    func setCallLive(_ live: Bool) {
        let wasRecording = voice.isBusy && voice.reviewText == nil
        voice.setCallLive(live)
        if live && wasRecording { recorder.cancel(); level = 0 }
    }

    func tapMic() {
        guard voice.tapMic() else { return }
        recorder.authorize { [weak self] granted, problem in
            guard let self else { return }
            self.voice.permission(granted: granted, now: Date())
            if !granted { if let problem { self.voice.failed(problem) }; return }
            let started = self.recorder.start(
                partial: { [weak self] text in self?.voice.heard(text) },
                level: { [weak self] l in self?.level = l },
                finished: { [weak self] text, problem in
                    guard let self else { return }
                    self.level = 0
                    if let problem { self.voice.failed(problem) } else { self.voice.finished(text) }
                    if self.voice.reviewText != nil { self.focusComposer += 1 }
                })
            if !started { self.voice.failed("Couldn't open the microphone.") }
        }
    }

    func stopRecording() {
        guard voice.isRecording else { return }
        voice.stop()
        recorder.stop()
    }

    func cancelVoice() {
        if voice.isBusy { recorder.cancel() }
        voice.cancel(); level = 0
    }

    private func tick() {
        now = Date()
        if voice.overLimit(now: now) { stopRecording() }
    }

    // MARK: Preview hooks (screenshots)

    func preview(voice flow: VoiceMessageFlow, level: Float) { self.voice = flow; self.level = level }
    func preview(open id: String, detail: HistoryDetail) {
        target = .session(id); details[id] = detail
    }
    func preview(newChat: Bool) { if newChat { target = .newChat } }
    func preview(outbox: HistoryOutbox) { self.outbox = outbox }

    static func message(_ error: Error) -> String {
        if let e = error as? ServerClient.HTTPError { return e.message }
        if (error as? URLError) != nil { return "Couldn't reach your Hermes. Check the connection and try again." }
        return error.localizedDescription
    }
}
