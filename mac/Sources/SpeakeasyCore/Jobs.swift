import Foundation

// Jobs: the board groups tasks into jobs (one piece of work that may span several requests,
// calls and days). The plugin names the job on each task (`job: {id, name}`); an older plugin
// sends nothing, and then every task is its own job named after the task.

/// The job a task belongs to, as the plugin reported it.
public struct JobRef: Equatable, Sendable {
    public var id: String
    public var name: String
    public init(id: String, name: String) { self.id = id; self.name = name }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) else { return nil }
        self.id = id
        let name = nonEmpty(o["name"])?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        self.name = name.isEmpty ? "Job" : String(name.prefix(80))
    }
}

// MARK: - Decisions (question cards with context, options and a recommendation)

public struct DecisionTag: Equatable, Sendable {
    public var text: String
    /// good | warn | bad | neutral
    public var tone: String
    public init(text: String, tone: String = "neutral") { self.text = text; self.tone = tone }
}

public struct DecisionOption: Equatable, Sendable {
    public var text: String
    public var detail: String?
    public var tags: [DecisionTag]
    /// low | medium | high, when the plugin said so.
    public var risk: String?
    /// nil = not stated.
    public var reversible: Bool?
    /// Card image number for a picture option.
    public var image: Int?

    public init(text: String, detail: String? = nil, tags: [DecisionTag] = [], risk: String? = nil,
                reversible: Bool? = nil, image: Int? = nil) {
        self.text = text; self.detail = detail; self.tags = tags; self.risk = risk; self.reversible = reversible; self.image = image
    }

    /// A string (older plugins) or an object `{text, detail, tags, risk, reversible, image}`.
    public init?(json: JSONValue) {
        if case .string(let s) = json {
            let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !t.isEmpty else { return nil }
            self.init(text: t); return
        }
        guard let text = json["text"].string?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else { return nil }
        let tags = json["tags"].array.prefix(4).compactMap { raw -> DecisionTag? in
            if let s = raw.string, case .string = raw { return DecisionTag(text: s) }
            guard let t = raw["text"].string else { return nil }
            return DecisionTag(text: String(t.prefix(40)), tone: raw["tone"].string ?? "neutral")
        }
        let risk = json["risk"].string.map { $0.lowercased() }
        self.init(text: text, detail: json["detail"].string, tags: Array(tags),
                  risk: ["low", "medium", "high"].contains(risk ?? "") ? risk : nil,
                  reversible: json["reversible"].bool, image: json["image"].double.map { Int($0) })
    }
}

/// A `question` view read as a decision brief. Every field beyond the question and options is optional.
public struct Decision: Equatable, Sendable {
    public var question: String
    public var context: String?
    public var why: String?
    public var affects: String?
    public var options: [DecisionOption]
    public var recommended: Int?
    /// One-tap Approve is allowed (the plugin decided; never inferred here).
    public var approvable: Bool

    public init(question: String, context: String? = nil, why: String? = nil, affects: String? = nil,
                options: [DecisionOption], recommended: Int? = nil, approvable: Bool = false) {
        self.question = question; self.context = context; self.why = why; self.affects = affects
        self.options = options; self.recommended = recommended; self.approvable = approvable
    }

    public init?(card: ViewCard) {
        guard card.kind == "question", let q = card["question"].string else { return nil }
        let options = card["options"].array.prefix(4).compactMap(DecisionOption.init(json:))
        guard !options.isEmpty else { return nil }
        let rec = card["recommended"].double.map { Int($0) }
        self.init(question: q, context: card["context"].string, why: card["why"].string, affects: card["affects"].string,
                  options: Array(options), recommended: rec.flatMap { (0..<options.count).contains($0) ? $0 : nil },
                  approvable: card["approvable"].bool ?? false)
        // Approve needs something to approve.
        if recommended == nil { approvable = false }
    }

    public var recommendedOption: DecisionOption? { recommended.map { options[$0] } }
}

public extension WorkInfo {
    /// The decision this task's answer asks for, if any.
    var decision: Decision? { views.lazy.compactMap(Decision.init(card:)).first }
}

// MARK: - Jobs and the board

