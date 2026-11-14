import Foundation

// Pure presentation logic for the native popup. Views render this; they do
// not decide wording or which axis wins.

/// Parses one data-channel message into reducer events.
public func parseDataChannelMessage(_ data: Data) -> [VoiceEvent] {
    guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let type = object["type"] as? String else { return [] }
    switch type {
    case "session.started": return [.sessionStarted]
    case "session.input_transcript.delta": return [.inputDelta(object["delta"] as? String ?? "")]
    case "session.output_transcript.delta": return [.outputDelta(object["delta"] as? String ?? "")]
    case "input_transcript.added":
        guard let text = (object["item"] as? [String: Any])?["text"] as? String else { return [] }
        return [.inputDelta(text)]
    case "output_transcript.added":
        guard let text = (object["item"] as? [String: Any])?["text"] as? String else { return [] }
        return [.outputDelta(text)]
    case "session.delegation.created", "delegation.created": return [.delegationCreated]
    case "session.closed": return [.sessionClosed]
    case "error":
        let message = (object["error"] as? [String: Any])?["message"] as? String ?? "Something went wrong"
        return [.error(message)]
    default: return []
    }
}

/// Parses one api SSE event (contract v2) into reducer events.
public func parseServerStreamEvent(_ event: SSEEvent) -> [VoiceEvent] {
    let json = try? JSONSerialization.jsonObject(with: Data(event.data.utf8), options: [.fragmentsAllowed])
    switch event.name {
    case "snapshot":
        guard let object = json as? [String: Any] else { return [] }
        var events: [VoiceEvent] = []
        if let interaction = InteractionSnapshot(json: object["interaction"]) { events += interactionEvents(interaction) }
        if let work = WorkInfo(json: object["work"]) { events.append(.work(work)) }
        if object.keys.contains("approval") { events.append(.approval(ApprovalInfo(json: object["approval"]))) }
        if let tasks = TaskItem.list(json: object["tasks"]) { events.append(.tasks(tasks)) }
        return events
    case "interaction":
        return InteractionSnapshot(json: json).map(interactionEvents) ?? []
    case "work":
        return WorkInfo(json: json).map { [.work($0)] } ?? []
    case "approval":
        return [.approval(ApprovalInfo(json: json))]
    case "tasks":
        return TaskItem.list(json: json).map { [.tasks($0)] } ?? []
    case "show":
        return ShowRequest(json: json).map { [.show($0)] } ?? []
    case "capture":
        // The plugin wants a capture of the frontmost window for a request (screen sharing is on).
        guard let id = nonEmpty((json as? [String: Any])?["capture_id"]) else { return [] }
        return [.sharing(.captureRequested(id))]
    case "screen.hint":
        // A request needs the screen while sharing is off (or Screen Recording is): the button pulses.
        let reason = nonEmpty((json as? [String: Any])?["reason"]) ?? "screen"
        return [.sharing(.hint(String(reason.prefix(40))))]
    case "screen.state":
        // Sharing changed on the plugin's side ("stop looking at my screen").
        return ScreenToggle(json: json).map { [.sharing(.state($0))] } ?? []
    case "transcript":
        // A turn typed on a call screen joined the call (plugin input-text). Spoken turns don't come
        // here; the sender's own local echo of the same turn is matched in the reducer, not shown twice.
        // A copy carrying a task_id is a message typed to one job (POST /voice/work/{run}/message): the
        // sender already showed it as `typedToTask`, with the job's name, so it isn't a call turn.
        guard let object = json as? [String: Any], object["role"] as? String == "user",
              object["source"] as? String == "typed", nonEmpty(object["task_id"]) == nil,
              let text = nonEmpty(object["text"]) else { return [] }
        return [.typedTurn(text, origin: .stream(id: event.id))]
    case "closed":
        let object = json as? [String: Any]
        let fin: Finalization = (object?["finalization"] as? String) == "complete" ? .complete : .incomplete
        return [.serverClosed(fin, error: object?["error"] as? String, reason: nonEmpty(object?["reason"]))]
    case "reconnect_required":
        // The voice provider dropped the call; the plugin holds it for a resume.
        return [.reconnectRequired(interactionID: nonEmpty((json as? [String: Any])?["interaction_id"]))]
    default:
        return []
    }
}

public enum StatusTone: Equatable, Sendable {
    case plain        // static secondary text
    case glimmer      // fresh, active authored/tool status
    case attention    // approval — high contrast
    case warning      // stale / unconfirmed
    case success
    case error
}

public struct PillPresentation: Equatable, Sendable {
    public enum Mark: Equatable, Sendable { case speaking, listening, thinking, muted, connecting, idle }
    public var mark: Mark
    public var primary: String
    public var secondary: String
    public var tone: StatusTone
    public var micLabel: String
    public var micMuted: Bool
    public var micEnabled: Bool
    public var showQuiet: Bool
    public var statusClickable: Bool
    public var showWorkStop: Bool
    /// Pause/Resume button: nil when hidden.
    public var pauseLabel: String?
    public var pauseEnabled: Bool
}

