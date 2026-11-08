import Combine
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
    /// New chat; returns the new task's run id (or a session id). `agent`: the Hermes profile it
    /// talks to (servers with `agents`; nil = Speakeasy's own).
    func startChat(text: String, clientID: String, choice: ModelChoice?, agent: String?) async throws -> String?
    /// Servers with `agents`: the agent picker's list.
    func agents() async throws -> AgentRoster
    func answerApproval(sessionID: String, runID: String, choice: String) async throws
    /// ✕ on a decision card: close (or, `undo`, reopen) one task's question everywhere; tells the agent nothing.
    func dismissQuestion(runID: String, undo: Bool) async throws
    /// Servers with `model_choice`: the catalog, Use for this chat, Make default.
    func models() async throws -> ModelCatalog
    func setSessionModel(_ request: SessionModelRequest) async throws
    func setDefaultModel(_ defaults: JobModelDefaults) async throws
    /// Rename / pin / archive / mark read (plugins from 0.3.34).
    func act(_ action: HistorySessionAction, sessionID: String) async throws
    /// A message held for when the task finishes: cancel it, or send it now (servers with `message_modes`).
    func cancelQueued(sessionID: String, clientID: String) async throws
    func steerQueued(sessionID: String, clientID: String) async throws
    /// Servers with `slash_skills`: the skills "/" offers in the composer.
    func skills() async throws -> [SlashSkill]
    /// A voice note (servers with `voice_notes`): the recording itself is the message.
    func sendVoiceNote(_ note: VoiceNoteRequest) async throws -> VoiceNoteReply
}

/// What the row's context menu (and swipes) do to a conversation.
enum HistorySessionAction: Equatable {
    case rename(String), pin(Bool), archive(Bool), read
    /// Stop the session's running task (its run ids, newest last).
    case stop([String])
}

extension HistoryBackend {
    func act(_ action: HistorySessionAction, sessionID: String) async throws {}
    func dismissQuestion(runID: String, undo: Bool) async throws {}
    func cancelQueued(sessionID: String, clientID: String) async throws {}
    func steerQueued(sessionID: String, clientID: String) async throws {}
    func agents() async throws -> AgentRoster { AgentRoster(agents: []) }
    func skills() async throws -> [SlashSkill] { [] }
    func sendVoiceNote(_ note: VoiceNoteRequest) async throws -> VoiceNoteReply {
        throw ServerClient.HTTPError(status: 404, message: "Your Hermes can't take voice messages yet.")
    }
}

