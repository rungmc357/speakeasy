import Foundation

// Build 73's Home state (capability `home_stack`, docs/API.md "Home state"): which part of Home's
// thread is the current exchange, the widget stack of things that need you or are new, and up to
// three recommended next things. All three are additive keys on `GET /voice/now`; an older plugin
// sends none of them, so each has a fallback the app works out from what it does send.

// MARK: - Current exchange

/// `stream.current`: the latest message, the replies after it, the cards of the jobs it started and
/// their results. Everything else in `stream.entries` is transcript only. `clean` after 20 minutes
/// without a message from either side.
public struct HomeCurrent: Equatable, Sendable {
    /// The plugin's reset interval (`RESET_AFTER_S`): 20 minutes after the last message.
    public static let resetAfter: TimeInterval = 20 * 60

    public var clean: Bool
    /// Every entry of the exchange, in thread order (the message first).
    public var entryIDs: [String]
    public var messageID: String?
    public var replyIDs: [String]
    public var cardIDs: [String]
    public var runIDs: [String]
    public var lastActivityAt: Date?
    /// When Home goes clean if nothing else is said (nil when already clean).
    public var resetsAt: Date?
    /// Worked out by the app from an older plugin's thread, not sent by the plugin.
    public var derived = false
    /// home-ack: the agent's side of the exchange in order (`reply_ids` with each one's part:
    /// "ack", "answer" or "reply"), and whether the answer/job is still being worked out.
    public struct Part: Equatable, Sendable { public var id: String; public var part: String
        public init(id: String, part: String) { self.id = id; self.part = part } }
    public var parts: [Part] = []
    public var pending = false

    public static let empty = HomeCurrent(clean: true)

    public init(clean: Bool, entryIDs: [String] = [], messageID: String? = nil, replyIDs: [String] = [],
                cardIDs: [String] = [], runIDs: [String] = [], lastActivityAt: Date? = nil, resetsAt: Date? = nil,
                derived: Bool = false) {
        self.clean = clean; self.entryIDs = entryIDs; self.messageID = messageID; self.replyIDs = replyIDs
        self.cardIDs = cardIDs; self.runIDs = runIDs; self.lastActivityAt = lastActivityAt; self.resetsAt = resetsAt
        self.derived = derived
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any] else { return nil }
        func ids(_ k: String) -> [String] { (o[k] as? [Any] ?? []).compactMap(nonEmpty) }
        func date(_ k: String) -> Date? { (o[k] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) } }
        let entryIDs = ids("entry_ids")
        self.init(clean: (o["clean"] as? Bool) ?? entryIDs.isEmpty, entryIDs: entryIDs, messageID: nonEmpty(o["message_id"]),
                  replyIDs: ids("reply_ids"), cardIDs: ids("card_ids"), runIDs: ids("run_ids"),
                  lastActivityAt: date("last_activity_at"), resetsAt: date("resets_at"))
        parts = (o["parts"] as? [Any] ?? []).compactMap { raw in
            guard let p = raw as? [String: Any], let id = nonEmpty(p["id"]) else { return nil }
            return Part(id: id, part: nonEmpty(p["part"]) ?? "reply")
        }
        pending = o["pending"] as? Bool ?? false
        if clean { self.entryIDs = []; resetsAt = nil; parts = []; pending = false }
    }

    public func contains(_ entryID: String) -> Bool { !clean && entryIDs.contains(entryID) }

    /// As of *now*: clean once the plugin's `resetsAt` has passed, even if the refresh scheduled for
    /// that moment hasn't landed yet (the plugin's time, never an app timer).
    public func asOf(_ now: Date) -> HomeCurrent {
        guard !clean, let resetsAt, now >= resetsAt else { return self }
        return HomeCurrent(clean: true, lastActivityAt: lastActivityAt, derived: derived)
    }

    /// The plugin's rule, for an older plugin's thread: the latest user message, the assistant replies
    /// after it (never an SPK-314 answer line), the cards of the jobs those name, and those jobs' own
    /// results and answer lines. No reset by default (D1: the reset is the plugin's; an older plugin
    /// has none); pass *resetAfter* only to mirror the plugin's rule in tests.
    public static func derive(from entries: [NowStream.Entry], now: Date = Date(),
                              resetAfter: TimeInterval? = nil) -> HomeCurrent {
        guard let last = entries.last(where: { $0.kind == .message && $0.role == .user }) else {
            return HomeCurrent(clean: true, derived: true)
        }
        let replies = entries.filter {
            $0.id != last.id && $0.at >= last.at && $0.kind == .message && $0.role == .assistant && !$0.answer
        }
        var runs = Set([last.runID].compactMap { $0 } + replies.compactMap(\.runID))
        let activity = ([last.at] + replies.map(\.at)).max() ?? last.at
        if let resetAfter, now.timeIntervalSince(activity) >= resetAfter {
            return HomeCurrent(clean: true, lastActivityAt: activity, derived: true)
        }
        let cards = entries.filter { $0.kind == .card && runs.contains(String($0.id.dropFirst(2))) }
        let tied = entries.filter {
            guard let run = $0.runID, runs.contains(run) else { return false }
            return $0.kind == .result || ($0.kind == .message && $0.answer)
        }
        var ids: [String] = []
        for e in [last] + replies + cards + tied where !ids.contains(e.id) { ids.append(e.id) }
        runs.formUnion(cards.map { String($0.id.dropFirst(2)) })
        return HomeCurrent(clean: false, entryIDs: ids, messageID: last.id, replyIDs: replies.map(\.id),
                                   cardIDs: cards.map(\.id), runIDs: runs.sorted(), lastActivityAt: activity,
                                   resetsAt: resetAfter.map { activity.addingTimeInterval($0) }, derived: true)
    }
}

