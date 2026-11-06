import Foundation
import Combine

// Hermes Kanban boards, read-only (servers with `boards`; docs/API.md "Boards"). The Board screen on
// the Mac History window and on iPhone/iPad: pick a board (remembered), four columns (Ready, Running,
// Blocked, Done), each card with its title, how long it's run or when it finished, and "Needs you"
// when blocked. A card opens the session it ran in. Refreshes every few seconds while on screen.

/// One row of `GET /voice/boards`.
public struct KanbanBoardSummary: Equatable, Sendable, Identifiable, Hashable {
    public var slug: String
    public var name: String
    public var ready: Int
    public var running: Int
    public var blocked: Int
    public var done: Int
    public var activeAt: Date?
    public var id: String { slug }

    public init(slug: String, name: String, ready: Int = 0, running: Int = 0, blocked: Int = 0, done: Int = 0,
                activeAt: Date? = nil) {
        self.slug = slug; self.name = name; self.ready = ready; self.running = running
        self.blocked = blocked; self.done = done; self.activeAt = activeAt
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let slug = nonEmpty(o["slug"]) else { return nil }
        let counts = o["counts"] as? [String: Any] ?? [:]
        func n(_ key: String) -> Int { (counts[key] as? NSNumber)?.intValue ?? 0 }
        self.init(slug: slug, name: String((nonEmpty(o["name"]) ?? slug).prefix(60)),
                  ready: n("ready"), running: n("running"), blocked: n("blocked"), done: n("done"),
                  activeAt: unixDate(o["active_at"]))
    }

    /// "2 running · 1 needs you", "Nothing going on".
    public var subtitle: String {
        var parts: [String] = []
        if running > 0 { parts.append("\(running) running") }
        if blocked > 0 { parts.append("\(blocked) need\(blocked == 1 ? "s" : "") you") }
        if ready > 0 { parts.append("\(ready) ready") }
        if parts.isEmpty { return done > 0 ? "\(done) done this week" : "Nothing going on" }
        return parts.joined(separator: " · ")
    }
}

public struct KanbanBoardList: Equatable, Sendable {
    public var boards: [KanbanBoardSummary]
    /// The board to open with no remembered pick.
    public var suggested: String?
    public init(boards: [KanbanBoardSummary] = [], suggested: String? = nil) {
        self.boards = boards; self.suggested = suggested
    }
    public init(json: [String: Any]) {
        boards = (json["boards"] as? [Any] ?? []).compactMap(KanbanBoardSummary.init(json:))
        suggested = nonEmpty(json["suggested"])
    }
}

public struct KanbanCard: Equatable, Sendable, Identifiable, Hashable {
    public enum Column: String, CaseIterable, Sendable { case ready, running, blocked, done }

    public var id: String
    public var title: String
    /// Hermes' own status (`todo`, `review`…): the column says the rest.
    public var status: String
    public var column: Column
    /// When it was queued / started / blocked / finished, per column.
    public var since: Date?
    public var needsYou: Bool
    /// The session it ran in (opens in History), when there is one in this profile.
    public var sessionID: String?
    /// "Waiting on another card", "Ready for review".
    public var note: String?
    /// Why it's blocked.
    public var reason: String?
    /// A finished card's handoff, one line.
    public var summary: String?

    public init(id: String, title: String, status: String = "", column: Column, since: Date? = nil,
                needsYou: Bool = false, sessionID: String? = nil, note: String? = nil, reason: String? = nil,
                summary: String? = nil) {
        self.id = id; self.title = title; self.status = status.isEmpty ? column.rawValue : status
        self.column = column; self.since = since; self.needsYou = needsYou; self.sessionID = sessionID
        self.note = note; self.reason = reason; self.summary = summary
    }