public struct Job: Equatable, Sendable, Identifiable {
    public enum Need: String, Equatable, Sendable {
        case decide, approve, review
        /// The pill on the board tile.
        public var label: String {
            switch self { case .decide: return "Decide"; case .approve: return "Approve"; case .review: return "Review" }
        }
    }
    public enum Stage: Equatable, Sendable {
        case needsYou(Need), failed, running, done
        public var needsYou: Bool { if case .needsYou = self { return true }; return false }
    }

    public var id: String
    public var name: String
    /// Oldest first, like `VoiceState.tasks`.
    public var tasks: [TaskItem]
    public var stage: Stage
    /// The task whose question waits on you (stage `.needsYou(.decide)`).
    public var decisionTaskID: String?
    /// Its question (or approval) was set aside ("Not needed") and nothing else waits on the user:
    /// it sits in Done today labeled "Set aside".
    public var setAside = false
    /// A finished task whose pictures wait to be looked at (a design review, not an answer with
    /// pictures along). Nothing waits on the user for it, so it is never Needs you: the board
    /// shows it with the new answers (Look) until it is opened or dismissed.
    public var lookTaskID: String?

    public var newest: TaskItem { tasks[tasks.count - 1] }
    public var updated: Date? { tasks.compactMap { $0.info.updatedAt ?? $0.info.updated }.max() }
    public var decisionTask: TaskItem? { decisionTaskID.flatMap { id in tasks.first { $0.id == id } } }
    public var decision: Decision? { decisionTask?.info.decision }
    /// The task the job is busy with right now: the newest one actually doing work (running, with
    /// a tool log, or waiting on an approval), else the newest still open. A follow-up that only
    /// queued behind it (not started, nothing logged) never hides the work in progress.
    public var activeTask: TaskItem? {
        let open = tasks.filter(\.isActive)
        return open.last(where: \.isWorking) ?? open.last
    }

    /// The finished task this job answered with: it completed with a result and nothing waits on
    /// the user (no question, approval, draft or review). Information brought back, not a decision.
    /// Design reviews (pictures that are the deliverable) are not answers; pictures that came
    /// along with a report (`WorkInfo.reviewIsIncidental`) are, and fold under it.
    public var answerTask: TaskItem? {
        let t = newest
        guard stage == .done, !setAside, t.info.status == "completed", t.info.result != nil, t.info.decision == nil,
              t.info.pendingDrafts.isEmpty, t.info.reviewImages.isEmpty || t.info.reviewIsIncidental else { return nil }
        return t
    }

    /// The finished task with pictures to look at, while the job is done (see `lookTaskID`).
    public var lookTask: TaskItem? {
        guard stage == .done, !setAside, let id = lookTaskID else { return nil }
        return tasks.first { $0.id == id }
    }

    /// What makes this job new on the board: its answer, else its pictures to look at.
    public var newTask: TaskItem? { answerTask ?? lookTask }
}

/// How an answer reads on the board: a one-line headline and the card worth a glance.
public enum JobAnswer {
    /// The line for pictures to look at: how many, and what the job said about them if it did.
    public static func lookLine(_ info: WorkInfo) -> String {
        let n = info.reviewImages.count
        return n > 1 ? "\(n) pictures to look at" : "Pictures to look at"
    }

    /// The report's headline, else the result's label, else the first sentence of what was said.
    public static func headline(_ info: WorkInfo) -> String? {
        if let h = info.result?.shape?.headline.trimmingCharacters(in: .whitespacesAndNewlines), !h.isEmpty { return h }
        if let l = info.result?.label?.trimmingCharacters(in: .whitespacesAndNewlines), !l.isEmpty { return l }
        guard let spoken = info.result?.spoken?.trimmingCharacters(in: .whitespacesAndNewlines), !spoken.isEmpty else { return nil }
        var first = spoken
        if let r = spoken.range(of: #"[.!?](\s|$)"#, options: .regularExpression) { first = String(spoken[..<r.upperBound]) }
        first = first.trimmingCharacters(in: .whitespacesAndNewlines)
        return first.count > 140 ? String(first.prefix(139)) + "…" : first
    }

    /// Kinds a board tile can draw small, best first.
    public static let glanceKinds = ["chart", "weather", "quote", "comparison", "stats", "fact", "conversion", "math", "progress"]

    /// The result card the board tile shows small, when one has something to draw.
    public static func glanceCard(_ info: WorkInfo) -> ViewCard? {
        let cards = info.views.filter { $0.kind != "question" && hasGlance($0) }
        return glanceKinds.lazy.compactMap { kind in cards.first { $0.kind == kind } }.first
    }

    static func hasGlance(_ c: ViewCard) -> Bool {
        switch c.kind {
        case "chart": return c["series"].array.first.map { $0["values"].doubles.count >= 2 } ?? false
        case "weather": return c["temp"].double != nil
        case "quote": return c["price"].double != nil
        case "comparison": return c["winner"].string != nil
        case "stats": return c["items"].array.first?["value"].string != nil
        case "fact": return c["value"].string != nil
        case "conversion": return c["to_value"].string != nil
        case "math": return c["result"].string != nil
        case "progress": return c["value"].double != nil && (c["total"].double ?? 0) > 0
        default: return false
        }
    }
}

public enum JobBoard {
    /// How long an unanswered decision stays on the board.
    public static let decisionKeptFor: TimeInterval = 24 * 3600

