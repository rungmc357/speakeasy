import Foundation

/// How a calendar card's event times read. Agents send `start`/`end` either as words the user
/// would read ("9:30 AM", "Noon") or as machine timestamps ("2026-03-02T14:00:00Z"); timestamps
/// are shown as a local clock time ("2:00 PM"), and an event that ends on the same half of the
/// day drops the repeated AM/PM ("9:00 – 10:00 AM"). Anything else passes through unchanged.
public enum EventTimes {
    /// "9:00 – 10:00 AM", "11:00 AM – 1:00 PM", "9:30 AM", or nil when neither time is given.
    public static func span(start: String?, end: String?, calendar: Calendar = .current,
                            locale: Locale = .current) -> String? {
        let s = clean(start), e = clean(end)
        guard s != nil || e != nil else { return nil }
        let sd = s.flatMap(date), ed = e.flatMap(date)
        let st = sd.map { clock($0, calendar: calendar, locale: locale) } ?? s
        let et = ed.map { clock($0, calendar: calendar, locale: locale) } ?? e
        if let sd, let ed, let st, let et {
            let half = period(sd, calendar: calendar, locale: locale)
            if let half, half == period(ed, calendar: calendar, locale: locale),
               calendar.isDate(sd, inSameDayAs: ed), st.hasSuffix(half), et.hasSuffix(half) {
                let bare = st.dropLast(half.count).trimmingCharacters(in: .whitespaces)
                return "\(bare) – \(et)"
            }
        }
        return [st, et].compactMap { $0 }.joined(separator: " – ")
    }

    /// The shown form of one time: a timestamp becomes "2:00 PM", words pass through.
    public static func one(_ value: String?, calendar: Calendar = .current, locale: Locale = .current) -> String? {
        guard let v = clean(value) else { return nil }
        return date(v).map { clock($0, calendar: calendar, locale: locale) } ?? v
    }

    /// A machine timestamp ("2026-03-02T14:00:00Z", with or without seconds, fraction or zone).
    public static func date(_ value: String) -> Date? {
        let v = value.trimmingCharacters(in: .whitespaces)
        guard v.count >= 16, v.contains("T"), v.first?.isNumber == true else { return nil }
        for options: ISO8601DateFormatter.Options in [[.withInternetDateTime, .withFractionalSeconds], [.withInternetDateTime]] {
            let f = ISO8601DateFormatter(); f.formatOptions = options
            if let d = f.date(from: v) { return d }
        }
        // No zone: the user's own clock.
        for format in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm"] {
            let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.dateFormat = format
            if let d = f.date(from: v) { return d }
        }
        return nil
    }

    private static func clean(_ s: String?) -> String? {
        guard let t = s?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty else { return nil }
        return t
    }

    private static func clock(_ d: Date, calendar: Calendar, locale: Locale) -> String {
        let f = DateFormatter(); f.calendar = calendar; f.locale = locale; f.timeZone = calendar.timeZone
        f.setLocalizedDateFormatFromTemplate("jmm")
        return f.string(from: d)
    }

    /// "AM"/"PM" in the locale's words, or nil on a 24-hour clock.
    private static func period(_ d: Date, calendar: Calendar, locale: Locale) -> String? {
        let f = DateFormatter(); f.calendar = calendar; f.locale = locale; f.timeZone = calendar.timeZone
        f.setLocalizedDateFormatFromTemplate("jmm")
        guard f.dateFormat.contains("a") else { return nil }
        f.dateFormat = "a"
        return f.string(from: d)
    }
}
