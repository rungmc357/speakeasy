import Foundation
import SpeakeasyCore

/// The plugin-update notice's live state, shared by the Mac panel and the iPhone board. The apps
/// feed it the latest published version, the server's status and the task list; it persists what was
/// dismissed or started (per version) and says what to show (`phase`).
@MainActor
public final class PluginUpdateNotice: ObservableObject {
    public static let recordKey = "pluginUpdateRecord"

    @Published public private(set) var record: PluginUpdateRecord
    @Published public private(set) var latest: String?
    @Published public private(set) var running: String?
    @Published public private(set) var note: String?
    @Published public private(set) var canStartJob = false
    @Published public private(set) var inCall = false
    @Published public private(set) var lastCallEnded: Date?
    @Published public private(set) var starting = false
    @Published public var error: String?
    /// Ticks so "Update didn't take" appears on time without new server data.
    @Published public private(set) var now = Date()

    private let defaults: UserDefaults
    private var noteFor: String?
    private var confirmTask: Task<Void, Never>?
    private var clock: Task<Void, Never>?

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        record = defaults.data(forKey: Self.recordKey).flatMap { try? JSONDecoder().decode(PluginUpdateRecord.self, from: $0) }
            ?? PluginUpdateRecord()
    }

    /// Update can start right here (an update is offered and the plugin runs it as a job).
    public var canUpdateNow: Bool { canStartJob && phase.offersUpdate }

    public var phase: PluginUpdatePhase {
        PluginUpdateFlow.phase(latest: latest, running: running, record: record, note: note, inCall: inCall,
                               lastCallEnded: lastCallEnded, now: now)
    }

    /// What to show right now: the "available" notice only off a call (it's a home-board thing).
    public var visiblePhase: PluginUpdatePhase {
        let p = phase
        if case .available = p, inCall { return .none }
        return p
    }

    // MARK: Inputs

    public func setLatest(_ version: String?) { latest = version }
    public func setNote(_ line: String?, for version: String) { noteFor = version; note = line }
    public func needsNote(for version: String) -> Bool { noteFor != version }

    public func setCall(active: Bool) {
        if inCall && !active { lastCallEnded = Date() }
        inCall = active
        tickIfWaiting()
    }

    /// Feed the server's status (its plugin version and capabilities) and its task list.
    public func observe(running version: String?, startJobs: Bool, tasks: [TaskItem]) {
        canStartJob = startJobs
        running = version
        let job = record.runID.flatMap { id in tasks.first { $0.info.runID == id || $0.id == id } }
        let settled = job.map { $0.isSettled } ?? false
        save(PluginUpdateFlow.observe(record, running: version, jobSettled: settled))
        if case .updated = phase { scheduleConfirmClear() }
        tickIfWaiting()
    }

    // MARK: Actions

    public func dismiss() {
        guard let version = phase.version else { return }
        save(PluginUpdateFlow.dismiss(record, version: version))
    }

    /// Update: start the update request as a normal job on the user's Hermes.
    public func update(start: (String) async throws -> String?) async {
        guard case .available(let version, _) = phase, !starting else { return }
        starting = true; error = nil
        defer { starting = false }
        do {
            let runID = try await start(PluginUpdateFlow.requestText(version: version))
            save(PluginUpdateFlow.started(record, version: version, runID: runID))
            tickIfWaiting()
        } catch {
            self.error = (error as? LocalizedError)?.errorDescription ?? "Your Hermes didn't start the update."
        }
    }

    // MARK: Private

    private func save(_ next: PluginUpdateRecord) {
        guard next != record else { return }
        record = next
        if let data = try? JSONEncoder().encode(next) { defaults.set(data, forKey: Self.recordKey) }
    }

    private func scheduleConfirmClear() {
        guard confirmTask == nil else { return }
        confirmTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(PluginUpdateFlow.confirmSeconds * 1_000_000_000))
            guard let self, !Task.isCancelled else { return }
            self.save(PluginUpdateFlow.acknowledge(self.record))
            self.confirmTask = nil
        }
    }

    /// While a finished job waits for its version, tick every few seconds so the 30 s settle shows.
    private func tickIfWaiting() {
        now = Date()
        guard record.jobEndedAt != nil, clock == nil else { return }
        clock = Task { [weak self] in
            while let self, !Task.isCancelled, self.record.jobEndedAt != nil {
                if case .updating = self.phase {} else { break }
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                self.now = Date()
            }
            self?.clock = nil
        }
    }

    /// Screenshot / test states (film frames are built in release too).
    public func preview(_ phase: PluginUpdatePhase, latest: String = "0.9.1", running: String = "0.9.0") {
        self.latest = latest; self.running = running; canStartJob = true
        switch phase {
        case .none: record = PluginUpdateRecord(dismissed: latest)
        case .available(_, let line): record = PluginUpdateRecord(); note = line; noteFor = latest
        case .updating(let v, let id): record = PluginUpdateRecord(target: v, runID: id, startedAt: Date())
        case .updated(let v): record = PluginUpdateRecord(target: v); self.running = v
        case .didntTake(let v, let id):
            record = PluginUpdateRecord(target: v, runID: id, startedAt: Date(), jobEndedAt: Date().addingTimeInterval(-60))
        }
        now = Date()
    }
}

/// What the plugin-update notice needs from the panel state, compared cheaply.
public struct PluginBoardSnapshot: Equatable, Sendable {
    public var tasks: [TaskItem]
    public var inCall: Bool
    public init(tasks: [TaskItem], inCall: Bool) { self.tasks = tasks; self.inCall = inCall }
}
