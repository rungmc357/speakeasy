import Foundation

/// A card's `as_of` as every app reads it (SPK-456). The plugin stamps each stored result's views
/// with epoch seconds (`views.stamp_as_of`); agents and data sources write ISO times or their own
/// words ("Thursday's close"). Every card that shows an "As of" line reads it through here, so a
/// machine number ("1762934400.123") never reaches the screen.
public enum CardTime {
    /// The card's `as_of` as text the time readers understand: an epoch number (seconds, or
    /// milliseconds) becomes an ISO 8601 time, and so does an epoch written as digits; any other
    /// words pass through as written. Nil when there is nothing readable.
    public static func asOf(_ value: JSONValue) -> String? {
        switch value {
        case .number(let n):
            if let t = iso(epoch: n) { return t }
            // A bare year ("2025") still reads as one; any other number is no time at all.
            if n.rounded() == n, (1900...2100).contains(n) { return String(Int(n)) }
            return nil
        case .string(let s):
            return text(s)
        default:
            return nil
        }
    }

    /// The same for a value already read as text (an older caller's `card["as_of"].string`, which
    /// turns a number into its digits).
    public static func text(_ raw: String?) -> String? {
        guard let t = raw?.trimmingCharacters(in: .whitespacesAndNewlines), !t.isEmpty else { return nil }
        guard isMachineNumber(t) else { return t }
        return Double(t).flatMap { iso(epoch: $0) }
    }

    /// The quote card's line: a time reads "As of 3:42 PM" (its date too when it isn't today); the
    /// source's own words pass through as written ("last 24 hours"); nil when there is nothing.
    public static func quoteLine(_ value: JSONValue, now: Date = Date(), calendar: Calendar = .current) -> String? {
        guard let raw = asOf(value) else { return nil }
        guard ChartFacts.parseTime(raw, timeZone: calendar.timeZone) != nil else { return raw }
        return ChartFacts.freshness(source: nil, asOf: raw, delayed: false, now: now, calendar: calendar).text
    }

    /// Nine or more digits, maybe with a fraction: an epoch count, never words anyone wrote.
    static func isMachineNumber(_ s: String) -> Bool {
        s.range(of: #"^\d{9,}(\.\d+)?$"#, options: .regularExpression) != nil
    }

    /// Epoch seconds (or milliseconds, from a JavaScript source) as an ISO 8601 time; nil outside
    /// 2000–2100, which no data's "as of" falls in.
    static func iso(epoch: Double) -> String? {
        guard epoch.isFinite, epoch > 0 else { return nil }
        let seconds = epoch > 100_000_000_000 ? epoch / 1000 : epoch
        guard seconds >= 946_684_800, seconds < 4_102_444_800 else { return nil }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: Date(timeIntervalSince1970: seconds.rounded(.down)))
    }
}
