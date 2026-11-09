import Foundation

// Hermes' projects in Speakeasy (servers with `projects`, SPK-254; docs/API.md "Projects").
// A project is Hermes' own (`hermes project list` shows the same ones); chats and jobs that have no
// working folder are filed into one by an explicit action, stored by the plugin.

/// A project named on a conversation row or a Home job card: just enough to show and open it.
public struct ProjectRef: Equatable, Sendable, Hashable, Identifiable {
    public var id: String
    public var name: String
    public init(id: String, name: String) { self.id = id; self.name = name }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]), let name = nonEmpty(o["name"]) else { return nil }
        self.init(id: id, name: String(name.prefix(60)))
    }
}

/// The colours a project may carry (the plugin's `COLORS`): names, never hex, so every app draws them
/// in its own palette and in light and dark.
public enum ProjectColor: String, CaseIterable, Sendable, Identifiable {
    case blue, teal, green, yellow, orange, red, pink, purple, gray
    public var id: String { rawValue }
    public var label: String { rawValue.capitalized }
}

/// SF Symbols offered in the New project sheet (any valid symbol name from the server is drawn too).
public enum ProjectSymbols {
    public static let choices = ["folder", "hammer", "leaf", "house", "briefcase", "book.closed", "airplane",
                                 "cart", "heart", "star", "wrench.and.screwdriver", "paintbrush",
                                 "chevron.left.forwardslash.chevron.right", "graduationcap", "camera", "music.note"]
    public static let fallback = "folder"
    /// A server icon is shown only when it looks like a symbol name.
    public static func valid(_ name: String?) -> Bool {
        guard let name, !name.isEmpty, name.count <= 60 else { return false }
        return name.range(of: #"^[a-z0-9]+(\.[a-z0-9]+){0,4}$"#, options: .regularExpression) != nil
    }
}

/// One project of `GET /voice/projects`.
public struct HermesProject: Equatable, Sendable, Identifiable, Hashable {
    public var id: String
    public var slug: String
    public var name: String
    public var icon: String?
    public var color: ProjectColor?
    /// The Hermes Kanban board bound to it (`hermes project bind-board`), if any.
    public var board: String?
    /// Coding sessions in its folders join it on their own.
    public var hasFolders: Bool
    public var chats: Int
    public var jobs: Int
    /// Standing notes added to every job filed into it.
    public var notes: String
    /// Jobs filed into it that are running now (the panel's quiet "2 running").
    public var running: Int
    /// Its newest activity: a filed job's last update or a chat filed into it.
    public var activeAt: Date?