    /// Groups tasks into jobs, newest job first.
    /// - answered: task ids whose question was answered here.
    /// - dismissed: task ids whose cards the user closed.
    /// - reviewing: task ids with a design review still waiting.
    /// - setAside: task ids the user just set aside here (the api confirms with `set_aside`).
    public static func jobs(_ tasks: [TaskItem], answered: Set<String> = [], dismissed: Set<String> = [],
                            reviewing: Set<String> = [], setAside: Set<String> = [], now: Date = Date()) -> [Job] {
        var order: [String] = []
        var grouped: [String: [TaskItem]] = [:]
        var names: [String: String] = [:]
        for task in tasks {
            let key = task.info.job.map { "job:" + $0.id } ?? "task:" + task.id
            if grouped[key] == nil { order.append(key); names[key] = task.info.job?.name ?? task.name }
            grouped[key, default: []].append(task)
        }
        var jobs: [(job: Job, last: Int)] = []
        for key in order {
            let items = grouped[key] ?? []
            let last = items.compactMap { t in tasks.lastIndex { $0.id == t.id } }.max() ?? 0
            var decisionID: String?
            var need: Job.Need?
            var asideAny = false
            var lookID: String?
            for t in items.reversed() {
                // Set aside: whatever it was waiting on is closed, never failed.
                if t.info.setAside || setAside.contains(t.id) { asideAny = true; continue }
                if t.info.status == "waiting_for_approval" { need = need ?? .approve; continue }
                // Needs you only for something to act on: an email draft (Send / Deny / Revise).
                if !t.info.pendingDrafts.isEmpty { if need != .decide && need != .approve { need = .review }; continue }
                // Pictures to look at wait on nobody: the job is new, not Needs you (a question that
                // came with them still is, below). Pictures along with a report fold under the answer.
                if reviewing.contains(t.id) && !t.info.reviewIsIncidental && t.info.decision == nil {
                    if lookID == nil { lookID = t.id }
                    continue
                }
                if decisionID == nil, isDecisionPending(t, answered: answered, dismissed: dismissed, now: now) {
                    decisionID = t.id
                    if need == nil || need == .review { need = .decide }
                }
            }
            if decisionID != nil && need != .approve { need = .decide }
            let newest = items[items.count - 1]
            let stage: Job.Stage
            let newestAside = newest.info.setAside || setAside.contains(newest.id)
            if let need { stage = .needsYou(need) }
            else if newestAside { stage = .done }
            else if items.contains(where: \.isActive) { stage = .running }
            else if newest.info.status == "failed" { stage = .failed }
            else { stage = .done }
            jobs.append((Job(id: key, name: names[key] ?? newest.name, tasks: items, stage: stage, decisionTaskID: decisionID,
                             setAside: asideAny && stage == .done && newestAside, lookTaskID: lookID), last))
        }
        return jobs.sorted { $0.last > $1.last }.map(\.job)
    }

    static func isDecisionPending(_ t: TaskItem, answered: Set<String>, dismissed: Set<String>, now: Date) -> Bool {
        // A question card waits on the user whether or not the run has wrapped up (a run that asks
        // and keeps going must not sit under a Running pill).
        guard t.info.isTerminal || t.info.status == "working" || t.info.status == "running",
              t.info.decision != nil, !answered.contains(t.id), !dismissed.contains(t.id),
              let at = t.info.updatedAt ?? t.info.updated else { return false }
        return now.timeIntervalSince(at) < decisionKeptFor
    }

