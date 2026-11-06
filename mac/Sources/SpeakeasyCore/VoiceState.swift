import Foundation

// Four independent state axes. Each event has one owner axis; in particular
// work/api events can never change `speech`, and speech events never change `work`.

public enum Finalization: Equatable, Sendable { case complete, incomplete }

public enum ConnectionPhase: Equatable, Sendable {
    case idle
    case connecting
    case live
    case ending
    /// Pause: the billed voice session is closed, the conversation and its
    /// tasks are kept, and Resume opens a new session that picks up from here.
    case paused
    case ended(Finalization)
    case failed(String)

    public var isInCall: Bool {
        switch self { case .connecting, .live, .ending: return true; default: return false }
    }
    /// In a call or paused: the conversation is still open.
    public var isOpen: Bool { isInCall || self == .paused }
}

public enum MicState: Equatable, Sendable { case live, muted }

public enum SpeechState: Equatable, Sendable {
    case idle
    /// the assistant is speaking; `lastDelta` is when the latest output transcript delta arrived.
    case speaking(lastDelta: Date)
    /// Remote audio disabled by Quiet reply until the user speaks again.
    case quieted
}

public enum WorkPhase: Equatable, Sendable {
    case none
    case waiting(since: Date)
    /// The voice handed a request off but no task ever appeared: it never reached the agent.
    case notReceived(since: Date)
    case active(short: String, detail: String?, source: String?, updatedAt: Date)
    case stale
    case approval(ApprovalInfo)
    case done(status: String, result: WorkResult?)
}

public struct Exchange: Equatable, Sendable {
    public var you = ""
    public var assistant = ""
    public var replyStarted = false
    /// Input received after a reply that so far contains only non-speech tags
    /// (e.g. `[cough]`); it must not clear the visible exchange.
    public var pendingInput = ""
    public init(you: String = "", assistant: String = "", replyStarted: Bool = false) {
        self.you = you; self.assistant = assistant; self.replyStarted = replyStarted
    }
    /// Raw provider text. Views render `cleaned`, which hides bracketed tags.
    public var isEmpty: Bool { cleaned.you.isEmpty && cleaned.assistant.isEmpty }
    public var cleaned: Exchange {
        Exchange(you: cleanTranscript(you), assistant: cleanTranscript(assistant), replyStarted: replyStarted)
    }
}

/// One side's turn in the running conversation. Nothing here is ever erased
/// during a call: a new turn only appends.
public struct TranscriptTurn: Equatable, Sendable, Identifiable {
    public enum Speaker: Equatable, Sendable { case you, assistant }
    /// `speech` is what was said on the call; `text` is a task's full written answer, shown only in the
    /// expanded transcript (the voice still speaks its short spoken version as usual). `typed` is a
    /// message the user typed: to the call (no task, `session.input_text`, counts exactly like saying it)
    /// or to one task (the reply bar, "Something else…"; carries the task). Always the user's turn.
    public enum Kind: Equatable, Sendable { case speech, text, typed }
    public var id: Int
    public var speaker: Speaker
    public var text: String
    public var kind: Kind = .speech
    /// The task a text reply answers, and its name for the bubble's label.
    public var taskID: String?
    public var taskName: String?
    public init(id: Int, speaker: Speaker, text: String) { self.id = id; self.speaker = speaker; self.text = text }
    /// What the user typed to the call itself (`session.input_text`): the user's turn, no task.
    public init(typed text: String, id: Int) {
        self.id = id; self.speaker = .you; self.text = text; self.kind = .typed
    }
    public init(textReply text: String, id: Int, taskID: String, taskName: String?) {
        self.id = id; self.speaker = .assistant; self.text = text; self.kind = .text
        self.taskID = taskID; self.taskName = taskName
    }
    /// What the user typed to one task (the reply bar, "Something else…").
    public init(typed text: String, id: Int, taskID: String, taskName: String?) {
        self.id = id; self.speaker = .you; self.text = text; self.kind = .typed
        self.taskID = taskID; self.taskName = taskName
    }
    /// Written replies stay as written: the speech cleaner would eat bracketed markdown link names.
    public var cleanedText: String {
        kind != .speech ? text.trimmingCharacters(in: .whitespacesAndNewlines) : cleanTranscript(text)
    }
}