    public init(id: String, slug: String = "", name: String, icon: String? = nil, color: ProjectColor? = nil,
                board: String? = nil, hasFolders: Bool = false, chats: Int = 0, jobs: Int = 0, notes: String = "",
                running: Int = 0, activeAt: Date? = nil) {
        self.id = id; self.slug = slug.isEmpty ? id : slug; self.name = name
        self.icon = ProjectSymbols.valid(icon) ? icon : nil; self.color = color
        self.board = board; self.hasFolders = hasFolders; self.chats = chats; self.jobs = jobs; self.notes = notes
        self.running = max(0, running); self.activeAt = activeAt
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]), let name = nonEmpty(o["name"]) else { return nil }
        self.init(id: id, slug: nonEmpty(o["slug"]) ?? id, name: String(name.prefix(60)), icon: nonEmpty(o["icon"]),
                  color: (o["color"] as? String).flatMap(ProjectColor.init(rawValue:)), board: nonEmpty(o["board"]),
                  hasFolders: o["has_folders"] as? Bool ?? false, chats: (o["chats"] as? NSNumber)?.intValue ?? 0,
                  jobs: (o["jobs"] as? NSNumber)?.intValue ?? 0, notes: String((nonEmpty(o["notes"]) ?? "").prefix(1200)),
                  running: (o["running"] as? NSNumber)?.intValue ?? 0,
                  activeAt: (o["active_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) })
    }

    public var symbol: String { icon ?? ProjectSymbols.fallback }
    public var ref: ProjectRef { ProjectRef(id: id, name: name) }

    /// "3 chats · 1 job", "No chats yet".
    public var countLine: String {
        var parts: [String] = []
        if chats > 0 { parts.append("\(chats) chat\(chats == 1 ? "" : "s")") }
        if jobs > 0 { parts.append("\(jobs) job\(jobs == 1 ? "" : "s")") }
        return parts.isEmpty ? "No chats yet" : parts.joined(separator: " \u{00B7} ")
    }

    /// The side panel's quiet second line: "2 running" while work runs, else how long since anything
    /// happened ("3h ago", "Yesterday", "Mar 4"), else the counts.
    public func activityLine(now: Date = Date(), calendar: Calendar = .current) -> String {
        if running > 0 { return "\(running) running" }
        guard let at = activeAt else { return countLine }
        let s = max(0, now.timeIntervalSince(at))
        if s < 60 { return "Just now" }
        if s < 3600 { return "\(Int(s / 60))m ago" }
        if calendar.isDate(at, inSameDayAs: now) { return "\(Int(s / 3600))h ago" }
        if let y = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(at, inSameDayAs: y) { return "Yesterday" }
        let f = DateFormatter()
        f.calendar = calendar; f.timeZone = calendar.timeZone; f.locale = Locale(identifier: "en_US")
        f.setLocalizedDateFormatFromTemplate(calendar.isDate(at, equalTo: now, toGranularity: .year) ? "MMMd" : "MMMdyyyy")
        return f.string(from: at)
    }
}

/// Where Home works: the composer's project chip, the call dock's chip, the "Working in" line and
/// the side panel's selection all read this one value. nil = Home, not in a project.
public enum HomeSpace {
    public static let homeSymbol = "house"
    public static let homeName = "Home"
    /// The chip's sheet's first row.
    public static let homeRow = "Home \u{2014} not in a project"
}

/// "Make a project for X?": three or more recent chats share a subject and no project has it.
public struct ProjectOffer: Equatable, Sendable {
    public var subject: String
    public var chats: Int
    public init(subject: String, chats: Int) { self.subject = subject; self.chats = chats }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let subject = nonEmpty(o["subject"]) else { return nil }
        self.init(subject: String(subject.prefix(60)), chats: (o["chats"] as? NSNumber)?.intValue ?? 0)
    }
    public var prompt: String { "Make a project for \(subject)?" }
}

public struct ProjectList: Equatable, Sendable {
    public var projects: [HermesProject]
    public var offer: ProjectOffer?
    /// The project Home works in (stored by the plugin so every device agrees); nil = Home.
    public var selected: String?
    public init(projects: [HermesProject] = [], offer: ProjectOffer? = nil, selected: String? = nil) {
        self.projects = projects; self.offer = offer; self.selected = selected
    }
    public init(json: [String: Any]) {
        projects = (json["projects"] as? [Any] ?? []).compactMap(HermesProject.init(json:))
        offer = ProjectOffer(json: json["offer"])
        let sel = nonEmpty(json["selected"])
        selected = projects.contains { $0.id == sel } ? sel : nil
    }
    /// The side panel shows its Projects section when there is a project or an offer to make one:
    /// with neither, the panel is exactly what it was before projects existed.
    public var showsSection: Bool { !projects.isEmpty || offer != nil }

    /// The side panel's one Projects row, its quiet second line (SPK-263): "2 running" while work
    /// runs in any project, else how many there are, else the offer ("1 suggestion").
    public var panelLine: String {
        let running = projects.reduce(0) { $0 + $1.running }
        if running > 0 { return "\(running) running" }
        if !projects.isEmpty { return "\(projects.count) project\(projects.count == 1 ? "" : "s")" }
        return offer != nil ? "1 suggestion" : "None yet"
    }
}

/// A job on a project page.
public struct ProjectJob: Equatable, Sendable, Identifiable, Hashable {
    public var runID: String
    public var title: String
    public var status: String
    public var sessionID: String?
    public var line: String
    public var startedAt: Date?
    public var finishedAt: Date?
    public var id: String { runID }
    public init(runID: String, title: String, status: String, sessionID: String? = nil, line: String = "",
                startedAt: Date? = nil, finishedAt: Date? = nil) {
        self.runID = runID; self.title = title; self.status = status; self.sessionID = sessionID; self.line = line
        self.startedAt = startedAt; self.finishedAt = finishedAt
    }
    public init?(json: Any?) {
        guard let o = json as? [String: Any], let run = nonEmpty(o["run_id"]) ?? nonEmpty(o["task_id"]) else { return nil }
        func date(_ k: String) -> Date? { (o[k] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) } }
        self.init(runID: run, title: String((nonEmpty(o["title"]) ?? "Untitled job").prefix(120)),
                  status: nonEmpty(o["status"]) ?? "completed", sessionID: nonEmpty(o["session_id"]),
                  line: String((nonEmpty(o["now_line"]) ?? nonEmpty(o["short_status"]) ?? nonEmpty(o["summary"]) ?? "").prefix(200)),
                  startedAt: date("started_at"), finishedAt: date("finished_at"))
    }
    public var isRunning: Bool { !["completed", "failed", "cancelled", "interrupted", "rejected"].contains(status) }
}

