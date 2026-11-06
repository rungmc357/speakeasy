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
    func startChat(text: String, clientID: String, choice: ModelChoice?) async throws -> String?
    func answerApproval(sessionID: String, runID: String, choice: String) async throws
    /// Servers with `model_choice`: the catalog, Use for this chat, Make default.
    func models() async throws -> ModelCatalog
    func setSessionModel(_ request: SessionModelRequest) async throws
    func setDefaultModel(_ defaults: JobModelDefaults) async throws
    /// Rename / pin / archive / mark read (plugins from 0.3.34).
    func act(_ action: HistorySessionAction, sessionID: String) async throws
    /// Servers with `slash_skills`: the skills "/" offers in the composer.
    func skills() async throws -> [SlashSkill]
}

/// What the row's context menu (and swipes) do to a conversation.
enum HistorySessionAction: Equatable {
    case rename(String), pin(Bool), archive(Bool), read
    /// Stop the session's running task (its run ids, newest last).
    case stop([String])
}

extension HistoryBackend {
    func act(_ action: HistorySessionAction, sessionID: String) async throws {}
    func skills() async throws -> [SlashSkill] { [] }
}

@MainActor
final class LiveHistoryBackend: HistoryBackend {
    let api: ServerClient
    init(api: ServerClient) { self.api = api }
    func page(_ query: HistoryQuery) async throws -> HistoryPage { try await api.historyPage(query) }
    func conversation(_ sessionID: String) async throws -> HistoryConversation { try await api.historySession(sessionID) }
    func send(_ request: HistoryMessageRequest) async throws -> HistoryMessageReply { try await api.sendToSession(request) }
    func startChat(text: String, clientID: String, choice: ModelChoice?) async throws -> String? {
        try await api.startChat(text: text, clientID: clientID, choice: choice)
    }
    func answerApproval(sessionID: String, runID: String, choice: String) async throws {
        try await api.answerSessionApproval(sessionID: sessionID, runID: runID, choice: choice)
    }
    func models() async throws -> ModelCatalog { try await api.models() }
    func skills() async throws -> [SlashSkill] { try await api.slashSkills() }
    func setSessionModel(_ request: SessionModelRequest) async throws { try await api.setSessionModel(request) }
    func setDefaultModel(_ defaults: JobModelDefaults) async throws { try await api.setJobModelDefaults(defaults) }
    func act(_ action: HistorySessionAction, sessionID: String) async throws {
        switch action {
        case .rename(let title): try await api.renameSession(sessionID, title: title)
        case .pin(let on): try await api.pinSession(sessionID, pinned: on)
        case .archive(let on): try await api.archiveSession(sessionID, archived: on)
        case .read: try await api.markSessionRead(sessionID)
        case .stop(let runs): try await api.stopSession(runIDs: runs)
        }
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
    /// The server has `capabilities.model_choice`: the composer's model chip.
    @Published var modelsSupported = false { didSet { if modelsSupported && !oldValue { loadModels() } } }
    @Published private(set) var catalog: ModelCatalog?
    /// New chat's pick (its first job runs on it, and the chat keeps it).
    @Published var newChatModel: ModelChoice?
    @Published private(set) var modelError: String?
    /// The server has `capabilities.slash_skills`: typing "/" lists the user's skills.
    @Published var skillsSupported = false { didSet { if skillsSupported && !oldValue { loadSkills() } } }
    /// The "/" pop-up over the composer.
    @Published var slash = SlashMenu()
    /// Scheduled (Hermes cron jobs; servers with `schedules`): the sidebar row and its pane.
    let schedules = SchedulesModel()
    let scheduleDictation = ScheduleDictation()
    @Published var schedulesSupported = false
    @Published private(set) var showingSchedules = false
    @Published var now = Date()

    var backend: HistoryBackend?
    var recorder: VoiceMessageRecording = VoiceMessageRecorder()
    private var searchTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var clockTimer: Timer?

    init() {}

    // MARK: List

    /// The live history feed (plugins from 0.3.34); nil on demo backends.
    private var feed: HistoryLiveFeed?

    func start() {
        guard supported, backend != nil else { return }
        if list.sessions.isEmpty && !list.loading { run(list.reset()) }
        if modelsSupported { loadModels() }
        if skillsSupported { loadSkills() }
        if schedulesSupported, !schedules.loaded { Task { await schedules.reload() } }
        if feed == nil, let live = backend as? LiveHistoryBackend {
            let api = live.api
            feed = HistoryLiveFeed(api: { api }) { [weak self] event, sid in self?.liveChange(event, sessionID: sid) }
        }
        feed?.start()
        if clockTimer == nil {
            clockTimer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
                Task { @MainActor in self?.tick() }
            }
        }
    }

    func stop() {
        clockTimer?.invalidate(); clockTimer = nil
        pollTask?.cancel(); pollTask = nil
        feed?.stop()
        cancelVoice()
    }

