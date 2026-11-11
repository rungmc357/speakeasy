import Foundation

// Home (SPK-124): `GET /voice/now`, one ordered list of what's live and new. The plugin owns the
// order and the header sentence (so iPhone, iPad, Mac, widgets and the Live Activity agree); the
// app only caps each tier for the screen it's on and draws it.

public struct NowHome: Equatable, Sendable {
    public var rev: Int
    public var asOf: Date
    public var sentence: String
    public var counts: Counts
    public var tiers: [Int: Int]
    public var items: [NowItem]
    public var background: [NowBackgroundItem]
    public var backgroundRunning: Int
    public var notice: String?
    /// The live call (phase 3): which card the voice is talking about now. Nil off a call.
    public var call: NowCall?
    /// The "Your call · 6 min" card after a call ends, until it's cleared or rolls over.
    public var callSummary: NowCallSummary?
    /// SPK-208 (plugin 0.3.46+, capability `home_stream`): the same cards as one thread. Nil from an older plugin.
    public var stream: NowStream?
    /// The tracker pill's words at any volume ("3 need you · 5 running"), from the plugin; nil from an
    /// older plugin (the app counts for itself). Counts, never a cut-off title.
    public var pill: String?
    /// b73 (capability `home_stack`): the widget stack, exactly as the plugin sent it. Nil from an
    /// older plugin; read `widgetStack` for the stack with a fallback derived from `items`.
    public var stack: HomeStack?
    /// b73: up to three recommended next things. Nil from an older plugin; read
    /// `recommendationsOrFallback` for the fixed generic ideas then.
    public var recommendations: HomeRecommendations?

    public struct Counts: Equatable, Sendable {
        public var needsYou = 0, stopped = 0, working = 0, waiting = 0, finishedUnread = 0, onItsOwn = 0
        public init() {}
    }

    public init(rev: Int = 0, asOf: Date = Date(), sentence: String = "All quiet.", counts: Counts = Counts(),
                tiers: [Int: Int] = [:], items: [NowItem] = [], background: [NowBackgroundItem] = [],
                backgroundRunning: Int = 0, notice: String? = nil) {
        self.rev = rev; self.asOf = asOf; self.sentence = sentence; self.counts = counts; self.tiers = tiers
        self.items = items; self.background = background; self.backgroundRunning = backgroundRunning; self.notice = notice
    }

    public init(json: [String: Any]) {
        rev = (json["rev"] as? NSNumber)?.intValue ?? 0
        asOf = Date(timeIntervalSince1970: (json["as_of"] as? NSNumber)?.doubleValue ?? Date().timeIntervalSince1970)
        sentence = nonEmpty(json["sentence"]) ?? "All quiet."
        let c = json["counts"] as? [String: Any] ?? [:]
        var counts = Counts()
        func n(_ k: String) -> Int { (c[k] as? NSNumber)?.intValue ?? 0 }
        counts.needsYou = n("needs_you"); counts.stopped = n("stopped"); counts.working = n("working")
        counts.waiting = n("waiting"); counts.finishedUnread = n("finished_unread"); counts.onItsOwn = n("on_its_own_running")
        self.counts = counts
        var tiers: [Int: Int] = [:]
        for (k, v) in json["tiers"] as? [String: Any] ?? [:] {
            if let t = Int(k), let v = (v as? NSNumber)?.intValue { tiers[t] = v }
        }
        self.tiers = tiers
        items = (json["items"] as? [Any] ?? []).compactMap(NowItem.init(json:))
        let bg = json["background"] as? [String: Any] ?? [:]
        background = (bg["items"] as? [Any] ?? []).compactMap(NowBackgroundItem.init(json:))
        backgroundRunning = (bg["running"] as? NSNumber)?.intValue ?? background.count
        notice = nonEmpty((json["notice"] as? [String: Any])?["text"])
        call = NowCall(json: json["call"])
        callSummary = NowCallSummary(json: json["call_summary"])
        stream = NowStream(json: json["stream"])
        pill = nonEmpty(json["pill"])
        stack = HomeStack(json: json["stack"])
        recommendations = HomeRecommendations(json: json["recommendations"])
    }

    /// Nothing live or new: Home shows a quiet "All caught up." instead of a header (build 69 drops
    /// the counting sentence from Home; widgets and the Live Activity keep `sentence`).
    public var caughtUp: Bool { items.isEmpty && callSummary == nil }
    public static let caughtUpLine = "All caught up."
    /// What Clear moves to Earlier: every finished answer, seen or not.
    public var clearable: [NowItem] { items.filter { $0.kind == .newAnswer || $0.kind == .seenAnswer } }

    /// The items of one kind, in the plugin's order.
    public func items(_ kind: NowItem.Kind) -> [NowItem] { items.filter { $0.kind == kind } }
}

