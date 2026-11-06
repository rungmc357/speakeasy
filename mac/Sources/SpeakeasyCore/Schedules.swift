import Foundation

// Hermes scheduled jobs (cron), seen and managed from the apps (servers with `schedules`;
// docs/API.md "Schedules"). The server says each schedule in plain English; the apps add the
// relative times ("in 12 min", "Tomorrow 8:00 AM") so they stay fresh between refreshes.

/// One row of `GET /voice/schedules`.
public struct ScheduledJob: Equatable, Sendable, Identifiable, Hashable {
    public enum RunStatus: String, Equatable, Sendable { case ok, failed, cancelled, running, missed }
    public enum State: String, Equatable, Sendable { case scheduled, paused, running, completed, error }

    public var id: String
    public var name: String
    /// Ready to show: "Weekdays at 8:00 AM", "Every 2 hours", "Once, tomorrow at 9:00 AM".
    public var scheduleLabel: String
    /// `cron`, `interval` or `once`.
    public var kind: String
    /// Nil while paused or after a one-time job ran.
    public var nextRun: Date?
    public var lastRun: Date?
    public var lastStatus: RunStatus?
    /// A short plain-text line: the last run's answer, or its error.
    public var lastResult: String
    /// The last run's result didn't reach its destination (plain sentence), else nil.
    public var deliveryProblem: String?
    public var enabled: Bool
    public var state: State
    /// "Telegram", "Discord · #garden", "Saved only".
    public var deliveryLabel: String

    public init(id: String, name: String, scheduleLabel: String, kind: String = "cron", nextRun: Date? = nil,
                lastRun: Date? = nil, lastStatus: RunStatus? = nil, lastResult: String = "", deliveryProblem: String? = nil,
                enabled: Bool = true, state: State = .scheduled, deliveryLabel: String = "Saved only") {
        self.id = id; self.name = name; self.scheduleLabel = scheduleLabel; self.kind = kind; self.nextRun = nextRun
        self.lastRun = lastRun; self.lastStatus = lastStatus; self.lastResult = lastResult
        self.deliveryProblem = deliveryProblem; self.enabled = enabled; self.state = state; self.deliveryLabel = deliveryLabel
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) else { return nil }
        let state = (o["state"] as? String).flatMap(State.init(rawValue:)) ?? .scheduled
        self.init(id: id, name: String((nonEmpty(o["name"]) ?? "Scheduled job").prefix(80)),
                  scheduleLabel: String((nonEmpty(o["schedule_label"]) ?? "Custom schedule").prefix(160)),
                  kind: nonEmpty(o["kind"]) ?? "cron",
                  nextRun: decodeISODate(o["next_run_at"]), lastRun: decodeISODate(o["last_run_at"]),
                  lastStatus: (o["last_status"] as? String).flatMap(RunStatus.init(rawValue:)),
                  lastResult: String((nonEmpty(o["last_result"]) ?? "").prefix(300)),
                  deliveryProblem: nonEmpty(o["delivery_problem"]),
                  enabled: o["enabled"] as? Bool ?? (state != .paused && state != .completed),
                  state: (o["running"] as? Bool == true) ? .running : state,
                  deliveryLabel: nonEmpty(o["delivery_label"]) ?? "Saved only")
    }

    public var isPaused: Bool { state == .paused }
    public var isRunning: Bool { state == .running || lastStatus == .running }
    public var isFinished: Bool { state == .completed }
    /// Paused and finished rows are drawn dimmed.
    public var isDimmed: Bool { isPaused || isFinished }
    /// Run now is offered (not for a one-time job that already ran, nor while it runs).
    public var canRunNow: Bool { !isFinished && !isRunning }
}

/// One run on a schedule's page.
public struct ScheduleRun: Equatable, Sendable, Identifiable, Hashable {
    public var id: String
    public var startedAt: Date?
    public var finishedAt: Date?
    public var status: ScheduledJob.RunStatus
    public var result: String
    public var error: String?
    /// The Hermes session the run happened in (open with the session page); nil for script jobs.
    public var sessionID: String?