    func refresh() { run(list.reset()) }
    func loadMore() { if let q = list.loadMore() { run(q) } }
    func retryList() { if let q = list.retry() { run(q) } }
    func setSource(_ id: String?) { if let q = list.setSource(id) { run(q) } }
    func setArchived(_ on: Bool) { if let q = list.setArchived(on) { run(q) } }

    // MARK: Row actions (context menu)

    /// The last archive, for Undo (the toast).
    @Published private(set) var undoArchive: (title: String, id: String)?
    @Published private(set) var actionError: String?
    private var undoTask: Task<Void, Never>?

    /// Applied to the row at once, then sent; put back with a note if the server refuses.
    func act(_ action: HistorySessionAction, on id: String) {
        guard let backend else { return }
        let before = list.sessions.first { $0.id == id }
        var removed: (HistorySession, Int)?
        switch action {
        case .rename(let title):
            let clean = title.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            guard !clean.isEmpty else { return }
            list.update(id) { $0.title = clean }
            conversations[id]?.title = clean
        case .pin(let on): list.update(id) { $0.pinned = on }
        case .read: list.update(id) { $0.unread = false }
        case .archive(let on) where !list.activeQuery.isEmpty:
            list.update(id) { $0.archived = on }   // a search keeps archived matches (marked)
            if on, let row = before { showUndo(row.title, id) }
        case .archive(let on):
            removed = list.remove(id)
            if on, let row = removed?.0 { showUndo(row.title, id) }
            if target == .session(id), on { target = nil }
        case .stop:
            list.update(id) { $0.state = .idle }   // the live feed brings the cancelled state
        }
        actionError = nil
        Task { [weak self] in
            do { try await backend.act(action, sessionID: id) } catch {
                guard let self else { return }
                if let removed { self.list.restore(removed.0, at: removed.1) }
                else if let before { self.list.update(id) { $0 = before } }
                self.actionError = Self.message(error)
            }
        }
    }