/// `GET /voice/projects/{id}`.
public struct ProjectPage: Equatable, Sendable {
    public var project: HermesProject
    public var chats: [HistorySession]
    public var jobs: [ProjectJob]
    public var board: KanbanBoardSummary?
    public init(project: HermesProject, chats: [HistorySession] = [], jobs: [ProjectJob] = [], board: KanbanBoardSummary? = nil) {
        self.project = project; self.chats = chats; self.jobs = jobs; self.board = board
    }
    public init?(json: [String: Any]) {
        guard let project = HermesProject(json: json["project"]) else { return nil }
        self.init(project: project, chats: (json["chats"] as? [Any] ?? []).compactMap(HistorySession.init(json:)),
                  jobs: (json["jobs"] as? [Any] ?? []).compactMap(ProjectJob.init(json:)),
                  board: KanbanBoardSummary(json: json["board"]))
    }
    public var running: [ProjectJob] { jobs.filter(\.isRunning) }
    public var done: [ProjectJob] { jobs.filter { !$0.isRunning } }
    /// The page's segments: Board only when a Kanban board is bound to the project.
    public var tabs: [ProjectTab] { board == nil && project.board == nil ? [.chats, .work] : [.chats, .work, .board] }
    /// The newest chats first (the side panel's inline preview shows three).
    public func recentChats(_ limit: Int = 3) -> [HistorySession] {
        Array(chats.sorted { ($0.lastActivity ?? .distantPast) > ($1.lastActivity ?? .distantPast) }.prefix(limit))
    }
}

/// A project page's segmented control.
public enum ProjectTab: String, CaseIterable, Sendable, Identifiable {
    case chats, work, board
    public var id: String { rawValue }
    public var label: String {
        switch self {
        case .chats: return "Chats"
        case .work: return "Work"
        case .board: return "Board"
        }
    }
}

/// The Projects state every surface shares (the panel section, project pages, Move to project, the
/// job card's chip). Writes are applied at once and put back when the server refuses.
@MainActor
public final class ProjectsModel: ObservableObject {
    @Published public private(set) var list = ProjectList()
    @Published public private(set) var loaded = false
    @Published public private(set) var pages: [String: ProjectPage] = [:]
    @Published public var error: String?
    /// The conversation whose Move to project sheet is open (the panel presents it).
    @Published public var moving: HistorySession?
    /// The New project sheet is open (optionally from an offer).
    @Published public var creating: NewProject?
    /// Jobs filed from Home's chip, by run id (shown at once, before Home refreshes).
    @Published public private(set) var filedRuns: [String: ProjectRef] = [:]
    /// Suggestions declined from Home's chip, by run id.
    @Published public private(set) var declinedRuns: Set<String> = []
    /// Servers with `projects`; off: nothing loads and every surface hides.
    @Published public var supported = false {
        didSet { if supported && !oldValue { Task { await reload() } }; if !supported { list = ProjectList(); pages = [:] } }
    }
    public var backend: ProjectsBackend?
    private var selecting = 0

    public init(backend: ProjectsBackend? = nil) { self.backend = backend }

    public var showsSection: Bool { supported && list.showsSection }
    public func project(_ id: String) -> HermesProject? { list.projects.first { $0.id == id } }