    public init(id: String, startedAt: Date?, finishedAt: Date? = nil, status: ScheduledJob.RunStatus = .ok,
                result: String = "", error: String? = nil, sessionID: String? = nil) {
        self.id = id; self.startedAt = startedAt; self.finishedAt = finishedAt; self.status = status
        self.result = result; self.error = error; self.sessionID = sessionID
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let id = nonEmpty(o["id"]) else { return nil }
        self.init(id: id, startedAt: decodeISODate(o["started_at"]), finishedAt: decodeISODate(o["finished_at"]),
                  status: (o["status"] as? String).flatMap(ScheduledJob.RunStatus.init(rawValue:)) ?? .ok,
                  result: String((nonEmpty(o["result"]) ?? "").prefix(1500)), error: nonEmpty(o["error"]),
                  sessionID: nonEmpty(o["session_id"]))
    }

    /// Seconds the run took, when known.
    public var duration: TimeInterval? {
        guard let startedAt, let finishedAt else { return nil }
        return max(0, finishedAt.timeIntervalSince(startedAt))
    }
}

/// `GET /voice/schedules/{id}`.
public struct ScheduleDetail: Equatable, Sendable {
    public var job: ScheduledJob
    public var prompt: String
    public var skills: [String]
    /// The schedule as Hermes stores it ("0 8 * * 1-5", "every 120m"): the edit field's start.
    public var scheduleText: String
    public var runs: [ScheduleRun]
    public var scriptOnly: Bool
    /// Run now just queued it (it runs within a minute) / and it was paused, so that resumed it.
    public var queued: Bool
    public var resumed: Bool

    public init(job: ScheduledJob, prompt: String, skills: [String] = [], scheduleText: String = "",
                runs: [ScheduleRun] = [], scriptOnly: Bool = false, queued: Bool = false, resumed: Bool = false) {
        self.job = job; self.prompt = prompt; self.skills = skills; self.scheduleText = scheduleText
        self.runs = runs; self.scriptOnly = scriptOnly; self.queued = queued; self.resumed = resumed
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let job = ScheduledJob(json: o) else { return nil }
        self.init(job: job, prompt: (o["prompt"] as? String) ?? "",
                  skills: (o["skills"] as? [Any] ?? []).compactMap { nonEmpty($0) },
                  scheduleText: (o["schedule_text"] as? String) ?? "",
                  runs: (o["runs"] as? [Any] ?? []).compactMap(ScheduleRun.init(json:)),
                  scriptOnly: o["script_only"] as? Bool ?? false,
                  queued: o["queued"] as? Bool ?? false, resumed: o["resumed"] as? Bool ?? false)
    }

    public static func path(_ id: String) -> String {
        "/voice/schedules/" + (id.addingPercentEncoding(withAllowedCharacters: .alphanumerics) ?? id)
    }
}

/// `POST /voice/schedules/preview`: what would be saved. The app shows it to confirm.
public struct SchedulePreview: Equatable, Sendable {
    public struct Target: Equatable, Sendable, Identifiable, Hashable {
        public var id: String
        public var label: String
        public init(id: String, label: String) { self.id = id; self.label = label }
    }

    /// What `POST /voice/schedules` takes back as the schedule.
    public var schedule: String
    public var scheduleLabel: String
    public var kind: String
    public var prompt: String
    public var name: String
    public var nextRuns: [Date]
    /// A part that was filled in ("9:00 AM" when no time was said).
    public var assumed: String?
    public var deliver: String
    public var targets: [Target]

    public init(schedule: String, scheduleLabel: String, kind: String = "cron", prompt: String, name: String,
                nextRuns: [Date], assumed: String? = nil, deliver: String = "local",
                targets: [Target] = [Target(id: "local", label: "Saved only")]) {
        self.schedule = schedule; self.scheduleLabel = scheduleLabel; self.kind = kind; self.prompt = prompt
        self.name = name; self.nextRuns = nextRuns; self.assumed = assumed; self.deliver = deliver; self.targets = targets
    }

    public init?(json: Any?) {
        guard let o = json as? [String: Any], let schedule = nonEmpty(o["schedule"]),
              let label = nonEmpty(o["schedule_label"]) else { return nil }
        var runs = (o["next_runs"] as? [Any] ?? []).compactMap(decodeISODate)
        if runs.isEmpty, let first = decodeISODate(o["next_run_at"]) { runs = [first] }
        let targets = (o["delivery_targets"] as? [Any] ?? []).compactMap { raw -> Target? in
            guard let t = raw as? [String: Any], let id = nonEmpty(t["id"]), let label = nonEmpty(t["label"]) else { return nil }
            return Target(id: id, label: label)
        }
        self.init(schedule: schedule, scheduleLabel: label, kind: nonEmpty(o["kind"]) ?? "cron",
                  prompt: (o["prompt"] as? String) ?? "", name: nonEmpty(o["name"]) ?? "Scheduled job",
                  nextRuns: runs, assumed: nonEmpty(o["assumed"]), deliver: nonEmpty(o["deliver"]) ?? "local",
                  targets: targets.isEmpty ? [Target(id: "local", label: "Saved only")] : targets)
    }