public func formatElapsed(_ seconds: TimeInterval) -> String {
    let total = max(0, Int(seconds))
    if total < 60 { return "\(total)s" }
    if total < 3600 { return "\(total / 60)m \(String(format: "%02d", total % 60))s" }
    return "\(total / 3600)h \(String(format: "%02d", (total % 3600) / 60))m"
}

/// `failure` names why a failed task failed ("Didn't finish · Out of credits"); pass the task's
/// `WorkInfo.failure`.
public func workStatusLine(_ work: WorkPhase, now: Date,
                           assistantName: String = VoiceState.defaultAssistantName,
                           failure: WorkFailure? = nil) -> (String, StatusTone)? {
    switch work {
    case .none: return nil
    case .waiting(let since): return ("Waiting for \(assistantName) · \(formatElapsed(now.timeIntervalSince(since)))", .plain)
    case .active(let short, _, _, let at):
        let fresh = now.timeIntervalSince(at) <= VoiceState.freshFor
        return (short, fresh ? .glimmer : .plain)
    case .stale: return ("Status unconfirmed", .warning)
    case .notReceived: return ("\(assistantName) didn't get that", .warning)
    case .approval: return ("Needs your approval", .attention)
    case .done(let status, let result):
        switch status {
        case "completed":
            // The answer itself is in the transcript and Work view; the status
            // line only names what finished.
            if let label = result?.label { return ("Done · \(label)", .success) }
            return ("Done", .success)
        case "cancelled": return ("Work stopped", .plain)
        case "failed":
            if let label = failure?.label { return ("Didn't finish · \(label)", .error) }
            return ("Didn't finish", .error)
        default: return ("Work ended · \(status)", .warning)
        }
    }
}

/// One task's status line in the task list (same wording as the call's status).
public func taskStatusLine(_ info: WorkInfo, now: Date,
                           assistantName: String = VoiceState.defaultAssistantName) -> (String, StatusTone) {
    if info.status == "waiting_for_approval" { return ("Needs your approval", .attention) }
    if info.status == "cancel_requested" { return ("Stopping…", .plain) }
    if info.status == "waiting_on_task" {
        // Held so two tasks don't edit one project at once; it starts when that one ends.
        return (info.waitingFor.map { "Waiting for \u{201C}\($0)\u{201D}" } ?? "Waiting for another task", .plain)
    }
    var s = VoiceState()
    s.now = now
    s.workInfo = info
    s.delegationAt = info.events.first { $0.kind == "request" }?.at ?? info.updated ?? now
    return workStatusLine(deriveWork(s, now: now), now: now, assistantName: assistantName,
                          failure: info.failure) ?? ("Working", .plain)
}

/// The task detail's explanation under "Didn't finish": what happened and what to do, or nil when
/// the task didn't fail (or the api gave no reason).
public func failureDetail(_ info: WorkInfo?) -> String? {
    guard let info, info.status == "failed" else { return nil }
    return info.failure?.text
}