    public init?(json: Any?, column fallback: Column) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) else { return nil }
        let column = (o["column"] as? String).flatMap(Column.init(rawValue:)) ?? fallback
        self.init(id: id, title: String((nonEmpty(o["title"]) ?? "Untitled card").prefix(160)),
                  status: nonEmpty(o["status"]) ?? "", column: column, since: unixDate(o["since"]),
                  needsYou: o["needs_you"] as? Bool ?? (column == .blocked),
                  sessionID: nonEmpty(o["session_id"]), note: nonEmpty(o["note"]),
                  reason: nonEmpty(o["reason"]).map { String($0.prefix(240)) },
                  summary: nonEmpty(o["summary"]).map { String($0.prefix(200)) })
    }

    /// "Running 25 min", "Finished 4:12 PM", "Waiting on you · 6 min", "Queued 12 min ago".
    public func timeLine(now: Date, calendar: Calendar = .current) -> String {
        guard let since else {
            switch column {
            case .running: return "Running"
            case .done: return "Finished"
            case .blocked: return needsYou ? "Waiting on you" : "Blocked"
            case .ready: return "Ready"
            }
        }
        let span = BoardFormat.span(from: since, to: now)
        switch column {
        case .running: return "Running \(span)"
        case .done: return "Finished \(BoardFormat.when(since, now: now, calendar: calendar))"
        case .blocked: return status == "review" ? "Waiting for review · \(span)" : "Waiting on you · \(span)"
        case .ready: return since > now.addingTimeInterval(-60) ? "Queued just now" : "Queued \(span) ago"
        }
    }
}

public struct KanbanColumn: Equatable, Sendable, Identifiable {
    public var key: KanbanCard.Column
    public var title: String
    /// Everything in the column (cards may be fewer on a huge board).
    public var count: Int
    public var cards: [KanbanCard]
    public var id: String { key.rawValue }
    public init(key: KanbanCard.Column, title: String? = nil, count: Int? = nil, cards: [KanbanCard]) {
        self.key = key; self.title = title ?? key.title; self.count = count ?? cards.count; self.cards = cards
    }
}

extension KanbanCard.Column {
    public var title: String {
        switch self {
        case .ready: return "Ready"
        case .running: return "Running"
        case .blocked: return "Blocked"
        case .done: return "Done"
        }
    }
    /// The empty column's line.
    public var emptyLine: String {
        switch self {
        case .ready: return "Nothing queued"
        case .running: return "Nothing running"
        case .blocked: return "Nothing waiting on you"
        case .done: return "Nothing finished this week"
        }
    }
}

/// `GET /voice/boards/{slug}`.
public struct KanbanBoardPage: Equatable, Sendable {
    public var slug: String
    public var name: String
    public var columns: [KanbanColumn]
    /// Moves whenever the board does.
    public var change: String

    public init(slug: String, name: String, columns: [KanbanColumn], change: String = "") {
        self.slug = slug; self.name = name; self.change = change
        // Always the four columns, in order.
        self.columns = KanbanCard.Column.allCases.map { key in
            columns.first { $0.key == key } ?? KanbanColumn(key: key, cards: [])
        }
    }

    public init?(json: [String: Any]) {
        guard let slug = nonEmpty(json["slug"]) else { return nil }
        let columns: [KanbanColumn] = (json["columns"] as? [Any] ?? []).compactMap { item in
            guard let o = item as? [String: Any], let key = (o["key"] as? String).flatMap(KanbanCard.Column.init(rawValue:))
            else { return nil }
            let cards = (o["cards"] as? [Any] ?? []).compactMap { KanbanCard(json: $0, column: key) }
            return KanbanColumn(key: key, title: nonEmpty(o["title"]), count: (o["count"] as? NSNumber)?.intValue ?? cards.count,
                                cards: cards)
        }
        self.init(slug: slug, name: String((nonEmpty(json["name"]) ?? slug).prefix(60)), columns: columns,
                  change: nonEmpty(json["change"]) ?? "")
    }

    public func column(_ key: KanbanCard.Column) -> KanbanColumn {
        columns.first { $0.key == key } ?? KanbanColumn(key: key, cards: [])
    }
    public var needsYouCount: Int { column(.blocked).cards.filter(\.needsYou).count }
    public var isEmpty: Bool { columns.allSatisfy { $0.cards.isEmpty } }
}