public extension NowStream {
    /// The current exchange as of *now*: the plugin's when it sent one, else worked out from the thread.
    func currentExchange(now: Date = Date()) -> HomeCurrent {
        (current ?? HomeCurrent.derive(from: entries, now: now)).asOf(now)
    }

    /// The entries of the current exchange, in thread order (empty when Home is clean).
    func currentEntries(now: Date = Date()) -> [Entry] {
        let cur = currentExchange(now: now)
        return entries.filter { cur.contains($0.id) }
    }
}

// MARK: - Widget stack

/// One thing in Home's widget stack: a decision (question or approval), a stopped job, or a new result.
public struct HomeStackItem: Equatable, Sendable, Identifiable {
    /// `unknown`: a kind a newer plugin added (its `needs_you` still says where it sorts).
    public enum Kind: String, Sendable { case decision, stopped, result, unknown }
    public struct Source: Equatable, Sendable {
        public enum Kind: String, Sendable { case job, chat }
        public var kind: Kind
        public var runID: String?
        public var sessionID: String?
        public init(kind: Kind, runID: String? = nil, sessionID: String? = nil) {
            self.kind = kind; self.runID = runID; self.sessionID = sessionID
        }
        public init(json: Any?, runID fallback: String?) {
            let o = json as? [String: Any] ?? [:]
            let run = nonEmpty(o["run_id"]) ?? fallback
            self.init(kind: (o["kind"] as? String).flatMap(Kind.init(rawValue:)) ?? (run == nil ? .chat : .job),
                      runID: run, sessionID: nonEmpty(o["session_id"]))
        }
    }

    /// `w:q:<run>:<asked_at>`, `w:s:<run>` or `w:r:<run>`.
    public var id: String
    public var kind: Kind
    public var needsYou: Bool
    /// The Home item (`NowItem.id`) it stands for.
    public var itemID: String?
    /// The job's card in the thread (`c:<run>`).
    public var cardID: String?
    /// The job's posted result (`x:<run>`) or answer line (`d:<run>`); nil when the card is the result.
    public var resultID: String?
    public var title: String
    public var line: String
    public var source: Source
    public var runID: String?
    public var addedAt: Date?
    public var at: Date?
    /// The stack item this one came from (the question answered, the result replied to).
    public var follows: String?
    /// The current exchange already shows this job in place.
    public var inCurrent = false
    /// Dismissed on Home and still open (listed on Work).
    public var dismissedOnHomeAt: Date?
    /// Worked out by the app from an older plugin's Home items; dismissing it uses the old swipe route.
    public var derived = false

