import Foundation

// Typed, sanitized views of the api contract (docs/live-contract.md v2).
// Decoding is deliberately tolerant: unknown fields are ignored and missing
// optional fields decode as nil, so additive api changes never break the app.

public struct WorkEventItem: Equatable, Sendable {
    public var kind: String   // request | milestone | result
    public var text: String
    public var at: Date?
    public init(kind: String, text: String, at: Date?) { self.kind = kind; self.text = text; self.at = at }
}

/// Something the user added to a running task, and what is known of it. `queued`: Hermes took it, the
/// task hasn't read it yet. `landed`: it is in the task's transcript. `notRead`: the task ended first.
public struct FollowUp: Equatable, Sendable {
    public enum State: String, Equatable, Sendable { case queued, landed, notRead = "not_read" }
    public var text: String
    /// The matching "You added: …" step on the card.
    public var milestone: String
    public var state: State
    public var at: Date?
    public init(text: String, milestone: String, state: State, at: Date? = nil) {
        self.text = text; self.milestone = milestone; self.state = state; self.at = at
    }
    static func list(json: Any?) -> [FollowUp] {
        (json as? [Any] ?? []).compactMap { raw in
            guard let o = raw as? [String: Any], let milestone = nonEmpty(o["milestone"]),
                  let state = (o["state"] as? String).flatMap(State.init(rawValue:)) else { return nil }
            return FollowUp(text: nonEmpty(o["text"]) ?? "", milestone: milestone, state: state, at: decodeDate(o["at"]))
        }
    }
    /// The step's second line.
    public var label: String {
        switch state {
        case .queued: return "Queued: it reads this at its next step"
        case .landed: return "Read by the task"
        case .notRead: return "Not read: it ended first"
        }
    }
}

public struct ProductCard: Equatable, Sendable, Identifiable {
    public let number: Int
    public let name: String
    public let url: URL
    public let imageURL: URL?
    public let price: String?
    public let store: String?
    public let rating: String?
    public let specs: [String]
    public var id: Int { number }

    public init?(json: Any?, number: Int) {
        guard let raw = json as? [String: Any], let name = raw["name"] as? String,
              let destination = raw["url"] as? String, let url = URL(string: destination),
              ["https", "http"].contains(url.scheme?.lowercased() ?? ""), url.host != nil else { return nil }
        self.number = number; self.name = name; self.url = url
        if let image = raw["image_url"] as? String, let candidate = URL(string: image),
           candidate.scheme?.lowercased() == "https", candidate.host != nil {
            imageURL = candidate
        } else { imageURL = nil }
        price = raw["price"] as? String; store = raw["store"] as? String
        rating = raw["rating"] as? String; specs = raw["specs"] as? [String] ?? []
    }
}

/// A picture a task produced (a Hermes render sent to Discord, or a web image). The app
/// only knows its number and name; bytes come from the authenticated api route
/// `/voice/card-image/<run>/<number>`, never from a path or URL the app opens itself.
public struct ImageCard: Equatable, Sendable, Identifiable {
    public let number: Int
    public let name: String
    public var id: Int { number }

    public init(number: Int, name: String) { self.number = number; self.name = name }

