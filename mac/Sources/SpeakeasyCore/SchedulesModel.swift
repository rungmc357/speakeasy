import Foundation
import Combine

// The Scheduled screen's state, shared by the Mac History window and the iPhone/iPad app: the
// list, one schedule's page, its actions, and New schedule (a sentence → a preview card → Save).

/// An error that carries an HTTP status (the client's `HTTPError`; `ScheduleProblem` in demos).
public protocol StatusCarrying { var status: Int { get } }

/// A refusal with a status and a plain sentence (demo and test backends).
public struct ScheduleProblem: LocalizedError, StatusCarrying, Equatable {
    public let status: Int
    public let message: String
    public init(status: Int, message: String) { self.status = status; self.message = message }
    public var errorDescription: String? { message }
}

/// Where schedules come from: the paired Hermes, or invented demo data.
@MainActor
public protocol ScheduleBackend: AnyObject {
    func list() async throws -> ScheduleList
    func detail(_ id: String) async throws -> ScheduleDetail
    func preview(text: String) async throws -> SchedulePreview
    func preview(schedule: String, prompt: String) async throws -> SchedulePreview
    func create(_ preview: SchedulePreview, name: String?, deliver: String?) async throws -> ScheduleDetail
    func pause(_ id: String) async throws -> ScheduleDetail
    func resume(_ id: String) async throws -> ScheduleDetail
    func runNow(_ id: String) async throws -> ScheduleDetail
    func update(_ id: String, schedule: String?, prompt: String?, name: String?) async throws -> ScheduleDetail
    func delete(_ id: String) async throws
}

/// New schedule: what's typed, then the preview to confirm.
public struct ScheduleDraft: Equatable, Sendable {
    public enum Phase: Equatable, Sendable { case writing, reading, preview(SchedulePreview), saving, failed(String) }
    public var text = ""
    public var phase: Phase = .writing
    /// The preview card's editable name and destination.
    public var name = ""
    public var deliver = "local"

    public init(text: String = "") { self.text = text }

    public var preview: SchedulePreview? {
        switch phase {
        case .preview(let p): return p
        default: return lastPreview
        }
    }
    /// Kept while saving, so the card stays on screen.
    public var lastPreview: SchedulePreview?
    public var error: String? { if case .failed(let e) = phase { return e }; return nil }
    public var busy: Bool { phase == .reading || phase == .saving }
    public var canPreview: Bool { !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !busy }
    public var showsCard: Bool { if case .preview = phase { return true }; return phase == .saving }

    public mutating func reading() { phase = .reading }
    public mutating func show(_ p: SchedulePreview) {
        lastPreview = p; name = p.name; deliver = p.deliver; phase = .preview(p)
    }
    public mutating func fail(_ message: String) { phase = .failed(message) }
    /// Back to the words (Edit on the card, or the text changed).
    public mutating func edit() { phase = .writing; lastPreview = nil }
}

@MainActor
public final class SchedulesModel: ObservableObject {
    @Published public private(set) var jobs: [ScheduledJob] = []
    @Published public private(set) var timezone: String?
    @Published public private(set) var loading = false
    @Published public private(set) var loaded = false
    @Published public private(set) var error: String?
    @Published public private(set) var details: [String: ScheduleDetail] = [:]
    @Published public private(set) var detailErrors: [String: String] = [:]
    /// Actions in flight, per schedule id.
    @Published public private(set) var busy: Set<String> = []
    /// The last action's line ("Running now. Results in a minute.", or why it didn't work), per id.
    @Published public private(set) var notices: [String: Notice] = [:]
    @Published public var draft = ScheduleDraft()
    /// The schedule a page has open (nil: the list).
    @Published public var selected: String?

    public struct Notice: Equatable, Sendable {
        public var text: String
        public var isError: Bool
        public init(_ text: String, isError: Bool = false) { self.text = text; self.isError = isError }
    }

    public var backend: ScheduleBackend?
    private var refreshTask: Task<Void, Never>?

    public init(backend: ScheduleBackend? = nil) { self.backend = backend }

    public var sections: [(ScheduleList.Section, [ScheduledJob])] { ScheduleList.sections(jobs) }
    public func job(_ id: String) -> ScheduledJob? { details[id]?.job ?? jobs.first { $0.id == id } }
    /// The soonest active schedule (the side panel's "next" line).
    public var nextUp: ScheduledJob? {
        jobs.filter { !$0.isDimmed && $0.nextRun != nil }.min { ($0.nextRun ?? .distantFuture) < ($1.nextRun ?? .distantFuture) }
    }
    public var activeCount: Int { jobs.filter { !$0.isDimmed }.count }

    // MARK: Reading

    public func reload() async {
        guard let backend else { return }
        loading = !loaded
        defer { loading = false }
        do {
            let list = try await backend.list()
            jobs = list.schedules
            timezone = list.timezone
            loaded = true
            error = nil
        } catch {
            if !loaded || jobs.isEmpty { self.error = Self.message(error) }
        }
    }

    public func load(_ id: String) async {
        guard let backend else { return }
        do {
            let d = try await backend.detail(id)
            apply(d)
            detailErrors[id] = nil
        } catch {
            if details[id] == nil { detailErrors[id] = Self.message(error) }
            if (error as? StatusCarrying)?.status == 404 { forget(id) }
        }
    }