public struct NowCall: Equatable, Sendable {
    public var interactionID: String
    public var startedAt: Date?
    /// The item id of the card the voice is talking about (lit with a brass border), if any.
    public var focus: String?
    public var focusLabel: String?
    public init(interactionID: String, startedAt: Date? = nil, focus: String? = nil, focusLabel: String? = nil) {
        self.interactionID = interactionID; self.startedAt = startedAt; self.focus = focus; self.focusLabel = focusLabel
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["interaction_id"]) else { return nil }
        self.init(interactionID: id, startedAt: (o["started_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) },
                  focus: nonEmpty(o["focus"]), focusLabel: nonEmpty(o["focus_label"]))
    }
}

public struct NowCallSummary: Equatable, Sendable, Identifiable {
    public var id: String
    public var title: String
    public var minutes: Int
    public var endedAt: Date
    public var sessionID: String?
    public var things: [String]
    public var moreThings: Int
    public var decisions: [String]
    /// The plugin's own line (0.3.43+): what the call started, answered and asked.
    public var line: String?
    public init(id: String, title: String, minutes: Int, endedAt: Date, sessionID: String? = nil, things: [String] = [],
                moreThings: Int = 0, decisions: [String] = [], line: String? = nil) {
        self.id = id; self.title = title; self.minutes = minutes; self.endedAt = endedAt; self.sessionID = sessionID
        self.things = things; self.moreThings = moreThings; self.decisions = decisions; self.line = line
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]),
              let ended = (o["ended_at"] as? NSNumber)?.doubleValue else { return nil }
        self.init(id: id, title: nonEmpty(o["title"]) ?? "Your call", minutes: (o["minutes"] as? NSNumber)?.intValue ?? 1,
                  endedAt: Date(timeIntervalSince1970: ended), sessionID: nonEmpty(o["session_id"]),
                  things: (o["things"] as? [Any] ?? []).compactMap(nonEmpty), moreThings: (o["more_things"] as? NSNumber)?.intValue ?? 0,
                  decisions: (o["decisions"] as? [Any] ?? []).compactMap(nonEmpty), line: nonEmpty(o["line"]))
    }
    /// Line 2: the plugin's line when it sends one; else what the call started.
    public var summaryLine: String {
        if let line { return line }
        return started + (youSaid.map { " You said: \($0)." } ?? "")
    }
    /// What the call started ("Short call, nothing started." when it started nothing).
    public var started: String {
        guard !things.isEmpty else { return "Short call, nothing started." }
        let list = things.count == 1 ? things[0] : things.dropLast().joined(separator: ", ") + " and " + things.last!
        return "Started \(list)" + (moreThings > 0 ? " and \(moreThings) more." : ".")
    }
    public var youSaid: String? { decisions.isEmpty ? nil : decisions.joined(separator: "; ") }
}

public struct NowBackgroundItem: Equatable, Sendable, Identifiable {
    public var id: String { sessionID }
    public var sessionID: String
    public var title: String
    public var line: String
    public var sourceLabel: String?
    public init(sessionID: String, title: String, line: String, sourceLabel: String? = nil) {
        self.sessionID = sessionID; self.title = title; self.line = line; self.sourceLabel = sourceLabel
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let sid = nonEmpty(o["session_id"]) else { return nil }
        self.init(sessionID: sid, title: nonEmpty(o["title"]) ?? "Untitled", line: nonEmpty(o["line"]) ?? "",
                  sourceLabel: nonEmpty(o["source_label"]))
    }
}