public func present(_ s: VoiceState) -> PillPresentation {
    let now = s.now
    let muted = s.mic == .muted
    var mark: PillPresentation.Mark
    var primary: String
    switch s.connection {
    case .idle:
        mark = .idle; primary = s.workOnly ? "\(s.assistantName)'s work" : s.assistantName
    case .connecting:
        if s.micHealth == .repairing { mark = .connecting; primary = "Fixing the mic…" }
        else if s.earlyListening && !muted { mark = .listening; primary = "Listening" }
        else { mark = .connecting; primary = "Connecting…" }
    case .live:
        if case .speaking = s.speech { mark = .speaking; primary = s.assistantName }
        else if muted { mark = .muted; primary = "Muted" }
        else if s.micHealth == .broken { mark = .idle; primary = "Mic isn't working" }
        else if s.micHealth == .repairing { mark = .connecting; primary = "Fixing the mic…" }
        // The user stopped talking and the reply hasn't started: say it's being worked on.
        else if s.isThinking { mark = .thinking; primary = "Thinking…" }
        // While the mic is still being checked it shows as Listening: the check only speaks up
        // when it finds a real fault, so a working call never flashes a mic status.
        else { mark = .listening; primary = "Listening" }
    case .ending:
        mark = .connecting
        primary = s.micHealth == .repairing ? "Fixing the mic…" : (s.pausing ? "Pausing…" : "Ending…")
    case .paused:
        mark = .idle; primary = "Paused"
    case .ended(.complete):
        mark = .idle; primary = "Call ended"
    case .ended(.incomplete):
        mark = .idle; primary = "Call ended"
    case .failed:
        mark = .idle; primary = "Couldn't connect"
    }
    if muted && mark != .speaking && s.connection.isInCall && s.connection != .ending { mark = .muted }

    var secondary: String
    var tone: StatusTone = .plain
    if let (text, workTone) = workStatusLine(s.work, now: now, assistantName: s.assistantName,
                                             failure: s.workInfo?.failure) {
        secondary = text; tone = workTone
        // Parallel tasks: say how many are running unless one needs the user.
        let running = s.activeTasks.count
        // The task list shows each task's status; the header just counts them.
        if running >= 2 && workTone != .attention { secondary = "\(running) tasks" }
    } else {
        switch s.connection {
        case .connecting where s.micHealth == .repairing:
            secondary = "Mic wasn't picking you up · reconnecting, conversation kept"
        case .live where !muted && s.micHealth == .broken && s.speech != .quieted:
            secondary = "End the call and start a new one"; tone = .error
        case .connecting:
            if s.earlyListening && !muted {
                let heard = cleanTranscript(s.earlyHeard)
                secondary = heard.isEmpty ? "Go ahead · connecting" : "\u{201C}" + (heard.count > 80 ? "…" + String(heard.suffix(80)) : heard) + "\u{201D}"
            } else {
                secondary = s.interactionID == nil ? "Opening microphone" : "Getting ready"
            }
        case .live:
            if muted { secondary = "Mic off · call still open" }
            else if s.speech == .quieted { secondary = "Reply silenced · call continues" }
            else if case .speaking = s.speech { secondary = "Talk to interrupt" }
            else if s.isThinking { secondary = "Working on a reply"; tone = .glimmer }
            else { secondary = "Go ahead" }
        case .ending where s.micHealth == .repairing:
            secondary = "Mic wasn't picking you up · reconnecting, conversation kept"
        case .ending: secondary = s.pausing ? "Holding your place" : "Hanging up"
        case .paused:
            if let minutes = s.quietPausedMinutes { secondary = quietPauseLine(minutes) }
            else { secondary = "Voice off · not billed · Resume to continue" }
        // Plain facts about the call, never about metering: a dropped call is not a billing problem.
        case .ended(.complete): secondary = "Call again any time"
        case .ended(.incomplete): secondary = "Connection dropped"
        case .failed(let message): secondary = message; tone = .error
        case .idle: secondary = s.workOnly ? "Nothing running" : s.callShortcutHint
        }
    }
    if case .failed = s.connection, let error = s.lastError { secondary = error; tone = .error }
    // Paused for quiet: say so plainly, unless something needs the user.
    if s.connection == .paused, let minutes = s.quietPausedMinutes, tone != .attention {
        secondary = quietPauseLine(minutes); tone = .plain
    }
    // A refused drop or the Screen Recording hint shows for a few seconds; an approval still wins.
    if s.connection.isInCall, let notice = s.sharing.notice, notice.isFresh(at: now), tone != .attention {
        secondary = notice.text
        tone = .warning
    }

    let inCall = s.connection == .live || s.connection == .connecting
    let workActive: Bool = {
        switch s.work { case .waiting, .active, .stale, .approval: return true; default: return false }
    }()
    return PillPresentation(
        mark: mark, primary: primary, secondary: secondary, tone: tone,
        micLabel: muted ? "Muted" : "Mic on", micMuted: muted, micEnabled: s.connection == .live,
        showQuiet: { if case .speaking = s.speech { return s.connection == .live }; return false }(),
        statusClickable: s.work != .none || !s.tasks.isEmpty,
        showWorkStop: workActive && (inCall || s.connection == .paused) && s.runID != nil,
        pauseLabel: s.connection == .paused ? "Resume" : (s.connection.isInCall && !s.workOnly ? "Pause" : nil),
        pauseEnabled: s.connection == .paused || s.canPause
    )
}

/// Minimum-dwell debouncer for the status line: a new value replaces the shown
/// value only after the shown one has been visible for `minimumDwell`.
/// Attention (approval) and error changes bypass the dwell.
public struct StatusDwell: Equatable, Sendable {
    public private(set) var shown: String?
    public private(set) var shownAt: Date?
    public private(set) var pending: String?
    public let minimumDwell: TimeInterval
    public init(minimumDwell: TimeInterval = 1.5) { self.minimumDwell = minimumDwell }

    /// Offer a desired value; returns true when `shown` changed.
    @discardableResult
    public mutating func offer(_ value: String, urgent: Bool = false, now: Date) -> Bool {
        if value == shown { pending = nil; return false }
        if shown == nil || urgent || (shownAt.map { now.timeIntervalSince($0) >= minimumDwell } ?? true) {
            shown = value; shownAt = now; pending = nil
            return true
        }
        pending = value
        return false
    }

    /// Promote a pending value once the dwell has elapsed; returns true when `shown` changed.
    @discardableResult
    public mutating func tick(now: Date) -> Bool {
        guard let pending, let shownAt, now.timeIntervalSince(shownAt) >= minimumDwell else { return false }
        shown = pending; self.shownAt = now; self.pending = nil
        return true
    }
}

/// What a call that paused itself for quiet says: why it stopped, and that the work didn't.
public func quietPauseLine(_ minutes: Int) -> String {
    "Paused after \(max(1, minutes)) min of quiet · your jobs keep running"
}