    /// The project Home works in, nil for Home. The chip, the call dock, "Working in" and the panel's
    /// selection all read this.
    public var selected: HermesProject? {
        guard supported, let id = list.selected else { return nil }
        return project(id)
    }
    /// The chip's symbol and name: "house" + "Home" until a project is chosen (never "No project").
    public var chipSymbol: String { selected?.symbol ?? HomeSpace.homeSymbol }
    public var chipName: String { selected?.name ?? HomeSpace.homeName }
    /// The chip appears only on servers with projects (an older Hermes: Home looks as before).
    public var showsChip: Bool { supported && loaded }
    /// The chip's sheet is open.
    @Published public var choosing = false
    /// Which projects are expanded inline in the side panel.
    @Published public var expanded: Set<String> = []

    /// Choose where Home works (nil = Home). Applied at once; put back when the server refuses.
    /// Selecting a project in the side panel calls this too: one notion of "where I'm working".
    public func select(_ projectID: String?) async {
        let before = list.selected
        guard projectID != before else { return }
        list.selected = projectID
        selecting += 1
        defer { selecting -= 1 }
        do {
            try await backend?.select(projectID)
            error = nil
        } catch {
            list.selected = before
            self.error = error.localizedDescription
        }
    }

    public func toggleExpanded(_ id: String) {
        if expanded.contains(id) { expanded.remove(id) } else {
            expanded.insert(id)
            if pages[id] == nil { Task { await loadPage(id) } }
        }
    }

    public func reload() async {
        guard supported, let backend else { return }
        do {
            var fresh = try await backend.projects()
            if fresh.selected != list.selected, selecting > 0 { fresh.selected = list.selected }
            if fresh != list { list = fresh }
            loaded = true
        } catch {
            loaded = true
        }
    }