/// How the four columns sit in a pane of a given width (Mac History › Board, iPad). When all four fit
/// at the minimum width they share the pane. When they don't, the row scrolls sideways a whole column
/// at a time: as many full columns as fit plus a clear peek of the next one, so the edge always shows
/// there's more, and the board opens scrolled so the column with a card that needs you is in view.
public struct KanbanBoardLayout: Equatable, Sendable {
    public static let count = KanbanCard.Column.allCases.count

    /// Each column's width.
    public var columnWidth: CGFloat
    /// Whole columns in view at once (all four when nothing scrolls).
    public var visible: Int
    public var scrolls: Bool { visible < Self.count }
    /// The highest leading column the row can scroll to.
    public var lastLeading: Int { Self.count - visible }

    /// - width: the pane; `inset`: the margin on each side; `peek`: how much of the next column shows.
    public init(width: CGFloat, minColumn: CGFloat, spacing: CGFloat, inset: CGFloat, peek: CGFloat) {
        let n = CGFloat(Self.count)
        let all = (width - 2 * inset - (n - 1) * spacing) / n
        if all >= minColumn {
            columnWidth = all; visible = Self.count; return
        }
        // k full columns from the leading margin, then a gap and `peek` of the next one at the edge.
        let room = width - inset - peek
        let k = max(1, min(Self.count - 1, Int(((room) / (minColumn + spacing)).rounded(.down))))
        visible = k
        columnWidth = max(minColumn * 0.75, (room - CGFloat(k) * spacing) / CGFloat(k))
    }

    /// Columns fully in view when `leading` is the first one.
    public func inView(leading: Int) -> Range<Int> {
        let start = min(max(0, leading), lastLeading)
        return start..<(start + visible)
    }

    /// The column to open on: the start, unless that hides the column with a card that needs you —
    /// then just far enough along that it's the last full column in view.
    public func openingLeading(for page: KanbanBoardPage?) -> Int {
        guard scrolls, let target = page?.attentionIndex else { return 0 }
        return min(lastLeading, max(0, target - visible + 1))
    }

    /// The "needs you" column when it's out of view: which way it is, so the edge can point to it.
    public func offscreenAttention(leading: Int, page: KanbanBoardPage?) -> Edge? {
        guard scrolls, let target = page?.attentionIndex else { return nil }
        let shown = inView(leading: leading)
        if shown.contains(target) { return nil }
        return target < shown.lowerBound ? .leading : .trailing
    }

    public enum Edge: Sendable { case leading, trailing }
}

extension KanbanBoardPage {
    /// Where the column with a card that needs you sits (Blocked), when there is one.
    public var attentionIndex: Int? {
        guard needsYouCount > 0 else { return nil }
        return columns.firstIndex { $0.key == .blocked }
    }
}

public enum BoardFormat {
    /// "under a minute", "25 min", "1 hr 5 min", "3 hr", "2 days".
    public static func span(from start: Date, to end: Date) -> String {
        let minutes = max(0, Int(end.timeIntervalSince(start) / 60))
        if minutes < 1 { return "under a minute" }
        if minutes < 60 { return "\(minutes) min" }
        let hours = minutes / 60, rest = minutes % 60
        if hours < 24 { return rest == 0 || hours >= 6 ? "\(hours) hr" : "\(hours) hr \(rest) min" }
        let days = hours / 24
        return days == 1 ? "1 day" : "\(days) days"
    }

    /// US time: "4:12 PM" today, "yesterday 9:30 AM", "Tue 3:05 PM" this week, else "Oct 28".
    public static func when(_ date: Date, now: Date, calendar: Calendar = .current) -> String {
        let time = DateFormatter()
        time.locale = Locale(identifier: "en_US"); time.timeZone = calendar.timeZone; time.dateFormat = "h:mm a"
        if calendar.isDate(date, inSameDayAs: now) { return time.string(from: date) }
        if let y = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: y) {
            return "yesterday \(time.string(from: date))"
        }
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US"); f.timeZone = calendar.timeZone
        f.dateFormat = now.timeIntervalSince(date) < 6 * 86400 ? "EEE h:mm a" : "MMM d"
        return f.string(from: date)
    }
}

