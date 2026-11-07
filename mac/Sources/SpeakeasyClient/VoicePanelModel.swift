import Combine
import SwiftUI
import SpeakeasyCore

/// Observable model the SwiftUI panel renders. The controller owns all mutation.
@MainActor
public final class VoicePanelModel: ObservableObject {
    public init() {
        // The update notice lives inside scrolling content hosted on its own (the board): pass its
        // changes on so the panel redraws and re-measures, or the card's height arrives a draw late
        // and the panel clips its top and bottom.
        pluginUpdateRelay = pluginUpdate.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
        bindPending()
    }
    private var pluginUpdateRelay: AnyCancellable?
    private var pendingRelay: AnyCancellable?
    /// The app's one pending-pick store (shared with the session pages); tests give a model its own.
    public var pending: PendingPicks = .shared { didSet { bindPending() } }
    private func bindPending() {
        pendingRelay = pending.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
    }
    public static let defaultWidth: CGFloat = 360
    @Published public var state = VoiceState() {
        didSet { if state.tasks != oldValue.tasks { reconcilePicks() } }
    }
    @Published public var workExpanded = false
    @Published public var captionExpanded = false
    /// Drafts whose body is expanded ("Show all"). Lives here, not in the card, so the panel re-measures and
    /// the card's Show less / Send row stays inside the window.
    @Published public var expandedDraftIDs: Set<String> = []
    /// Status line text after the minimum-dwell debounce.
    @Published public var shownStatus = ""
    @Published public var shownTone: StatusTone = .plain
    @Published public var busy = false
    /// Live captions of the conversation (Settings › General).
    @Published public var showCaptions = true
    /// e.g. "⌃⌥M: tap to mute/unmute, hold to talk". Empty when no shortcut.
    @Published public var muteShortcutHint = ""
    /// 0...1 audio level the orb follows (assistant voice while speaking, mic while listening).
    @Published public var orbLevel: Double = 0
    /// the user's panel size: width, plus extra height given to the transcript and
    /// Work scroll areas. Set by dragging the panel's right/bottom edge.
    @Published public var panelWidth: CGFloat = VoicePanelModel.defaultWidth
    @Published public var extraHeight: CGFloat = 0

    /// Orb mode (was "slim"): the panel folds into a small orb with the mic and hang-up buttons. The orb's
    /// colour says what's going on (talking, working, something needs you, a failure). Remembered across
    /// calls under the old key, so an existing "slim" preference opens as the orb.
    @Published public var slim: Bool = UserDefaults.standard.bool(forKey: VoicePanelModel.slimKey) {
        didSet { UserDefaults.standard.set(slim, forKey: VoicePanelModel.slimKey) }
    }
    public nonisolated static let slimKey = "panel.slim"
    public var onToggleSlim: () -> Void = {}
    /// Slim only applies to an open call; a finished or work-only panel is always full.
    public var showsSlim: Bool { slim && !state.workOnly && state.connection.isOpen }

    /// The first-call tour is running in this call (shows a Skip tour button).
    @Published public var tourActive = false
    public var onSkipTour: () -> Void = {}
    /// The plugin-update notice (shown on the board / panel with no call) and its Update button.
    public let pluginUpdate = PluginUpdateNotice()
    public var onPluginUpdate: () -> Void = {}
    /// "Open the job" on the update notice: the update job's run id.
    public var onOpenPluginUpdateJob: (String) -> Void = { _ in }
    /// Older plugins (no `start_jobs`): copy the request instead.
    public var onCopyPluginUpdate: () -> Void = {}

    // Listening mode (Mac). It lives beside the call, not in it: the strip shows only while no
    // call is open, and turning listening off starts the call.
    /// What the listening strip shows; nil hides it.
    @Published public var room: RoomPresentation?
    /// A one-off note about listening mode ("stopped when your Mac went to sleep…"), shown in the
    /// strip until dismissed or listening is turned on again.
    @Published public var roomNotice: String?
    /// The panel offers the Listening mode button (supported here, and no call is open).
    @Published public var roomOffered = false
    /// Why the button is disabled (e.g. the plugin needs an update); nil when it can be turned on.
    @Published public var roomBlocked: String?
    /// What it heard can still be asked about or discarded (on, or kept after it stopped by itself).
    @Published public var roomCanAsk = false
    /// e.g. "⌃⌥L: turn listening mode on or off". Empty when no shortcut.
    @Published public var roomShortcutHint = ""
    /// Turn listening mode on, or off into a call.
    public var onToggleRoom: () -> Void = {}
    /// Stop listening and drop what was heard, without a call.
    public var onDiscardRoom: () -> Void = {}
    /// Start the call that carries what was heard (turning listening off if it's on).
    public var onAskRoom: () -> Void = {}
    public var onDismissRoomNotice: () -> Void = {}