    public init?(json: Any?, number: Int) {
        guard let raw = json as? [String: Any], raw["kind"] as? String == "image", (1...8).contains(number) else { return nil }
        let name = (raw["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        self.number = number
        self.name = name.isEmpty ? "Image \(number)" : String(name.prefix(120))
    }
}

/// Why a task failed, in Speakeasy's own words (the api maps Hermes' error; the provider's raw
/// text never reaches the app). Present only on `failed` tasks.
public struct WorkFailure: Equatable, Sendable {
    /// `billing`, `auth`, `rate_limit`, `model_not_found`, `hermes_key`, `hermes_unreachable` or `unknown`.
    public var kind: String
    /// Short tag for the status line ("Out of credits"); nil when the reason isn't known.
    public var label: String?
    /// What happened and what to do, for the task detail.
    public var text: String

    public init(kind: String, label: String? = nil, text: String) {
        self.kind = kind; self.label = label; self.text = text
    }

    public init?(json: Any?) {
        guard let raw = json as? [String: Any], let text = nonEmpty(raw["text"]) else { return nil }
        kind = nonEmpty(raw["kind"]) ?? "unknown"
        label = nonEmpty(raw["label"]).map { String($0.prefix(60)) }
        self.text = String(text.prefix(400))
    }
}

public struct WorkResult: Equatable, Sendable {
    public var spoken: String?
    public var full: String?
    /// Short past-tense label for the one-line status ("Weather checked").
    public var label: String?
    public var products: [ProductCard]
    public var images: [ImageCard]
    /// Visual cards for the answer (a price chart, the weather, a game…), drawn above the text.
    public var views: [ViewCard]
    /// The report's structure (type, checks, what shipped, what's open, sections), when the api sorted it.
    public var shape: ResultShape?
    /// Up to four short key points (plugin `summary_points`); empty from older plugins.
    public var summaryPoints: [String] = []
    /// What came of the task as one short fact (plugin `headline`), never the voice's opener; nil
    /// from older plugins and on results that ask a question.
    public var headline: String?
    public init(spoken: String?, full: String?, label: String? = nil, products: [ProductCard] = [],
                images: [ImageCard] = [], views: [ViewCard] = []) {
        self.spoken = spoken; self.full = full; self.label = label; self.products = products; self.images = images
        self.views = views
    }
}

/// A long report sorted into parts, so it can be laid out as panels instead of a wall of text.
public struct ResultShape: Equatable, Sendable {
    public struct Check: Equatable, Sendable { public let label: String; public let value: String; public let good: Bool }
    public struct Section: Equatable, Sendable { public let title: String; public let points: [String] }
    /// build, setup, research, ideas or answer.
    public let type: String
    public let headline: String
    public let checks: [Check]
    public let shipped: [String]
    public let open: [String]
    public let sections: [Section]
    public let undo: Bool

    public init(type: String, headline: String, checks: [Check] = [], shipped: [String] = [], open: [String] = [],
                sections: [Section] = [], undo: Bool = false) {
        self.type = type; self.headline = headline; self.checks = checks; self.shipped = shipped; self.open = open
        self.sections = sections; self.undo = undo
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let type = o["type"] as? String else { return nil }
        func strings(_ k: String) -> [String] { (o[k] as? [Any] ?? []).compactMap { $0 as? String }.filter { !$0.isEmpty } }
        self.type = type
        headline = o["headline"] as? String ?? ""
        checks = (o["checks"] as? [Any] ?? []).compactMap { raw in
            guard let c = raw as? [String: Any], let l = c["label"] as? String, let v = c["value"] as? String else { return nil }
            return Check(label: l, value: v, good: c["good"] as? Bool ?? true)
        }
        shipped = strings("shipped")
        open = strings("open")
        sections = (o["sections"] as? [Any] ?? []).compactMap { raw in
            guard let s = raw as? [String: Any], let t = s["title"] as? String else { return nil }
            return Section(title: t, points: (s["points"] as? [Any] ?? []).compactMap { $0 as? String })
        }
        undo = o["undo"] as? Bool ?? false
    }
}

public struct WorkInfo: Equatable, Sendable {
    public var runID: String?
    public var status: String
    public var stale: Bool
    public var updated: Date?
    public var shortStatus: String?
    public var detail: String?
    public var updatedAt: Date?
    public var statusSource: String?
    public var events: [WorkEventItem]
    public var result: WorkResult?
    /// Short semantic name for the task list ("Weekend weather"); nil until named.
    public var title: String?
    /// One clean sentence of what the user asked for; nil until the api rewrites it.
    public var summary: String?
    /// Emails this task drafted; pending ones need the user's decision.
    public var emailDrafts: [EmailDraft] = []
    /// Image card numbers still waiting on the call panel's review card (finished tasks only).
    public var reviewImages: [Int] = []
    public var reviewSettledAt: Date?
    /// The latest image the task produced or is looking at (live view); bytes via `/voice/live-image/<run>`.
    public var liveImage: LiveImage?
    /// Why a `failed` task failed (out of credits, rejected key, …); nil for any other status.
    public var failure: WorkFailure?
    /// The screen, pictures and files this task carried to Hermes (names only; pictures load through
    /// `/voice/shared-image/<run>/<number>`).
    public var shared: [SharedItem] = []
    /// The steps the task will go through, for the progress line; empty until the api plans it.
    public var plan: [String] = []
    /// The job this task belongs to (nil from older plugins: the task is its own job).
    public var job: JobRef?
    /// The user set this task's question, decision or approval aside ("Not needed"): it was
    /// already handled or isn't needed, so nothing waits on them here any more.
    public var setAside = false
    /// Nothing in it to read: a finished home command ("lights off") the server already counts as
    /// seen, so it never sits in New answers. The answer is still there when the job is opened.
    public var seen = false
    /// The user cleared this finished task from the board (on any device; the plugin's `dismissed`).
    public var dismissed = false
    /// Its question was answered (on any device): it waits on nobody (the plugin's one truth).
    public var answered = false
    /// History pages (plugins from 0.3.34): whether THIS task's question is the one still waiting.
    public var questionOpen: Bool?
    /// Its question closed without an answer (plugins with `question_actions`): `dismissed` (the ✕,
    /// undoable for a few seconds) or `moved_on` (the conversation went on without it). It waits on
    /// nobody; the thread keeps a quiet "Question · moved on" line.
    public var questionClosed: String?
    /// The server's one record of its question (plugins with `question_state`), nil when it asks
    /// nothing or on older plugins (then `questionStateOrDerived` reads the older fields).
    public var questionState: QuestionState?
    /// The words its question was answered with (on any device), when the server saw them.
    public var questionAnswer: String?
    /// Its question's state from the server: `question_state`, else derived from answered /
    /// set_aside / question_closed. nil when the task asks nothing.
    public var questionStateOrDerived: QuestionState? {
        if let questionState { return questionState }
        guard decision != nil else { return nil }
        if answered { return .answered }
        if let closed = questionClosed { return QuestionState(rawValue: closed) ?? .movedOn }
        if setAside { return .setAside }
        return .open
    }
    /// An earlier call's task kept on this call's list only for its pending draft or review card
    /// (the server marks it); it's not an answer of this conversation.
    public var carried = false
    /// What the user added while it ran, and whether the task read it (see FollowUp).
    public var followUps: [FollowUp] = []
    /// Held back so two tasks don't edit one project at once: the running task it waits for.
    public var waitingFor: String?
    public var waitingProject: String?
    // Job progress (contract §3; servers with `job_progress`). All optional.
    public var startedAt: Date?
    public var finishedAt: Date?
    /// 1-based step the task is on, and how many steps it has.
    public var stepIndex: Int?
    public var stepCount: Int?
    /// Only when the server has a real basis for it.
    public var etaSeconds: Int?
    /// One plain sentence; nil unless failed.
    public var failureReason: String?
    // Live activity (servers with `job_activity`; see JobActivity.swift). All optional.
    /// What it did, newest last.
    public var activity: [JobActivityItem] = []
    /// How many activity items the server has kept for the task (it streams only the newest 40).
    public var activityTotal: Int?
    /// What it learned so far, newest last.
    public var findings: [JobFinding] = []
    /// Files, sites and commands it touched.
    public var touched: [JobTouched] = []
    /// The picture for its kind of work.
    public var hero: JobHero?
    /// Helpers it started (servers with `job_subagents`; JobSubagents.swift), oldest first.
    public var subagents: [JobSubagent] = []
    /// The Hermes session its conversation lives in (servers with `history`): "Show full session".
    public var sessionID: String?
    /// The model doing (or that did) the work and how hard it thinks (servers with `model_choice`).
    public var model: ModelBadge?
    /// A running task's live line from what it really did in the last couple of minutes (plugin
    /// 0.3.36+, live_status.py): "Editing store.py and router.py", "Waiting on the tests · 4 min".
    public var nowLine: String?
    /// One growing recap of what it has done so far ("Read the code, edited 2 files, ran the tests").
    public var soFar: String?
    /// Plugin 0.3.37+: which model answered its latest call and how full the context was (an estimate).
    public var context: ContextUse?

    /// The receipt for one "You added: …" step, if the api sent one.
    public func followUp(forStep text: String) -> FollowUp? { followUps.last { $0.milestone == text } }

    public init(runID: String?, status: String, stale: Bool = false, updated: Date? = nil,
                shortStatus: String? = nil, detail: String? = nil, updatedAt: Date? = nil,
                statusSource: String? = nil, events: [WorkEventItem] = [], result: WorkResult? = nil,
                title: String? = nil, summary: String? = nil, emailDrafts: [EmailDraft] = []) {
        self.emailDrafts = emailDrafts
        self.runID = runID; self.status = status; self.stale = stale; self.updated = updated
        self.shortStatus = shortStatus; self.detail = detail; self.updatedAt = updatedAt
        self.statusSource = statusSource; self.events = events; self.result = result
        self.title = title; self.summary = summary
    }

    public static let terminalStatuses: Set<String> = ["completed", "failed", "cancelled", "interrupted"]
    public var isTerminal: Bool { Self.terminalStatuses.contains(status) }

    /// Request text the user actually said: the api's `request` (kept even once the request event
    /// has scrolled out of `events`), else the request event.
    public var request: String? { askedText ?? events.first { $0.kind == "request" }?.text }
    /// The api's `request` field (plugin 0.3.37+).
    public var askedText: String?

    /// What the user asked, as one clean sentence (rewritten by the api); the raw speech
    /// fragment is the fallback.
    public var askedFor: String? { summary ?? request }

    /// Numbering is scoped to this task's final answer; it survives call pause/reopen.
    public var cards: [ResultCard] { ResultCard.links(in: result?.full ?? result?.spoken ?? "") }
    public var products: [ProductCard] { result?.products ?? [] }
    public var images: [ImageCard] { result?.images ?? [] }
    public var views: [ViewCard] { result?.views ?? [] }

    /// Progress steps worth showing: everything except the raw request fragment.
    public var steps: [WorkEventItem] { events.filter { $0.kind != "request" } }

    /// Meaningful authored milestones only: receipts are not progress.
    public var milestones: [WorkEventItem] {
        events.filter { $0.kind == "milestone" && !Self.receiptTexts.contains($0.text) }
    }
    static let receiptTexts: Set<String> = ["Request received", "Started working"]

    public init?(json: Any?) {
        guard let object = json as? [String: Any] else { return nil }
        runID = (object["run_id"] as? String) ?? (object["source_run_id"] as? String)
        status = object["status"] as? String ?? "running"
        stale = object["stale"] as? Bool ?? false
        updated = decodeDate(object["updated"])
        shortStatus = nonEmpty(object["short_status"])
        detail = nonEmpty(object["detail"])
        updatedAt = decodeDate(object["updated_at"])
        statusSource = object["status_source"] as? String
        events = (object["events"] as? [Any] ?? []).compactMap { raw in
            guard let item = raw as? [String: Any], let kind = item["kind"] as? String else { return nil }
            let text = item["text"].map { "\($0)" } ?? ""
            return WorkEventItem(kind: kind, text: text, at: decodeDate(item["at"]))
        }
        title = nonEmpty(object["title"])
        summary = nonEmpty(object["summary"])
        askedText = nonEmpty(object["request"]).map { String($0.prefix(400)) }
        emailDrafts = EmailDraft.list(json: object["email_drafts"])
        job = JobRef(json: object["job"])
        sessionID = nonEmpty(object["session_id"])
        model = ModelBadge(json: object["model"])
        nowLine = nonEmpty(object["now_line"])
        soFar = nonEmpty(object["so_far"])
        context = ContextUse(json: object["context"])
        setAside = object["set_aside"] as? Bool ?? false
        seen = object["seen"] as? Bool ?? false
        dismissed = object["dismissed"] as? Bool ?? false
        answered = object["answered"] as? Bool ?? false
        questionOpen = object["question_open"] as? Bool
        questionClosed = nonEmpty(object["question_closed"])
        questionState = (object["question_state"] as? String).flatMap(QuestionState.init(rawValue:))
        questionAnswer = nonEmpty(object["question_answer"])
        carried = object["carried"] as? Bool ?? false
        followUps = FollowUp.list(json: object["follow_ups"])
        if status == "waiting_on_task", let waiting = object["waiting"] as? [String: Any] {
            waitingFor = nonEmpty(waiting["name"]).map { String($0.prefix(120)) }
            waitingProject = nonEmpty(waiting["project"]).map { String($0.prefix(80)) }
        }
        startedAt = decodeDate(object["started_at"])
        finishedAt = decodeDate(object["finished_at"])
        stepIndex = (object["step_index"] as? NSNumber)?.intValue
        stepCount = (object["step_count"] as? NSNumber)?.intValue
        etaSeconds = (object["eta_seconds"] as? NSNumber)?.intValue
        failureReason = status == "failed" ? nonEmpty(object["failure_reason"]).map { String($0.prefix(240)) } : nil
        plan = (object["plan"] as? [Any] ?? []).compactMap { $0 as? String }.filter { !$0.isEmpty }
        liveImage = LiveImage(json: object["live_image"])
        decodeActivity(object)
        failure = status == "failed" ? WorkFailure(json: object["failure"]) : nil
        shared = SharedItem.list(json: object["shared"])
        if let review = object["review"] as? [String: Any] {
            reviewImages = (review["images"] as? [Any] ?? []).compactMap { ($0 as? NSNumber)?.intValue }.filter { (1...8).contains($0) }
            reviewSettledAt = decodeDate(review["settled_at"])
        }
        if let raw = object["result"] as? [String: Any] {
            // Numbers are positions in the api's card list, so they address the image route.
            let cards = Array((raw["cards"] as? [Any] ?? []).prefix(8)).enumerated()
            let products = cards.compactMap { ($0.element as? [String: Any])?["kind"] as? String == "image"
                ? nil : ProductCard(json: $0.element, number: $0.offset + 1) }
            let images = cards.compactMap { ImageCard(json: $0.element, number: $0.offset + 1) }
            let views = ViewCard.list(json: raw["views"])
            var value = WorkResult(spoken: nonEmpty(raw["spoken"]), full: nonEmpty(raw["full"]),
                                   label: nonEmpty(raw["label"]), products: products, images: images, views: views)
            value.shape = ResultShape(json: raw["shape"])
            value.summaryPoints = Array((raw["summary_points"] as? [Any] ?? []).compactMap { nonEmpty($0) }.prefix(4))
            value.headline = nonEmpty(raw["headline"]).map { String($0.prefix(160)) }
            // Cards alone are still a result: never drop cards because the text is empty.
            result = (value.spoken == nil && value.full == nil && products.isEmpty && images.isEmpty && views.isEmpty)
                ? nil : value
        } else {
            result = nil
        }
    }
}

/// Something a task carried to Hermes: a screen capture, a picture or a file. Only its kind and
/// names reach the app; a capture's or picture's bytes come from the authenticated
/// `/voice/shared-image/<run>/<number>` route, never from a path.
public struct SharedItem: Equatable, Sendable, Identifiable {
    public enum Kind: String, Equatable, Sendable { case screen, picture, file }
    /// Position in the task's `shared` list, from 1 (addresses the image route, like card numbers).
    public let number: Int
    public let kind: Kind
    /// The app a screen capture shows ("Xcode").
    public let app: String?
    /// A file's (or picture's) name.
    public let name: String?
    public var id: Int { number }

    public init(number: Int, kind: Kind, app: String? = nil, name: String? = nil) {
        self.number = number; self.kind = kind; self.app = app; self.name = name
    }

    public init?(json: Any?, number: Int) {
        guard let o = json as? [String: Any], let kind = (o["kind"] as? String).flatMap(Kind.init(rawValue:)) else { return nil }
        self.init(number: number, kind: kind, app: nonEmpty(o["app"]).map { String($0.prefix(60)) },
                  name: nonEmpty(o["name"]).map { String($0.prefix(120)) })
    }

    /// Pictures and screen captures have an image to show; files show a chip.
    public var isImage: Bool { kind != .file }

    /// "Xcode", "report.pdf", or the kind when nothing else is known.
    public var label: String {
        switch kind {
        case .screen: return app.map { "\($0) window" } ?? "Screen"
        case .picture: return name ?? "Picture"
        case .file: return name ?? "File"
        }
    }

    /// How many things one task's card keeps (the plugin's `MAX_SHARED`). The plugin keeps the first
    /// ones shared, so a position never renumbers; the image route is asked for 1...this.
    public static let maxItems = 12

    /// Numbers count every listed entry (an unknown kind keeps its place) so they match the server's.
    public static func list(json: Any?) -> [SharedItem] {
        Array((json as? [Any] ?? []).prefix(maxItems).enumerated()).compactMap { SharedItem(json: $0.element, number: $0.offset + 1) }
    }
}

/// What a running task is looking at right now. Only a name and a sequence number reach the app;
/// the bytes come from the authenticated `/voice/live-image/<run>` route and `seq` says when to refetch.
public struct LiveImage: Equatable, Sendable {
    public var name: String
    public var source: String
    public var seq: Int
    public var at: Date?
    public init(name: String, source: String = "viewed", seq: Int, at: Date? = nil) {
        self.name = name; self.source = source; self.seq = seq; self.at = at
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let seq = (o["seq"] as? NSNumber)?.intValue, seq > 0 else { return nil }
        let name = (o["name"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        self.init(name: name.isEmpty ? "Image" : String(name.prefix(120)), source: o["source"] as? String ?? "viewed",
                  seq: seq, at: decodeDate(o["at"]))
    }
    /// Section title in the task detail.
    public var label: String { source == "generated" ? "Latest image" : "Looking at" }
}

/// One voice task of the call. Several can run in parallel; `id` is the
/// GPT-Live delegation id (stable across Pause/Resume).
public struct TaskItem: Equatable, Sendable, Identifiable {
    public var id: String
    public var info: WorkInfo
    public init(id: String, info: WorkInfo) { self.id = id; self.info = info }

    public init?(json: Any?) {
        guard let object = json as? [String: Any], let id = object["task_id"] as? String,
              let info = WorkInfo(json: object) else { return nil }
        self.init(id: id, info: info)
    }

    /// What the task list calls this task: its short title, else what the user said.
    public var name: String { info.title ?? info.request ?? "Earlier request" }

    /// Finished one way or another: can be cleared from the list. An unconfirmed run ("ambiguous":
    /// the stream dropped before its final event) counts too; it shows as stopped and nothing is
    /// waiting on it, so it must be clearable (the plugin settles it from its session later).
    public var isSettled: Bool { info.isTerminal || info.status == "rejected" || info.status == "ambiguous" }

    /// Still in flight: counts toward "N tasks" and can be stopped.
    public var isActive: Bool { !info.isTerminal && info.status != "rejected" && info.status != "ambiguous" }
    /// Started and doing something: running with activity or progress, or waiting on an approval.
    /// Not a task still queued (`admitting`, `waiting_on_task`) with nothing logged.
    public var isWorking: Bool {
        guard isActive, info.status != "waiting_on_task" else { return false }
        if info.status == "waiting_for_approval" { return true }
        return !info.activity.isEmpty || (info.status != "admitting" && info.status != "accepted")
    }

    /// Active, has a Hermes run to stop, and no stop is already in flight.
    public var canStop: Bool { isActive && info.runID != nil && info.status != "cancel_requested" }

    public static func list(json: Any?) -> [TaskItem]? {
        guard let array = json as? [Any] else { return nil }
        return array.compactMap(TaskItem.init(json:))
    }
}

public struct ApprovalInfo: Equatable, Sendable {
    public var runID: String
    public var requestID: String
    public var description: String
    /// Optional detail a newer plugin may send: the command or tool it wants to run, what it acts
    /// on, and why it needs to.
    public var command: String?
    public var target: String?
    public var reason: String?
    public init(runID: String, requestID: String, description: String, command: String? = nil,
                target: String? = nil, reason: String? = nil) {
        self.runID = runID; self.requestID = requestID; self.description = description
        self.command = command; self.target = target; self.reason = reason
    }

    /// What will happen, to what, and why, from what the server sent and the task it belongs to.
    public func context(task: TaskItem?) -> (action: String, target: String?, why: String?) {
        let action = description
        let target = self.target ?? command ?? task?.info.title
        let why = reason ?? task?.info.request.map { "You asked: \u{201C}\($0)\u{201D}" }
        return (action, target == action ? nil : target, why)
    }
    public init?(json: Any?) {
        guard let object = json as? [String: Any],
              let requestID = object["request_id"] as? String,
              let runID = object["run_id"] as? String else { return nil }
        self.init(runID: runID, requestID: requestID,
                  description: object["description"] as? String ?? "Approval needed to continue.",
                  command: nonEmpty(object["command"]), target: nonEmpty(object["target"]),
                  reason: nonEmpty(object["reason"]) ?? nonEmpty(object["why"]))
    }
}

public struct InteractionSnapshot: Equatable, Sendable {
    public var interactionID: String?
    public var status: String?
    public var runID: String?
    public var approval: ApprovalInfo?
    public var finalization: String?
    public var error: String?
    public var paused: Bool
    /// Screen sharing in force and what the call declared (plugins with attachments).
    public var screen: ScreenSnapshot?
    /// Capture requests still open: a polling client serves these like `capture` events.
    public var captures: [String]
    /// Pictures and files the plugin holds for the next request; nil when the plugin predates them.
    public var attachments: [ServerAttachment]?
    /// A request waiting for the screen ("Waiting for your screen"), or how it ended.
    public var hold: ScreenHold?
    /// The snapshot carries `hold` (null included), so a missing one means "none" rather than "unknown".
    public var hasHold: Bool

    public init(interactionID: String? = nil, status: String? = nil, runID: String? = nil,
                approval: ApprovalInfo? = nil, finalization: String? = nil, error: String? = nil,
                paused: Bool = false, screen: ScreenSnapshot? = nil, captures: [String] = [],
                attachments: [ServerAttachment]? = nil, hold: ScreenHold? = nil, hasHold: Bool = false) {
        self.interactionID = interactionID; self.status = status; self.runID = runID
        self.approval = approval; self.finalization = finalization; self.error = error
        self.paused = paused
        self.screen = screen; self.captures = captures; self.attachments = attachments
        self.hold = hold; self.hasHold = hasHold || hold != nil
    }

    public init?(json: Any?) {
        guard let object = json as? [String: Any] else { return nil }
        interactionID = object["interaction_id"] as? String
        status = object["status"] as? String
        runID = (object["backend_run_id"] as? String) ?? (object["run_id"] as? String)
        approval = ApprovalInfo(json: object["approval"])
        finalization = object["finalization"] as? String
        error = nonEmpty(object["error"])
        paused = object["paused"] as? Bool ?? false
        screen = ScreenSnapshot(json: object["screen"])
        var seen = Set<String>()
        captures = (object["captures"] as? [Any] ?? []).compactMap { raw in
            guard let id = nonEmpty((raw as? [String: Any])?["capture_id"]), seen.insert(id).inserted else { return nil }
            return id
        }
        attachments = (object["attachments"] as? [Any]).map { $0.compactMap(ServerAttachment.init(json:)) }
        hold = ScreenHold(json: object["hold"])
        hasHold = object.keys.contains("hold")
    }
}

/// Response of `POST /voice/sessions`. The live api returns
/// `{"interaction_id", "transport": {"sdp"}}`; contract v2 documents a top-level `sdp`.
/// Grok Voice answers `transport: {"type": "relay", "path", "format"}` with no SDP: the plugin
/// relays the audio over a WebSocket and the prepared peer connection is dropped.
public struct SessionAdmission: Equatable, Sendable {
    public var interactionID: String
    /// The WebRTC answer; empty for a relay call.
    public var answerSDP: String
    public var voiceProvider: String
    public var transport: SessionTransport
    public var relay: RelayTransport? {
        if case .relay(let relay) = transport { return relay }
        return nil
    }
    public init?(json: Any?) {
        guard let object = json as? [String: Any],
              let interactionID = object["interaction_id"] as? String else { return nil }
        let transport = object["transport"] as? [String: Any]
        if (transport?["type"] as? String) == "relay" {
            guard let relay = RelayTransport(json: transport) else { return nil }
            self.transport = .relay(relay)
            self.answerSDP = ""
        } else {
            guard let sdp = (transport?["sdp"] as? String) ?? (object["sdp"] as? String), !sdp.isEmpty else { return nil }
            self.transport = .webrtc(sdp: sdp)
            self.answerSDP = sdp
        }
        self.interactionID = interactionID
        self.voiceProvider = object["voice_provider"] as? String ?? "openai"
    }
}

/// Encodes the offer body without touching the SDP: its final CRLF is significant.
/// `resumeFrom` names a paused call whose conversation the new session continues.
/// `tour` asks for the one-time first-call tour; values are the shortcut labels to mention
/// (`call`, `mute`, `pause`). Never sent with `resumeFrom`.
/// `screen` says whether this client can share its screen; pass it only when `/voice/status`
/// advertises attachments (an older plugin refuses unknown keys). Resumes declare it again.
/// `room` is what listening mode heard (rendered transcript lines): with a new call, or with the
/// resume of a call that listening mode paused mid-conversation. Only to servers whose status says
/// `room_listening`, and it replaces the tour: a call opened from listening mode isn't a tour.
public func sessionRequestBody(sdp: String, resumeFrom: String? = nil, tour: [String: String]? = nil,
                               screen: ScreenDeclaration? = nil, room: String? = nil) throws -> Data {
    var body: [String: Any] = ["sdp": sdp]
    if let room, !room.isEmpty { body["room"] = room }
    if let resumeFrom { body["resume_from"] = resumeFrom }
    else if body["room"] == nil, let tour { body["tour"] = tour }
    if let screen { body["screen"] = screen.rawValue }
    return try JSONSerialization.data(withJSONObject: body, options: [])
}

func decodeDate(_ value: Any?) -> Date? {
    if let number = value as? NSNumber { return Date(timeIntervalSince1970: number.doubleValue) }
    if let double = value as? Double { return Date(timeIntervalSince1970: double) }
    return nil
}

func nonEmpty(_ value: Any?) -> String? {
    guard let text = value as? String else { return nil }
    let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? nil : text
}