/// One step a running job took, as the plugin words it ("Searched mail for “cabin”"): its kind for
/// the glyph, when it started, and whether it is the call still going.
public struct NowStep: Equatable, Sendable, Hashable {
    public var text: String
    public var kind: String
    public var at: Date?
    public var running: Bool
    public init(text: String, kind: String, at: Date? = nil, running: Bool = false) {
        self.text = text; self.kind = kind; self.at = at; self.running = running
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let text = o["text"] as? String,
              case let t = text.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty else { return nil }
        self.init(text: String(t.prefix(120)), kind: (o["kind"] as? String) ?? "tool",
                  at: (o["at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) },
                  running: o["running"] as? Bool ?? false)
    }
}

public struct NowItem: Equatable, Sendable, Identifiable {
    public enum Kind: String, Sendable, CaseIterable {
        case question, stopped, working, waiting, newAnswer = "new_answer", seenAnswer = "seen_answer"
    }
    public enum Provenance: Equatable, Sendable { case yesterday(Date), earlierDay(Date), earlierCall(Date), later(Date) }
    public enum Fix: String, Sendable { case credits, signIn = "sign_in", retry, setup, open }

    public var id: String
    public var kind: Kind
    public var sessionID: String?
    public var runID: String?
    public var title: String
    public var line: String
    public var sourceLabel: String?
    public var startedAt: Date?
    public var updatedAt: Date?
    public var finishedAt: Date?
    public var unread: Bool
    public var idleSeconds: Int?
    public var thumbs: [String]
    /// The same pictures with what Home draws for each: a person's caption (never a file name) and,
    /// for a wide contact sheet, a readable crop (`hero`). Same order as `thumbs`.
    public var pictures: [NowPicture] = []
    /// The job's whole title when `title` is Home's short name for it (the job's own page shows this).
    public var fullTitle: String?
    /// A question's verdict said before it asked ("Misi's my pick, but I haven't checked tables
    /// yet."): Home's thread shows it above what the task found. The line stays the question.
    public var reply: String?
    /// A question nobody answered that a newer one took over: when, and which card took over.
    public var replacedAt: Date?
    public var replacedBy: String?
    /// Canceled from Home's chat: the card stays in the thread, closed ("Canceled.").
    public var canceled = false
    /// The model that did (or is doing) the job, as recorded by the plugin (what Hermes served once
    /// known). Home's chat shows it under a job's card; nil when nothing was recorded.
    public var workModel: ReplyAttribution?
    /// SPK-254: the project this job is filed into (servers with `projects`).
    public var project: ProjectRef?
    /// SPK-254: the one existing project the job's own words name, offered as a one-tap chip
    /// ("Lantern?"). Never set once filed or declined; nothing is filed until the person taps it.
    public var projectSuggestion: ProjectRef?
    public var provenance: Provenance?
    public var fix: Fix?
    public var question: NowQuestion?
    /// On a call: `asking` (the question the voice is asking now, first on Home) or `heard` (the voice
    /// read this answer out, so it's already read).
    public var onCall: OnCall?
    /// Created or explicitly brought into the current call (other work stays in the capsule).
    public var onCurrentCall = false
    /// Asking on a call: a spoken answer may pick a choice (false: a risky one, it needs a tap).
    public var sayOK: Bool = true
    /// Asking on a call with a risky choice: "Can't be undone · Needs a tap".
    public var lockLine: String?
    /// Waiting behind another job on the same project: the job it starts after ("Checkout fix").
    public var waitingOn: String?
    /// A waiting job can start now, beside the other one (`POST /voice/work/{run_id}/start-now`).
    public var canStartNow = false
    public enum OnCall: String, Sendable { case asking, heard }
    /// A running job's kind of current step (`step_kind`: "mail", "calendar", "web", "terminal", "doc",
    /// "edit", "search", "think", "message", "helper", "tool"), for the step capsule's glyph. nil from
    /// an older plugin (the app then reads the kind from the step's words, `LiveStepKind.infer`).
    public var stepKind: String?
    /// A running job's newest steps, oldest first, in plain words (`steps`; home-fixes-73).
    public var steps: [NowStep] = []
    /// When its data is from (plugin widgets-decide `as_of`); the card says "as of <time>" once that's
    /// older than its kind stays true for (`NowFreshness`).
    public var asOf: Date?
    /// A done answer that still asks something it never got an answer to (plugin `asks`).
    public var asks: NowQuestion?
    /// Emails it drafted that wait on Send (plugin `unsent_drafts`).
    public var unsentDrafts = 0
    public init(id: String, kind: Kind, sessionID: String? = nil, runID: String? = nil, title: String, line: String,
                sourceLabel: String? = nil, startedAt: Date? = nil, updatedAt: Date? = nil, finishedAt: Date? = nil,
                unread: Bool = false, idleSeconds: Int? = nil, thumbs: [String] = [], provenance: Provenance? = nil,
                fix: Fix? = nil, question: NowQuestion? = nil, onCall: OnCall? = nil, sayOK: Bool = true, lockLine: String? = nil) {
        self.onCall = onCall; self.sayOK = sayOK; self.lockLine = lockLine
        self.id = id; self.kind = kind; self.sessionID = sessionID; self.runID = runID; self.title = title
        self.line = line; self.sourceLabel = sourceLabel; self.startedAt = startedAt; self.updatedAt = updatedAt
        self.finishedAt = finishedAt; self.unread = unread; self.idleSeconds = idleSeconds; self.thumbs = thumbs
        self.provenance = provenance; self.fix = fix; self.question = question
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]),
              let kind = (o["kind"] as? String).flatMap(Kind.init(rawValue:)) else { return nil }
        func date(_ k: String) -> Date? { (o[k] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) } }
        var prov: Provenance?
        if let p = o["provenance"] as? [String: Any], let at = (p["at"] as? NSNumber)?.doubleValue {
            let d = Date(timeIntervalSince1970: at)
            switch p["kind"] as? String {
            case "yesterday": prov = .yesterday(d)
            case "earlier_day": prov = .earlierDay(d)
            case "earlier_call": prov = .earlierCall(d)
            case "later": prov = .later(d)
            default: prov = nil
            }
        }
        self.init(id: id, kind: kind, sessionID: nonEmpty(o["session_id"]), runID: nonEmpty(o["run_id"]),
                  title: String((nonEmpty(o["title"]) ?? "Untitled").prefix(120)),
                  line: String((nonEmpty(o["line"]) ?? "").prefix(400)), sourceLabel: nonEmpty(o["source_label"]),
                  startedAt: date("started_at"), updatedAt: date("updated_at"), finishedAt: date("finished_at"),
                  unread: o["unread"] as? Bool ?? false, idleSeconds: (o["idle_seconds"] as? NSNumber)?.intValue,
                  thumbs: (o["thumbs"] as? [Any] ?? []).compactMap { nonEmpty(($0 as? [String: Any])?["url"]) },
                  provenance: prov, fix: (((o["fix"] as? [String: Any])?["kind"]) as? String).flatMap(Fix.init(rawValue:)),
                  question: NowQuestion(json: o["question"]))
        onCall = (o["on_call"] as? String).flatMap(OnCall.init(rawValue:))
        onCurrentCall = o["on_current_call"] as? Bool ?? false
        sayOK = o["say_ok"] as? Bool ?? true
        lockLine = nonEmpty(o["lock_line"])
        asOf = date("as_of")
        asks = NowQuestion(json: o["asks"])
        unsentDrafts = (o["unsent_drafts"] as? NSNumber)?.intValue ?? 0
        stepKind = kind == .working ? nonEmpty(o["step_kind"]) : nil
        steps = kind == .working ? (o["steps"] as? [Any] ?? []).prefix(12).compactMap(NowStep.init(json:)) : []
        pictures = (o["thumbs"] as? [Any] ?? []).compactMap(NowPicture.init(json:))
        fullTitle = nonEmpty(o["full_title"])
        reply = nonEmpty(o["reply"]).map { String($0.prefix(400)) }
        workModel = ReplyAttribution(json: o["model"])
        project = ProjectRef(json: o["project"])
        projectSuggestion = project == nil ? ProjectRef(json: o["project_suggestion"]) : nil
        canceled = o["canceled"] as? Bool ?? false
        waitingOn = kind == .waiting ? nonEmpty(o["waiting_on"]) : nil
        canStartNow = kind == .waiting && (o["can_start_now"] as? Bool ?? false) && runID != nil
        if let r = o["replaced"] as? [String: Any] {
            replacedAt = (r["at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) } ?? updatedAt ?? finishedAt
            replacedBy = nonEmpty(r["by"])
        }
    }

    /// The one footnote that says where a card came from ("From yesterday · 9:40 PM").
    /// By real calendar days in the phone's zone: "From yesterday · 8:13 PM" only for yesterday,
    /// "From Wednesday · 8:13 PM" within the week, else "From Oct 28" (never "yesterday" for older).
    public func provenanceLine(calendar: Calendar = .current, now: Date = Date()) -> String? {
        let f = DateFormatter(); f.calendar = calendar; f.timeZone = calendar.timeZone
        f.locale = Locale(identifier: "en_US"); f.dateFormat = "h:mm a"
        func day(_ d: Date) -> String? {
            let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: d), to: calendar.startOfDay(for: now)).day ?? 0
            if days <= 0 { return nil }
            if days == 1 { return "From yesterday \u{00B7} \(f.string(from: d))" }
            let w = DateFormatter(); w.calendar = calendar; w.timeZone = calendar.timeZone; w.locale = Locale(identifier: "en_US")
            if days < 7 { w.dateFormat = "EEEE"; return "From \(w.string(from: d)) \u{00B7} \(f.string(from: d))" }
            w.dateFormat = "MMM d"; return "From \(w.string(from: d))"
        }
        switch provenance {
        case .yesterday(let d)?, .earlierDay(let d)?: return day(d)
        case .earlierCall(let d)?: return "From the earlier call · \(f.string(from: d))"
        case .later?: return "Back from Later"
        case nil: return nil
        }
    }
}

