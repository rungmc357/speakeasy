import Foundation
import SwiftUI
import SpeakeasyCore
import SpeakeasyClient

/// Where history comes from: the paired Hermes, or (screenshots) a demo.
@MainActor
protocol HistoryBackend: AnyObject {
    func page(_ query: HistoryQuery) async throws -> HistoryPage
    func conversation(_ sessionID: String) async throws -> HistoryConversation
    func send(_ request: HistoryMessageRequest) async throws -> HistoryMessageReply
    /// New chat; returns the new task's run id (or a session id).
    func startChat(text: String, clientID: String) async throws -> String?
    func answer(sessionID: String, sendID: String, choice: String) async throws
    func answerApproval(sessionID: String, runID: String, choice: String) async throws
}

@MainActor
final class LiveHistoryBackend: HistoryBackend {
    let api: ServerClient
    init(api: ServerClient) { self.api = api }
    func page(_ query: HistoryQuery) async throws -> HistoryPage { try await api.historyPage(query) }
    func conversation(_ sessionID: String) async throws -> HistoryConversation { try await api.historySession(sessionID) }
    func send(_ request: HistoryMessageRequest) async throws -> HistoryMessageReply { try await api.sendToSession(request) }
    func startChat(text: String, clientID: String) async throws -> String? { try await api.startChat(text: text, clientID: clientID) }
    func answer(sessionID: String, sendID: String, choice: String) async throws {
        try await api.answerSend(sessionID: sessionID, sendID: sendID, choice: choice)
    }
    func answerApproval(sessionID: String, runID: String, choice: String) async throws {
        try await api.answerSessionApproval(sessionID: sessionID, runID: runID, choice: choice)
    }
}

/// The history window's state: the list, the open session, the composer.
@MainActor
final class HistoryStore: ObservableObject {
    @Published private(set) var list = HistoryList()
    /// nil: nothing open yet.
    @Published private(set) var target: HistoryTarget?
    @Published private(set) var conversations: [String: HistoryConversation] = [:]
    @Published private(set) var detailErrors: [String: String] = [:]
    @Published private(set) var outbox = HistoryOutbox()
    @Published var searchText = "" { didSet { scheduleSearch() } }
    /// What's typed, per target ("new" for New chat).
    @Published var drafts: [String: String] = [:]
    @Published private(set) var voice = MacVoiceMessage()
    @Published private(set) var level: Float = 0
    @Published var openSections: Set<String> = []
    @Published private(set) var newChatError: String?
    @Published private(set) var startingChat = false
    @Published private(set) var answering: Set<String> = []
    @Published private(set) var answerError: String?
    /// Bumped to move focus (⌘F, ⌘N, a transcript to review).
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

    /// Search now, no debounce (screenshots).
    func searchNow(_ text: String) {
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
        Task { [weak self] in
            do {
                let c = try await backend.conversation(sessionID)
                guard let self else { return }
                self.conversations[sessionID] = c
                self.detailErrors[sessionID] = nil
                self.outbox.prune(HistoryThread(c).messages, session: sessionID)
            } catch {
                self?.detailErrors[sessionID] = Self.message(error)
            }
        }
    }