    public struct Sections: Equatable, Sendable {
        /// Decisions first, then approvals, reviews, and failures.
        public var needsYou: [Job]
        /// Finished today with information the user hasn't opened yet.
        public var newAnswers: [Job] = []
        public var running: [Job]
        public var doneToday: [Job]
        /// Board order, for the arrow keys.
        public var all: [Job] { needsYou + newAnswers + running + doneToday }
        public var isEmpty: Bool { all.isEmpty }
    }

    /// The id an answer is remembered as seen by (its run, else its task).
    public static func seenKey(_ task: TaskItem) -> String { task.info.runID ?? task.id }

    /// - seen: run (or task) ids of answers the user opened or whose cards they closed.
    public static func sections(_ jobs: [Job], seen: Set<String> = [], now: Date = Date(), calendar: Calendar = .current) -> Sections {
        func rank(_ j: Job) -> Int {
            switch j.stage {
            case .needsYou(.decide): return 0
            case .needsYou(.approve): return 1
            case .needsYou(.review): return 2
            default: return 3
            }
        }
        let needs = jobs.filter { $0.stage.needsYou || $0.stage == .failed }
            .enumerated().sorted { (rank($0.element), $0.offset) < (rank($1.element), $1.offset) }.map(\.element)
        let running = jobs.filter { $0.stage == .running }
        func unseen(_ j: Job, _ t: TaskItem) -> Bool { !t.info.seen && !seen.contains(seenKey(t)) && !seen.contains(t.id) }
        // Pictures still to look at stay new past the day they came (until opened or dismissed).
        let done = jobs.filter { j in
            j.stage == .done && ((j.updated.map { calendar.isDate($0, inSameDayAs: now) } ?? true)
                                 || j.lookTask.map { unseen(j, $0) } == true)
        }
        let unread = done.filter { j in
            guard let t = j.newTask else { return false }
            return unseen(j, t)
        }
        let unreadIDs = Set(unread.map(\.id))
        return Sections(needsYou: needs, newAnswers: unread, running: running, doneToday: done.filter { !unreadIDs.contains($0.id) })
    }

    /// Plan progress for a running job: (steps done, total), or nil without a plan.
    public static func progress(_ info: WorkInfo) -> (done: Int, total: Int)? {
        guard !info.plan.isEmpty else { return nil }
        if info.isTerminal && info.status == "completed" { return (info.plan.count, info.plan.count) }
        return (min(info.milestones.count, info.plan.count - 1), info.plan.count)
    }
}

// MARK: - The orb

/// What the folded panel's orb shows.
public enum OrbState: Equatable, Sendable {
    case idle, talking, working
    case needsYou(Int)
    case failed(needs: Int)

    /// - userTalking: the mic is open and hearing the user.
    public static func of(jobs: [Job], approvalPending: Bool = false, userTalking: Bool) -> OrbState {
        var needs = jobs.filter(\.stage.needsYou).count
        if approvalPending && !jobs.contains(where: { $0.stage == .needsYou(.approve) }) { needs += 1 }
        if jobs.contains(where: { $0.stage == .failed }) { return .failed(needs: needs) }
        if needs > 0 { return .needsYou(needs) }
        if userTalking { return .talking }
        if jobs.contains(where: { $0.stage == .running }) { return .working }
        return .idle
    }

    /// The small blue dot for answers waiting to be read. Something that needs the user (amber) or a
    /// failure (red) wins.
    public func showsAnswerDot(unread: Int) -> Bool {
        switch self { case .needsYou, .failed: return false; default: return unread > 0 }
    }

    public var badge: Int? {
        switch self {
        case .needsYou(let n): return n
        case .failed(let n): return n > 0 ? n : nil
        default: return nil
        }
    }

    /// VoiceOver and tooltip text.
    public var label: String {
        switch self {
        case .idle: return "Nothing running"
        case .talking: return "Listening to you"
        case .working: return "Working"
        case .needsYou(let n): return n == 1 ? "1 thing needs you" : "\(n) things need you"
        case .failed: return "A task failed"
        }
    }
}