/// One picture on a Home card (plugin 0.3.47+ adds caption/hero/aspect; older plugins send only url).
public struct NowPicture: Equatable, Sendable {
    public var url: String
    public var caption: String?
    /// For a wide contact sheet: the readable crop (`/voice/card-image/<run>/<n>?v=hero`).
    public var hero: String?
    public var aspect: Double?
    public init(url: String, caption: String? = nil, hero: String? = nil, aspect: Double? = nil) {
        self.url = url; self.caption = caption; self.hero = hero; self.aspect = aspect
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let url = nonEmpty(o["url"]) else { return nil }
        self.init(url: url, caption: nonEmpty(o["caption"]).map { String($0.prefix(120)) },
                  hero: nonEmpty(o["hero"]), aspect: (o["aspect"] as? NSNumber)?.doubleValue)
    }
    /// The card-image number in `url` (`/voice/card-image/<run>/<n>`).
    public var number: Int? {
        Int(url.split(separator: "?").first?.split(separator: "/").last ?? "")
    }
}

/// How long a card's data stays true, by card kind (QA 74 F6): past it, the card says "As of <time>"
/// so a 4-day-old forecast or a light's old state never reads as live.
public enum NowFreshness {
    public static func seconds(kind: String?) -> TimeInterval? {
        switch kind ?? "" {
        case "transit", "devices", "lights", "device": return 15 * 60
        case "game", "games", "score", "metric", "markets", "stocks", "quote": return 60 * 60
        case "weather", "forecast", "flight", "status": return 3 * 3600
        case "package", "rows", "inbox", "list", "places", "day", "calendar_day", "agenda", "comparison": return 12 * 3600
        case "": return nil
        default: return 24 * 3600
        }
    }

    /// "As of 3:14 PM" (today), "As of yesterday 9:40 PM", "As of Nov 3"; nil while still fresh.
    public static func line(kind: String?, asOf: Date?, now: Date = Date(), calendar: Calendar = .current) -> String? {
        guard let asOf, let limit = seconds(kind: kind), now.timeIntervalSince(asOf) > limit else { return nil }
        let f = DateFormatter(); f.calendar = calendar; f.timeZone = calendar.timeZone; f.locale = Locale(identifier: "en_US")
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: asOf), to: calendar.startOfDay(for: now)).day ?? 0
        switch days {
        case ...0: f.dateFormat = "h:mm a"; return "As of \(f.string(from: asOf))"
        case 1: f.dateFormat = "h:mm a"; return "As of yesterday \(f.string(from: asOf))"
        default: f.dateFormat = "MMM d"; return "As of \(f.string(from: asOf))"
        }
    }
}

public struct NowQuestion: Equatable, Sendable {
    public enum Shape: String, Sendable { case words, things, approval }
    public struct Option: Equatable, Sendable, Identifiable {
        public var id: String
        public var text: String
        public var detail: String?
        public var image: String?
        public var link: URL?
        public var facts: [String]
        public var risk: String?
        public init(id: String, text: String, detail: String? = nil, image: String? = nil, link: URL? = nil,
                    facts: [String] = [], risk: String? = nil) {
            self.id = id; self.text = text; self.detail = detail; self.image = image; self.link = link
            self.facts = facts; self.risk = risk
        }
    }
    public var shape: Shape
    public var prompt: String
    public var context: String?
    public var options: [Option]
    public var recommended: Int?
    public var approvalChoices: [String]
    /// Why the recommended option (plugin: `why`); what it touches (`affects`).
    public var why: String?
    public var affects: String?
    /// A task's own approvable ask drawn as an approval: the option Allow answers with (plugin
    /// widgets-decide: `approve.option_id`). Nil for a Hermes command approval.
    public var approveOptionID: String?

    public init(shape: Shape, prompt: String, context: String? = nil, options: [Option] = [], recommended: Int? = nil,
                approvalChoices: [String] = [], why: String? = nil, affects: String? = nil, approveOptionID: String? = nil) {
        self.shape = shape; self.prompt = prompt; self.context = context; self.options = options
        self.recommended = recommended; self.approvalChoices = approvalChoices
        self.why = why; self.affects = affects; self.approveOptionID = approveOptionID
    }