public struct VoiceState: Equatable, Sendable {
    public var connection: ConnectionPhase = .idle
    public var mic: MicState = .live
    public var speech: SpeechState = .idle
    public var work: WorkPhase = .none
    public var exchange = Exchange()
    public var interactionID: String?
    public var runID: String?
    public var workInfo: WorkInfo?
    public var approval: ApprovalInfo?
    public var delegationAt: Date?
    public var supersededRunIDs: Set<String> = []
    public var lastError: String?
    /// Opened from the menu bar without a call (never billed).
    public var workOnly = false
    /// Every task of this call (parallel runs), oldest first.
    public var tasks: [TaskItem] = []
    /// Finished tasks the user cleared; kept out of later task snapshots.
    public var dismissedRunIDs: Set<String> = []
    /// Review cards the user dismissed ("run|card"); hidden right away, the api confirms.
    public var dismissedReviews: Set<String> = []
    /// The latest "show me" action from the server (the client acts on it once per seq).
    public var showRequest: ShowRequest?
    /// Closing the session to pause (not end) the call.
    public var pausing = false
    /// The paused call the current connection is resuming.
    public var resumeFrom: String?
    /// The whole call so far, oldest first (kept across Pause/Resume).
    public var transcript: [TranscriptTurn] = []
    /// Typed turns on screen whose other copy may still arrive: the sender's local echo and the
    /// stream's `transcript` event are the same turn (shown once, whichever comes first).
    public var typedEchoes: [TypedTurnEcho] = []
    /// Stream events (call|SSE id) already shown as typed turns: a replay doesn't show one again.
    public var typedStreamIDs: Set<String> = []
    /// Tasks whose full answer is already a text reply in `transcript` (one bubble per task).
    public var textRepliedTaskIDs: Set<String> = []
    /// The call's first task list arrived: tasks in it that were already finished (an earlier call's
    /// draft or review) are not answers of this conversation; ones that show up finished later are.
    public var tasksBaselined = false
    /// When the last piece of the user's speech arrived; transcription of his words
    /// can trail behind the assistant's first words by a moment.
    public var lastInputAt: Date?
    /// Last sign of conversation: either side's speech, a mic change, or an approval answer.
    public var lastActivityAt: Date?
    public var now: Date = Date(timeIntervalSince1970: 0)
    /// Display name of the assistant (from the server's `assistant_name`); never a persona baked in.
    public var assistantName: String = VoiceState.defaultAssistantName
    /// The call paused itself after this many quiet minutes (not the user's Pause): the paused call
    /// says "Paused after N min of quiet · your jobs keep running". Nil otherwise; cleared on Resume.
    public var quietPausedMinutes: Int?
    /// Quiet time before a live call auto-pauses (server `idle_pause_minutes`; 0 disables).
    public var idleTimeout: TimeInterval = VoiceState.defaultIdleTimeout
    /// Hint shown on the idle panel (the user's call shortcut, e.g. "⌃⌥Space").
    public var callShortcutHint: String = "Control–Option–Space"
    /// Listening on the device while the call connects (words said now aren't lost).
    public var earlyListening = false
    /// Whether the call's mic is really delivering sound (see MicCheck). `.ok` unless the client checks.
    public var micHealth: MicHealth = .ok
    /// What was heard while connecting, as transcribed so far.
    public var earlyHeard = ""
    /// Screen sharing, capture requests and pending pictures and files ("Look at this"). Off at the
    /// start of every call and every resume.
    public var sharing = CallSharing()

    public init() {}

    public static let defaultAssistantName = "Hermes"

    /// A fresh state that keeps the per-install configuration (name, idle timeout).
    public func fresh() -> VoiceState {
        var s = VoiceState()
        s.assistantName = assistantName
        s.idleTimeout = idleTimeout
        s.callShortcutHint = callShortcutHint
        // What the user cleared stays cleared: a new call or reopening the board must not let a
        // list fetched before the server had the clear bring it back (the server keeps it too).
        s.dismissedRunIDs = dismissedRunIDs
        s.dismissedReviews = dismissedReviews
        return s
    }