    public var nextRun: Date? { nextRuns.first }

    /// The card's one line: "Weekdays at 8:00 AM · next: Mon 8:00 AM".
    public func summary(now: Date = Date(), calendar: Calendar = .current, locale: Locale = .current) -> String {
        guard let next = nextRun else { return scheduleLabel }
        return "\(scheduleLabel) · next: \(ScheduleFormat.when(next, now: now, calendar: calendar, locale: locale))"
    }

    /// `POST /voice/schedules` body: confirmed, with the person's edits to the name and destination.
    public func createBody(name: String? = nil, deliver: String? = nil) -> [String: Any] {
        let clean = (name ?? self.name).trimmingCharacters(in: .whitespacesAndNewlines)
        return ["schedule": schedule, "prompt": prompt, "name": clean.isEmpty ? self.name : String(clean.prefix(80)),
                "deliver": deliver ?? self.deliver, "confirmed": true]
    }

    /// The preview request: one sentence, or a separate when and what.
    public static func requestBody(text: String) -> [String: Any]? {
        let clean = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !clean.isEmpty else { return nil }
        return ["text": String(clean.prefix(4000))]
    }

    public static func requestBody(schedule: String, prompt: String) -> [String: Any]? {
        let when = schedule.trimmingCharacters(in: .whitespacesAndNewlines)
        let what = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !when.isEmpty, !what.isEmpty else { return nil }
        return ["schedule": String(when.prefix(200)), "prompt": String(what.prefix(4000))]
    }
}

/// `GET /voice/schedules`.
public struct ScheduleList: Equatable, Sendable {
    public var schedules: [ScheduledJob]
    public var timezone: String?
    public init(schedules: [ScheduledJob], timezone: String? = nil) { self.schedules = schedules; self.timezone = timezone }
    public init(json: Any?) {
        let o = json as? [String: Any] ?? [:]
        schedules = (o["schedules"] as? [Any] ?? []).compactMap(ScheduledJob.init(json:))
        timezone = nonEmpty(o["timezone"])
    }

    /// The zone as a person says it: "America/New_York" → "New York", "America/Argentina/Buenos_Aires"
    /// → "Buenos Aires". Never shows the raw identifier; "UTC" / "Etc/UTC" stay "UTC".
    public static func zoneName(_ id: String) -> String {
        let last = id.split(separator: "/").last.map(String.init) ?? id
        let name = last.replacingOccurrences(of: "_", with: " ").trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? id : name
    }

    /// The line above the Scheduled list: "Hermes time · New York" (just "Hermes" with no zone).
    public static func zoneLabel(_ timezone: String?) -> String {
        guard let tz = timezone?.trimmingCharacters(in: .whitespaces), !tz.isEmpty else { return "Hermes" }
        return "Hermes time · \(zoneName(tz))"
    }

    /// The list's sections, in order: running and upcoming, paused, finished one-time jobs.
    public enum Section: String, Sendable, CaseIterable { case upcoming = "Upcoming", paused = "Paused", done = "Done" }

    public static func sections(_ jobs: [ScheduledJob]) -> [(Section, [ScheduledJob])] {
        var upcoming: [ScheduledJob] = [], paused: [ScheduledJob] = [], done: [ScheduledJob] = []
        for job in jobs {
            if job.isFinished { done.append(job) } else if job.isPaused { paused.append(job) } else { upcoming.append(job) }
        }
        upcoming.sort { a, b in
            if a.isRunning != b.isRunning { return a.isRunning }
            return (a.nextRun ?? .distantFuture) < (b.nextRun ?? .distantFuture)
        }
        return [(.upcoming, upcoming), (.paused, paused), (.done, done)].filter { !$0.1.isEmpty }
    }
}