@MainActor
final class LiveHistoryBackend: HistoryBackend {
    let api: ServerClient
    init(api: ServerClient) { self.api = api }
    func page(_ query: HistoryQuery) async throws -> HistoryPage { try await api.historyPage(query) }
    func conversation(_ sessionID: String) async throws -> HistoryConversation { try await api.historySession(sessionID) }
    func send(_ request: HistoryMessageRequest) async throws -> HistoryMessageReply { try await api.sendToSession(request) }
    func startChat(text: String, clientID: String, choice: ModelChoice?, agent: String?) async throws -> String? {
        try await api.startChat(text: text, clientID: clientID, choice: choice, agent: agent)
    }
    func agents() async throws -> AgentRoster { try await api.agents() }
    func answerApproval(sessionID: String, runID: String, choice: String) async throws {
        try await api.answerSessionApproval(sessionID: sessionID, runID: runID, choice: choice)
    }
    func dismissQuestion(runID: String, undo: Bool) async throws { try await api.dismissQuestion(runID: runID, undo: undo) }
    func models() async throws -> ModelCatalog { try await api.models() }
    func skills() async throws -> [SlashSkill] { try await api.slashSkills() }
    func setSessionModel(_ request: SessionModelRequest) async throws { try await api.setSessionModel(request) }
    func setDefaultModel(_ defaults: JobModelDefaults) async throws { try await api.setJobModelDefaults(defaults) }
    func cancelQueued(sessionID: String, clientID: String) async throws {
        try await api.cancelQueued(sessionID: sessionID, clientID: clientID)
    }
    func steerQueued(sessionID: String, clientID: String) async throws {
        _ = try await api.steerQueued(sessionID: sessionID, clientID: clientID)
    }
    func sendVoiceNote(_ note: VoiceNoteRequest) async throws -> VoiceNoteReply { try await api.sendVoiceNote(note) }
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
    @Published private(set) var list = HistoryList(shown: HistoryAutomation.shown)
    /// nil: nothing open yet.
    @Published private(set) var target: HistoryTarget? {
        didSet { if oldValue != target { choosingSendMode = false } }
    }
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
    /// "Reply…" on a decision card: the composer answers that task's question (its run id, its words).
    @Published private(set) var replyingTo: HistoryWaiting?
    /// Picks and ✕ dismisses not yet confirmed by the server: the app's one pending-pick store, shared
    /// with the panel and board (SPK-79), keyed by the asking task's run id. Everything else about a
    /// question is the server's `question_state` / `question_open`.
    let pending: PendingPicks = .shared
    private var pendingRelay: Any?
    private func bindPending() {
        guard pendingRelay == nil else { return }
        pendingRelay = pending.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
    }
    /// The Undo toast after a ✕ (a few seconds).
    @Published private(set) var undoQuestion: HistoryWaiting?
    private var undoTimer: Task<Void, Never>?
    /// Bumped to move focus (⌘F, ⌘N, a transcript to review).
    @Published var focusSearch = 0
    @Published var focusComposer = 0
    /// The server has `capabilities.history`; false shows the update note.
    @Published var supported = true
    /// The server has `capabilities.model_choice`: the composer's model chip.
    /// The server has `capabilities.voice_notes`: on a session page, stop sends the recording itself.
    @Published var voiceNotesSupported = false
    /// The server has `capabilities.now`: the window's Now column (SPK-124).
    @Published var nowSupported = false
    @Published var modelsSupported = false { didSet { if modelsSupported && !oldValue { loadModels() } } }
    @Published private(set) var catalog: ModelCatalog?
    /// New chat's pick (its first job runs on it, and the chat keeps it).
    @Published var newChatModel: ModelChoice?
    @Published private(set) var modelError: String?
    /// The server has `capabilities.agents`: New chat's agent chip, when more than one is allowed.
    @Published var agentsSupported = false { didSet { if agentsSupported && !oldValue { loadAgents() } } }
    @Published private(set) var roster: AgentRoster?
    /// New chat's agent (nil = Speakeasy's own); the chat it becomes stays with it.
    @Published var newChatAgent: String?
    /// The server has `capabilities.slash_skills`: typing "/" lists the user's skills.
    @Published var skillsSupported = false { didSet { if skillsSupported && !oldValue { loadSkills() } } }
    /// The "/" pop-up over the composer.
    @Published var slash = SlashMenu()
    /// Scheduled (Hermes cron jobs; servers with `schedules`): the sidebar row and its pane.
    let schedules = SchedulesModel()
    let scheduleDictation = ScheduleDictation()
    @Published var schedulesSupported = false
    @Published private(set) var showingSchedules = false { didSet { if showingSchedules { showingBoard = false; showingFiles = false } } }
    /// Board (Hermes Kanban boards, read-only; servers with `boards`): the sidebar row and its pane.
    let board = BoardModel()
    @Published var boardsSupported = false
    @Published private(set) var showingBoard = false {
        didSet { if showingBoard { showingFiles = false } else { board.stopRefreshing() } }
    }
    /// Files your agent made (servers with `files`): the sidebar row and its pane.
    /// Hermes' projects and where Home works (servers with `projects`, SPK-254).
    let projects = ProjectsModel()
    let files = AgentFilesModel()
    @Published var filesSupported = false
    @Published private(set) var showingFiles = false { didSet { if showingFiles { showingBoard = false } } }
    @Published var now = Date()

    var backend: HistoryBackend?
    /// The call panel's model, set by the app: a session page shows its tasks' email drafts and
    /// pictures to look at from the same state the panel uses, so acting here clears them there.
    weak var panel: VoicePanelModel?
    var recorder: VoiceMessageRecording = VoiceMessageRecorder()
    private var searchTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?
    private var clockTimer: Timer?