    /// A live call with no speech from either side for this long closes its paid voice
    /// session (pause, so Resume keeps the conversation and tasks keep running).
    public static let defaultIdleTimeout: TimeInterval = 15 * 60
    public static let speechQuietGap: TimeInterval = 6
    /// the user's words arriving this soon after his previous words are the tail
    /// of the same sentence, even if the assistant has already started replying.
    public static let lateInputGrace: TimeInterval = 2.5
    public static let staleAfter: TimeInterval = 90
    /// A handoff with no task after this long never reached the agent (routing and opening a
    /// thread take well under this; a crash or a broken install never produces a task).
    public static let notReceivedAfter: TimeInterval = 45
    public static let freshFor: TimeInterval = 45

    /// How long one copy of a typed turn waits for its other copy (local echo vs stream event).
    public static let typedEchoWindow: TimeInterval = 60

    /// Remote audio should play unless the user quieted the assistant.
    public var remoteAudioEnabled: Bool { speech != .quieted }
    /// Local capture track state; muted keeps the call (and billing) open.
    public var localAudioEnabled: Bool { mic == .live }
    public var activeTasks: [TaskItem] { tasks.filter(\.isActive) }
    public var canPause: Bool { connection == .live && interactionID != nil && !pausing }
    /// Quiet for `idleTimeout` on a live call. Not while the assistant is talking or an approval waits on the user.
    public func idleExpired(at now: Date) -> Bool {
        guard connection == .live, !pausing, approval == nil, let last = lastActivityAt else { return false }
        if case .speaking = speech { return false }
        guard idleTimeout > 0 else { return false }
        return now.timeIntervalSince(last) >= idleTimeout
    }
}

public enum VoiceEvent: Equatable, Sendable {
    case startRequested
    case workOnlyOpened
    case sessionAdmitted(interactionID: String)
    case sessionStarted
    case inputDelta(String)
    case outputDelta(String)
    case delegationCreated
    case sessionClosed
    case serverClosed(Finalization, error: String?)
    case endRequested
    case endTimedOut
    case transportLost
    case failed(String)
    case error(String)
    case toggleMic
    case setMic(MicState)
    case quietAssistant
    case work(WorkInfo?)
    case interaction(InteractionSnapshot)
    case approval(ApprovalInfo?)
    case approvalResolved
    case tasks([TaskItem])
    /// the server asked the panel to show a task's image ("show me").
    case show(ShowRequest)
    /// the user cleared finished tasks (the x); hidden right away, api confirms.
    case tasksDismissed([String])
    /// the user dismissed a review card (run id, card numbers); hidden right away, api confirms.
    case reviewDismissed(String, [Int])
    case pauseRequested
    /// The call went quiet for `minutes`: pause it the same way, and say why once it's paused.
    case quietPauseRequested(minutes: Int)
    /// The api refused to hold the call: stay live.
    case pauseFailed(String)
    case resumeRequested
    case tick
    case reset
    /// On-device listening while connecting started (true) or stopped (false).
    case earlyListening(Bool)
    /// Words heard while connecting, so far.
    case earlyHeard(String)
    /// The mic check's verdict changed.
    case micHealth(MicHealth)
    /// Screen sharing, capture requests, pictures and files.
    case sharing(SharingEvent)
    /// The user typed to a task (sent): the user's turn in the transcript, kind `typed`.
    case typedToTask(text: String, taskID: String, taskName: String?)
    /// A turn typed on a call screen that the server took (`session.input_text`): this device's own
    /// send after the 200, or the call's SSE `transcript` event. Text is as the server took it.
    case typedTurn(String, origin: TypedTurnOrigin)
}