    public func loadPage(_ id: String) async {
        guard let backend else { return }
        do {
            let page = try await backend.page(id)
            pages[id] = page
            if let i = list.projects.firstIndex(where: { $0.id == id }), list.projects[i] != page.project {
                list.projects[i] = page.project
            }
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }

    @discardableResult
    public func create(_ project: NewProject) async -> HermesProject? {
        guard let backend else { return nil }
        do {
            let made = try await backend.create(project)
            if !list.projects.contains(where: { $0.id == made.id }) { list.projects.append(made) }
            if project.fromOffer != nil { list.offer = nil }
            error = nil
            if project.select { await select(made.id) }
            Task { await reload() }
            return made
        } catch {
            self.error = error.localizedDescription
            return nil
        }
    }

    /// Move a conversation into a project (nil: out of its project).
    public func move(_ session: HistorySession, to projectID: String?) async -> Bool {
        guard let backend else { return false }
        do {
            try await backend.file(session: session, into: projectID)
            for (id, var page) in pages {
                page.chats.removeAll { $0.id == session.id }
                if id == projectID {
                    var row = session
                    row.project = projectID.flatMap { project($0)?.ref }
                    page.chats.insert(row, at: 0)
                }
                pages[id] = page
            }
            error = nil
            await reload()
            return true
        } catch {
            self.error = error.localizedDescription
            return false
        }
    }

    /// Home's chip: file this job into the suggested project (one tap; nothing before it).
    public func file(runID: String, into project: ProjectRef) async {
        filedRuns[runID] = project
        do { try await backend?.file(runID: runID, into: project.id) } catch {
            filedRuns[runID] = nil
            self.error = error.localizedDescription
        }
        await reload()
    }

    /// Home's chip ✕: not this project, and don't suggest it again for this job.
    public func decline(runID: String) async {
        declinedRuns.insert(runID)
        do { try await backend?.file(runID: runID, into: "") } catch { declinedRuns.remove(runID) }
    }

    public func setNotes(_ notes: String, for projectID: String) async {
        guard let backend else { return }
        let before = pages[projectID]?.project.notes
        pages[projectID]?.project.notes = notes
        do { try await backend.setNotes(notes, for: projectID) } catch {
            pages[projectID]?.project.notes = before ?? ""
            self.error = error.localizedDescription
        }
        await reload()
    }

    /// "Not now" on the offer: never offered again for that subject.
    public func dismissOffer() async {
        guard let offer = list.offer else { return }
        list.offer = nil
        try? await backend?.dismissOffer(offer.subject)
    }
}

/// What the New project sheet sends.
public struct NewProject: Equatable, Sendable, Identifiable {
    public var id: String { fromOffer ?? "new" }
    public var name: String
    public var icon: String?
    public var color: ProjectColor?
    public var fromOffer: String?
    public var sessions: [String]
    /// Made from the Home chip's sheet: Home starts working in it right away (not sent to the server).
    public var select: Bool
    public init(name: String, icon: String? = nil, color: ProjectColor? = nil, fromOffer: String? = nil, sessions: [String] = [],
                select: Bool = false) {
        self.name = name; self.icon = icon; self.color = color; self.fromOffer = fromOffer; self.sessions = sessions
        self.select = select
    }
    /// The trimmed name, nil when empty or too long (60 characters, the plugin's limit).
    public var cleanName: String? {
        let n = name.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        return n.isEmpty || n.count > 60 ? nil : n
    }
    public func body() -> [String: Any]? {
        guard let name = cleanName else { return nil }
        var out: [String: Any] = ["name": name]
        if let icon, ProjectSymbols.valid(icon) { out["icon"] = icon }
        if let color { out["color"] = color.rawValue }
        if let fromOffer { out["from_offer"] = fromOffer }
        if !sessions.isEmpty { out["sessions"] = sessions }
        return out
    }
}

/// Talks to the server for every Projects surface (a protocol so demos and tests run without one).
@MainActor
public protocol ProjectsBackend: AnyObject {
    func projects() async throws -> ProjectList
    func page(_ id: String) async throws -> ProjectPage
    func create(_ project: NewProject) async throws -> HermesProject
    func file(session: HistorySession, into projectID: String?) async throws
    func file(runID: String, into projectID: String?) async throws
    func setNotes(_ notes: String, for projectID: String) async throws
    func dismissOffer(_ subject: String) async throws
    /// Where Home works (nil = Home): `PUT /voice/projects/selected`.
    func select(_ projectID: String?) async throws
}

/// Invented projects for demos, screenshots and UI tests (no real names or places).
@MainActor
public final class DemoProjectsBackend: ProjectsBackend {
    public var list: ProjectList
    public var pages: [String: ProjectPage]
    public private(set) var filed: [(String, String?)] = []

    public init(empty: Bool = false, offer: Bool = false, selected: String? = nil) {
        let now = Date()
        let shed = HermesProject(id: "p_shed", name: "Garden shed", icon: "hammer", color: .green, chats: 3, jobs: 2,
                                 notes: "Measurements in inches. Cedar, not pine.", running: 1,
                                 activeAt: now.addingTimeInterval(-240))
        let lantern = HermesProject(id: "p_lantern", name: "Lantern app", icon: "chevron.left.forwardslash.chevron.right",
                                    color: .blue, board: "lantern", hasFolders: true, chats: 1, jobs: 0,
                                    activeAt: now.addingTimeInterval(-5400))
        let trip = HermesProject(id: "p_trip", name: "Lakeside trip", icon: "airplane", color: .orange, chats: 1,
                                 activeAt: now.addingTimeInterval(-2 * 86400))
        let office = HermesProject(id: "p_office", name: "Home office", icon: "desktopcomputer", color: .purple)
        list = empty ? ProjectList() : ProjectList(projects: [shed, lantern, trip, office])
        let chats = [
            HistorySession(id: "demo_shed_1", title: "Cedar board prices", source: "speakeasy", sourceLabel: "Speakeasy",
                           lastActivity: now.addingTimeInterval(-1800), preview: "Twelve 8 ft boards come to about $180."),
            HistorySession(id: "demo_shed_2", title: "Roof pitch for a 10 × 12 shed", source: "telegram", sourceLabel: "Telegram",
                           lastActivity: now.addingTimeInterval(-86400), preview: "A 4/12 pitch sheds snow and keeps the walls low."),
            HistorySession(id: "demo_shed_3", title: "Shed permit rules", source: "speakeasy", sourceLabel: "Speakeasy",
                           lastActivity: now.addingTimeInterval(-3 * 86400), preview: "Under 120 sq ft needs no permit here."),
        ]
        let jobs = [
            ProjectJob(runID: "demo_run_shed_a", title: "Compare shed door hinges", status: "running",
                       line: "Checking which hinges carry 40 lb", startedAt: now.addingTimeInterval(-240)),
            ProjectJob(runID: "demo_run_shed_b", title: "Cut list for the floor frame", status: "completed",
                       line: "Nine 2 × 6 joists at 16 in spacing", startedAt: now.addingTimeInterval(-7200),
                       finishedAt: now.addingTimeInterval(-6900)),
        ]
        pages = ["p_shed": ProjectPage(project: shed, chats: chats, jobs: jobs),
                 "p_lantern": ProjectPage(project: lantern, chats: [
                    HistorySession(id: "demo_lantern_1", title: "Wick timer off by a minute", source: "cli", sourceLabel: "Terminal",
                                   lastActivity: now.addingTimeInterval(-5400), preview: "The timer counts seconds; the wick table uses minutes.")],
                                          jobs: [],
                                          board: KanbanBoardSummary(slug: "lantern", name: "Lantern app", ready: 2, running: 1)),
                 "p_trip": ProjectPage(project: trip, chats: [
                    HistorySession(id: "demo_trip_1", title: "Cabins near the lake in June", source: "speakeasy", sourceLabel: "Speakeasy",
                                   lastActivity: now.addingTimeInterval(-2 * 86400), preview: "Two cabins sleep four and allow dogs.")]),
                 "p_office": ProjectPage(project: office)]
        if offer { list.offer = ProjectOffer(subject: "Compost", chats: 3) }
        list.selected = selected
    }