    public init(id: String, kind: Kind, needsYou: Bool? = nil, itemID: String? = nil, cardID: String? = nil,
                resultID: String? = nil, title: String, line: String = "", source: Source, runID: String? = nil,
                addedAt: Date? = nil, at: Date? = nil, follows: String? = nil, inCurrent: Bool = false,
                dismissedOnHomeAt: Date? = nil, derived: Bool = false) {
        self.id = id; self.kind = kind; self.needsYou = needsYou ?? (kind == .decision || kind == .stopped)
        self.itemID = itemID
        self.cardID = cardID; self.resultID = resultID; self.title = title; self.line = line; self.source = source
        self.runID = runID; self.addedAt = addedAt; self.at = at; self.follows = follows; self.inCurrent = inCurrent
        self.dismissedOnHomeAt = dismissedOnHomeAt; self.derived = derived
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]),
              let rawKind = nonEmpty(o["kind"]) else { return nil }
        let kind = Kind(rawValue: rawKind) ?? .unknown
        func date(_ k: String) -> Date? { (o[k] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) } }
        let source = Source(json: o["source"], runID: nonEmpty(o["run_id"]))
        let run = nonEmpty(o["run_id"]) ?? source.runID
        self.init(id: id, kind: kind, needsYou: o["needs_you"] as? Bool, itemID: nonEmpty(o["item_id"]),
                  cardID: nonEmpty(o["card_id"]) ?? run.map { "c:" + $0 }, resultID: nonEmpty(o["result_id"]),
                  title: String((nonEmpty(o["title"]) ?? "Untitled").prefix(120)),
                  line: String((nonEmpty(o["line"]) ?? "").prefix(400)), source: source, runID: run,
                  addedAt: date("added_at"), at: date("at"), follows: nonEmpty(o["follows"]),
                  inCurrent: o["in_current"] as? Bool ?? false, dismissedOnHomeAt: date("dismissed_on_home_at"))
    }
}

/// `stack` on `/voice/now` and `GET /voice/home/stack`: what's on Home's stack (needs-you first, then
/// results, newest first in each), how many in all, and what was dismissed on Home but is still open.
public struct HomeStack: Equatable, Sendable {
    public var items: [HomeStackItem]
    /// Everything on the stack, which can be more than `items` carries.
    public var count: Int
    /// Dismissed on Home, still open: Work lists these until they're dealt with.
    public var onWork: [HomeStackItem]
    /// Worked out by the app from an older plugin's Home items.
    public var derived = false

    public static let empty = HomeStack()

    public init(items: [HomeStackItem] = [], count: Int? = nil, onWork: [HomeStackItem] = [], derived: Bool = false) {
        self.items = items; self.count = max(count ?? items.count, items.count); self.onWork = onWork; self.derived = derived
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any] else { return nil }
        let items = (o["items"] as? [Any] ?? []).compactMap(HomeStackItem.init(json:))
        self.init(items: items, count: (o["count"] as? NSNumber)?.intValue,
                  onWork: (o["on_work"] as? [Any] ?? []).compactMap(HomeStackItem.init(json:)))
    }

    public var needsYou: [HomeStackItem] { items.filter(\.needsYou) }
    public var results: [HomeStackItem] { items.filter { !$0.needsYou } }

    /// An older plugin's stack, from its Home items: an open question (`decision`), a stopped job, a new
    /// unread answer (`result`); never working, waiting or canceled work. Same ids and order as the
    /// plugin's. Nothing on Work: an older plugin has no Home-only dismiss.
    public static func derive(from items: [NowItem], stream: NowStream? = nil, currentRunIDs: Set<String> = []) -> HomeStack {
        var resultFor: [String: String] = [:]   // card id -> its posted result
        for e in stream?.entries ?? [] where e.kind == .card { if let r = e.resultID { resultFor[e.id] = r } }
        var out: [HomeStackItem] = []
        for item in items where !item.canceled {
            let kind: HomeStackItem.Kind
            let ref: String
            switch item.kind {
            case .question where item.replacedAt == nil:
                kind = .decision
                let asked = Int((item.updatedAt ?? item.startedAt)?.timeIntervalSince1970 ?? 0)
                ref = item.runID.map { "\($0):\(asked)" } ?? item.id
            case .stopped where item.runID != nil: kind = .stopped; ref = item.runID!
            case .newAnswer where item.runID != nil: kind = .result; ref = item.runID!
            default: continue
            }
            let letter = kind == .decision ? "q" : kind == .stopped ? "s" : "r"
            let card = "c:" + (item.runID ?? item.id)
            out.append(HomeStackItem(
                id: "w:\(letter):\(ref)", kind: kind, itemID: item.id, cardID: card, resultID: resultFor[card],
                title: item.title, line: item.line,
                source: .init(kind: item.runID == nil ? .chat : .job, runID: item.runID, sessionID: item.sessionID),
                runID: item.runID, at: item.finishedAt ?? item.updatedAt ?? item.startedAt,
                inCurrent: item.runID.map(currentRunIDs.contains) ?? false, derived: true))
        }
        let ordered = out.enumerated().sorted { a, b in
            if a.element.needsYou != b.element.needsYou { return a.element.needsYou }
            let ta = a.element.at ?? .distantPast, tb = b.element.at ?? .distantPast
            return ta != tb ? ta > tb : a.offset < b.offset
        }.map(\.element)
        return HomeStack(items: ordered, derived: true)
    }
}

