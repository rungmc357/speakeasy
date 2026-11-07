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
        self.init(sessionID: sid, title: nonEmpty(o["title"]) ?? "Untitled", line: nonEmpty(o["line"]) ?? "Working on it",
                  sourceLabel: nonEmpty(o["source_label"]))
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
    public var provenance: Provenance?
    public var fix: Fix?
    public var question: NowQuestion?
    /// On a call: `asking` (the question the voice is asking now, first on Home) or `heard` (the voice
    /// read this answer out, so it's already read).
    public var onCall: OnCall?
    /// Asking on a call: a spoken answer may pick a choice (false: a risky one, it needs a tap).
    public var sayOK: Bool = true
    /// Asking on a call with a risky choice: "Can't be undone · Needs a tap".
    public var lockLine: String?
    public enum OnCall: String, Sendable { case asking, heard }

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
        sayOK = o["say_ok"] as? Bool ?? true
        lockLine = nonEmpty(o["lock_line"])
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

    public init(shape: Shape, prompt: String, context: String? = nil, options: [Option] = [], recommended: Int? = nil,
                approvalChoices: [String] = []) {
        self.shape = shape; self.prompt = prompt; self.context = context; self.options = options
        self.recommended = recommended; self.approvalChoices = approvalChoices
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
                  approvalChoices: ((o["approval"] as? [String: Any])?["choices"] as? [String]) ?? [])
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
