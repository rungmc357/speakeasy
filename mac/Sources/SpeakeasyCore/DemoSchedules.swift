import Foundation

/// Invented schedules for screenshots and demo builds (no real jobs, names or content). Actions
/// work in memory; New schedule reads a few sentence shapes the way the server does.
@MainActor
public final class DemoScheduleBackend: ScheduleBackend {
    public private(set) var jobs: [ScheduledJob]
    public private(set) var detailsByID: [String: ScheduleDetail]
    private let now: Date
    private let calendar: Calendar

    public init(now: Date = Date(), calendar: Calendar = .current) {
        self.now = now; self.calendar = calendar
        (jobs, detailsByID) = Self.make(now: now, calendar: calendar)
    }

    public func list() async throws -> ScheduleList { ScheduleList(schedules: jobs, timezone: calendar.timeZone.identifier) }

    public func detail(_ id: String) async throws -> ScheduleDetail {
        guard let d = detailsByID[id] else { throw ScheduleProblem(status: 404, message: "I couldn't find that schedule.") }
        return d
    }

    public func preview(text: String) async throws -> SchedulePreview {
        try? await Task.sleep(nanoseconds: 250_000_000)
        let lower = text.lowercased()
        let hour = Int(lower.range(of: #"\b(\d{1,2})\b"#, options: .regularExpression).map { String(lower[$0]) } ?? "") ?? 9
        let h = lower.contains("pm") && hour < 12 ? hour + 12 : hour
        let weekdays = lower.contains("weekday")
        // One named day ("every monday at 9"): weekly on it. Calendar weekday: Sunday = 1.
        let names = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
        let one = weekdays ? nil : names.firstIndex { lower.contains($0) }
        let label = one.map { "\(names[$0].prefix(1).uppercased() + names[$0].dropFirst())s at \(Self.clock(h, 0))" }
            ?? "\(weekdays ? "Weekdays" : "Every day") at \(Self.clock(h, 0))"
        var what = text
        if let r = what.range(of: #"^.*?\b\d{1,2}(:\d{2})?\s*(am|pm)?\b[,]?\s*"#, options: [.regularExpression, .caseInsensitive]) {
            what.removeSubrange(r)
        }
        what = what.trimmingCharacters(in: .whitespaces)
        guard !what.isEmpty else { throw ScheduleProblem(status: 400, message: "What should it do? Add that after the time, like “… summarize my inbox”.") }
        what = what.prefix(1).uppercased() + what.dropFirst()
        var runs: [Date] = []
        var day = calendar.startOfDay(for: now)
        while runs.count < 3 {
            if let t = calendar.date(bySettingHour: h, minute: 0, second: 0, of: day), t > now,
               !weekdays || !calendar.isDateInWeekend(t),
               one.map({ calendar.component(.weekday, from: t) == $0 + 1 }) ?? true { runs.append(t) }
            day = calendar.date(byAdding: .day, value: 1, to: day)!
        }
        return SchedulePreview(schedule: "0 \(h) * * \(weekdays ? "1-5" : one.map(String.init) ?? "*")", scheduleLabel: label, prompt: what,
                               name: String(what.prefix(40)), nextRuns: runs,
                               targets: [.init(id: "local", label: "Saved only"), .init(id: "telegram", label: "Telegram"),
                                         .init(id: "discord", label: "Discord")])
    }

    public func preview(schedule: String, prompt: String) async throws -> SchedulePreview {
        try await preview(text: schedule + " " + prompt)
    }

    public func create(_ preview: SchedulePreview, name: String?, deliver: String?) async throws -> ScheduleDetail {
        let id = "demo\(jobs.count + 1)"
        let target = preview.targets.first { $0.id == (deliver ?? preview.deliver) }?.label ?? "Saved only"
        let job = ScheduledJob(id: id, name: name ?? preview.name, scheduleLabel: preview.scheduleLabel, kind: preview.kind,
                               nextRun: preview.nextRun, deliveryLabel: target)
        let d = ScheduleDetail(job: job, prompt: preview.prompt, scheduleText: preview.schedule)
        jobs.append(job); detailsByID[id] = d
        return d
    }

    public func pause(_ id: String) async throws -> ScheduleDetail {
        try change(id) { $0.enabled = false; $0.state = .paused; $0.nextRun = nil }
    }

    public func resume(_ id: String) async throws -> ScheduleDetail {
        try change(id) { [now] in $0.enabled = true; $0.state = .scheduled; $0.nextRun = now.addingTimeInterval(3600 * 5) }
    }

    public func runNow(_ id: String) async throws -> ScheduleDetail {
        let wasPaused = detailsByID[id]?.job.isPaused == true
        var d = try change(id) { [now] in $0.enabled = true; $0.state = .scheduled; $0.nextRun = now.addingTimeInterval(20) }
        d.queued = true; d.resumed = wasPaused
        return d
    }

    public func update(_ id: String, schedule: String?, prompt: String?, name: String?) async throws -> ScheduleDetail {
        guard var d = detailsByID[id] else { throw ScheduleProblem(status: 404, message: "I couldn't find that schedule.") }
        if let prompt { d.prompt = prompt }
        if let name { d.job.name = name }
        if let schedule {
            let p = try await preview(text: schedule + " x")
            d.scheduleText = p.schedule; d.job.scheduleLabel = p.scheduleLabel; d.job.nextRun = d.job.isPaused ? nil : p.nextRun
        }
        detailsByID[id] = d
        if let i = jobs.firstIndex(where: { $0.id == id }) { jobs[i] = d.job }
        return d
    }

    public func delete(_ id: String) async throws {
        jobs.removeAll { $0.id == id }
        detailsByID[id] = nil
    }

    private func change(_ id: String, _ edit: (inout ScheduledJob) -> Void) throws -> ScheduleDetail {
        guard var d = detailsByID[id] else { throw ScheduleProblem(status: 404, message: "I couldn't find that schedule.") }
        edit(&d.job)
        detailsByID[id] = d
        if let i = jobs.firstIndex(where: { $0.id == id }) { jobs[i] = d.job }
        return d
    }

    static func clock(_ h: Int, _ m: Int) -> String {
        "\(h % 12 == 0 ? 12 : h % 12):\(String(format: "%02d", m)) \(h < 12 ? "AM" : "PM")"
    }

    /// The demo set: five schedules (one running, one paused, a failure, a finished one-time).
    static func make(now: Date, calendar cal: Calendar) -> ([ScheduledJob], [String: ScheduleDetail]) {
        func at(_ daysAhead: Int, _ h: Int, _ m: Int = 0) -> Date {
            cal.date(bySettingHour: h, minute: m, second: 0, of: cal.date(byAdding: .day, value: daysAhead, to: now)!)!
        }
        func nextWeekday(_ h: Int, _ m: Int = 0) -> Date {
            var d = 0
            while true {
                let t = at(d, h, m)
                if t > now && !cal.isDateInWeekend(t) { return t }
                d += 1
            }
        }
        func lastWeekday(_ h: Int, _ m: Int = 0, skip: Int = 0) -> Date {
            var d = 0, seen = 0
            while true {
                let t = at(d, h, m)
                if t < now && !cal.isDateInWeekend(t) { if seen == skip { return t }; seen += 1 }
                d -= 1
            }
        }
        func runs(_ id: String, _ starts: [Date], took: TimeInterval, results: [String], status: [ScheduledJob.RunStatus]? = nil,
                  sessions: Bool = true) -> [ScheduleRun] {
            starts.enumerated().map { i, s in
                let st = status?[i] ?? .ok
                return ScheduleRun(id: "\(id)_r\(i)", startedAt: s, finishedAt: s.addingTimeInterval(took + Double(i * 7)),
                                   status: st, result: results[i % results.count],
                                   error: st == .failed ? results[i % results.count] : nil,
                                   sessionID: sessions ? "cron_\(id)_run\(i)" : nil)
            }
        }
        let gardenRuns = runs("garden", (0..<5).map { lastWeekday(7, 30, skip: $0) }, took: 41, results: [
            "Light frost tonight (31 °F at 5 AM). Cover the basil and bring the lemon tree in.",
            "Dry and mild. No watering needed; the beds still hold yesterday's rain.",
            "Wind gusts to 30 mph after 2 PM. Stake the tall tomatoes.",
            "Clear, 54 °F. A good morning to plant the garlic.",
            "Showers until noon, then sun. Skip watering."])
        let inboxRuns = runs("inbox", (0..<4).map { lastWeekday(8, 0, skip: $0) }, took: 96, results: [
            "3 need you: the venue quote for the 14th, Priya's draft, and a renewal due Friday. 18 newsletters archived.",
            "1 needs you: the plumber can come Thursday at 10. 9 receipts filed.",
            "Nothing urgent. 2 shipping updates; the lamp arrives Tuesday.",
            "2 need you: the school form and Sam's question about the rota."])
        let buildRuns = runs("build", [now.addingTimeInterval(-60 * 70), now.addingTimeInterval(-60 * 190), now.addingTimeInterval(-60 * 310)],
                             took: 22, results: ["The nightly build is green. 412 tests passed in 6 min.",
                                                 "Green. The flaky upload test passed 20 of 20.",
                                                 "Green."])
        let readingRuns = runs("reading", [at(-3, 9), at(-10, 9)], took: 55, results: [
            "The feed for Field Notes timed out twice. The other four are in your list.",
            "Five picks this week: two on soil, one on bees, two essays."], status: [.failed, .ok])
        let plantRuns = runs("plants", [at(-1, 18)], took: 4, results: ["Reminded you on Telegram: water the window boxes."])

        var jobs = [
            ScheduledJob(id: "garden", name: "Morning garden check", scheduleLabel: "Weekdays at 7:30 AM", nextRun: nextWeekday(7, 30),
                         lastRun: gardenRuns[0].startedAt, lastStatus: .ok, lastResult: gardenRuns[0].result, deliveryLabel: "Telegram"),
            ScheduledJob(id: "inbox", name: "Inbox summary", scheduleLabel: "Weekdays at 8:00 AM", nextRun: nextWeekday(8),
                         lastRun: inboxRuns[0].startedAt, lastStatus: .ok, lastResult: inboxRuns[0].result, deliveryLabel: "Speakeasy"),
            ScheduledJob(id: "build", name: "Nightly build watch", scheduleLabel: "Every 2 hours", kind: "interval",
                         nextRun: now.addingTimeInterval(60 * 14), lastRun: buildRuns[0].startedAt, lastStatus: .running,
                         lastResult: buildRuns[0].result, state: .running, deliveryLabel: "Discord · #builds"),
            ScheduledJob(id: "reading", name: "Weekly reading list", scheduleLabel: "Mondays at 9:00 AM", nextRun: nil,
                         lastRun: readingRuns[0].startedAt, lastStatus: .failed, lastResult: readingRuns[0].result,
                         enabled: false, state: .paused, deliveryLabel: "Saved only"),
            ScheduledJob(id: "plants", name: "Water the window boxes", scheduleLabel: "Once, yesterday at 6:00 PM", kind: "once",
                         lastRun: plantRuns[0].startedAt, lastStatus: .ok, lastResult: plantRuns[0].result,
                         enabled: false, state: .completed, deliveryLabel: "Telegram"),
        ]
        var details: [String: ScheduleDetail] = [:]
        let prompts = [
            "garden": "Check today's forecast for the garden. Flag frost, high wind or heavy rain, and say what to do about it in one or two lines.",
            "inbox": "Summarize my inbox since yesterday morning: what needs me today, what can wait, and archive the newsletters.",
            "build": "Check the nightly build and the test dashboard. Only tell me if something is red or flaky.",
            "reading": "Collect this week's best posts from my five garden feeds into a short reading list.",
            "plants": "Remind me to water the window boxes.",
        ]
        let texts = ["garden": "30 7 * * 1-5", "inbox": "0 8 * * 1-5", "build": "every 120m", "reading": "0 9 * * 1", "plants": ""]
        let allRuns = ["garden": gardenRuns, "inbox": inboxRuns, "build": [ScheduleRun(id: "build_live", startedAt: now.addingTimeInterval(-40), status: .running)] + buildRuns,
                       "reading": readingRuns, "plants": plantRuns]
        for j in jobs {
            details[j.id] = ScheduleDetail(job: j, prompt: prompts[j.id] ?? "", skills: j.id == "garden" ? ["weather"] : [],
                                           scheduleText: texts[j.id] ?? "", runs: allRuns[j.id] ?? [])
        }
        jobs = jobs.map { details[$0.id]?.job ?? $0 }
        return (jobs, details)
    }
}
