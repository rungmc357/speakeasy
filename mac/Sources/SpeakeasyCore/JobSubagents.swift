import Foundation

// Helpers a job started (servers with `capabilities.job_subagents`): the subagents its Hermes run
// handed parts of the work to. Each one only as the server reported it; a server without the field
// sends none and the job page has no helpers section.

/// One helper: its name, how it's going, its latest step, and its answer once it ends.
public struct JobSubagent: Equatable, Sendable, Identifiable {
    public enum Status: String, Equatable, Sendable { case running, done, failed, stopped }

    public var id: String
    public var title: String
    public var status: Status
    public var goal: String?
    public var startedAt: Date?
    public var endedAt: Date?
    /// Its latest step in plain words (running only).
    public var now: String?
    /// Its answer, once it ended (already redacted on the server).
    public var summary: String?
    /// Why it stopped without one ("Stopped with the task").
    public var note: String?
    /// 2+ when another helper started it.
    public var depth: Int?
    public var parentID: String?
    /// How many steps the server kept for it.
    public var stepsTotal: Int?
    public var toolCount: Int?
    /// Its own steps (from `GET /voice/work/{run_id}` only; the stream leaves them out).
    public var activity: [JobActivityItem]
    public var touched: [JobTouched]

    public init(id: String, title: String, status: Status = .running, goal: String? = nil, startedAt: Date? = nil,
                endedAt: Date? = nil, now: String? = nil, summary: String? = nil, note: String? = nil,
                depth: Int? = nil, parentID: String? = nil, stepsTotal: Int? = nil, toolCount: Int? = nil,
                activity: [JobActivityItem] = [], touched: [JobTouched] = []) {
        self.id = id; self.title = title; self.status = status; self.goal = goal; self.startedAt = startedAt
        self.endedAt = endedAt; self.now = now; self.summary = summary; self.note = note; self.depth = depth
        self.parentID = parentID; self.stepsTotal = stepsTotal; self.toolCount = toolCount
        self.activity = activity; self.touched = touched
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]), id.count <= 64 else { return nil }
        let status = (o["status"] as? String).flatMap(Status.init(rawValue:)) ?? .running
        let depth = (o["depth"] as? NSNumber)?.intValue
        self.init(id: id, title: nonEmpty(o["title"]).map { String($0.prefix(80)) } ?? "Helper", status: status,
                  goal: nonEmpty(o["goal"]).map { String($0.prefix(400)) },
                  startedAt: decodeDate(o["started_at"]), endedAt: decodeDate(o["ended_at"]),
                  now: status == .running ? nonEmpty(o["now"]).map { String($0.prefix(120)) } : nil,
                  summary: nonEmpty(o["summary"]).map { String($0.prefix(600)) },
                  note: nonEmpty(o["note"]).map { String($0.prefix(120)) },
                  depth: depth.flatMap { $0 > 1 ? $0 : nil }, parentID: nonEmpty(o["parent_id"]),
                  stepsTotal: (o["steps_total"] as? NSNumber)?.intValue,
                  toolCount: (o["tool_count"] as? NSNumber)?.intValue,
                  activity: JobActivityItem.list(json: o["activity"]), touched: JobTouched.list(json: o["touched"]))
    }

    public static let maxItems = 12

    public static func list(json: Any?) -> [JobSubagent] {
        var seen = Set<String>()
        return (json as? [Any] ?? []).compactMap(JobSubagent.init(json:)).filter { seen.insert($0.id).inserted }
            .prefix(maxItems).map { $0 }
    }

    public var isRunning: Bool { status == .running }

    /// How long it ran (or has been running, up to `now`).
    public func elapsed(now date: Date) -> TimeInterval? {
        guard let startedAt else { return nil }
        guard let end = endedAt ?? (isRunning ? date : nil) else { return nil }
        return max(0, end.timeIntervalSince(startedAt))
    }

    /// The row's second line: what it's doing now, else its answer's first line, else why it stopped.
    public var line: String? {
        switch status {
        case .running: return now ?? "Getting started"
        case .done: return summary.map(Self.firstLine) ?? "Finished"
        case .failed: return summary.map(Self.firstLine) ?? note ?? "Didn't finish"
        case .stopped: return note ?? summary.map(Self.firstLine) ?? "Stopped"
        }
    }

    /// One word for its status.
    public var statusWord: String {
        switch status {
        case .running: return "Working"
        case .done: return "Done"
        case .failed: return "Didn't finish"
        case .stopped: return "Stopped"
        }
    }

    /// Its steps with what a fuller copy (the task's detail) knows filled in.
    public func merged(with fuller: JobSubagent?) -> JobSubagent {
        guard let fuller, fuller.id == id else { return self }
        var out = self
        if activity.isEmpty { out.activity = fuller.activity }
        if touched.isEmpty { out.touched = fuller.touched }
        out.goal = goal ?? fuller.goal
        out.summary = summary ?? fuller.summary
        return out
    }

    static func firstLine(_ text: String) -> String {
        let line = text.split(whereSeparator: \.isNewline).first.map(String.init) ?? text
        return line.trimmingCharacters(in: .whitespaces)
    }
}

extension WorkInfo {
    /// The helpers still working, and how many there are in all ("2 of 3 working").
    public var helpersSummary: String? {
        guard !subagents.isEmpty else { return nil }
        let running = subagents.filter(\.isRunning).count
        if running == 0 { return subagents.count == 1 ? "1 helper" : "\(subagents.count) helpers" }
        return "\(running) of \(subagents.count) working"
    }
}