    /// While the open session works, waits on you, or has a message on its way: keep it fresh.
    private func poll() {
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                guard let self, !Task.isCancelled, let id = self.target?.sessionID else { return }
                let c = self.conversations[id]
                if c?.isWorking == true || c?.state == .needsYou || self.outbox.pending(in: id) { self.load(id) }
            }
        }
    }

    func thread(_ c: HistoryConversation) -> HistoryThread { HistoryThread(c, outbox: outbox) }

    // MARK: Composer

    var draftKey: String { target?.sessionID ?? "new" }
    var draft: String {
        get { drafts[draftKey] ?? "" }
        set { drafts[draftKey] = newValue }
    }

    /// ↩ / Send: what's in the field. Words from a voice message under review go as `voice_message`.
    func send() {
        let source: HistoryMessageRequest.Source = voice.reviewing ? .voiceMessage : .typed
        guard deliver(draft, source: source) else { return }
        drafts[draftKey] = nil
        voice.handle(.sent)
    }

    /// Send by id (a session) or start one (New chat). False when there was nothing to send.
    @discardableResult
    private func deliver(_ text: String, source: HistoryMessageRequest.Source) -> Bool {
        guard let target, let outgoing = target.outgoing(text, source: source), let backend else { return false }
        switch outgoing {
        case .message(let request):
            outbox.add(request, at: Date())
            transmit(request, backend)
        case .newChat(let text, let clientID):
            startingChat = true; newChatError = nil
            Task { [weak self] in
                do {
                    let ref = try await backend.startChat(text: text, clientID: clientID)
                    await self?.openStarted(ref)
                } catch {
                    self?.startingChat = false
                    self?.newChatError = Self.message(error)
                    self?.drafts["new"] = text
                }
            }
        }
        return true
    }

    /// A new chat started: find its session (the row listing the new run, or the id itself) and
    /// open it. Hermes can take a moment to record the session.
    private func openStarted(_ ref: String?) async {
        defer { startingChat = false }
        guard let backend, let ref else { refresh(); return }
        for attempt in 0..<10 {
            if attempt > 0 { try? await Task.sleep(nanoseconds: 1_000_000_000) }
            guard target == .newChat else { refresh(); return }   // the user moved on
            if let page = try? await backend.page(HistoryQuery(limit: 20, generation: -1)),
               let s = page.sessions.first(where: { $0.id == ref || $0.runIDs.contains(ref) }) {
                list.upsert(s)
                open(s.id)
                refresh()
                return
            }
        }
        refresh()
        newChatError = "Started. It shows up in the list as soon as Hermes records it."
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

    /// The session just got a message: it moves to the top of the list, working.
    private func bump(_ sessionID: String, text: String) {
        guard var s = list.sessions.first(where: { $0.id == sessionID }) else { return }
        s.lastActivity = Date(); s.preview = text
        if s.state == .idle { s.state = .running }
        list.upsert(s)
    }

    /// An approval / question button. A send's prompt and a task's approval each have their own
    /// answer route; a task's question is answered by a message to the session (the server hands it
    /// to the waiting task as the answer).
    func answer(_ waiting: HistoryWaiting, choice: String) {
        guard waiting.sendID != nil || waiting.runID != nil else { deliver(choice, source: .typed); return }
        guard let sessionID = target?.sessionID, let backend else { return }
        answering.insert(waiting.id); answerError = nil
        Task { [weak self] in
            do {
                if let sendID = waiting.sendID {
                    try await backend.answer(sessionID: sessionID, sendID: sendID, choice: choice)
                } else if let runID = waiting.runID {
                    try await backend.answerApproval(sessionID: sessionID, runID: runID, choice: choice)
                }
            } catch { self?.answerError = Self.message(error) }
            self?.answering.remove(waiting.id)
            self?.load(sessionID)
        }
    }

    // MARK: Voice messages

    func setCallLive(_ live: Bool) {
        if voice.setCallLive(live) == .cancelRecording { recorder.cancel(); level = 0 }
    }

    /// The mic button (and Stop: the same tap ends a recording).
    func tapMic() {
        apply(voice.handle(.micTapped(now: Date()), draft: draft))
    }

    func cancelVoice() {
        apply(voice.handle(.cancel))
    }

    private func apply(_ effect: VoiceMessageFlow.Effect) {
        switch effect {
        case .none: break
        case .askPermissions:
            recorder.authorize { [weak self] granted, onDevice in
                guard let self else { return }
                self.apply(self.voice.handle(.permissions(granted: granted, onDevice: onDevice)))
            }
        case .startRecording:
            let started = recorder.start(
                partial: { [weak self] text in self?.voice.handle(.partial(text)) },
                level: { [weak self] l in self?.level = l },
                finished: { [weak self] text, problem in
                    guard let self else { return }
                    self.level = 0
                    if let problem { self.apply(self.voice.handle(.failed(problem))); return }
                    self.apply(self.voice.handle(.final(text ?? "")))
                    if self.voice.reviewing {
                        self.drafts[self.draftKey] = self.voice.flow.fieldText
                        self.focusComposer += 1
                    }
                })
            if !started { apply(voice.handle(.failed("Couldn't open the microphone."))) }
        case .stopRecording:
            recorder.stop()
        case .cancelRecording:
            recorder.cancel(); level = 0
        }
    }

    private func tick() {
        now = Date()
        if voice.flow.shouldAutoStop(now: now) { tapMic() }
    }

    // MARK: Screenshots

    func preview(voice: MacVoiceMessage, level: Float) { self.voice = voice; self.level = level }
    func preview(outbox: HistoryOutbox) { self.outbox = outbox }

    static func message(_ error: Error) -> String {
        if let e = error as? ServerClient.HTTPError { return e.message }
        if (error as? URLError) != nil { return "Couldn't reach your Hermes. Check the connection and try again." }
        return error.localizedDescription
    }
}