    /// Keep the list (and an open page) fresh while on screen: quicker while something runs.
    public func startRefreshing() {
        guard refreshTask == nil else { return }
        refreshTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.reload()
                if let id = self.selected { await self.load(id) }
                let running = self.jobs.contains { $0.isRunning } || self.selected.flatMap { self.job($0) }?.isRunning == true
                try? await Task.sleep(nanoseconds: (running ? 4 : 20) * 1_000_000_000)
            }
        }
    }

    public func stopRefreshing() { refreshTask?.cancel(); refreshTask = nil }

    // MARK: Actions

    public func pause(_ id: String) async { await act(id, done: "Paused. It won't run until you resume it.") { try await $0.pause(id) } }
    public func resume(_ id: String) async { await act(id, done: "Resumed.") { try await $0.resume(id) } }

    public func runNow(_ id: String) async {
        await act(id, done: nil) { try await $0.runNow(id) }
        guard let d = details[id], notices[id]?.isError != true else { return }
        notices[id] = Notice(d.resumed ? "Running now, and resumed. Results in a minute." : "Running now. Results in a minute.")
        // The scheduler picks it up on its next tick: watch for the run.
        Task { [weak self] in
            for _ in 0..<6 {
                try? await Task.sleep(nanoseconds: 10_000_000_000)
                guard let self, !Task.isCancelled else { return }
                await self.load(id)
                await self.reload()
            }
        }
    }

    /// Saves a changed schedule (words or Hermes' forms) and/or prompt. False when it didn't save.
    @discardableResult
    public func save(_ id: String, schedule: String?, prompt: String?, name: String? = nil) async -> Bool {
        let current = details[id]
        let when = schedule.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        let what = prompt.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        let changedWhen = (when?.isEmpty == false && when != current?.scheduleText) ? when : nil
        let changedWhat = (what?.isEmpty == false && what != current?.prompt) ? what : nil
        let changedName = (name?.isEmpty == false && name != current?.job.name) ? name : nil
        guard changedWhen != nil || changedWhat != nil || changedName != nil else { return true }
        await act(id, done: "Saved.") { try await $0.update(id, schedule: changedWhen, prompt: changedWhat, name: changedName) }
        return notices[id]?.isError != true
    }

    public func delete(_ id: String) async -> Bool {
        guard let backend else { return false }
        busy.insert(id)
        defer { busy.remove(id) }
        do {
            try await backend.delete(id)
            forget(id)
            return true
        } catch {
            notices[id] = Notice(Self.message(error), isError: true)
            return false
        }
    }

    private func act(_ id: String, done: String?, _ body: (ScheduleBackend) async throws -> ScheduleDetail) async {
        guard let backend, !busy.contains(id) else { return }
        busy.insert(id)
        notices[id] = nil
        defer { busy.remove(id) }
        do {
            let d = try await body(backend)
            apply(d)
            if let done { notices[id] = Notice(done) }
        } catch {
            notices[id] = Notice(Self.message(error), isError: true)
        }
    }

    private func apply(_ d: ScheduleDetail) {
        details[d.job.id] = d
        if let i = jobs.firstIndex(where: { $0.id == d.job.id }) { jobs[i] = d.job } else { jobs.append(d.job) }
    }

    private func forget(_ id: String) {
        jobs.removeAll { $0.id == id }
        details[id] = nil
        notices[id] = nil
        if selected == id { selected = nil }
    }

    public func clearNotice(_ id: String) { notices[id] = nil }

    // MARK: New schedule

    public func startDraft(_ text: String = "") { draft = ScheduleDraft(text: text) }

    /// Reads the sentence into a preview card. Nothing is saved.
    public func previewDraft() async {
        guard let backend, draft.canPreview else { return }
        let text = draft.text
        draft.reading()
        do {
            let p = try await backend.preview(text: text)
            guard draft.text == text else { return }
            draft.show(p)
        } catch {
            draft.fail(Self.message(error))
        }
    }

    /// Saves the confirmed card; returns the new schedule's id.
    @discardableResult
    public func confirmDraft() async -> String? {
        guard let backend, let p = draft.preview, draft.showsCard, draft.phase != .saving else { return nil }
        draft.phase = .saving
        do {
            let d = try await backend.create(p, name: draft.name, deliver: draft.deliver)
            apply(d)
            draft = ScheduleDraft()
            notices[d.job.id] = Notice("Scheduled. Next: \(d.job.nextRun.map { ScheduleFormat.when($0) } ?? d.job.scheduleLabel).")
            return d.job.id
        } catch {
            draft.lastPreview = p
            draft.fail(Self.message(error))
            return nil
        }
    }

    public static func message(_ error: Error) -> String {
        if (error as? URLError) != nil { return "Couldn't reach your Hermes. Check the connection and try again." }
        if let e = error as? LocalizedError, let text = e.errorDescription { return text }
        return error.localizedDescription
    }

    /// Screenshots and tests: state without a server.
    public func preview(jobs: [ScheduledJob], details: [String: ScheduleDetail] = [:]) {
        self.jobs = jobs; self.details = details; loaded = true; loading = false; error = nil
    }
    public func preview(notice: Notice?, for id: String) { notices[id] = notice }
}