/// Where a stack item is dismissed: on Home it leaves the stack and waits on Work; on Work it's gone.
public enum HomeDismissPlace: String, Sendable { case home, work }

/// `POST /voice/home/stack/{id}/dismiss`'s reply.
public struct HomeStackDismissal: Equatable, Sendable {
    public enum State: String, Sendable { case onWork = "on_work", gone }
    public var id: String
    public var from: HomeDismissPlace
    public var state: State
    public init(id: String, from: HomeDismissPlace, state: State) { self.id = id; self.from = from; self.state = state }
    public init(json: [String: Any], id: String, from: HomeDismissPlace) {
        self.init(id: nonEmpty(json["id"]) ?? id,
                  from: (json["from"] as? String).flatMap(HomeDismissPlace.init(rawValue:)) ?? from,
                  state: (json["state"] as? String).flatMap(State.init(rawValue:)) ?? (from == .work ? .gone : .onWork))
    }
}

// MARK: - Recommendations

/// One recommended next thing: `text` is what the chip shows, `prompt` what sending it says.
public struct HomeRecommendation: Equatable, Sendable, Identifiable {
    public struct Source: Equatable, Sendable {
        public enum Kind: String, Sendable {
            case unsentDraft = "unsent_draft", skippedQuestion = "skipped_question", stoppedJob = "stopped_job"
            case dismissedOnHome = "dismissed_on_home", followUp = "follow_up", generic
            /// A kind a newer plugin added; `rawKind` keeps its name.
            case other
        }
        public var kind: Kind
        public var rawKind: String
        /// Ready to show ("Unsent draft", "Just finished").
        public var label: String
        public var runID: String?
        public var stackID: String?
        public var draftID: String?
        public init(kind: Kind, label: String, runID: String? = nil, stackID: String? = nil, draftID: String? = nil) {
            self.kind = kind; rawKind = kind.rawValue; self.label = label; self.runID = runID; self.stackID = stackID
            self.draftID = draftID
        }
        public init(json: Any?) {
            let o = json as? [String: Any] ?? [:]
            let raw = nonEmpty(o["kind"]) ?? "generic"
            self.init(kind: Kind(rawValue: raw) ?? .other, label: nonEmpty(o["label"]) ?? "Idea",
                      runID: nonEmpty(o["run_id"]), stackID: nonEmpty(o["stack_id"]), draftID: nonEmpty(o["draft_id"]))
            rawKind = raw
        }
    }

