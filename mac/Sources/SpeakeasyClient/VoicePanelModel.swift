import SwiftUI
import SpeakeasyCore

/// Observable model the SwiftUI panel renders. The controller owns all mutation.
@MainActor
public final class VoicePanelModel: ObservableObject {
    public init() {}
    public static let defaultWidth: CGFloat = 360
    @Published public var state = VoiceState()
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
    /// Shows the pick at once and sends it after `undoWindow` unless undone.
    func commitPick(_ taskID: String, _ text: String) {
        answers[taskID] = text
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
    public var callProblem: CallProblem? { previewProblem ?? CallProblem.of(state) }
    /// Film frames show a problem without a real failure.
    @Published public var previewProblem: CallProblem?
    /// Answers already given, by task id, so a card stays settled after the tap (kept across launches).
    @Published public var answers: [String: String] = VoicePanelModel.loadAnswers() {
        didSet { VoicePanelModel.save(answers.suffix(200), key: VoicePanelModel.answersKey) }
    }
    /// Fetches a task picture (run id, card number) through the authenticated api.
    public var loadCardImage: (String, Int) async -> Data? = { _, _ in nil }
    public func questionActions(for taskID: String) -> QuestionActions {
        let runID = state.tasks.first(where: { $0.id == taskID })?.info.runID
        let load = loadCardImage
        return QuestionActions(onAnswer: { [weak self] text in
            self?.commitPick(taskID, text)
        }, answered: answers[taskID], image: runID.map { run in { number in await load(run, number) } },
           undoUntil: pendingPicks[taskID], onUndo: { [weak self] in self?.undoPick(taskID) })
    }
    /// "Not needed": set aside a task's question, decision or approval without answering it (run id).
    public var onSetAside: (String) -> Void = { _ in }
    /// Tasks the user set aside here, hidden from Needs you right away (the api confirms).
    @Published public var setAsideTasks: Set<String> = []
    /// nil shows the task list; an id opens that task.
    public var onSelectTask: (String?) -> Void = { _ in }
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
    static let answersKey = "speakeasy.questionAnswers"
    static func loadList(_ key: String) -> [String] { UserDefaults.standard.stringArray(forKey: key) ?? [] }
    static func loadAnswers() -> [String: String] {
        (UserDefaults.standard.dictionary(forKey: answersKey) as? [String: String]) ?? [:]
    }
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
                                  thumbnail: attachmentThumbnails[item.id])
        }
    }
    /// Previews of pending pictures, by attachment id (files have none).
    @Published public var attachmentThumbnails: [String: CGImage] = [:]
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
    /// "Tell it something": turn the mic on; what's said next goes to this job (job id).
    public var onTellJob: (String) -> Void = { _ in }
    /// The job waiting to hear from the user after "Tell it something".
    @Published public var tellingJobID: String?
    /// Jobs whose report text is expanded ("Full report").
    @Published public var expandedReports: Set<String> = []

    /// Every task grouped into jobs, newest first.
    public var jobs: [Job] {
        JobBoard.jobs(state.tasks, answered: Set(answers.keys), dismissed: dismissedViewTasks,
                      reviewing: Set(pinnedReviews.map(\.taskID)), setAside: setAsideTasks, now: state.now)
    }
    public var boardSections: JobBoard.Sections {
        JobBoard.sections(jobs, seen: Set(seenAnswers).union(dismissedViewTasks), now: state.now)
    }
    /// Answers the user opened (run ids, oldest first). Kept across launches, like closed cards:
    /// finished tasks come back with every call, and a read answer must stay read.
    @Published public var seenAnswers: [String] = VoicePanelModel.loadList(VoicePanelModel.seenAnswersKey) {
        didSet { UserDefaults.standard.set(Array(seenAnswers.suffix(300)), forKey: VoicePanelModel.seenAnswersKey) }
    }
    static let seenAnswersKey = "speakeasy.seenAnswers"
    /// Opening a job reads its answer: it moves from New answers to Done today.
    public func markAnswerSeen(_ job: Job) {
        guard let t = job.answerTask else { return }
        let key = JobBoard.seenKey(t)
        if !seenAnswers.contains(key) { seenAnswers.append(key) }
    }
    /// Dismiss: the answer leaves New answers without being opened (it stays in Done today).
    public func dismissAnswer(_ job: Job) { markAnswerSeen(job) }
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
            return ("\(max(failed, 1)) failed" + (needs > 0 ? " · \(needs) need\(needs == 1 ? "s" : "") you" : ""), .error)
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
    public var headerStatus: String { showingShareNotice ? shownStatus : (orbReason?.text ?? shownStatus) }
    public var headerTone: StatusTone { showingShareNotice ? shownTone : (orbReason?.tone ?? shownTone) }
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

    public init(id: String, kind: PendingAttachment.Kind, name: String, phase: PendingAttachment.Phase, thumbnail: CGImage?) {
        self.id = id; self.kind = kind; self.name = name; self.phase = phase; self.thumbnail = thumbnail
    }
}

/// A short note shown at the bottom of the panel for a few seconds.
public struct PanelToast: Equatable, Identifiable {
    public let id = UUID()
    public var text: String
    public var ok: Bool
}
