import Foundation

/// When things happened on a session page, kept quiet: a day divider when the day changes, a small
/// time where the conversation picks up again after a pause, and each message's exact time on
/// tap (iPhone/iPad) or hover (Mac). Never on panel previews.
public enum ChatTimeline {
    /// What shows above one message.
    public struct Mark: Equatable, Sendable {
        /// "Today", "Yesterday", "Tuesday", "Mon, Oct 3": a day divider (the first message of a day).
        public var day: String?
        /// "9:41 PM": when this part of the conversation started.
        public var time: String
        public init(day: String?, time: String) { self.day = day; self.time = time }
    }

    /// A pause this long or longer between two messages starts a new timed stretch.
    public static let gap: TimeInterval = 15 * 60

    /// Marks keyed by message id. The first timed message of each day gets the day and its time;
    /// later ones get a time only after a pause of at least `gap`. Messages without a time get none.
    public static func marks(_ items: [(id: String, at: Date?)], now: Date = Date(), calendar: Calendar = .current,
                             locale: Locale = .current, gap: TimeInterval = ChatTimeline.gap) -> [String: Mark] {
        var out: [String: Mark] = [:]
        var last: Date?
        for item in items {
            guard let at = item.at else { continue }
            if let prev = last, calendar.isDate(prev, inSameDayAs: at) {
                if at.timeIntervalSince(prev) >= gap {
                    out[item.id] = Mark(day: nil, time: clock(at, calendar: calendar, locale: locale))
                }
            } else {
                out[item.id] = Mark(day: day(at, now: now, calendar: calendar, locale: locale),
                                    time: clock(at, calendar: calendar, locale: locale))
            }
            last = max(last ?? at, at)
        }
        return out
    }

    /// "Today", "Yesterday", "Tuesday" (this week), "Mon, Oct 3" (this year), "Oct 3, 2025".
    public static func day(_ date: Date, now: Date = Date(), calendar: Calendar = .current, locale: Locale = .current) -> String {
        if calendar.isDate(date, inSameDayAs: now) { return "Today" }
        if let y = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: y) { return "Yesterday" }
        let f = formatter(calendar, locale)
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: date), to: calendar.startOfDay(for: now)).day ?? 99
        if days > 0 && days < 7 {
            f.setLocalizedDateFormatFromTemplate("EEEE")
        } else if calendar.component(.year, from: date) == calendar.component(.year, from: now) {
            f.setLocalizedDateFormatFromTemplate("EEEMMMd")
        } else {
            f.setLocalizedDateFormatFromTemplate("MMMdyyyy")
        }
        return f.string(from: date)
    }

    /// "9:41 PM".
    public static func clock(_ date: Date, calendar: Calendar = .current, locale: Locale = .current) -> String {
        let f = formatter(calendar, locale)
        f.setLocalizedDateFormatFromTemplate("jmm")
        return f.string(from: date)
    }

    /// A message's own time, revealed on tap: "9:41 PM" today, else "Yesterday, 9:41 PM" /
    /// "Mon, Oct 3, 9:41 PM".
    public static func exact(_ date: Date, now: Date = Date(), calendar: Calendar = .current, locale: Locale = .current) -> String {
        let time = clock(date, calendar: calendar, locale: locale)
        if calendar.isDate(date, inSameDayAs: now) { return time }
        return day(date, now: now, calendar: calendar, locale: locale) + ", " + time
    }

    static func formatter(_ calendar: Calendar, _ locale: Locale) -> DateFormatter {
        let f = DateFormatter()
        f.locale = locale; f.calendar = calendar; f.timeZone = calendar.timeZone
        return f
    }
}

/// A task's own times: when it started, when it finished, how long it took.
public enum TaskTimes {
    /// The task view's line: "Started 8:52 PM · Done 9:05 PM · Took 13 min", or while it runs
    /// "Started 8:52 PM · 13 min so far". Follow-ups in the same view count as one stretch of work
    /// (earliest start to latest finish). Nil when nothing carries a start time.
    public static func line(_ jobs: [WorkInfo], now: Date = Date(), calendar: Calendar = .current,
                            locale: Locale = .current) -> String? {
        guard let start = jobs.compactMap(\.startedAt).min() else { return nil }
        let at = { TaskLineCopy.time($0, now: now, calendar: calendar, locale: locale) }
        var parts = ["Started " + at(start)]
        let running = jobs.contains { !SessionTaskLayout.finishedStates.contains($0.status) }
        if running {
            parts.append(duration(now.timeIntervalSince(start)) + " so far")
            return parts.joined(separator: " · ")
        }
        guard let end = jobs.compactMap({ $0.finishedAt ?? $0.updatedAt ?? $0.updated }).max(), end >= start else {
            return parts.joined(separator: " · ")
        }
        let failed = jobs.allSatisfy { $0.status == "failed" }, stopped = jobs.allSatisfy { $0.status == "cancelled" }
        parts.append((failed ? "Ended " : stopped ? "Stopped " : "Done ") + at(end))
        parts.append("Took " + duration(end.timeIntervalSince(start)))
        return parts.joined(separator: " · ")
    }

    /// "40 s", "13 min", "1 h 4 min", "2 h".
    public static func duration(_ seconds: TimeInterval) -> String {
        let s = max(0, Int(seconds.rounded()))
        if s < 60 { return "\(max(1, s)) s" }
        let m = Int((Double(s) / 60).rounded())
        if m < 60 { return "\(m) min" }
        let h = m / 60, rest = m % 60
        return rest == 0 ? "\(h) h" : "\(h) h \(rest) min"
    }
}

extension ToolCallLog {
    /// When a group of steps ran: the first step's start.
    public static func startedAt(_ items: [JobActivityItem]) -> Date? {
        items.compactMap { $0.startedAt ?? $0.at }.min()
    }

    /// The quiet suffix of a folded group of steps: "9:02 PM · 6 s" ("Tue 9:02 PM" another day).
    public static func whenLine(_ items: [JobActivityItem], now: Date = Date(), calendar: Calendar = .current,
                                locale: Locale = .current) -> String? {
        var parts: [String] = []
        if let start = startedAt(items) { parts.append(TaskLineCopy.time(start, now: now, calendar: calendar, locale: locale)) }
        let total = items.compactMap { $0.duration(now: now) }.reduce(0, +)
        if total > 0, !items.contains(where: { $0.status == .running }) { parts.append(JobActivityItem.durationText(total)) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
}