    /// The recommended option, when there is one.
    public var recommendedOption: Option? {
        recommended.flatMap { $0 >= 0 && $0 < options.count ? options[$0] : nil }
    }

    /// Risky options (anything but low risk), named for a flag on the card.
    public func riskFlag(_ option: Option) -> String? {
        switch option.risk?.lowercased() {
        case "high"?: return "High risk"
        case "medium"?: return "Some risk"
        default: return nil
        }
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let shape = (o["shape"] as? String).flatMap(Shape.init(rawValue:)) else { return nil }
        let options: [Option] = (o["options"] as? [Any] ?? []).compactMap { raw in
            guard let p = raw as? [String: Any], let id = nonEmpty(p["id"]), let text = nonEmpty(p["text"]) else { return nil }
            let link = nonEmpty(p["link"]).flatMap(URL.init(string:)).flatMap { $0.scheme == "https" ? $0 : nil }
            return Option(id: id, text: text, detail: nonEmpty(p["detail"]), image: nonEmpty(p["image"]), link: link,
                          facts: (p["facts"] as? [Any] ?? []).compactMap(nonEmpty).prefix(3).map { $0 },
                          risk: nonEmpty(p["risk"]))
        }
        self.init(shape: shape, prompt: nonEmpty(o["prompt"]) ?? "", context: nonEmpty(o["context"]), options: options,
                  recommended: (o["recommended"] as? NSNumber)?.intValue,
                  approvalChoices: ((o["approval"] as? [String: Any])?["choices"] as? [String]) ?? [],
                  why: nonEmpty(o["why"]), affects: nonEmpty(o["affects"]),
                  approveOptionID: nonEmpty((o["approve"] as? [String: Any])?["option_id"]))
    }

    /// Things to compare side by side (pictures, links, facts): the Compare sheet's rows.
    public var comparable: Bool { shape == .things && options.contains { $0.image != nil || !$0.facts.isEmpty } }
}

// MARK: - Device caps

/// How many cards of each tier fit before an overflow row ("2 more questions"), per screen.
public struct NowCaps: Equatable, Sendable {
    public var questions, working, newAnswers, seen: Int
    public static let phone = NowCaps(questions: 3, working: 3, newAnswers: 5, seen: 3)
    public static let small = NowCaps(questions: 1, working: 2, newAnswers: 3, seen: 2)  // iPhone SE, Duo outer
    public static let wide = NowCaps(questions: 3, working: 5, newAnswers: 8, seen: 5)   // iPad, Mac

    public func cap(_ kind: NowItem.Kind) -> Int {
        switch kind {
        case .question: return questions
        case .stopped, .waiting: return 3
        case .working: return working
        case .newAnswer: return newAnswers
        case .seenAnswer: return seen
        }
    }

    /// "2 more questions" / "3 more in the works" / "4 more new answers".
    public static func overflowLabel(_ kind: NowItem.Kind, _ n: Int) -> String {
        switch kind {
        case .question: return "\(n) more question\(n == 1 ? "" : "s")"
        case .stopped: return "\(n) more stopped"
        case .working: return "\(n) more in the works"
        case .waiting: return "\(n) more waiting"
        case .newAnswer: return "\(n) more new answer\(n == 1 ? "" : "s")"
        case .seenAnswer: return "\(n) more from today"
        }
    }
}

// MARK: - Earlier's filter menu

/// `GET /voice/history?kinds=`: Earlier's checkboxes, in Hermes' own words, with counts.
public struct EarlierKind: Equatable, Sendable, Identifiable {
    public var id: String
    public var label: String
    public var count: Int
    public var on: Bool
    public static let all = ["conversations", "scheduled", "kanban", "subagents", "archived"]
    public static let labels = ["conversations": "Conversations", "scheduled": "Scheduled jobs", "kanban": "Kanban",
                                "subagents": "Subagents", "archived": "Archived"]
    public init(id: String, label: String, count: Int, on: Bool) {
        self.id = id; self.label = label; self.count = count; self.on = on
    }
    public static func parse(_ json: Any?) -> [EarlierKind] {
        (json as? [Any] ?? []).compactMap { raw in
            guard let o = raw as? [String: Any], let id = nonEmpty(o["id"]), all.contains(id) else { return nil }
            return EarlierKind(id: id, label: nonEmpty(o["label"]) ?? labels[id] ?? id,
                               count: (o["count"] as? NSNumber)?.intValue ?? 0, on: o["on"] as? Bool ?? false)
        }
    }
    /// The query value for a set of checked kinds (Conversations alone when nothing is checked).
    public static func query(_ on: Set<String>) -> String {
        let picked = all.filter(on.contains)
        return (picked.isEmpty ? ["conversations"] : picked).joined(separator: ",")
    }
}

// MARK: - Home as one stream (SPK-208)