/// Pure reducer. `now` is supplied by the caller so tests are deterministic.
public func reduce(_ state: VoiceState, _ event: VoiceEvent, now: Date) -> VoiceState {
    var s = state
    s.now = now
    switch event {
    case .startRequested:
        s = s.fresh()
        s.now = now
        s.connection = .connecting

    case .workOnlyOpened:
        // Already showing the board (back to the foreground, a second open): keep it as it is,
        // never blank it while the next list loads.
        if !s.connection.isInCall && !s.workOnly {
            s = s.fresh()
            s.now = now
            s.workOnly = true
        }

    case .sessionAdmitted(let id):
        s.interactionID = id
        s.sharing.admitted()

    case .earlyListening(let on):
        s.earlyListening = on && s.connection == .connecting
        if !s.earlyListening { s.earlyHeard = "" }

    case .micHealth(let health):
        s.micHealth = health

    case .earlyHeard(let text):
        if s.earlyListening { s.earlyHeard = text; s.lastActivityAt = now }

    case .pauseRequested:
        if s.canPause { s.pausing = true; s.connection = .ending; s.speech = .idle; s.quietPausedMinutes = nil }

    case .quietPauseRequested(let minutes):
        if s.canPause { s.pausing = true; s.connection = .ending; s.speech = .idle; s.quietPausedMinutes = max(1, minutes) }

    case .pauseFailed(let message):
        if s.pausing && s.connection == .ending { s.connection = .live }
        s.pausing = false
        s.quietPausedMinutes = nil
        s.lastError = message

    case .resumeRequested:
        guard s.connection == .paused else { break }
        // Keep the conversation, tasks and mic choice; open a new session.
        s.quietPausedMinutes = nil
        s.resumeFrom = s.interactionID
        s.interactionID = nil
        s.connection = .connecting
        s.speech = .idle
        s.lastError = nil
        s.exchange.replyStarted = true   // next words start a fresh visible exchange
        s.lastInputAt = nil
        s.sharing.resumed()

    case .tasks(let tasks):
        let before = s.tasks
        // A cleared task stays gone; one that runs again (the server never clears a running task) shows.
        s.tasks = tasks.filter { !($0.isSettled && s.dismissedRunIDs.contains($0.info.runID ?? "")) }
        appendTextReplies(&s, before: before)
        s.tasksBaselined = true
        // The list carries the newest flags for the header's run too (cleared or opened elsewhere).
        if let run = s.workInfo?.runID, let row = tasks.first(where: { $0.info.runID == run })?.info {
            if row.dismissed { s.workInfo?.dismissed = true }
            if row.seen { s.workInfo?.seen = true }
        }

    case .show(let request):
        s.showRequest = request

    case .typedToTask(let text, let taskID, let taskName):
        guard let clean = TaskMessageRequest.clean(text) else { break }
        s.transcript.append(TranscriptTurn(typed: clean, id: s.transcript.count, taskID: taskID, taskName: taskName))

    case .tasksDismissed(let runIDs):
        let ids = Set(runIDs)
        let cleared = s.tasks.filter { $0.isSettled && ids.contains($0.info.runID ?? "") }
        s.dismissedRunIDs.formUnion(cleared.compactMap(\.info.runID))
        s.tasks.removeAll { task in cleared.contains(task) }
        // The header's run is cleared too, even when the list no longer showed it.
        if let info = s.workInfo, let run = info.runID, ids.contains(run), isSettledWork(info) {
            s.dismissedRunIDs.insert(run)
        }

    case .reviewDismissed(let runID, let cards):
        s.dismissedReviews.formUnion(cards.map { "\(runID)|\($0)" })

    case .sessionStarted:
        if s.connection == .connecting { s.connection = .live }
        s.lastActivityAt = now

    case .inputDelta(let text):
        guard !text.isEmpty else { break }
        let heardBefore = cleanTranscript(s.exchange.you)
        let heardWords: Bool
        // The end of the user's sentence can be transcribed after the assistant'starts
        // talking; that is the same turn, not a new one.
        let unfinished = heardBefore.last.map { !".?!".contains($0) } ?? false
        let trailing = s.exchange.replyStarted && unfinished && s.exchange.pendingInput.isEmpty
            && s.lastInputAt.map { now.timeIntervalSince($0) < VoiceState.lateInputGrace } == true
        if s.exchange.replyStarted && !trailing {
            let candidate = s.exchange.pendingInput + text
            // A cough or throat-clear after the assistant's reply is not a new turn.
            if cleanTranscript(candidate).isEmpty { s.exchange.pendingInput = candidate; break }
            s.exchange = Exchange(you: candidate)
            s.transcript.append(TranscriptTurn(id: s.transcript.count, speaker: .you, text: candidate))
            heardWords = true
        } else {
            s.exchange.you += text
            if trailing, let i = s.transcript.lastIndex(where: { $0.speaker == .you && $0.kind == .speech }) {
                s.transcript[i].text += text
            } else if let i = s.transcript.lastIndex(where: { $0.kind == .speech }), s.transcript[i].speaker == .you {
                // A text reply may sit after the user's unfinished sentence; the sentence still continues.
                s.transcript[i].text += text
            } else {
                s.transcript.append(TranscriptTurn(id: s.transcript.count, speaker: .you, text: text))
            }
            heardWords = cleanTranscript(s.exchange.you) != heardBefore
        }
        s.lastInputAt = now
        if heardWords { s.lastActivityAt = now }
        // the user speaking real words again un-quiets the assistant and interrupts the speaking label.
        if s.mic == .live && heardWords { s.speech = .idle }

    case .typedTurn(let raw, let origin):
        // One complete user turn of its own: it never joins the user's spoken words and never runs
        // into the assistant's reply, and it leaves the speaking label and mic state alone.
        let text = TypedText.normalized(raw)
        guard !text.isEmpty, s.connection.isOpen else { break }
        if case .stream(let id?) = origin {
            let key = "\(s.interactionID ?? s.resumeFrom ?? "")|\(id)"
            guard !s.typedStreamIDs.contains(key) else { break }   // a replayed event
            s.typedStreamIDs.insert(key)
        }
        s.typedEchoes.removeAll { now.timeIntervalSince($0.at) > VoiceState.typedEchoWindow }
        if let i = s.typedEchoes.firstIndex(where: { $0.text == text && $0.local != origin.isLocal }) {
            s.typedEchoes.remove(at: i)   // the other copy of this turn is already on screen
            break
        }
        s.typedEchoes.append(TypedTurnEcho(text: text, local: origin.isLocal, at: now))
        if s.typedEchoes.count > 16 { s.typedEchoes.removeFirst(s.typedEchoes.count - 16) }
        s.transcript.append(TranscriptTurn(typed: text, id: s.transcript.count))
        // The next spoken words or reply start fresh turns after it (no late-input join either).
        s.exchange = Exchange(you: text, replyStarted: true)
        s.lastInputAt = nil
        s.lastActivityAt = now

    case .outputDelta(let text):
        guard !text.isEmpty else { break }
        s.exchange.replyStarted = true
        s.exchange.assistant += text
        s.lastActivityAt = now
        if let last = s.transcript.last, last.speaker == .assistant, last.kind == .speech {
            s.transcript[s.transcript.count - 1].text += text
        } else if case .speaking = s.speech, let i = s.transcript.lastIndex(where: { $0.kind == .speech }),
                  s.transcript[i].speaker == .assistant, s.transcript[(i + 1)...].allSatisfy({ $0.kind == .text }) {
            // A text reply landed mid-sentence: the rest of the sentence stays with its start.
            s.transcript[i].text += text
        } else {
            s.transcript.append(TranscriptTurn(id: s.transcript.count, speaker: .assistant, text: text))
        }
        if s.speech != .quieted { s.speech = .speaking(lastDelta: now) }

    case .delegationCreated:
        if let old = s.runID { s.supersededRunIDs.insert(old) }
        s.runID = nil
        s.workInfo = nil
        s.delegationAt = now

    case .sessionClosed:
        if !isEnded(s.connection) && s.connection != .paused { s.connection = .ended(.complete) }
        s.speech = .idle

    case .serverClosed(let fin, let error):
        if !isEnded(s.connection) && s.connection != .paused { s.connection = .ended(fin) }
        if let error, !s.pausing { s.lastError = error }
        s.speech = .idle

    case .endRequested:
        switch s.connection {
        case .connecting where s.interactionID == nil:
            s.connection = .ended(.complete)   // nothing admitted, nothing billed
        case .connecting, .live:
            s.connection = .ending
        case .ending where s.pausing:
            s.pausing = false   // End while pausing: the close already in flight ends the call
        case .paused:
            s.connection = .ended(.complete)   // voice session already closed; nothing billed
        default: break
        }

    case .endTimedOut:
        if s.connection == .ending { s.connection = .ended(.incomplete) }

    case .transportLost:
        switch s.connection {
        case .live, .connecting:
            s.connection = .ended(.incomplete)
            s.lastError = "Voice connection lost; final usage confirmation unavailable"
            s.speech = .idle
        default: break   // .ending: wait for api finalization or the deadline
        }

    case .failed(let message):
        if !isEnded(s.connection) { s.connection = .failed(message) }
        s.lastError = message
        s.speech = .idle

    case .error(let message):
        s.lastError = message

    case .toggleMic:
        if s.connection == .live || s.connection == .connecting { s.mic = s.mic == .live ? .muted : .live; s.lastActivityAt = now }

    case .setMic(let mic):
        if s.connection == .live || s.connection == .connecting { s.mic = mic; s.lastActivityAt = now }

    case .quietAssistant:
        if s.connection == .live { s.speech = .quieted }

    case .work(let info):
        if let info {
            if let run = info.runID, s.supersededRunIDs.contains(run) { break }
            if let current = s.runID, let run = info.runID, run != current { break }
            if s.runID == nil { s.runID = info.runID }
        }
        s.workInfo = info

    case .interaction(let snapshot):
        if let run = snapshot.runID, !s.supersededRunIDs.contains(run), run != s.runID {
            if let old = s.runID { s.supersededRunIDs.insert(old) }
            s.runID = run
            if s.workInfo?.runID != run { s.workInfo = nil }
            if s.delegationAt == nil { s.delegationAt = now }
        }
        s.approval = snapshot.approval
        if let error = snapshot.error { s.lastError = error }
        // Sharing state only from this call's own snapshots, and never while it pauses (the plugin
        // has dropped what was waiting; it goes again after Resume).
        if s.connection.isInCall, !s.pausing, let current = s.interactionID,
           snapshot.interactionID == nil || snapshot.interactionID == current {
            var own = snapshot
            own.interactionID = current
            s.sharing.reconcile(own, now: now)
        }
        switch snapshot.finalization {
        case "confirmed", "complete":
            if s.connection.isInCall && s.connection != .connecting { s.connection = .ended(.complete) }
        case "incomplete":
            if s.connection == .ending { s.connection = .ended(.incomplete) }
        default: break
        }

    case .approval(let approval):
        s.approval = approval

    case .approvalResolved:
        s.approval = nil
        s.lastActivityAt = now

    case .tick:
        if case .speaking(let last) = s.speech, now.timeIntervalSince(last) >= VoiceState.speechQuietGap {
            s.speech = .idle
        }
        s.sharing.tick(now: now)

    case .sharing(let event):
        let live = (s.connection == .live || s.connection == .connecting) && !s.pausing
        s.sharing.apply(event, now: now, admitted: s.interactionID, live: live)

    case .reset:
        s = s.fresh()
        s.now = now
    }
    // A pausing call that closes (cleanly or not) is paused, not ended: the
    // api holds its transcript and tasks for Resume.
    if s.pausing, case .ended = s.connection {
        s.connection = .paused
        s.pausing = false
        s.speech = .idle
        if s.lastError == "Voice connection lost; final usage confirmation unavailable" { s.lastError = nil }
    }
    // Paused: sharing is off and nothing is captured; pictures and files not sent yet wait for Resume.
    // Ended: nothing of the call's sharing is kept.
    if s.connection == .paused { s.sharing.callClosed(paused: true) }
    else if !s.connection.isOpen { s.sharing.callClosed(paused: false) }
    forgetClearedWork(&s)
    s.work = deriveWork(s, now: now)
    return s
}