    public func projects() async throws -> ProjectList { list }
    public func page(_ id: String) async throws -> ProjectPage {
        if let p = pages[id] { return p }
        if let p = list.projects.first(where: { $0.id == id }) { return ProjectPage(project: p) }
        throw ServerClientError.notFound
    }
    public func create(_ project: NewProject) async throws -> HermesProject {
        guard let name = project.cleanName else { throw ServerClientError.invalid("A project needs a name.") }
        let made = HermesProject(id: "p_new\(list.projects.count)", name: name, icon: project.icon, color: project.color,
                                 chats: project.sessions.count + (project.fromOffer == nil ? 0 : list.offer?.chats ?? 0))
        list.projects.append(made)
        if project.fromOffer != nil { list.offer = nil }
        pages[made.id] = ProjectPage(project: made)
        return made
    }
    /// Moves the row between the invented pages and fixes the counts, as the plugin would.
    public func file(session: HistorySession, into projectID: String?) async throws {
        filed.append((session.id, projectID))
        for (id, var page) in pages where page.chats.contains(where: { $0.id == session.id }) {
            page.chats.removeAll { $0.id == session.id }
            page.project.chats = max(0, page.project.chats - 1)
            pages[id] = page
            if let i = list.projects.firstIndex(where: { $0.id == id }) { list.projects[i].chats = page.project.chats }
        }
        guard let projectID, let i = list.projects.firstIndex(where: { $0.id == projectID }) else { return }
        var row = session
        row.project = list.projects[i].ref
        list.projects[i].chats += 1
        var page = pages[projectID] ?? ProjectPage(project: list.projects[i])
        page.chats.insert(row, at: 0)
        page.project = list.projects[i]
        pages[projectID] = page
    }
    public func file(runID: String, into projectID: String?) async throws { filed.append((runID, projectID)) }
    public func setNotes(_ notes: String, for projectID: String) async throws {
        if let i = list.projects.firstIndex(where: { $0.id == projectID }) { list.projects[i].notes = notes }
        pages[projectID]?.project.notes = notes
    }
    public func dismissOffer(_ subject: String) async throws { list.offer = nil }
    public func select(_ projectID: String?) async throws { list.selected = projectID }
}

public enum ServerClientError: Error, LocalizedError, Equatable {
    case notFound
    case invalid(String)
    public var errorDescription: String? {
        switch self {
        case .notFound: return "I couldn't find that project."
        case .invalid(let m): return m
        }
    }
}