    /// `rec:…` (from your own stuff) or `gen:…` (a fixed generic idea).
    public var id: String
    public var text: String
    public var prompt: String
    public var generic: Bool
    public var source: Source
    public init(id: String, text: String, prompt: String? = nil, generic: Bool = false, source: Source) {
        self.id = id; self.text = text; self.prompt = prompt ?? text; self.generic = generic; self.source = source
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]), let text = nonEmpty(o["text"]) else { return nil }
        let source = Source(json: o["source"])
        self.init(id: id, text: String(text.prefix(160)), prompt: nonEmpty(o["prompt"]),
                  generic: o["generic"] as? Bool ?? (id.hasPrefix("gen:") || source.kind == .generic), source: source)
    }
}

/// `recommendations` on `/voice/now` and `GET /voice/home/recommendations`.
public struct HomeRecommendations: Equatable, Sendable {
    public var items: [HomeRecommendation]
    /// Nothing personal to suggest: these are the fixed generic ideas.
    public var generic: Bool
    public var computedAt: Date?
    /// The app's own fallback for an older plugin (dismissing one is local only).
    public var derived = false

    public init(items: [HomeRecommendation] = [], generic: Bool? = nil, computedAt: Date? = nil, derived: Bool = false) {
        self.items = items; self.generic = generic ?? (!items.isEmpty && items.allSatisfy(\.generic))
        self.computedAt = computedAt; self.derived = derived
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any] else { return nil }
        let items = (o["items"] as? [Any] ?? []).compactMap(HomeRecommendation.init(json:))
        self.init(items: Array(items.prefix(3)), generic: o["generic"] as? Bool,
                  computedAt: (o["computed_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) })
    }

    /// The plugin's fixed generic ideas (home_state.GENERIC), for an older plugin that sends none.
    public static let genericFallback = HomeRecommendations(items: [
        ("gen:catch-up", "Catch me up on what came in today", "Catch me up on what came in today."),
        ("gen:plan-week", "Plan the rest of my week", "Help me plan the rest of my week."),
        ("gen:calendar", "What's on my calendar tomorrow?", "What's on my calendar tomorrow?"),
    ].map { HomeRecommendation(id: $0.0, text: $0.1, prompt: $0.2, generic: true,
                                source: .init(kind: .generic, label: "Idea")) }, generic: true, derived: true)

    /// Without the ones dismissed on this device (for an older plugin's fallback, which the server can't drop).
    public func without(_ ids: Set<String>) -> HomeRecommendations {
        var copy = self
        copy.items.removeAll { ids.contains($0.id) }
        return copy
    }
}

/// The URL path piece for a stack item or recommendation id (`w:q:<run>:<t>` -> `w%3Aq%3A<run>%3A<t>`):
/// the whole id percent-encoded, which the plugin's routes decode.
public enum HomeStatePath {
    private static let unreserved = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_.~"))
    public static func segment(_ id: String) -> String { id.addingPercentEncoding(withAllowedCharacters: unreserved) ?? id }
    public static func stackDismiss(_ id: String) -> String { "/voice/home/stack/\(segment(id))/dismiss" }
    public static func recommendationDismiss(_ id: String) -> String { "/voice/home/recommendations/\(segment(id))/dismiss" }
}

// MARK: - On Home

public extension NowHome {
    /// The current exchange as of *now* (the plugin's, or worked out from an older plugin's thread).
    func currentExchange(now: Date = Date()) -> HomeCurrent {
        stream?.currentExchange(now: now) ?? HomeCurrent(clean: true, derived: true)
    }

    /// The widget stack: the plugin's, else worked out from an older plugin's items.
    func widgetStack(now: Date = Date()) -> HomeStack {
        if let stack { return stack }
        return HomeStack.derive(from: items, stream: stream, currentRunIDs: Set(currentExchange(now: now).runIDs))
    }

    /// What needs you: the `needs_you` items on the stack and on Work (a stopped job counts); with no
    /// `stack` (an older plugin), `counts.needs_you + counts.stopped`.
    var needsYouCount: Int {
        guard let stack else { return counts.needsYou + counts.stopped }
        return stack.items.filter(\.needsYou).count + stack.onWork.filter(\.needsYou).count
    }

    /// The plugin's recommendations, else the fixed generic ideas.
    var recommendationsOrFallback: HomeRecommendations { recommendations ?? .genericFallback }
}