/// A picture, clip or file the user sent with a Home message (SPK-220): enough to draw it in the
/// thread (a poster from the app's own cache, or a labeled tile). Never a path.
public struct SentFile: Equatable, Sendable, Identifiable {
    public enum Kind: String, Sendable { case video, picture, file }
    public var id: String
    public var kind: Kind
    public var name: String?
    public var mime: String?
    /// video: its length in seconds.
    public var duration: TimeInterval?
    public init(id: String, kind: Kind, name: String? = nil, mime: String? = nil, duration: TimeInterval? = nil) {
        self.id = id; self.kind = kind; self.name = name; self.mime = mime; self.duration = duration
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) else { return nil }
        let mime = nonEmpty(o["mime"])
        let kind = (o["kind"] as? String).flatMap(Kind.init(rawValue:))
            ?? (mime?.hasPrefix("video/") == true ? .video : mime?.hasPrefix("image/") == true ? .picture : .file)
        let duration = (o["duration"] as? NSNumber)?.doubleValue
        self.init(id: id, kind: kind, name: nonEmpty(o["name"]), mime: mime,
                  duration: duration.flatMap { $0.isFinite && $0 > 0 ? $0 : nil })
    }
    /// "0:12", "1:05": a clip's length as a chip shows it.
    public static func clock(_ seconds: TimeInterval) -> String {
        let s = max(0, Int(seconds.rounded()))
        return String(format: "%d:%02d", s / 60, s % 60)
    }
    /// The short label a tile shows: "Video · 0:12", a file's name, or "Photo".
    public var label: String {
        switch kind {
        case .video: return duration.map { "Video · " + Self.clock($0) } ?? "Video"
        case .picture: return name ?? "Photo"
        case .file: return name ?? "File"
        }
    }
    /// What VoiceOver reads: "Video, 12 seconds", "Photo", "File, lease.pdf".
    public var spoken: String {
        switch kind {
        case .video:
            guard let d = duration else { return "Video" }
            let s = max(1, Int(d.rounded()))
            if s < 60 { return "Video, \(s) second\(s == 1 ? "" : "s")" }
            let m = s / 60, r = s % 60
            return "Video, \(m) minute\(m == 1 ? "" : "s")" + (r > 0 ? " \(r) second\(r == 1 ? "" : "s")" : "")
        case .picture: return "Photo"
        case .file: return name.map { "File, \($0)" } ?? "File"
        }
    }
    /// The SF Symbol for a tile with no poster.
    public var symbol: String {
        switch kind {
        case .video: return "video.fill"
        case .picture: return "photo"
        case .file: return "doc"
        }
    }
}

/// A server-resolved snapshot of the Home message being answered. A job card quotes as a mini card:
/// its title, one status line and the run it stands for (`id` is then "r:<run id>").
public struct HomeReplyQuote: Equatable, Sendable {
    public var id: String
    public var role: NowStream.Entry.Role
    public var text: String
    /// A job card (not a message): drawn as a mini card (glyph + title + status line).
    public var isCard: Bool
    public var line: String?
    public var runID: String?
    public init(id: String, role: NowStream.Entry.Role, text: String) {
        self.id = id; self.role = role; self.text = text; isCard = false; line = nil; runID = nil
    }
    /// A job card on Home.
    public init(cardRunID run: String, title: String, line: String?) {
        id = "r:" + run; role = .assistant; text = title; isCard = true
        self.line = line.flatMap { $0.isEmpty ? nil : $0 }; runID = run
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]), let text = nonEmpty(o["text"]),
              let raw = o["role"] as? String else { return nil }
        if raw == "card" {
            guard let run = nonEmpty(o["run_id"]) else { return nil }
            self.init(cardRunID: run, title: text, line: nonEmpty(o["line"]))
            self.id = id
        } else {
            guard let role = NowStream.Entry.Role(rawValue: raw) else { return nil }
            self.init(id: id, role: role, text: text)
        }
    }
}