    init() { bindPending() }

    // MARK: List

    /// The live history feed (plugins from 0.3.34); nil on demo backends.
    private var feed: HistoryLiveFeed?

    func start() {
        guard supported, backend != nil else { return }
        if list.sessions.isEmpty && !list.loading { run(list.reset()) }
        if modelsSupported { loadModels() }
        if agentsSupported { loadAgents() }
        if skillsSupported { loadSkills() }
        if schedulesSupported, !schedules.loaded { Task { await schedules.reload() } }
        if boardsSupported, !board.loaded { Task { await board.reload() } }
        if showingBoard { board.startRefreshing() }
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
        board.stopRefreshing()
        pollTask?.cancel(); pollTask = nil
        feed?.stop()
        cancelVoice()
    }

    func refresh() { run(list.reset()) }
    func loadMore() { if let q = list.loadMore() { run(q) } }
    func retryList() { if let q = list.retry() { run(q) } }
    func setSource(_ id: String?) { if let q = list.setSource(id) { run(q) } }
    func setArchived(_ on: Bool) { if let q = list.setArchived(on) { run(q) } }
    /// The filter menu: list one kind of automated work or not (board workers, scheduled jobs,
    /// background helpers), remembered on this Mac.
    func setShown(_ kind: String, _ on: Bool) {
        var next = list.shown
        next[kind] = on
        setShown(next)
    }
    func setShown(_ next: HistoryShown) {
        guard let q = list.setShown(next) else { return }
        HistoryAutomation.shown = list.shown
        run(q)
    }

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
        showingBoard = false
        showingFiles = false
        guard target != .session(sessionID) else { return }
        cancelVoice()
        target = .session(sessionID)
        newChatError = nil; answerError = nil
        load(sessionID)
        poll()
    }

    func newChat() {
        showingSchedules = false
        showingBoard = false
        showingFiles = false
        cancelVoice()
        target = .newChat
        newChatError = nil
        newChatModel = nil
        newChatAgent = nil
        if agentsSupported { loadAgents() }   // the allowlist may have changed in Settings
        focusComposer += 1
    }

    /// The Scheduled pane (the list, or one schedule by id).
    func openSchedules(_ id: String? = nil) {
        guard schedulesSupported else { return }
        cancelVoice()
        showingFiles = false
        showingSchedules = true
        schedules.selected = id
        Task { await schedules.reload() }
    }

    /// The Board pane: the picked (or suggested) Kanban board, refreshed while it's open.
    func openBoard() {
        guard boardsSupported else { return }
        cancelVoice()
        showingSchedules = false
        showingBoard = true
        board.startRefreshing()
    }

    /// The Files pane: every file the agent handed over in a reply, across all chats.
    func openFiles() {
        guard filesSupported else { return }
        cancelVoice()
        showingSchedules = false
        showingFiles = true
        Task { await files.reload() }
    }

    /// A file's "Open chat": the conversation it came from.
    func openChat(of file: AgentFile) {
        guard !file.sessionID.isEmpty else { return }
        showingFiles = false
        if target == .session(file.sessionID) { return }
        open(file.sessionID)
    }

    private var detailGenerations: [String: Int] = [:]

    func load(_ sessionID: String) {
        guard let backend else { return }
        let revision = modelEpoch
        detailGenerations[sessionID, default: 0] += 1
        let generation = detailGenerations[sessionID]
        Task { [weak self] in
            do {
                let c = try await backend.conversation(sessionID)
                guard let self, self.detailGenerations[sessionID] == generation else { return }
                guard self.modelEpoch == revision, self.pendingModels.isEmpty else { return }
                self.conversations[sessionID] = c
                self.modelConfirmed[sessionID] = revision
                self.detailErrors[sessionID] = nil
                // The server's word on each question replaces what was pending here.
                let states = Dictionary(c.jobs.compactMap { j in j.runID.map { ($0, j) } }, uniquingKeysWith: { a, _ in a })
                self.pending.reconcile { id in
                    states[id].flatMap { $0.questionOpen == false ? ($0.questionStateOrDerived == .open ? .superseded : $0.questionStateOrDerived)
                                                                   : $0.questionStateOrDerived }
                }
                self.outbox.prune(HistoryThread(c).messages, session: sessionID)
                self.outbox.reconcile(c)
            } catch {
                guard let self, self.detailGenerations[sessionID] == generation else { return }
                self.detailErrors[sessionID] = Self.message(error)
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

    // MARK: Steer or queue (servers with `message_modes`, only while a run is active)

    /// Send (↩ or the button) while the open session's task runs: the Steer now / Queue for after
    /// pop-up at the Send button is open. Nothing is sent until a pick; Esc or a click outside
    /// closes it and the words stay in the field. Nothing is remembered between sends.
    @Published var choosingSendMode = false
    /// Messages this Mac steered into a running task: tagged "Steered" in the thread.
    @Published private(set) var steered = SteeredMarks()
    @Published private(set) var queuedBusy: Set<String> = []
    @Published var queuedError: String?

    /// What became of a steered message, by its client id only: the server's receipt, or (just
    /// steered here) "Sent to the running task".
    func steerReceipt(_ message: HistoryThreadMessage, session: String) -> SteeredMessage? {
        steered.receipt(clientID: message.clientID, at: message.at, in: conversations[session]?.steered ?? [])
    }

    /// The open session works and its server can hold a message for later: Send asks first.
    var offersMessageModes: Bool {
        guard let id = target?.sessionID else { return false }
        return conversations[id]?.offersMessageModes ?? false
    }

    /// Send opens the Steer now / Queue for after pop-up (not for an answer to a question).
    var asksSendMode: Bool { offersMessageModes && replyingTo == nil }

    func cancelQueued(_ clientID: String) { actOnQueued(clientID) { try await $0.cancelQueued(sessionID: $1, clientID: clientID) } }

    func steerQueued(_ clientID: String) {
        actOnQueued(clientID) { backend, id in
            try await backend.steerQueued(sessionID: id, clientID: clientID)
            self.steered.record(clientID: clientID)
        }
    }

    private func actOnQueued(_ clientID: String, _ body: @escaping (HistoryBackend, String) async throws -> Void) {
        guard let backend, let id = target?.sessionID, !queuedBusy.contains(clientID) else { return }
        queuedBusy.insert(clientID); queuedError = nil
        Task { [weak self] in
            do { try await body(backend, id) } catch { self?.queuedError = Self.message(error) }
            self?.queuedBusy.remove(clientID)
            self?.load(id)
        }
    }

    /// ↩ / Send: what's in the field. Words from a voice message under review go as `voice_message`.
    /// With the "/" list open, ↩ picks the highlighted skill instead.
    /// While the open session's task runs, this only opens the Steer now / Queue for after pop-up;
    /// `send(picked:)` sends.
    func send() {
        if slashOpen, let skill = slash.highlightedSkill { pickSkill(skill); return }
        if asksSendMode {
            guard !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
            choosingSendMode = true
            return
        }
        sendNow(mode: nil)
    }

    /// A pick in the pop-up. The mode only rides on the request while a run is still active (it
    /// may have finished while the pop-up was open: then a plain send).
    func send(picked: MessageMode) {
        choosingSendMode = false
        sendNow(mode: offersMessageModes ? picked : nil)
    }

    private func sendNow(mode: MessageMode?) {
        let source: HistoryMessageRequest.Source = voice.reviewing ? .voiceMessage : .typed
        let answers = replyingTo.flatMap { r in conversations[target?.sessionID ?? ""].map { thread($0) }?
            .waiting.contains(where: { $0.id == r.id }) == true ? r.questionRunID : nil }
        guard deliver(draft, source: source, answers: answers, mode: answers == nil ? mode : nil) else { return }
        drafts[draftKey] = nil
        replyingTo = nil
        voice.handle(.sent)
    }

    /// Send by id (a session) or start one (New chat). False when there was nothing to send.
    /// `answers`: the run id of the task whose question this answers.
    @discardableResult
    private func deliver(_ text: String, source: HistoryMessageRequest.Source, answers: String? = nil,
                         mode: MessageMode? = nil) -> Bool {
        if let id = target?.sessionID, !pendingModels.isEmpty || (modelEpoch > 0 && modelConfirmed[id] != modelEpoch) { return false }
        guard let target, let outgoing = target.outgoing(text, source: source, answers: answers), let backend else { return false }
        switch outgoing {
        case .message(var request):
            request.mode = mode
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
                    let ref = try await backend.startChat(text: text, clientID: clientID, choice: self?.newChatModel,
                                                          agent: self?.pickedAgent?.id)
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
        guard pendingModels[item.request.sessionID] == nil else { return }
        outbox.mark(clientID, .sending)
        if let note = item.voiceNote { transmit(voiceNote: note, session: item.request.sessionID, backend); return }
        transmit(item.request, backend)
    }

    private func transmit(_ request: HistoryMessageRequest, _ backend: HistoryBackend) {
        Task { [weak self] in
            do {
                let reply = try await backend.send(request)
                if reply.deliveredAs == .queued {
                    self?.outbox.remove(request.clientID)   // the page's queued list shows it now
                } else {
                    if reply.deliveredAs == .steer && request.mode != nil { self?.steered.record(clientID: request.clientID) }
                    self?.outbox.settle(request.clientID, reply.deliveredAs)
                }
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
        guard waiting.runID != nil else {
            if replyingTo?.id == waiting.id { replyingTo = nil }
            if let run = waiting.questionRunID { bindPending(); pending.set(run, .init(kind: .answer, text: choice)) }
            deliver(choice, source: .typed, answers: waiting.questionRunID); return
        }
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

    /// "Reply…": the composer answers this question; focus moves there.
    func reply(to waiting: HistoryWaiting) {
        replyingTo = waiting
        focusComposer += 1
    }

    func cancelReply() { replyingTo = nil }

    /// ✕: the question leaves this page at once and every other device on the server's next word;
    /// Undo is offered for a few seconds. The agent is told nothing.
    func dismiss(_ waiting: HistoryWaiting) {
        guard let runID = waiting.questionRunID else { return }
        bindPending()
        pending.set(runID, .init(kind: .dismissed))
        if replyingTo?.id == waiting.id { replyingTo = nil }
        undoQuestion = waiting
        undoTimer?.cancel()
        undoTimer = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            guard !Task.isCancelled else { return }
            if self?.undoQuestion?.id == waiting.id { self?.undoQuestion = nil }
        }
        let sessionID = target?.sessionID
        Task { [weak self] in
            // A plugin without question actions (404): the card stays hidden on this device only.
            try? await self?.backend?.dismissQuestion(runID: runID, undo: false)
            if let sessionID { self?.load(sessionID) }
        }
    }

    func undoDismiss() {
        guard let waiting = undoQuestion, let runID = waiting.questionRunID else { return }
        undoQuestion = nil; undoTimer?.cancel()
        let sessionID = target?.sessionID
        Task { [weak self] in
            try? await self?.backend?.dismissQuestion(runID: runID, undo: true)
            self?.pending.remove(runID)
            if let sessionID { self?.load(sessionID) }
        }
    }

    /// What the page shows as waiting: the thread's cards minus the ones dismissed here.
    func waiting(_ thread: HistoryThread) -> [HistoryWaiting] {
        bindPending()
        return thread.waiting.filter { w in
            guard let run = w.questionRunID, w.runID == nil else { return true }
            return pending[run] == nil
        }
    }

    // MARK: Model (servers with `model_choice`)
    private var modelRevisions: [String: Int] = [:]
    private var modelSaves: [String: Task<Void, Never>] = [:]
    private var modelSave: Task<Void, Never>?
    private var modelEpoch = 0
    private var modelConfirmed: [String: Int] = [:]
    private var pendingModels: [String: ModelChoice] = [:]

    func loadModels() {
        guard let backend else { return }
        Task { [weak self] in
            if let c = try? await backend.models(), c.available { self?.catalog = c }
        }
    }

    /// The open chat's own pick (New chat: its pending one).
    var chatModel: ModelChoice? {
        switch target {
        case .session(let id): return pendingModels[id] ?? conversations[id]?.modelChoice
        default: return newChatModel
        }
    }

    /// The chip's state for what's open now; nil hides the chip. A chat on another agent runs on that
    /// agent's own model, so it has no model chip.
    var picker: ModelPickerState? {
        guard modelsSupported, let catalog, catalog.available, openAgent == nil else { return nil }
        if let id = target?.sessionID {
            if let pending = pendingModels[id] { return ModelPickerState(catalog: catalog, chatChoice: pending) }
            guard modelEpoch == 0 || modelConfirmed[id] == modelEpoch else { return nil }
            guard let c = conversations[id] else { return nil }
            return ModelPickerState(catalog: catalog, chatChoice: chatModel,
                nextModel: pendingModels[id] == nil ? c.nextModel : nil,
                existingConversation: pendingModels[id] == nil)
        }
        return ModelPickerState(catalog: catalog, chatChoice: chatModel)
    }

    /// The sheet's Apply: save it for this chat, or as Speakeasy's default.
    func applyModel(_ state: ModelPickerState, scope: ModelPickerState.Scope) {
        modelError = nil
        guard let backend else { return }
        if scope == .makeDefault {
            let choice = state.selection
            Task { [weak self] in
                do {
                    try await backend.setDefaultModel(JobModelDefaults(model: choice.model, reasoning: choice.reasoning))
                    self?.loadModels()
                } catch { self?.modelError = Self.message(error) }
            }
            return
        }
        guard let id = target?.sessionID else { newChatModel = state.selection; return }
        saveModel(state.selection, in: id)
    }

    private func saveModel(_ choice: ModelChoice, in id: String) {
        guard let backend else { return }
        modelRevisions[id, default: 0] += 1
        let revision = modelRevisions[id]!
        modelEpoch += 1
        modelConfirmed.removeAll() // linked pages must re-read their authoritative next-send target
        let previous = modelSave
        pendingModels[id] = choice
        modelSaves[id] = Task { [weak self] in
            await previous?.value
            guard let self else { return }
            var failure: String?
            do { try await backend.setSessionModel(SessionModelRequest(sessionID: id, choice: choice)) }
            catch { failure = Self.message(error) }
            do {
                let fresh = try await backend.conversation(id)
                guard self.modelRevisions[id] == revision else { return }
                self.conversations[id] = fresh
                self.modelConfirmed[id] = self.modelEpoch
                self.modelError = failure
            } catch {
                if self.modelRevisions[id] == revision {
                    self.modelError = "Couldn't confirm the model. Choose it again to retry."
                }
                return // write outcome unknown: do not dispatch an old preference
            }
            guard self.modelRevisions[id] == revision else { return }
            self.pendingModels[id] = nil
            self.modelSaves[id] = nil
            if let open = self.target?.sessionID, self.modelConfirmed[open] != self.modelEpoch { self.load(open) }
        }
        modelSave = modelSaves[id]
    }

    /// Back to the default for this chat.
    func useDefaultModel() {
        guard let sessionID = target?.sessionID else { newChatModel = nil; return }
        saveModel(ModelChoice(), in: sessionID)
    }

    func preview(catalog: ModelCatalog?) { self.catalog = catalog; modelsSupported = catalog != nil }

    // MARK: Agents (servers with `agents`)

    func loadAgents() {
        guard let backend else { return }
        Task { [weak self] in
            guard let r = try? await backend.agents() else { return }
            self?.roster = r
            if let picked = self?.newChatAgent, r.agent(picked) == nil { self?.newChatAgent = nil }  // no longer allowed
        }
    }

    /// New chat's agent chip shows (more than one agent allowed).
    var showsAgentChip: Bool { agentsSupported && roster?.showsPicker == true && target == .newChat }

    /// What New chat's chip names: the picked agent, else Speakeasy's own.
    var newChatAgentInfo: AgentInfo? { roster?.agent(newChatAgent) }

    /// New chat's pick when it isn't Speakeasy's own agent.
    var pickedAgent: AgentInfo? {
        guard let info = newChatAgentInfo, !info.isDefault, info.id != roster?.defaultID else { return nil }
        return info
    }

    /// The agent the open page talks to, when it isn't Speakeasy's own.
    var openAgent: AgentInfo? {
        switch target {
        case .session(let id): return conversations[id]?.agent
        case .newChat: return pickedAgent
        default: return nil
        }
    }

    func pickAgent(_ id: String?) {
        newChatAgent = id == roster?.defaultID ? nil : id
        focusComposer += 1
    }

    func preview(roster: AgentRoster?) { self.roster = roster; agentsSupported = roster != nil }

    // MARK: Voice messages

    func setCallLive(_ live: Bool) {
        if voice.setCallLive(live) == .cancelRecording { recorder.cancel(); level = 0 }
    }

    /// The mic button (and Stop: the same tap ends a recording).
    func tapMic() {
        if !voice.flow.isBusy {
            voice.setVoiceNotes(voiceNotesSupported && target?.sessionID != nil, wholeClip: false)
        }
        apply(voice.handle(.micTapped(now: Date()), draft: draft))
    }

    /// Stop = sent: the whole recording goes to the session as a voice note. The bubble shows at
    /// once (play, waveform, length); Hermes writes it down on arrival and the words appear under it.
    private func sendVoiceNote() {
        level = 0
        guard let clip = recorder.finishClip(), clip.duration >= 0.4 else {
            apply(voice.handle(.failed(VoiceMessageFlow.nothingHeardText))); return
        }
        guard let session = target?.sessionID, let backend else { return }
        let answers = replyingTo.flatMap { r in conversations[session].map { thread($0) }?
            .waiting.contains(where: { $0.id == r.id }) == true ? r.questionRunID : nil }
        let note = VoiceNoteRequest(target: .session(session), audio: clip.audio, mimeType: VoiceMemoAudio.mimeType,
                                    duration: clip.duration,
                                    mode: nil,  // SPK-108: no remembered Steer/Queue choice; the server steers a running task
                                    answers: answers)
        VoiceClipStore.save(note.clientID, audio: clip.audio,
                            meta: .init(duration: clip.duration, levels: VoiceClipStore.bars(clip.levels)))
        replyingTo = nil
        outbox.add(voiceNote: note, session: session, at: Date())
        transmit(voiceNote: note, session: session, backend)
    }

    private func transmit(voiceNote note: VoiceNoteRequest, session: String, _ backend: HistoryBackend) {
        Task { [weak self] in
            do {
                let reply = try await backend.sendVoiceNote(note)
                self?.outbox.heard(note.clientID, reply.transcript)
                if reply.deliveredAs == .queued {
                    self?.outbox.remove(note.clientID)
                } else {
                    self?.outbox.settle(note.clientID, reply.deliveredAs)
                }
                self?.bump(session, text: reply.transcript ?? "Voice message")
                self?.load(session)
            } catch {
                self?.outbox.mark(note.clientID, .failed(Self.message(error)))
            }
        }
    }

    func cancelVoice() {
        apply(voice.handle(.cancel))
    }

    private func apply(_ effect: VoiceMessageFlow.Effect) {
        switch effect {
        case .none: break
        case .askPermissions where voice.sendsOnStop:
            recorder.authorizeMic { [weak self] granted in
                guard let self else { return }
                self.apply(self.voice.handle(.permissions(granted: granted, onDevice: true)))
            }
        case .askPermissions:
            recorder.authorize { [weak self] granted, onDevice in
                guard let self else { return }
                self.apply(self.voice.handle(.permissions(granted: granted, onDevice: onDevice)))
            }
        case .startRecording where voice.sendsOnStop:
            if !recorder.startClip(level: { [weak self] l in self?.level = l }) {
                apply(voice.handle(.failed("Couldn't open the microphone.")))
            }
        case .sendClip:
            sendVoiceNote()
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