private func isEnded(_ phase: ConnectionPhase) -> Bool {
    if case .ended = phase { return true }
    if case .failed = phase { return true }
    return false
}

/// A task of this call that finished with a written answer becomes one text reply in the
/// transcript (shown only in the expanded view). The voice is untouched: it still speaks the short
/// spoken version. A task of this call counts even when it first shows up already finished (a quick
/// task, or the first list after a reconnect); earlier calls' carried tasks never do.
private func appendTextReplies(_ s: inout VoiceState, before: [TaskItem]) {
    guard !s.workOnly else { return }
    let previous = Dictionary(before.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
    for task in s.tasks where task.info.status == "completed" && !s.textRepliedTaskIDs.contains(task.id) {
        if let was = previous[task.id] {
            guard !was.info.isTerminal else { continue }
        } else if task.info.carried {
            continue
        } else if !s.tasksBaselined, !task.info.emailDrafts.isEmpty || !task.info.reviewImages.isEmpty {
            // An older server doesn't mark carried tasks: on the call's first list, one holding a
            // draft or review card is an earlier call's.
            continue
        }
        // Home commands ("lights off") have nothing to read; the server already counts them as seen.
        guard !task.info.seen, let full = textReply(for: task.info.result) else { continue }
        s.textRepliedTaskIDs.insert(task.id)
        s.transcript.append(TranscriptTurn(textReply: full, id: s.transcript.count, taskID: task.id,
                                           taskName: task.info.title ?? task.info.summary))
    }
}

/// The written answer worth a text reply: the full text, unless it says nothing more than the
/// spoken line the voice reads out anyway (compared ignoring case, punctuation and spacing).
public func textReply(for result: WorkResult?) -> String? {
    guard let full = result?.full?.trimmingCharacters(in: .whitespacesAndNewlines), !full.isEmpty else { return nil }
    return replyWords(full) == replyWords(result?.spoken ?? "") ? nil : full
}

/// Lowercased words only, so "Lights off." and "lights off" read the same.
private func replyWords(_ text: String) -> String {
    text.lowercased()
        .components(separatedBy: CharacterSet.alphanumerics.inverted)
        .filter { !$0.isEmpty }
        .joined(separator: " ")
}

private func isSettledWork(_ info: WorkInfo) -> Bool {
    info.isTerminal || info.status == "rejected" || info.status == "ambiguous"
}

/// Whether a finished run is off the user's plate: cleared from the board (here or on another
/// device) or, for a failure, already opened. The header must not keep repeating it.
public func isClearedWork(_ info: WorkInfo, dismissedRunIDs: Set<String>) -> Bool {
    guard isSettledWork(info) else { return false }
    if info.dismissed { return true }
    if info.status == "failed" && info.seen { return true }
    return info.runID.map(dismissedRunIDs.contains) ?? false
}

/// The header follows the board: once the user clears (or opens) the finished run it shows, the
/// line goes quiet, never "Work failed" again and never "didn't get that" from the handoff time it
/// leaves behind. The run can't come back as the header's run either (the board view keeps fetching
/// the latest run, which may be this one); a newer run takes over as usual.
private func forgetClearedWork(_ s: inout VoiceState) {
    guard let info = s.workInfo, isClearedWork(info, dismissedRunIDs: s.dismissedRunIDs) else { return }
    if let run = info.runID {
        s.supersededRunIDs.insert(run)
        if s.runID == run { s.runID = nil }
    }
    s.workInfo = nil
    s.delegationAt = nil
}

/// Work axis is a pure function of api-owned inputs; never of speech or mic.
public func deriveWork(_ s: VoiceState, now: Date) -> WorkPhase {
    if let approval = s.approval { return .approval(approval) }
    guard let info = s.workInfo else {
        if let since = s.delegationAt {
            // A follow-up or a status question joins work that's already running instead of making a
            // new row: while anything is running, the request reached Hermes; never say it didn't.
            let busy = s.tasks.contains { !$0.info.isTerminal }
            return now.timeIntervalSince(since) > VoiceState.notReceivedAfter && !busy ? .notReceived(since: since)
                                                                                       : .waiting(since: since)
        }
        return .none
    }
    if info.isTerminal { return .done(status: info.status, result: info.result) }
    if info.stale { return .stale }
    if info.status == "waiting_for_approval" {
        // An email draft is the approval: it's on screen waiting for the user, not for Hermes.
        if !info.emailDrafts.isEmpty { return .approval(ApprovalInfo(runID: info.runID ?? "", requestID: "",
                                                                     description: "Review the email draft")) }
        return waitingFallback(info, s, now)
    }
    // Local staleness: the api row itself has not moved for 90 s.
    if let heartbeat = info.updated ?? info.updatedAt, now.timeIntervalSince(heartbeat) > VoiceState.staleAfter {
        return .stale
    }
    if let short = info.shortStatus, short != "Status unconfirmed", let at = info.updatedAt ?? info.updated {
        return .active(short: short, detail: info.detail, source: info.statusSource, updatedAt: at)
    }
    return waitingFallback(info, s, now)
}

private func waitingFallback(_ info: WorkInfo, _ s: VoiceState, _ now: Date) -> WorkPhase {
    // Approval pending per work status, but the approval object hasn't arrived: honest waiting.
    let since = info.events.first { $0.kind == "request" }?.at ?? s.delegationAt ?? info.updated ?? now
    return .waiting(since: since)
}