/// Home as a single thread: the user's asks and their cards in time order. A card keeps one id, so it
/// updates in place; older seen stretches arrive folded ("This morning · 6 done").
public struct NowStream: Equatable, Sendable {
    public struct Entry: Equatable, Sendable, Identifiable {
        /// `message`: Home's chat (plugins with `home_chat`): your words or the coordinator's reply.
        /// `result` (b73): a finished job's result posted as its own message at the bottom of the
        /// thread (an email it drafted); its job's card stays where it was asked, carrying `resultID`.
        public enum Kind: String, Sendable { case ask, card, group, message, result }
        public enum Role: String, Sendable { case user, assistant }
        /// A question, confirm or checkout is a widget in the thread: open -> confirmed -> done;
        /// replaced: nobody answered it and a newer question took over (it stays, closed, saying when).
        public enum Widget: String, Sendable { case open, confirmed, done, replaced }
        public var id: String
        public var kind: Kind
        public var at: Date
        /// ask: what the user asked.
        public var text: String?
        /// ask and card: the Home item this entry belongs to.
        public var itemID: String?
        public var widget: Widget?
        /// widget `replaced`: when the newer question took over.
        public var replacedAt: Date?
        /// group: the plain line ("This morning · 6 done") and the items it folds.
        public var line: String?
        public var itemIDs: [String]
        /// message: who said it, which model wrote a reply, a voice note's length, the job it started.
        public var role: Role?
        public var attribution: ReplyAttribution?
        public var voiceDuration: TimeInterval?
        public var runID: String?
        /// message (yours): the client id it was sent with, so the app's "sending" bubble gives way to it.
        public var clientID: String?
        /// message (yours): the pictures, clips and files sent with it (its `text` may be empty then).
        public var files: [SentFile]
        public var quote: HomeReplyQuote? = nil
        /// card: the id of the result this job posted further down (tap the card to scroll to it).
        /// result: the id of the job's card up where it was asked.
        public var resultID: String? = nil
        public var cardID: String? = nil
        /// b73: part of Home's current exchange (the plugin marks it; `NowStream.currentExchange`
        /// works it out for an older plugin).
        public var current = false
        /// An SPK-314 answer line (an older job's verdict posted as a message), never part of the
        /// current exchange it lands in.
        public var answer = false
        /// home-ack: an assistant message's place in its exchange: `.ack` (the quick line above the
        /// status capsule) or `.answer` (what followed it). Nil: a reply that came in time.
        public enum Part: String, Sendable { case ack, answer }
        public var part: Part? = nil
        /// home-ack: your message whose acknowledgement is up and whose answer/job isn't yet.
        public var pending = false
        public init(id: String, kind: Kind, at: Date, text: String? = nil, itemID: String? = nil, widget: Widget? = nil,
                    line: String? = nil, itemIDs: [String] = [], role: Role? = nil, attribution: ReplyAttribution? = nil,
                    voiceDuration: TimeInterval? = nil, runID: String? = nil, clientID: String? = nil,
                    files: [SentFile] = []) {
            self.id = id; self.kind = kind; self.at = at; self.text = text; self.itemID = itemID; self.widget = widget
            self.line = line; self.itemIDs = itemIDs
            self.role = role; self.attribution = attribution; self.voiceDuration = voiceDuration; self.runID = runID
            self.clientID = clientID; self.files = files
        }
        public init?(json: Any?) {
            guard let o = json as? [String: Any], let id = nonEmpty(o["id"]),
                  let kind = (o["kind"] as? String).flatMap(Kind.init(rawValue:)),
                  let at = (o["at"] as? NSNumber)?.doubleValue else { return nil }
            let role = (o["role"] as? String).flatMap(Role.init(rawValue:))
            let files = (o["files"] as? [Any] ?? []).compactMap(SentFile.init(json:))
            if kind == .message && (role == nil || (nonEmpty(o["text"]) == nil && files.isEmpty)) { return nil }
            self.init(id: id, kind: kind, at: Date(timeIntervalSince1970: at), text: nonEmpty(o["text"]),
                      itemID: nonEmpty(o["item_id"]), widget: (o["widget"] as? String).flatMap(Widget.init(rawValue:)),
                      line: nonEmpty(o["line"]), itemIDs: (o["item_ids"] as? [Any] ?? []).compactMap(nonEmpty),
                      role: role, attribution: role == .assistant ? ReplyAttribution(json: o["model"]) : nil,
                      voiceDuration: ((o["voice"] as? [String: Any])?["duration"] as? NSNumber)?.doubleValue,
                      runID: nonEmpty(o["run_id"]), clientID: nonEmpty(o["client_id"]), files: files)
            quote = HomeReplyQuote(json: o["quote"])
            resultID = nonEmpty(o["result_id"])
            cardID = nonEmpty(o["card_id"])
            if kind == .result && (itemID == nil || runID == nil) { return nil }
            replacedAt = (o["replaced_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
            current = o["current"] as? Bool ?? false
            answer = o["answer"] as? Bool ?? false
            part = (o["part"] as? String).flatMap(Part.init(rawValue:))
            pending = o["pending"] as? Bool ?? false
        }
    }
    public var entries: [Entry]
    /// Home's chat has messages (plugins with `home_chat`): the thread shows even with no cards.
    public var hasMessages: Bool { entries.contains { $0.kind == .message } }
    /// Item ids waiting on the user, for the count on the work strip.
    public var needsYou: [String]
    /// b73 (capability `home_stack`): the current exchange as the plugin sent it. Nil from an older
    /// plugin; read `currentExchange(now:)` for it with a fallback.
    public var current: HomeCurrent?
    public init(entries: [Entry] = [], needsYou: [String] = [], current: HomeCurrent? = nil) {
        self.entries = entries; self.needsYou = needsYou; self.current = current
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any] else { return nil }
        self.init(entries: (o["entries"] as? [Any] ?? []).compactMap(Entry.init(json:)),
                  needsYou: (o["needs_you"] as? [Any] ?? []).compactMap(nonEmpty),
                  current: HomeCurrent(json: o["current"]))
    }
}

/// `POST /voice/home/message`'s reply: your message, the coordinator's reply (none when it only
/// started a job and had nothing to add), what it did and the job it started or followed up.
public struct HomeChatReply: Equatable, Sendable {
    public enum Action: String, Sendable { case none, start, followUp = "follow_up", cancel, failed }
    public var message: NowStream.Entry?
    public var reply: NowStream.Entry?
    public var action: Action
    public var runID: String?
    public var sessionID: String?
    public init(message: NowStream.Entry? = nil, reply: NowStream.Entry? = nil, action: Action = .none,
                runID: String? = nil, sessionID: String? = nil) {
        self.message = message; self.reply = reply; self.action = action; self.runID = runID; self.sessionID = sessionID
    }
    public init?(json: [String: Any]) {
        guard json["ok"] as? Bool == true else { return nil }
        self.init(message: NowStream.Entry(json: json["message"]), reply: NowStream.Entry(json: json["reply"]),
                  action: (json["action"] as? String).flatMap(Action.init(rawValue:)) ?? .none,
                  runID: nonEmpty(json["run_id"]), sessionID: nonEmpty(json["session_id"]))
    }
}

/// Home chat's model (servers with `home_chat`, `GET /voice/home/model`): the coordinator model in
/// use, why (`chosen` on the Home chip, `auto` from the user's connected models, `main`...), and the
/// chip's choices.
public struct HomeModelStatus: Equatable, Sendable {
    public struct Option: Equatable, Sendable, Identifiable {
        public var id: String
        public var name: String
        public var providerName: String
        public init(id: String, name: String, providerName: String = "") {
            self.id = id; self.name = name; self.providerName = providerName
        }
        init?(json: Any?) {
            guard let d = json as? [String: Any], let id = d["id"] as? String, !id.isEmpty else { return nil }
            self.init(id: id, name: (d["name"] as? String) ?? id, providerName: (d["provider_name"] as? String) ?? "")
        }
    }
    public var model: Option?
    public var source: String
    public var auto: Option?
    public var options: [Option]
    /// Set when the chosen model didn't answer last time (another one replied).
    public var note: String?
    /// The model jobs started from Home run on (plugins with `work` in `/voice/home/model`): what
    /// Home's chip shows and picks. The fields above are the coordinator's, a secondary choice.
    public var work: Work?
    public struct Work: Equatable, Sendable {
        public var model: Option?
        /// `chosen` on Home's chip, else `default` (the user's Hermes default model).
        public var source: String
        public var defaultModel: Option?
        /// The few models the picker lists first (the default, then each provider's newest).
        public var options: [Option]
        /// Every other connected model, behind "More…" in the picker (newer plugins).
        public var more: [Option]
        public init(model: Option?, source: String, defaultModel: Option?, options: [Option], more: [Option] = []) {
            self.model = model; self.source = source; self.defaultModel = defaultModel; self.options = options
            self.more = more
        }
        init?(json: Any?) {
            guard let d = json as? [String: Any] else { return nil }
            self.init(model: Option(json: d["model"]), source: (d["source"] as? String) ?? "default",
                      defaultModel: Option(json: d["default"]),
                      options: ((d["options"] as? [Any]) ?? []).compactMap { Option(json: $0) },
                      more: ((d["more"] as? [Any]) ?? []).compactMap { Option(json: $0) })
        }
        public var chosen: Bool { source == "chosen" }
    }
    public init(model: Option?, source: String, auto: Option?, options: [Option], note: String? = nil, work: Work? = nil) {
        self.model = model; self.source = source; self.auto = auto; self.options = options; self.note = note
        self.work = work
    }
    public init(json: [String: Any]) {
        let c = (json["coordinator"] as? [String: Any]) ?? json
        self.init(model: Option(json: c["model"]), source: (c["source"] as? String) ?? "",
                  auto: Option(json: c["auto"]),
                  options: ((c["options"] as? [Any]) ?? []).compactMap { Option(json: $0) },
                  note: (c["note"] as? String).flatMap { $0.isEmpty ? nil : $0 },
                  work: Work(json: json["work"]))
    }
    /// The coordinator's model was picked by the user (otherwise Auto or a fallback is in use).
    public var chosen: Bool { source == "chosen" }
    /// How the picker names Auto: the fast model it picked, or — when none of the approved fast
    /// models is connected — the user's work model, which the coordinator then uses.
    public var autoLabel: String {
        if let auto { return "Auto · \(auto.name)" }
        return source == "work" ? "Auto · same as your work model" : "Auto"
    }
}

/// How Home's model picker presents a model: a provider glyph (an SF Symbol, never a brand logo)
/// and one plain line about what it's good for. Read from the id and provider only.
public extension HomeModelStatus.Option {
    private var provider: String { (id.split(separator: "/").first.map(String.init) ?? "").lowercased() }
    private var lowered: String { (id + " " + name).lowercased() }