func unixDate(_ value: Any?) -> Date? {
    guard let n = value as? NSNumber, n.doubleValue > 0 else { return nil }
    return Date(timeIntervalSince1970: n.doubleValue)
}

// MARK: - The screen's state

/// Where boards come from: the paired Hermes, or invented demo data.
@MainActor
public protocol BoardBackend: AnyObject {
    func boards() async throws -> KanbanBoardList
    func board(_ slug: String) async throws -> KanbanBoardPage
}

@MainActor
public final class BoardModel: ObservableObject {
    @Published public private(set) var boards: [KanbanBoardSummary] = []
    @Published public private(set) var page: KanbanBoardPage?
    /// The board on screen (remembered between launches).
    @Published public private(set) var selected: String?
    @Published public private(set) var loading = false
    @Published public private(set) var loaded = false
    @Published public private(set) var error: String?
    @Published public var now = Date()

    public var backend: BoardBackend?
    private let defaults: UserDefaults
    public static let rememberKey = "speakeasy.board.last"
    private var refreshTask: Task<Void, Never>?
    private var listCountdown = 0

    public init(backend: BoardBackend? = nil, defaults: UserDefaults = .standard) {
        self.backend = backend; self.defaults = defaults
        selected = defaults.string(forKey: Self.rememberKey)
    }

    public var current: KanbanBoardSummary? { boards.first { $0.slug == selected } }
    /// Blocked cards across every board (the sidebar row's badge).
    public var needsYouCount: Int { boards.reduce(0) { $0 + $1.blocked } }

    /// The boards, then the picked (or suggested) one.
    public func reload() async {
        guard let backend else { return }
        loading = !loaded
        defer { loading = false }
        do {
            let list = try await backend.boards()
            boards = list.boards
            if selected == nil || !list.boards.contains(where: { $0.slug == selected }) {
                selected = list.suggested ?? list.boards.first?.slug
            }
            error = nil
        } catch {
            if !loaded { self.error = SchedulesModel.message(error) }
            loaded = true
            return
        }
        await loadPage()
        loaded = true
    }

    /// Only the open board (the refresh loop between list reads).
    public func loadPage() async {
        guard let backend, let slug = selected else { page = nil; return }
        do {
            let p = try await backend.board(slug)
            guard slug == selected else { return }
            if p != page { page = p }
            error = nil
        } catch {
            guard slug == selected else { return }
            if (error as? StatusCarrying)?.status == 404 {
                page = nil; selected = nil; defaults.removeObject(forKey: Self.rememberKey)
                await reload()
                return
            }
            if page?.slug != slug { self.error = SchedulesModel.message(error) }
        }
    }

    public func select(_ slug: String) {
        guard slug != selected else { return }
        selected = slug
        defaults.set(slug, forKey: Self.rememberKey)
        if page?.slug != slug { page = nil }
        Task { await loadPage() }
    }

    /// Fresh while on screen: the open board every 5 s, the board list every 30 s.
    public func startRefreshing() {
        guard refreshTask == nil else { return }
        refreshTask = Task { [weak self] in
            await self?.reload()
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                guard let self, !Task.isCancelled else { return }
                self.now = Date()
                self.listCountdown += 1
                if self.listCountdown >= 6 { self.listCountdown = 0; await self.reload() } else { await self.loadPage() }
            }
        }
    }

    public func stopRefreshing() { refreshTask?.cancel(); refreshTask = nil }

    /// Screenshots and tests: state without a server.
    public func preview(boards: [KanbanBoardSummary], page: KanbanBoardPage?, now: Date = Date()) {
        self.boards = boards; self.page = page; selected = page?.slug ?? boards.first?.slug
        self.now = now; loaded = true; loading = false; error = nil
    }
}