    public var onToggleMic: () -> Void = {}
    public var onStart: () -> Void = {}
    public var onEnd: () -> Void = {}
    public var onApproval: (String) -> Void = { _ in }
    public var onStopWork: () -> Void = {}
    public var onCloseWork: () -> Void = {}
    public var onToggleWork: () -> Void = {}
    public var onTogglePause: () -> Void = {}
    public var onStopTask: (String) -> Void = { _ in }
    /// Answer a task's question card (task id, picked option).
    public var onAnswer: (String, String) -> Void = { _, _ in }
    /// How long a pick waits before it's sent, so Undo can take it back.
    public static let undoWindow: TimeInterval = 5
    /// Picks waiting out the undo window (task id → when it sends).
    @Published public var pendingPicks: [String: Date] = [:]
    private var pickTimers: [String: DispatchWorkItem] = [:]
    /// Take back a pick still inside its undo window.
    public func undoPick(_ taskID: String) {
        pickTimers.removeValue(forKey: taskID)?.cancel()
        pendingPicks[taskID] = nil
        answers[taskID] = nil
    }
    /// Whether this model holds picks for `undoWindow` itself. The iPhone app holds them in its own
    /// layer (and sends Live Activity picks at once), so only the Mac panel does it here.
    #if os(macOS)
    public var holdsPicksForUndo = true
    #else
    public var holdsPicksForUndo = false
    #endif
    /// Shows the pick at once and sends it after `undoWindow` unless undone.
    func commitPick(_ taskID: String, _ text: String) {
        answers[taskID] = text
        guard holdsPicksForUndo else { onAnswer(taskID, text); return }
        pickTimers.removeValue(forKey: taskID)?.cancel()
        let item = DispatchWorkItem { [weak self] in
            guard let self, self.pendingPicks[taskID] != nil else { return }
            self.pendingPicks[taskID] = nil
            self.pickTimers[taskID] = nil
            self.onAnswer(taskID, text)
        }
        pickTimers[taskID] = item
        pendingPicks[taskID] = Date().addingTimeInterval(Self.undoWindow)
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.undoWindow, execute: item)
    }

    // MARK: Server features, card actions, toasts
    /// What the server can do beyond the base api; nil = an older plugin (new controls hidden).
    @Published public var capabilities: ServerCapabilities?
    /// Runs a card button (task id, the card, the action); the reply calls back with (ok, message).
    public var onCardAction: (String, ViewCard, CardAction, @escaping (Bool, String) -> Void) -> Void = { _, _, _, done in
        done(false, "This server can't do that yet")
    }
    /// Retry a failed task (task id); only offered while a call can take it.
    public var onRetry: (String) -> Void = { _ in }
    public var canRetry: Bool { state.connection.isOpen && !state.workOnly }
    /// The fix button on a call problem (Open Settings, Restart call, Try again).
    public var onFixProblem: (CallProblem.Fix) -> Void = { _ in }
    /// A short note at the bottom of the panel ("Office lamp is off.").
    @Published public var toast: PanelToast?
    public func showToast(_ text: String, ok: Bool = true) {
        let t = PanelToast(text: text, ok: ok)
        toast = t
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.5) { [weak self] in
            if self?.toast?.id == t.id { self?.toast = nil }
        }
    }
    /// Card buttons with a request in flight ("task|view|action").
    @Published public var cardActionBusy: Set<String> = []
    public func runCardAction(taskID: String, card: ViewCard, action: CardAction) {
        let key = "\(taskID)|\(card.viewIndex)|\(action.id)"
        guard !cardActionBusy.contains(key) else { return }
        cardActionBusy.insert(key)
        onCardAction(taskID, card, action) { [weak self] ok, message in
            self?.cardActionBusy.remove(key)
            self?.showToast(message, ok: ok)
        }
    }
    /// Job-view sections the user opened ("job|details", "job|steps").
    @Published public var openSections: Set<String> = []
    public func toggleSection(_ key: String) {
        if openSections.contains(key) { openSections.remove(key) } else { openSections.insert(key) }
    }
    /// The voice's state right now: listening, hearing you, thinking, speaking.
    public var voicePhase: VoicePhase { state.voicePhase(userTalking: userTalking) }
    /// A call problem with a one-click fix, if there is one.
    public var callProblem: CallProblem? {
        previewProblem ?? CallProblem.of(state, canSignInCodex: capabilities?.codexLogin == true,
                                         canUpdatePlugin: pluginUpdate.canUpdateNow)
    }
    /// Film frames show a problem without a real failure.
    @Published public var previewProblem: CallProblem?
    /// The one record of decisions acted on here that the server hasn't confirmed yet (SPK-79): picks,
    /// ✕ dismisses and "Not needed", keyed by question id (the asking task's run id). Kept across
    /// launches until the server's `question_state` says the question closed; everything else about a
    /// question is shown from the server.
    public var picks: PickStore {
        get { pending.store }
        set { pending.update { $0 = newValue } }
    }
    /// The question id a task's pick is kept under: its run id once it has one, else its task id.
    public func questionID(_ taskID: String) -> String {
        state.tasks.first(where: { $0.id == taskID })?.info.runID ?? taskID
    }
    private func pickEntry(_ taskID: String) -> PickStore.Entry? {
        picks[questionID(taskID)] ?? picks[taskID]
    }
    private func setPick(_ taskID: String, _ entry: PickStore.Entry?) {
        var next = picks
        next.remove(taskID)
        next.set(questionID(taskID), entry)
        picks = next
    }
    private func pickedTasks(_ kind: PickStore.Kind) -> Set<String> {
        var out = Set(state.tasks.filter { pickEntry($0.id)?.kind == kind }.map(\.id))
        for (id, entry) in picks.entries where entry.kind == kind { out.insert(id) }
        return out
    }
    /// Answers by task id: a pick waiting for the server, else the words the server recorded
    /// (`question_answer`, answered on any device). Setting one records or drops a pending pick.
    public var answers: [String: String] {
        get {
            var out: [String: String] = [:]
            for (id, entry) in picks.entries where entry.kind == .answer { out[id] = entry.text }
            for t in state.tasks {
                if let e = pickEntry(t.id), e.kind == .answer { out[t.id] = e.text }
                else if t.info.questionStateOrDerived == .answered {
                    // Answered on another device (or before a relaunch): the server's words, or just
                    // "answered" from a plugin that doesn't record them.
                    out[t.id] = t.info.questionAnswer ?? out[t.id] ?? ""
                }
            }
            return out
        }
        set {
            let old = answers
            for id in old.keys where newValue[id] == nil {
                if pickEntry(id)?.kind == .answer { setPick(id, nil) }
            }
            for (id, text) in newValue where old[id] != text {
                setPick(id, PickStore.Entry(kind: .answer, text: text))
            }
        }
    }
    /// Drop picks the server has settled (its `question_state` left `open`), so nothing local outlives
    /// the server's word: a relaunch shows exactly what the server says.
    public func reconcilePicks() {
        let byID = Dictionary(state.tasks.map { ($0.info.runID ?? $0.id, $0) }, uniquingKeysWith: { a, _ in a })
        pending.reconcile { id in byID[id]?.info.questionStateOrDerived }
    }
    /// Fetches a task picture (run id, card number) through the authenticated api.
    public var loadCardImage: (String, Int) async -> Data? = { _, _ in nil }
    public func questionActions(for taskID: String) -> QuestionActions {
        let runID = state.tasks.first(where: { $0.id == taskID })?.info.runID
        let load = loadCardImage
        let jobID = taskMessagesEnabled ? jobs.first(where: { $0.tasks.contains { $0.id == taskID } })?.id : nil
        return QuestionActions(onAnswer: { [weak self] text in
            self?.commitPick(taskID, text)
        }, answered: answers[taskID], image: runID.map { run in { number in await load(run, number) } },
           undoUntil: pendingPicks[taskID], onUndo: { [weak self] in self?.undoPick(taskID) },
           onSomethingElse: jobID.map { id in { [weak self] text in self?.sendReply(jobID: id, text: text, choiceTaskID: taskID) ?? false } })
    }
    /// "Not needed": set aside a task's question, decision or approval without answering it (run id).
    public var onSetAside: (String) -> Void = { _ in }
    /// ✕ on a decision (a task's question): close it on every device without telling the agent (run id).
    public var onDismissQuestion: (String) -> Void = { _ in }
    /// Tasks whose question was dismissed here: off Needs you at once (a pending pick until the server
    /// confirms; `picks`).
    public var dismissedQuestionTasks: Set<String> {
        get { pickedTasks(.dismissed) }
        set { updatePicked(.dismissed, newValue) }
    }
    /// Tasks the user set aside here, hidden from Needs you right away (the api confirms; `picks`).
    public var setAsideTasks: Set<String> {
        get { pickedTasks(.setAside) }
        set { updatePicked(.setAside, newValue) }
    }
    private func updatePicked(_ kind: PickStore.Kind, _ ids: Set<String>) {
        let old = pickedTasks(kind)
        for id in old.subtracting(ids) { if pickEntry(id)?.kind == kind { setPick(id, nil) }; if picks[id]?.kind == kind { picks.remove(id) } }
        for id in ids.subtracting(old) { setPick(id, PickStore.Entry(kind: kind)) }
    }
    /// nil shows the task list; an id opens that task.
    public var onSelectTask: (String?) -> Void = { _ in }
    /// Open one task from the task list: its session page when it has one, else the panel's task view.
    public var onOpenTask: (String) -> Void = { _ in }
    /// Clear finished tasks (run ids) from the list.
    public var onDismissTasks: ([String]) -> Void = { _ in }
    /// Exact-run, authenticated image load through the api; no retailer request from the app.
    public var loadProductImage: (String, Int) async -> Data? = { _, _ in nil }
    /// The live "what it's looking at" image for a run (authenticated route; nil when none yet).
    public var loadLiveImage: (String) async -> Data? = { _ in nil }
    /// Task opened from the list (Work view), nil = list / current task.
    @Published public var selectedTaskID: String?
    /// e.g. "⌃⌥P". Empty when no shortcut.
    @Published public var pauseShortcutHint = ""

    // MARK: Email drafts
    /// Approve/Deny/Revise on a draft (draft as shown, action, revise instructions).
    public var onDraftAction: (EmailDraft, DraftAction, String?) -> Void = { _, _, _ in }
    /// Drafts with a request in flight (buttons disabled).
    @Published public var draftBusy: Set<String> = []
    /// Optimistic status after a tap, keyed "draftID|sha256" so a new version starts clean.
    @Published public var draftLocalStatus: [String: EmailDraft.Status] = [:]
    /// Per-draft notice, e.g. after a 409 "The draft changed — review it again".
    @Published public var draftNotice: [String: String] = [:]

    public func displayedStatus(of draft: EmailDraft) -> EmailDraft.Status {
        guard draft.status == .pending else { return draft.status }   // the server has moved on
        return draftLocalStatus["\(draft.draftID)|\(draft.sha256)"] ?? .pending
    }

    /// Pending drafts to pin in the call panel (like approvals).
    public var pinnedDrafts: [EmailDraft] {
        state.pendingDrafts.filter { displayedStatus(of: $0) == .pending || draftBusy.contains($0.draftID) }
    }

    // MARK: Answer cards (price, weather, game…)
    /// Tasks whose cards the user closed in the call panel (they stay in the task itself). Kept
    /// across launches: finished tasks come back with every call, and a closed card must stay closed.
    @Published public var dismissedViewTasks: Set<String> = Set(VoicePanelModel.loadList(VoicePanelModel.dismissedKey)) {
        didSet { UserDefaults.standard.set(Array(dismissedViewTasks.suffix(300)), forKey: VoicePanelModel.dismissedKey) }
    }
    public func dismissPinnedViews(_ taskID: String) { dismissedViewTasks.insert(taskID) }
    /// How long an unanswered question stays pinned. Past that it lives only in its task: a
    /// question from an old task must not greet you on every call.
    public static let questionPinnedFor: TimeInterval = 20 * 60
    static let dismissedKey = "speakeasy.dismissedCardTasks"
    static func loadList(_ key: String) -> [String] { UserDefaults.standard.stringArray(forKey: key) ?? [] }
    static func save(_ pairs: some Sequence<(key: String, value: String)>, key: String) {
        UserDefaults.standard.set(Dictionary(pairs.map { ($0.key, $0.value) }, uniquingKeysWith: { a, _ in a }), forKey: key)
    }
    /// The newest task with cards, shown in the call panel while it's fresh: a quick answer's card
    /// appears as it is spoken and stays until closed or until it's three minutes old.
    public var pinnedViews: (taskID: String, views: [ViewCard])? {
        // An unanswered question stays up (it's waiting on you); other cards fade after three minutes.
        if let asking = state.tasks.last(where: { t in t.info.views.contains { $0.kind == "question" }
                && answers[t.id] == nil && !dismissedViewTasks.contains(t.id) && t.info.isTerminal
                && !t.info.setAside && !setAsideTasks.contains(t.id)
                && t.info.updatedAt.map { Date().timeIntervalSince($0) < Self.questionPinnedFor } ?? false }) {
            return (asking.id, asking.info.views)
        }
        guard let task = state.tasks.last(where: { !$0.info.views.isEmpty && !dismissedViewTasks.contains($0.id) }) else { return nil }
        if let at = task.info.updatedAt, Date().timeIntervalSince(at) > 180 { return nil }
        return (task.id, task.info.views)
    }

    /// The job whose decision the pinned question card is showing, options and all (nil when the
    /// pinned card isn't a question). A board beside it leaves out its "Other options".
    public var pinnedQuestionJobID: String? {
        guard let pinned = pinnedViews, pinned.views.first?.kind == "question" else { return nil }
        return JobBoard.jobDeciding(taskID: pinned.taskID, in: jobs)
    }

    // MARK: Image review cards
    /// Dismiss a review card (run id); its images stay inside the task.
    public var onDismissReview: (String) -> Void = { _ in }
    /// Which image each review card shows (run id -> index into its images).
    @Published public var reviewIndex: [String: Int] = [:]
    /// The review card shown large (run id); nil = the newest. The others are compact rows.
    @Published public var focusedReviewID: String?
    /// Review cards to pin in the call panel (like drafts): most recent three.
    public var pinnedReviews: [ImageReview] { state.pendingReviews }

    /// The review card on screen when it has more than one image: reviews live in their job's view,
    /// so this is the open job's review.
    public var steppableReview: ImageReview? {
        guard workExpanded, let job = selectedJob else { return nil }
        let ids = Set(job.tasks.map(\.id))
        return pinnedReviews.last(where: { ids.contains($0.taskID) && $0.images.count > 1 })
    }

    /// Arrow keys on the panel: step the open review card's images. False when there's nothing to step
    /// (no review showing, one image, or the work view covers the cards) so the key goes elsewhere.
    @discardableResult
    public func stepOpenReview(by delta: Int) -> Bool {
        guard let review = steppableReview else { return false }
        let open = review.runID
        let current = min(reviewIndex[open] ?? 0, review.images.count - 1)
        reviewIndex[open] = ImageReviewLayout.step(current, by: delta, count: review.images.count)
        return true
    }

    // MARK: Look at this (screen, pictures and files)
    /// The screen button: unavailable (this call can't share), off, on, or needs Screen Recording.
    public var screenState: ScreenButton { state.sharing.button }
    /// Grows each time the plugin asks for the button to be noticed (`screen.hint`): pulse once per increase.
    public var screenHintPulse: Int { state.sharing.hintPulse }
    /// Why it last asked (`screen.hint` reason, e.g. "permission"); nil after a resume.
    public var screenHintReason: String? { state.sharing.hint }
    /// The screen button or shortcut.
    public var onToggleScreen: () -> Void = {}
    /// Speakeasy Settings › General › Screen, where Screen Recording is allowed (the "Set up" on the
    /// needs-permission line).
    public var onOpenScreenSettings: () -> Void = {}
    /// e.g. "⌃⌥S: share your screen or stop". Empty when no shortcut.
    @Published public var screenShortcutHint = ""

    /// This Hermes takes pictures and files (drops and pastes are offered).
    @Published public var attachmentsSupported = false
    /// A drag the panel would take hovers over it (thin accent ring).
    @Published public var dropTargeted = false
    /// Pictures and files waiting to go with the next request, in drop order.
    public var pendingAttachments: [PendingAttachmentItem] {
        state.sharing.visibleAttachments.map { item in
            PendingAttachmentItem(id: item.id, kind: item.kind, name: item.name, phase: item.phase,
                                  thumbnail: attachmentThumbnails[item.id], duration: attachmentDurations[item.id],
                                  progress: attachmentProgress[item.id], failed: attachmentFailures.contains(item.id))
        }
    }
    /// Previews of pending pictures, by attachment id (files have none).
    @Published public var attachmentThumbnails: [String: CGImage] = [:]
    @Published public var attachmentDurations: [String: Double] = [:]
    @Published public var attachmentProgress: [String: Double] = [:]
    @Published public var attachmentFailures: Set<String> = []
    public var onRetryAttachment: (String) -> Void = { _ in }
    /// Dropped or pasted items, already encoded with `AttachmentEncoder`.
    public var onAttach: ([EncodedAttachment]) -> Void = { _ in }
    /// An item that couldn't be encoded: its `AttachmentFailure` raw value, shown as a short line.
    public var onAttachRefused: (String) -> Void = { _ in }
    /// The ✕ on a pending picture or file (its id).
    public var onRemoveAttachment: (String) -> Void = { _ in }

    /// A request waiting for the screen, or how that ended (nil: none).
    public var hold: ScreenHold? { state.sharing.hold }
    /// "Waiting for your screen", "Not sent: screen sharing was off", or nil.
    public var holdText: String? { state.sharing.hold?.line }
    /// A short line about sharing ("Up to 3 at a time", "Screen Recording is off · Set up"); also
    /// on the status line while fresh. `action` says what tapping it does.
    public var shareNotice: ShareNotice? {
        guard let notice = state.sharing.notice, notice.isFresh(at: state.now) else { return nil }
        return notice
    }
    /// A staged attachment's local thumbnail (`OutgoingAttachment.id`) for a sent bubble; the phone's
    /// composer keeps them. Nil draws the file/picture symbol.
    public var outgoingThumbnail: (String) -> CGImage? = { _ in nil }
    /// A screen capture or picture a task carried (run id, `SharedItem.number`), through the authenticated api.
    public var loadSharedImage: (String, Int) async -> Data? = { _, _ in nil }

    // MARK: Panel visibility
    /// The close (x) button: hide the panel (ends nothing).
    public var onClosePanel: () -> Void = {}
    /// Pointer is over the panel (holds the post-call auto-hide).
    public var hovering = false

    public var presentation: PillPresentation { present(state) }

    /// Two or more tasks in this call: show the list.
    public var showsTaskList: Bool { state.tasks.count >= 2 }
    public var selectedTask: TaskItem? { selectedTaskID.flatMap { id in state.tasks.first { $0.id == id } } }

    // MARK: Jobs: the board, the job view and the orb

    /// Width of the folded orb panel.
    public static let orbWidth: CGFloat = 132
    /// Width of the folded orb panel in a call that can share its screen: the eye sits beside the mic,
    /// so sharing stays visible (and can be stopped) while folded.
    public static let orbWidthWithScreen: CGFloat = 164
    public var foldedWidth: CGFloat { screenState == .unavailable ? Self.orbWidth : Self.orbWidthWithScreen }
    /// The panel's width right now: the orb when folded, else the user's width.
    public var displayWidth: CGFloat { showsSlim ? foldedWidth : panelWidth }
    /// The job open in the job view (a `Job.id`); nil shows the board.
    @Published public var selectedJobID: String?
    /// The board tile the arrow keys are on.
    @Published public var boardFocusID: String?
    /// Open a job (nil = back to the board). The client also tells the call which job is on screen.
    public var onOpenJob: (String?) -> Void = { _ in }
    /// One task's full activity with its tool calls' args/result previews (`GET /voice/work/{run_id}`,
    /// servers with `tool_log`), for the tool-call log; nil when it can't be fetched.
    public var loadToolLog: (String) async -> [JobActivityItem]? = { _ in nil }
    /// One task's helpers with their own steps and previews (`GET /voice/work/{run_id}`, servers
    /// with `job_subagents`), for a helper's page; nil when it can't be fetched.
    public var loadSubagents: (String) async -> [JobSubagent]? = { _ in nil }
    /// "Tell it something": turn the mic on; what's said next goes to this job (job id).
    public var onTellJob: (String) -> Void = { _ in }
    /// The job waiting to hear from the user after "Tell it something".
    @Published public var tellingJobID: String?
    /// Jobs whose report text is expanded ("Full report").
    @Published public var expandedReports: Set<String> = []

    /// Every task grouped into jobs, newest first.
    public var jobs: [Job] {
        JobBoard.jobs(state.tasks, answered: Set(answers.keys), dismissed: dismissedViewTasks.union(dismissedQuestionTasks),
                      reviewing: Set(pinnedReviews.map(\.taskID)), setAside: setAsideTasks, now: state.now)
    }
    public var boardSections: JobBoard.Sections {
        var s = JobBoard.sections(jobs, seen: Set(seenAnswers).union(dismissedViewTasks), now: state.now)
        if !hiddenRunning.isEmpty {
            s.running.removeAll { job in job.activeTask.map { hiddenRunning[job.id] == $0.id } ?? false }
        }
        if !hiddenJobs.isEmpty {
            s.needsYou.removeAll { hiddenJobs.contains($0.id) }
            s.newAnswers.removeAll { hiddenJobs.contains($0.id) }
            s.running.removeAll { hiddenJobs.contains($0.id) }
            s.doneToday.removeAll { hiddenJobs.contains($0.id) }
        }
        return s
    }
    /// Answers the user opened (run ids, oldest first). Kept across launches, like closed cards:
    /// finished tasks come back with every call, and a read answer must stay read.
    @Published public var seenAnswers: [String] = VoicePanelModel.loadList(VoicePanelModel.seenAnswersKey) {
        didSet { UserDefaults.standard.set(Array(seenAnswers.suffix(300)), forKey: VoicePanelModel.seenAnswersKey) }
    }
    static let seenAnswersKey = "speakeasy.seenAnswers"
    /// Opening a job reads its answer: it moves from New answers to Done today.
    public func markAnswerSeen(_ job: Job) {
        guard let t = job.newTask else { return }
        let key = JobBoard.seenKey(t)
        if !seenAnswers.contains(key) {
            seenAnswers.append(key)
            if let run = t.info.runID { onAnswerSeen(run) }
        }
    }
    /// Tells the server an answer was opened (run id), so the voice won't read it out later.
    public var onAnswerSeen: (String) -> Void = { _ in }
    /// Dismiss: the answer leaves New answers without being opened (it stays in Done today). Pictures
    /// to look at are dismissed with it, so they don't come back on the next call.
    public func dismissAnswer(_ job: Job) {
        markAnswerSeen(job)
        if let run = job.lookTask?.info.runID { onDismissReview(run) }
    }

    /// Running jobs the user dismissed from the board (job id → the task that was running): hidden
    /// while that task runs, not stopped. When it settles the job comes back with its answer.
    @Published public var hiddenRunning: [String: String] = [:]

    /// The board's Dismiss on any card: Needs you is set aside ("Not needed"), a new answer is
    /// marked read, a running job is hidden without stopping it, and a finished or failed job is
    /// cleared from the board. False when there is nothing to dismiss.
    @discardableResult
    public func dismissFromBoard(_ job: Job) -> Bool {
        switch job.stage {
        case .needsYou:
            if dismissQuestion(job) { return true }
            if setAside(job) { return true }
            // An email draft to check: it leaves the board only through Send / Deny.
            return false
        case .running:
            guard let task = job.activeTask else { return false }
            hiddenRunning[job.id] = task.id
            return true
        case .done where job.newTask != nil && boardSections.newAnswers.contains { $0.id == job.id }:
            dismissAnswer(job)
            return true
        case .done, .failed:
            let runs = job.tasks.filter(\.isSettled).compactMap(\.info.runID)
            guard !runs.isEmpty else { return false }
            if let run = job.lookTask?.info.runID { onDismissReview(run) }
            onDismissTasks(runs)
            return true
        }
    }

    // MARK: Dismiss with Undo (touch)

    /// Whether the board holds a dismissal for `undoWindow` before telling the server (the iPhone
    /// app shows an Undo toast meanwhile). The Mac dismisses at once.
    public var holdsDismissalsForUndo = false
    /// A dismissal was held: show Undo (job, then the undo to run).
    public var onBoardDismissHeld: (Job, @escaping () -> Void) -> Void = { _, _ in }
    /// Finished or failed jobs dismissed but still inside the undo window: off the board meanwhile.
    @Published public private(set) var hiddenJobs: Set<String> = []
    /// Held dismissals (job id → what's sent when the window ends, and what Undo puts back).
    private var heldDismissals: [String: (commit: () -> Void, undo: () -> Void)] = [:]

    /// The board's Dismiss on touch: the card leaves at once (a new answer moves to Done today, a
    /// running job is hidden, a decision is set aside, a finished job leaves the board) and the
    /// server is told after `undoWindow` unless Undo is tapped. Without `holdsDismissalsForUndo`
    /// it is `dismissFromBoard`. False when there is nothing to dismiss.
    @discardableResult
    public func dismissFromBoardWithUndo(_ job: Job) -> Bool {
        guard canDismissFromBoard(job) else { return false }
        guard holdsDismissalsForUndo else { return dismissFromBoard(job) }
        let id = job.id
        var commit: () -> Void = {}
        var undo: () -> Void = {}
        switch job.stage {
        case .needsYou where questionTarget(job) != nil:
            guard let task = questionTarget(job), let run = task.info.runID else { return false }
            dismissedQuestionTasks.insert(task.id)
            commit = { [weak self] in self?.onDismissQuestion(run) }
            undo = { [weak self] in self?.dismissedQuestionTasks.remove(task.id) }
        case .needsYou:
            guard let task = setAsideTarget(job), let run = task.info.runID else { return false }
            setAsideTasks.insert(task.id)
            commit = { [weak self] in self?.onSetAside(run) }
            undo = { [weak self] in self?.setAsideTasks.remove(task.id) }
        case .running:
            guard let task = job.activeTask else { return false }
            hiddenRunning[id] = task.id
            undo = { [weak self] in self?.hiddenRunning[id] = nil }
        case .done where job.newTask != nil && boardSections.newAnswers.contains { $0.id == id }:
            let key = job.newTask.map(JobBoard.seenKey)
            markAnswerSeen(job)
            let look = job.lookTask?.info.runID
            commit = { [weak self] in if let look { self?.onDismissReview(look) } }
            undo = { [weak self] in if let key { self?.seenAnswers.removeAll { $0 == key } } }
        case .done, .failed:
            let runs = job.tasks.filter(\.isSettled).compactMap(\.info.runID)
            guard !runs.isEmpty else { return false }
            let look = job.lookTask?.info.runID
            hiddenJobs.insert(id)
            commit = { [weak self] in
                if let look { self?.onDismissReview(look) }
                self?.onDismissTasks(runs)
                self?.hiddenJobs.remove(id)
            }
            undo = { [weak self] in self?.hiddenJobs.remove(id) }
        }
        heldDismissals[id] = (commit, undo)
        onBoardDismissHeld(job) { [weak self] in self?.undoDismissal(id) }
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.undoWindow) { [weak self] in self?.commitDismissal(id) }
        return true
    }

    /// Undo a held dismissal: the card comes back where it was; nothing reaches the server.
    public func undoDismissal(_ jobID: String) {
        heldDismissals.removeValue(forKey: jobID)?.undo()
    }

    /// Send a held dismissal now (its window ended, or the app is leaving the foreground).
    public func commitDismissal(_ jobID: String) {
        heldDismissals.removeValue(forKey: jobID)?.commit()
    }

    /// Send every held dismissal now (the app is going to the background).
    public func commitHeldDismissals() {
        for id in Array(heldDismissals.keys) { commitDismissal(id) }
    }

    /// Whether a dismissal of this job is waiting out its undo window.
    public func isDismissalHeld(_ jobID: String) -> Bool { heldDismissals[jobID] != nil }

    /// Whether the board can dismiss this job's card (an email draft waits for Send / Deny).
    public func canDismissFromBoard(_ job: Job) -> Bool {
        switch job.stage {
        case .needsYou: return setAsideTarget(job) != nil
        case .running: return job.activeTask != nil
        case .done, .failed: return job.newTask != nil || job.tasks.contains { $0.isSettled && $0.info.runID != nil }
        }
    }

    /// The run a board card's Stop would stop (the job's running task), nil when it can't be stopped here.
    public func stoppableRun(_ job: Job) -> String? {
        guard job.stage == .running, let task = job.activeTask, task.canStop else { return nil }
        return task.info.runID
    }
    /// Answers finished today that the user hasn't opened.
    public var unreadAnswers: Int { boardSections.newAnswers.count }
    /// The orb's calm blue dot: answers to read and nothing more pressing.
    public var orbAnswerDot: Bool { orbState.showsAnswerDot(unread: unreadAnswers) }

    /// Why the orb has its colour, in a few words, for the header's second line: "1 needs you" next
    /// to amber, "2 new answers" next to the blue dot, "1 failed" next to red, "2 running" next to
    /// violet. nil when the orb is calm or listening: the live status shows instead.
    public var orbReason: (text: String, tone: StatusTone)? {
        let answers = unreadAnswers == 1 ? "1 new answer" : "\(unreadAnswers) new answers"
        switch orbState {
        case .failed(let needs):
            let failed = jobs.filter { $0.stage == .failed }.count
            return ("\(max(failed, 1)) didn't finish" + (needs > 0 ? " · \(needs) need\(needs == 1 ? "s" : "") you" : ""), .error)
        case .needsYou(let n):
            return ("\(n) need\(n == 1 ? "s" : "") you", .attention)
        case .working:
            let running = boardSections.running.count
            let base = running == 1 ? "1 running" : "\(max(running, 1)) running"
            return (orbAnswerDot ? base + " · " + answers : base, .plain)
        case .idle, .talking:
            return orbAnswerDot ? (answers, .plain) : nil
        }
    }
    /// The header's second line: the orb's reason when it has one, else the live status.
    /// A fresh screen-sharing notice ("Screen Recording is off · Set up", "Up to 3 at a time") is on
    /// the status line now: it wins over the orb's summary, which would otherwise hide it (and its tap).
    private var showingShareNotice: Bool { shareNotice.map { $0.text == shownStatus } ?? false }
    /// The call paused itself for quiet: the header says so (the board below shows what needs you).
    private var quietPauseStatus: String? {
        guard state.connection == .paused, let minutes = state.quietPausedMinutes else { return nil }
        return quietPauseLine(minutes)
    }
    public var headerStatus: String {
        if let quiet = quietPauseStatus { return quiet }
        // A problem with a fix button under the header: the status line points at it, not repeats it.
        if callProblem != nil && !showingShareNotice && orbReason == nil { return "One click to fix it, below" }
        // While the user is talking, the line under "Hearing you" must not say a reply is coming.
        if voicePhase == .hearing && !showingShareNotice && orbReason == nil { return "Keep going" }
        return showingShareNotice ? shownStatus : (orbReason?.text ?? shownStatus)
    }
    public var headerTone: StatusTone { quietPauseStatus != nil ? .plain : showingShareNotice ? shownTone : (orbReason?.tone ?? shownTone) }
    public var selectedJob: Job? { selectedJobID.flatMap { id in jobs.first { $0.id == id } } }
    /// The board replaces the task list once there's more than one task, anything needs you, or a
    /// task brought back a card. Cards never pile up under the board: each lives in its job's view.
    public var showsBoard: Bool {
        guard !showsSlim, !state.workOnly, !state.tasks.isEmpty else { return false }
        return showsTaskList || hasTaskCards || jobs.contains { $0.stage.needsYou || $0.stage == .failed }
    }
    /// Some task has a card, draft or review to show (they open from the board, never pinned under it).
    public var hasTaskCards: Bool {
        pinnedViews != nil || !pinnedDrafts.isEmpty || !pinnedReviews.isEmpty
    }

    // MARK: Cards inside a job view: one open, the rest one-line rows

    /// Cards the user opened or closed in a job view, by "job id|card key". Unset = the default
    /// (the most relevant card open, the others folded).
    @Published public var cardOpen: [String: Bool] = [:]
    public func isCardOpen(job: String, key: String, index: Int) -> Bool {
        cardOpen["\(job)|\(key)"] ?? (index == 0)
    }
    public func toggleCard(job: String, key: String, index: Int) {
        cardOpen["\(job)|\(key)"] = !isCardOpen(job: job, key: key, index: index)
    }
    /// Pictures that came with an answer fold to one row by default ("1 picture ›").
    public func isPicturesOpen(job: String, run: String) -> Bool {
        cardOpen["\(job)|pictures:\(run)"] ?? false
    }
    public func togglePictures(job: String, run: String) {
        cardOpen["\(job)|pictures:\(run)"] = !isPicturesOpen(job: job, run: run)
    }
    /// The mic is open and hearing the user right now.
    public var userTalking: Bool { (presentation.mark == .listening || presentation.mark == .thinking) && orbLevel > 0.08 }
    public var orbState: OrbState { OrbState.of(jobs: jobs, approvalPending: approvalPending, userTalking: userTalking) }
    /// The call's approval, unless the user set its task aside (amber only while something still needs them).
    var approvalPending: Bool {
        guard let approval = state.approval else { return false }
        return !state.tasks.contains { $0.info.runID == approval.runID && ($0.info.setAside || setAsideTasks.contains($0.id)) }
    }

    /// The next job that needs you, other than `id` (the "Next:" link).
    public func nextJobNeedingYou(after id: String?) -> Job? {
        boardSections.needsYou.first { $0.id != id && $0.stage.needsYou }
    }

    /// Answer the job's waiting decision with option `number` (1-based). False when there's nothing to pick.
    @discardableResult
    public func pick(_ number: Int, in job: Job) -> Bool {
        guard let task = job.decisionTask, let decision = task.info.decision, answers[task.id] == nil,
              (1...decision.options.count).contains(number) else { return false }
        questionActions(for: task.id).onAnswer?(decision.options[number - 1].text)
        return true
    }

    /// Approve: answer with the recommended option, only when the plugin allowed it.
    public func approve(_ job: Job) {
        guard let d = job.decision, d.approvable, let r = d.recommended else { return }
        pick(r + 1, in: job)
    }

    /// The task "Not needed" would set aside: the job's waiting question or decision, else its
    /// approval. nil when nothing there waits on the user (or it has no run to address).
    public func setAsideTarget(_ job: Job) -> TaskItem? {
        guard job.stage == .needsYou(.decide) || job.stage == .needsYou(.approve) else { return nil }
        let task = job.decisionTask ?? job.tasks.last { $0.info.status == "waiting_for_approval" }
        return task?.info.runID == nil ? nil : task
    }

    /// The task whose question (not an approval) the job waits on: what the board's Dismiss closes.
    public func questionTarget(_ job: Job) -> TaskItem? {
        guard job.stage == .needsYou(.decide), let task = job.decisionTask, task.info.runID != nil,
              task.info.status != "waiting_for_approval" else { return nil }
        return task
    }

    /// Dismiss a decision: the question leaves Needs you on every device and the agent is told
    /// nothing (unlike "Not needed", which tells a waiting run to wrap up). False: no question waits.
    @discardableResult
    public func dismissQuestion(_ job: Job) -> Bool {
        guard let task = questionTarget(job), let run = task.info.runID else { return false }
        dismissedQuestionTasks.insert(task.id)
        onDismissQuestion(run)
        return true
    }

    /// "Not needed": set aside what the job waits on without answering it (already handled, or not
    /// needed). It leaves Needs you at once and lands in Done today as "Set aside"; the api closes it,
    /// tells a run still waiting to wrap up, and tells the voice quietly so it never asks again.
    @discardableResult
    public func setAside(_ job: Job) -> Bool {
        guard let task = setAsideTarget(job), let run = task.info.runID else { return false }
        setAsideTasks.insert(task.id)
        onSetAside(run)
        return true
    }

    /// Undo a finished step of a job: the request goes to the job through the same answer route as
    /// anything else said to it (no separate undo endpoint), so the agent decides how to roll it back.
    public func undoStep(_ step: String, in job: Job) {
        onAnswer(job.newest.id, "Undo this step: \(step)")
    }

    // MARK: Messages to one task (typed)

    /// The server takes typed messages to a task (`capabilities.task_messages`). Without it the
    /// reply bar and "Something else…" are not shown at all.
    public var taskMessagesEnabled: Bool { capabilities?.taskMessages == true }
    /// Messages on their way to tasks (and the last ones sent), one at a time in order.
    @Published public var outbox = TaskOutbox()
    /// What's in each job's reply field (job id → text), kept while the user moves around.
    @Published public var replyDrafts: [String: String] = [:]
    /// Put the cursor in this job's reply field (the Live Activity's Reply, `?reply=1`).
    @Published public var replyFocusJobID: String?
    /// Sends one message (the client posts it); call back with the reply, or a plain failure sentence.
    public var deliverMessage: (OutgoingMessage, @escaping (TaskMessageReply?, String?) -> Void) -> Void = { _, done in
        done(nil, "This server can't take typed messages yet.")
    }
    /// A note after a send ("Sent to Speed up the dashboard", or the failure). The iPhone shows its own toast.
    public var onMessageNotice: (OutgoingMessage) -> Void = { _ in }

    /// Where a job's reply goes: the run that's working, else the one waiting on the user, else the newest.
    public func replyTarget(_ job: Job) -> (runID: String, taskName: String)? {
        let task = job.activeTask ?? job.decisionTask ?? job.newest
        guard let run = task.info.runID ?? job.newest.info.runID else { return nil }
        return (run, job.name)
    }

    /// Send what's in a job's reply field (or `text`). False when there's nothing to send or no target.
    @discardableResult
    public func sendReply(jobID: String, text: String? = nil, choiceTaskID: String? = nil,
                          source: TaskMessageSource = .typed, attachments: [OutgoingAttachment] = []) -> Bool {
        guard taskMessagesEnabled, let job = jobs.first(where: { $0.id == jobID }) else { return false }
        let body = text ?? replyDrafts[jobID] ?? ""
        let run: String?
        if let choiceTaskID { run = state.tasks.first { $0.id == choiceTaskID }?.info.runID ?? replyTarget(job)?.runID }
        else { run = replyTarget(job)?.runID }
        guard let run, outbox.enqueue(jobID: jobID, runID: run, taskName: job.name, text: body,
                                      source: source, choiceTaskID: choiceTaskID, attachments: attachments) != nil else { return false }
        if text == nil { replyDrafts[jobID] = nil }
        if let choiceTaskID, let clean = TaskMessageRequest.clean(body) { answers[choiceTaskID] = clean }
        pumpOutbox()
        return true
    }

    /// Retry a failed message (same client id, so a send that did land isn't doubled).
    public func retryMessage(_ id: String) {
        guard let m = outbox.message(id) else { return }
        outbox.retry(id)
        if let choice = m.choiceTaskID { answers[choice] = m.text }
        pumpOutbox()
    }

    public func discardMessage(_ id: String) { outbox.discard(id) }

    /// Sends the next queued message once nothing else is on the wire.
    public func pumpOutbox() {
        guard let next = outbox.startNext() else { return }
        deliverMessage(next) { [weak self] reply, failure in
            guard let self else { return }
            if let reply {
                self.outbox.markSent(next.id, as: reply.deliveredAs)
            } else {
                self.outbox.markFailed(next.id, failure ?? TaskMessageCopy.failure(status: nil, serverMessage: nil))
                // "Something else…" that didn't go: the decision is open again.
                if let choice = next.choiceTaskID, self.answers[choice] == next.text { self.answers[choice] = nil }
            }
            if let settled = self.outbox.message(next.id) { self.onMessageNotice(settled) }
            self.pumpOutbox()
        }
    }

    /// Board and job-view keys: up/down move between tiles, Return opens, 1-4 picks an option on the
    /// focused decision (or the open job's), N sets it aside ("Not needed"). False lets the key go elsewhere.
    public func handleKey(code: UInt16, characters: String) -> Bool {
        if showsSlim { return false }
        // Escape: out of a job, back to the board.
        if code == 53 && selectedJobID != nil { onOpenJob(nil); return true }
        let digit = Int(characters).flatMap { (1...4).contains($0) ? $0 : nil }
        let notNeeded = characters.lowercased() == "n"
        if workExpanded || state.workOnly {
            guard let job = selectedJob else { return false }
            if notNeeded { return setAside(job) }
            guard let digit else { return false }
            return pick(digit, in: job)
        }
        guard showsBoard else { return false }
        let all = boardSections.all
        guard !all.isEmpty else { return false }
        let at = all.firstIndex { $0.id == boardFocusID }
        switch code {
        case 125: boardFocusID = all[min((at ?? -1) + 1, all.count - 1)].id; return true
        case 126: boardFocusID = all[max((at ?? all.count) - 1, 0)].id; return true
        case 36, 76:
            guard let at else { return false }
            onOpenJob(all[at].id); return true
        default:
            if notNeeded {
                // The focused tile, else the first thing waiting on the user.
                let target = at.map { all[$0] } ?? all.first { setAsideTarget($0) != nil }
                return target.map { setAside($0) } ?? false
            }
            guard let digit else { return false }
            let target = at.map { all[$0] } ?? all.first { $0.stage == .needsYou(.decide) }
            return target.map { pick(digit, in: $0) } ?? false
        }
    }
}

/// A picture or file on the panel's pending row.
public struct PendingAttachmentItem: Identifiable {
    public let id: String
    public let kind: PendingAttachment.Kind
    public let name: String
    public let phase: PendingAttachment.Phase
    /// Pictures only, once made.
    public let thumbnail: CGImage?
    public var duration: Double?
    public var progress: Double?
    public var failed: Bool

    public init(id: String, kind: PendingAttachment.Kind, name: String, phase: PendingAttachment.Phase, thumbnail: CGImage?,
                duration: Double? = nil, progress: Double? = nil, failed: Bool = false) {
        self.id = id; self.kind = kind; self.name = name; self.phase = phase; self.thumbnail = thumbnail
        self.duration = duration; self.progress = progress; self.failed = failed
    }
}

/// A short note shown at the bottom of the panel for a few seconds.
public struct PanelToast: Equatable, Identifiable {
    public let id = UUID()
    public var text: String
    public var ok: Bool
}