    /// The provider's glyph in the picker.
    var glyph: String {
        let p = provider + " " + providerName.lowercased()
        if lowered.contains("claude") || p.contains("anthropic") { return "asterisk" }
        if lowered.contains("gemini") || p.contains("google") { return "sparkle" }
        if lowered.contains("grok") || p.contains("xai") { return "bolt" }
        if lowered.contains("gpt") || p.contains("openai") || p.contains("codex") { return "circle.hexagonpath" }
        if p.contains("openrouter") { return "arrow.triangle.branch" }
        if p.contains("studio") || p.contains("local") || p.contains("ollama") || p.contains("lmstudio") || p.contains("custom") {
            return "desktopcomputer"
        }
        return "cpu"
    }

    /// One plain line: what the model is good for, else who makes it.
    var blurb: String {
        let m = lowered
        let p = provider + " " + providerName.lowercased()
        if m.contains("opus") { return "Most capable. Best for long, hard jobs." }
        if m.contains("sonnet") { return "Fast and capable. Good for most jobs." }
        if m.contains("haiku") { return "Quickest. Best for light, simple jobs." }
        if m.range(of: #"\b(mini|nano|flash|lite|fast)\b"#, options: .regularExpression) != nil
            || m.contains("-mini") || m.contains("-nano") || m.contains("-flash") || m.contains("-fast") {
            return "Small and quick. Best for light jobs."
        }
        if p.contains("studio") || p.contains("local") || p.contains("ollama") || p.contains("lmstudio") {
            return "Runs on your own computer."
        }
        if m.contains("gemini") { return "Google's model for most jobs." }
        if m.contains("grok") { return "xAI's model for most jobs." }
        if m.contains("gpt") || m.contains("codex") { return "OpenAI's model for most jobs." }
        return providerName.isEmpty ? "One of your connected models." : "From \(providerName)."
    }
}

/// One row of Recent: a previous task, finished or not.
public struct NowRecentItem: Equatable, Sendable, Identifiable {
    public var id: String
    public var title: String
    public var state: String
    public var at: Date
    public var sessionID: String?
    public var line: String?
    public init(id: String, title: String, state: String, at: Date, sessionID: String? = nil, line: String? = nil) {
        self.id = id; self.title = title; self.state = state; self.at = at; self.sessionID = sessionID; self.line = line
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]), let at = (o["at"] as? NSNumber)?.doubleValue else { return nil }
        self.init(id: id, title: nonEmpty(o["title"]) ?? "Untitled", state: nonEmpty(o["state"]) ?? "completed",
                  at: Date(timeIntervalSince1970: at), sessionID: nonEmpty(o["session_id"]), line: nonEmpty(o["line"]))
    }
    /// "Done", "Stopped", "In progress" in plain words.
    public var statusWords: String {
        switch state {
        case "completed": return "Done"
        case "failed", "interrupted", "cancelled": return "Stopped"
        default: return "In progress"
        }
    }
}
