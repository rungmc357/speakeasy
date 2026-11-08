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

    /// When something last changed, as a card's corner reads it: a machine timestamp becomes "2:41 AM"
    /// today, "Yesterday", the weekday within the week, else "Nov 2" (with the year when it differs).
    /// Words the agent wrote ("2h ago") pass through. Tracker and GitHub data arrive as raw ISO times.
    public static func stamp(_ value: String?, now: Date = Date(), calendar: Calendar = .current,
                             locale: Locale = .current) -> String? {
        guard let v = clean(value) else { return nil }
        // A bare day ("2026-10-29", GitHub's search dates) has no clock time to show.
        let dayOnly = v.count == 10 && v.first?.isNumber == true && v.filter({ $0 == "-" }).count == 2
        let bare: Date? = dayOnly ? {
            let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.calendar = calendar
            f.timeZone = calendar.timeZone; f.dateFormat = "yyyy-MM-dd"; return f.date(from: v)
        }() : nil
        guard let d = bare ?? date(v) ?? spoken(v, now: now, calendar: calendar) else { return corner(v) }
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: d), to: calendar.startOfDay(for: now)).day ?? 0
        if days <= 0 { return bare != nil ? "Today" : clock(d, calendar: calendar, locale: locale) }
        if days == 1 { return "Yesterday" }
        let f = DateFormatter(); f.calendar = calendar; f.locale = locale; f.timeZone = calendar.timeZone
        if days < 7 { f.setLocalizedDateFormatFromTemplate("EEE"); return f.string(from: d) }
        let sameYear = calendar.component(.year, from: d) == calendar.component(.year, from: now)
        f.setLocalizedDateFormatFromTemplate(sameYear ? "MMMd" : "MMMdyyyy")
        return f.string(from: d)
    }

    /// A time inside a day (a timeline row sits under its day): a timestamp or a copied stamp ("Nov 8,
    /// 2:50 AM ET") shows only its clock time; words ("9:40", "Next") pass through.
    public static func clockTime(_ value: String?, now: Date = Date(), calendar: Calendar = .current,
                                 locale: Locale = .current) -> String? {
        guard let v = clean(value) else { return nil }
        guard let d = date(v) ?? spoken(v, now: now, calendar: calendar) else { return v }
        return clock(d, calendar: calendar, locale: locale)
    }

    /// A day word with a clock time, as an inbox writes it, cut to what a corner shows: "Today 7:12 AM"
    /// -> "7:12 AM", "Sat 9:03 PM" / "Yesterday, 6 PM" -> the day. Anything else passes through.
    static func corner(_ v: String) -> String {
        let clockRe = #"\d{1,2}(?::\d{2})?\s?[AaPp]\.?[Mm]\.?(?:\s+(?:ET|EST|EDT|PT|PST|PDT|UTC))?"#
        if let r = v.range(of: #"^(?:Today|Tonight|This morning|This afternoon|This evening),?\s+"# + clockRe + "$",
                           options: [.regularExpression, .caseInsensitive]) {
            let rest = v[r].replacingOccurrences(of: #"^[A-Za-z ]+?,?\s+(?=\d)"#, with: "", options: .regularExpression)
            return rest.replacingOccurrences(of: #"\s+(?:ET|EST|EDT|PT|PST|PDT|UTC)$"#, with: "", options: .regularExpression)
        }
        let days = "Yesterday|Mon|Tue|Tues|Wed|Thu|Thur|Thurs|Fri|Sat|Sun|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday"
        if v.range(of: "^(?:\(days))\\.?,?\\s+" + clockRe + "$", options: [.regularExpression, .caseInsensitive]) != nil,
           let word = v.split(whereSeparator: { $0 == " " || $0 == "," }).first {
            let w = String(word).trimmingCharacters(in: CharacterSet(charactersIn: "."))
            return w.lowercased() == "yesterday" ? "Yesterday" : String(w.prefix(3)).capitalized
        }
        return v
    }

    /// A written stamp the agent copied ("Nov 8, 2:50 AM ET", "Nov 7 7:20 PM"): month, day and clock
    /// time in the user's own zone (a trailing zone word is dropped), in *now*'s year.
    static func spoken(_ v: String, now: Date, calendar: Calendar) -> Date? {
        let zone = #"\s+(?:ET|EST|EDT|CT|CST|CDT|MT|MST|MDT|PT|PST|PDT|UTC|GMT)$"#
        let text = v.replacingOccurrences(of: zone, with: "", options: .regularExpression)
            .replacingOccurrences(of: ",", with: "")
        guard text.range(of: #"^[A-Z][a-z]{2,8}\.? \d{1,2} \d{1,2}:\d{2} ?[AaPp][Mm]$"#, options: .regularExpression) != nil
        else { return nil }
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.calendar = calendar
        f.timeZone = calendar.timeZone
        let year = calendar.component(.year, from: now)
        for format in ["MMM d h:mm a yyyy", "MMMM d h:mm a yyyy", "MMM d h:mma yyyy"] {
            f.dateFormat = format
            if let d = f.date(from: text.replacingOccurrences(of: ".", with: "") + " \(year)") {
                return d
            }
        }
        return nil
    }

    /// A calendar day as a heading: "2026-11-09" becomes "Monday, November 9" (with the year when it
    /// differs from now's). Anything that isn't a bare ISO day passes through as written.
    public static func dayTitle(_ value: String?, now: Date = Date(), calendar: Calendar = .current,
                                locale: Locale = .current) -> String? {
        guard let v = clean(value) else { return nil }
        let p = DateFormatter(); p.locale = Locale(identifier: "en_US_POSIX"); p.calendar = calendar
        p.timeZone = calendar.timeZone; p.dateFormat = "yyyy-MM-dd"
        guard v.count == 10, let d = p.date(from: v) else { return v }
        let f = DateFormatter(); f.locale = locale; f.calendar = calendar; f.timeZone = calendar.timeZone
        let sameYear = calendar.component(.year, from: d) == calendar.component(.year, from: now)
        f.setLocalizedDateFormatFromTemplate(sameYear ? "EEEEMMMMd" : "EEEEMMMMdyyyy")
        return f.string(from: d)
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