/// Relative times for schedules, en-US style: "in 12 min", "Today 8:00 AM", "Tomorrow 8:00 AM",
/// "Mon 8:00 AM", "Yesterday 6:00 PM", "Nov 20, 9:00 AM".
public enum ScheduleFormat {
    public static func clock(_ date: Date, calendar: Calendar = .current, locale: Locale = .current) -> String {
        let f = DateFormatter()
        f.locale = locale; f.calendar = calendar; f.timeZone = calendar.timeZone
        f.setLocalizedDateFormatFromTemplate("jmm")
        // The same plain space as the server's labels ("8:00 AM"), not the formatter's narrow one.
        return f.string(from: date).replacingOccurrences(of: "\u{202F}", with: " ")
    }

    private static func pattern(_ template: String, _ date: Date, _ calendar: Calendar, _ locale: Locale) -> String {
        let f = DateFormatter()
        f.locale = locale; f.calendar = calendar; f.timeZone = calendar.timeZone
        f.setLocalizedDateFormatFromTemplate(template)
        return f.string(from: date)
    }

    /// An absolute-ish time near now (past or future).
    public static func when(_ date: Date, now: Date = Date(), calendar: Calendar = .current, locale: Locale = .current) -> String {
        let time = clock(date, calendar: calendar, locale: locale)
        if calendar.isDate(date, inSameDayAs: now) { return "Today \(time)" }
        if let t = calendar.date(byAdding: .day, value: 1, to: now), calendar.isDate(date, inSameDayAs: t) { return "Tomorrow \(time)" }
        if let y = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: y) { return "Yesterday \(time)" }
        let days = abs(date.timeIntervalSince(now)) / 86400
        if days < 6 { return "\(pattern("EEE", date, calendar, locale)) \(time)" }
        let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: now)
        return "\(pattern(sameYear ? "MMMd" : "yMMMd", date, calendar, locale)), \(time)"
    }

    /// The next run: "Now", "in 12 min", "in 1 hr 5 min" when it's close, else `when`.
    public static func next(_ date: Date, now: Date = Date(), calendar: Calendar = .current, locale: Locale = .current) -> String {
        let seconds = date.timeIntervalSince(now)
        if seconds < 60 { return "Now" }
        if seconds < 3600 { return "in \(Int((seconds / 60).rounded(.down))) min" }
        return when(date, now: now, calendar: calendar, locale: locale)
    }

    /// A past run on the list: "12 min ago", else `when`.
    public static func last(_ date: Date, now: Date = Date(), calendar: Calendar = .current, locale: Locale = .current) -> String {
        let seconds = now.timeIntervalSince(date)
        if seconds < 60 { return "Just now" }
        if seconds < 3600 { return "\(Int(seconds / 60)) min ago" }
        return when(date, now: now, calendar: calendar, locale: locale)
    }

    /// "42 s", "3 min", "1 hr 10 min".
    public static func duration(_ seconds: TimeInterval) -> String {
        let s = Int(seconds.rounded())
        if s < 60 { return "\(max(1, s)) s" }
        let m = s / 60
        if m < 60 { return "\(m) min" }
        return m % 60 == 0 ? "\(m / 60) hr" : "\(m / 60) hr \(m % 60) min"
    }

    public static func statusWord(_ status: ScheduledJob.RunStatus?) -> String {
        switch status {
        case .ok: return "Done"
        case .failed: return "Didn't finish"
        case .cancelled: return "Stopped"
        case .running: return "Running"
        case .missed: return "Missed"
        case nil: return "Not run yet"
        }
    }
}

/// An ISO 8601 time from the server ("2026-11-06T08:00:00-05:00", with or without fractions), or
/// epoch seconds.
public func decodeISODate(_ value: Any?) -> Date? {
    if let date = decodeDate(value) { return date }
    guard let text = (value as? String)?.trimmingCharacters(in: .whitespaces), !text.isEmpty else { return nil }
    let plain = ISO8601DateFormatter()
    if let d = plain.date(from: text) { return d }
    let fractional = ISO8601DateFormatter()
    fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let d = fractional.date(from: text) { return d }
    // No offset: the server's local time; read as this device's.
    let local = DateFormatter()
    local.locale = Locale(identifier: "en_US_POSIX")
    for format in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm"] {
        local.dateFormat = format
        if let d = local.date(from: text) { return d }
    }
    return nil
}