    private func showUndo(_ title: String, _ id: String) {
        undoArchive = (title, id)
        undoTask?.cancel()
        undoTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            guard !Task.isCancelled else { return }
            self?.undoArchive = nil
        }
    }

    func undoLastArchive() {
        guard let undo = undoArchive, let backend else { return }
        undoArchive = nil
        Task { [weak self] in
            try? await backend.act(.archive(false), sessionID: undo.id)
            self?.refresh()
        }
    }

    /// A change arrived on the live feed (another device, another Hermes app, a task finishing):
    /// the list and the open page catch up now, never waiting for the next poll.
    func liveChange(_ event: String, sessionID: String?) {
        softRefresh()
        if let open = target?.sessionID, sessionID == nil || sessionID == open || event == "history_changed" { load(open) }
    }

    /// The first page again without clearing what's shown (no flash): rows update in place.
    private func softRefresh() {
        guard let backend, !list.loading else { return }
        var q = HistoryQuery(limit: HistoryList.pageSize, generation: list.generation)
        q.q = list.activeQuery; q.source = list.source; q.archived = list.archived
        Task { [weak self] in
            guard let page = try? await backend.page(q), let self else { return }
            self.list.merge(page, for: q)
        }
    }

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
        showingSchedules = false
        guard target != .session(sessionID) else { return }
        cancelVoice()
        target = .session(sessionID)
        newChatError = nil; answerError = nil
        load(sessionID)
        poll()
    }

    func newChat() {
        showingSchedules = false
        cancelVoice()
        target = .newChat
        newChatError = nil
        newChatModel = nil
        focusComposer += 1
    }

    /// The Scheduled pane (the list, or one schedule by id).
    func openSchedules(_ id: String? = nil) {
        guard schedulesSupported else { return }
        cancelVoice()
        showingSchedules = true
        schedules.selected = id
        Task { await schedules.reload() }
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
        set { drafts[draftKey] = newValue; slash.update(text: skillsSupported ? newValue : "") }
    }

    // MARK: Skills ("/" in the composer; servers with `slash_skills`)

    func loadSkills() {
        guard let backend else { return }
        Task { [weak self] in
            if let list = try? await backend.skills() { self?.slash.skills = list; self?.slash.update(text: self?.draft ?? "") }
        }
    }

    /// The "/" list shows: a lone "/word" is being typed in this composer and skills match it.
    var slashOpen: Bool {
        skillsSupported && slash.isOpen && SlashMenu.commandQuery(draft) == slash.query
    }

    /// The skill the draft starts with ("/soup-scaler for twelve" → soup-scaler): the hint names it.
    var draftSkill: SlashSkill? { skillsSupported ? slash.leadingSkill(in: draft) : nil }

    /// A row of the "/" list (click, ↩ or tab): "/command " in the field, ready for the request.
    func pickSkill(_ skill: SlashSkill) {
        draft = slash.pick(skill, in: draft)
        focusComposer += 1
    }

    /// ↩ / Send: what's in the field. Words from a voice message under review go as `voice_message`.
    /// With the "/" list open, ↩ picks the highlighted skill instead.
    func send() {
        if slashOpen, let skill = slash.highlightedSkill { pickSkill(skill); return }
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
            if startingChat {
                // The first message's chat is still starting: this one continues it once it has its
                // session (never a second new chat).
                queuedForNewChat.append((text, source)); return true
            }
            startingChat = true; newChatError = nil
            Task { [weak self] in
                do {
                    let ref = try await backend.startChat(text: text, clientID: clientID, choice: self?.newChatModel)
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
        defer { startingChat = false; flushQueuedForNewChat() }
        guard let backend, let ref else { refresh(); return }
        if HistoryStore.isSessionID(ref), target == .newChat {
            // Plugins from 0.3.34 name the session at once: the page becomes it now, so the next
            // message continues it by id.
            open(ref)
            refresh()
            return
        }
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

    /// Messages typed into a New chat while its first was still starting.
    private var queuedForNewChat: [(String, HistoryMessageRequest.Source)] = []

    private func flushQueuedForNewChat() {
        let queued = queuedForNewChat
        queuedForNewChat = []
        guard case .session = target else {
            if let first = queued.first { drafts["new"] = first.0 }   // never lost: back in the composer
            return
        }
        for (text, source) in queued { _ = deliver(text, source: source) }
    }

    /// A Hermes session id rather than a run id (run ids are `run_…` / `lr_…`).
    nonisolated static func isSessionID(_ ref: String) -> Bool {
        !(ref.hasPrefix("run_") || ref.hasPrefix("lr_") || ref.hasPrefix("job_"))
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

    /// An approval / question button. A task's approval has its own answer route; a task's question
    /// is answered by a message to the session (the server hands it to the waiting task as the answer).
    func answer(_ waiting: HistoryWaiting, choice: String) {
        guard waiting.runID != nil else { deliver(choice, source: .typed); return }
        guard let sessionID = target?.sessionID, let backend else { return }
        answering.insert(waiting.id); answerError = nil
        Task { [weak self] in
            do {
                if let runID = waiting.runID {
                    try await backend.answerApproval(sessionID: sessionID, runID: runID, choice: choice)
                }
            } catch { self?.answerError = Self.message(error) }
            self?.answering.remove(waiting.id)
            self?.load(sessionID)
        }
    }

    // MARK: Model (servers with `model_choice`)

    func loadModels() {
        guard let backend else { return }
        Task { [weak self] in
            if let c = try? await backend.models(), c.available { self?.catalog = c }
        }
    }

    /// The open chat's own pick (New chat: its pending one).
    var chatModel: ModelChoice? {
        switch target {
        case .session(let id): return conversations[id]?.modelChoice
        default: return newChatModel
        }
    }

    /// The chip's state for what's open now; nil hides the chip.
    var picker: ModelPickerState? {
        guard modelsSupported, let catalog, catalog.available else { return nil }
        return ModelPickerState(catalog: catalog, chatChoice: chatModel)
    }

    /// The sheet's Apply: save it for this chat, or as Speakeasy's default.
    func applyModel(_ state: ModelPickerState, scope: ModelPickerState.Scope) {
        var next = state
        next.apply(scope)
        catalog = next.catalog
        modelError = nil
        guard let backend else { return }
        let choice = scope == .chat ? next.selection : nil
        let sessionID = target?.sessionID
        if scope == .chat, sessionID == nil { newChatModel = choice; return }
        if let sessionID, scope == .chat, var c = conversations[sessionID] {
            c.modelChoice = choice; conversations[sessionID] = c  // at once; the server confirms below
        }
        Task { [weak self] in
            do {
                if scope == .makeDefault {
                    try await backend.setDefaultModel(JobModelDefaults(model: next.selection.model, reasoning: next.selection.reasoning))
                } else if let sessionID, let choice {
                    try await backend.setSessionModel(SessionModelRequest(sessionID: sessionID, choice: choice))
                }
                self?.loadModels()
                if let sessionID { self?.load(sessionID) }
            } catch {
                self?.modelError = Self.message(error)
                if let sessionID { self?.load(sessionID) }
            }
        }
    }

    /// Back to the default for this chat.
    func useDefaultModel() {
        guard let backend else { return }
        guard let sessionID = target?.sessionID else { newChatModel = nil; return }
        if var c = conversations[sessionID] { c.modelChoice = nil; conversations[sessionID] = c }
        Task { [weak self] in
            try? await backend.setSessionModel(SessionModelRequest(sessionID: sessionID, choice: ModelChoice()))
            self?.load(sessionID)
        }
    }

    func preview(catalog: ModelCatalog?) { self.catalog = catalog; modelsSupported = catalog != nil }

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
