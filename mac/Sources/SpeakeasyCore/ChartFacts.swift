import Foundation

/// What a chart card says, decided once for every app: what is plotted, in which order the bars
/// stand (and the bars really stand in that order), every value as readable text, and how fresh
/// the numbers are, from the card's own fields only (never an invented source or time).
///
/// Card fields (all optional except `labels`/`series`): `metric` (what the numbers are, "Share
/// price"), `sort` (`high_first` | `low_first` | `as_listed`), `order_by` (what decided a listed
/// order when it isn't the plotted number, "index weight"), `source`, `as_of` (ISO 8601 time or
/// date, or the source's own words), `delayed`.
public struct ChartFacts: Equatable, Sendable {
    public enum Order: Equatable, Sendable {
        case highFirst, lowFirst
        /// Kept as the card listed it: time order, or the order `order_by` names.
        case listed(by: String?)
    }

    public struct Row: Equatable, Sendable, Identifiable {
        public let label: String
        /// One value per series (nil where a series has no value for this label).
        public let values: [Double?]
        public var id: String { label }
    }

    public let title: String
    public let metric: String?
    public let order: Order
    /// The rows in drawn order.
    public let rows: [Row]
    public let seriesNames: [String]
    public let unit: String?
    /// Category bars (a ranking) are drawn as labeled rows with their values; a line, a time
    /// series or a long category list is drawn as a plot.
    public let ranked: Bool
    /// "Nasdaq · As of 3:42 PM · delayed", or nil when the card says nothing about it.
    public let freshness: String?
    /// The card's `as_of` is older than a day (the date is in `freshness`).
    public let stale: Bool

    /// Most rows drawn as a ranked list; longer category lists are plotted.
    public static let maxRankedRows = 15
    public static let staleAfter: TimeInterval = 24 * 3600

    public init(_ card: ViewCard, now: Date = Date(), calendar: Calendar = .current) {
        let labels = card["labels"].strings
        let series = Array(card["series"].array.prefix(4))
        let names = series.enumerated().map { i, s in s["name"].string ?? "Series \(i + 1)" }
        let count = max(labels.count, series.map { $0["values"].doubles.count }.max() ?? 0)
        var rows: [Row] = (0..<count).map { i in
            Row(label: i < labels.count ? labels[i] : "\(i + 1)",
                values: series.map { s in let v = s["values"].doubles; return i < v.count ? v[i] : nil })
        }
        let style = card["style"].string ?? "bar"
        let timeLike = Self.timeOrdered(labels)
        let orderBy = card["order_by"].string.map { $0.trimmingCharacters(in: .whitespaces) }.flatMap { $0.isEmpty ? nil : $0 }

        let order: Order
        switch Self.sortWord(card["sort"].string) {
        case "high": order = .highFirst
        case "low": order = .lowFirst
        case "listed": order = .listed(by: orderBy)
        default:
            // No order given: a line or a time series keeps its time order; a ranking of things
            // stands highest first, so its bars follow the number they show.
            if style == "line" || timeLike || orderBy != nil { order = .listed(by: orderBy) } else { order = .highFirst }
        }
        switch order {
        case .highFirst: rows = Self.stableSort(rows) { $0 > $1 }
        case .lowFirst: rows = Self.stableSort(rows) { $0 < $1 }
        case .listed: break
        }

        self.title = card["title"].string ?? "Chart"
        self.metric = card["metric"].string ?? (names.count == 1 && series.first?["name"].string != nil ? names[0] : nil)
        self.order = order
        self.rows = rows
        self.seriesNames = names
        self.unit = card["unit"].string
        self.decimals = Self.decimals(rows.flatMap { $0.values.compactMap { $0 } }, unit: card["unit"].string)
        self.ranked = style != "line" && !timeLike && !rows.isEmpty && rows.count <= Self.maxRankedRows
        let fresh = Self.freshness(source: card["source"].string, asOf: card["as_of"].string,
                                   delayed: card["delayed"].bool == true, now: now, calendar: calendar)
        self.freshness = fresh.text
        self.stale = fresh.stale
    }

    // MARK: - Words

    /// What's plotted and in which order: "Share price · highest first", "Share price · in
    /// order of index weight". Nil when the card names neither (a plain time series).
    public var subtitle: String? {
        let orderWords: String?
        switch order {
        case .highFirst: orderWords = "highest first"
        case .lowFirst: orderWords = "lowest first"
        case .listed(let by?): orderWords = "in order of \(by)"
        case .listed(nil): orderWords = nil
        }
        // The unit is on every value, so the metric stands alone.
        let parts = [metric, orderWords].compactMap { $0 }
        guard !parts.isEmpty else { return nil }
        let line = parts.joined(separator: " · ")
        // The metric keeps its own case ("p95", "iPhone sales"); order words alone start a sentence.
        return metric == nil ? line.prefix(1).uppercased() + line.dropFirst() : line
    }

    /// Decimals every value on the card shares, so a column reads evenly ("$97.60" beside
    /// "$431.10", never "$97.6"): the most any value needs, at most 2 (3 when every value is under 1).
    public let decimals: Int

    /// One value as text with its unit: "$1,450.32", "2.4%", "3.2T", "18 runs".
    public func text(_ value: Double) -> String { Self.text(value, unit: unit, decimals: decimals) }

    /// A row's values, series named when there are several: "Signups 120 · Trials 30".
    public func text(_ row: Row) -> String {
        zip(seriesNames, row.values).compactMap { name, v in
            v.map { (seriesNames.count > 1 ? "\(name) " : "") + text($0) }
        }.joined(separator: " · ")
    }

    /// The numbers at rest for a plot: the latest, highest and lowest of the first series
    /// ("Latest 85 · High 155 (Fri) · Low 85 (Sun)"); one latest per series when there are several.
    public var restLine: String? {
        guard !rows.isEmpty else { return nil }
        if seriesNames.count > 1 {
            let latest = seriesNames.indices.compactMap { s -> String? in
                guard let r = rows.last(where: { $0.values[s] != nil }), let v = r.values[s] else { return nil }
                return "\(seriesNames[s]) \(text(v))"
            }
            return latest.isEmpty ? nil : (rows.last.map { "\($0.label): " } ?? "") + latest.joined(separator: " · ")
        }
        let pts = rows.compactMap { r in r.values.first.flatMap { $0 }.map { (r.label, $0) } }
        guard let last = pts.last, let hi = pts.max(by: { $0.1 < $1.1 }), let lo = pts.min(by: { $0.1 < $1.1 }) else { return nil }
        if pts.count == 1 { return "\(last.0): \(text(last.1))" }
        return "Latest \(text(last.1)) · High \(text(hi.1)) (\(hi.0)) · Low \(text(lo.1)) (\(lo.0))"
    }

    /// The whole card as plain lines (copy, VoiceOver summary, share).
    public var plainText: String {
        var lines = [title]
        if let subtitle { lines.append(subtitle) }
        lines += rows.map { "\($0.label): \(text($0))" }
        if let freshness { lines.append(freshness) }
        return lines.joined(separator: "\n")
    }

    // MARK: - Labels

    /// Short axis labels that stay distinct: a label longer than `max` is cut, but never so far
    /// that two labels read the same (GOOG and GOOGL never both become "GO…").
    public static func shortLabels(_ labels: [String], max: Int) -> [String] {
        func cut(_ s: String, _ n: Int) -> String { s.count <= n ? s : String(s.prefix(Swift.max(1, n - 1))) + "…" }
        var lengths = labels.map { _ in max }
        for _ in 0..<64 {
            let short = zip(labels, lengths).map { cut($0, $1) }
            var seen: [String: [Int]] = [:]
            for (i, s) in short.enumerated() { seen[s, default: []].append(i) }
            let clashes = seen.values.filter { $0.count > 1 }
            if clashes.isEmpty { return short }
            var grew = false
            for group in clashes { for i in group where lengths[i] < labels[i].count { lengths[i] += 1; grew = true } }
            if !grew { return short }   // identical labels: nothing to tell apart
        }
        return zip(labels, lengths).map { cut($0, $1) }
    }

    // MARK: - Freshness

    /// "Nasdaq · As of 3:42 PM · delayed". Only what the card says; a time on another day shows
    /// its date ("As of Nov 4, 3:42 PM"), so stale numbers never pass for today's.
    public static func freshness(source: String?, asOf: String?, delayed: Bool, now: Date = Date(),
                                 calendar: Calendar = .current) -> (text: String?, stale: Bool) {
        var parts: [String] = []
        var stale = false
        if let source = source?.trimmingCharacters(in: .whitespaces), !source.isEmpty { parts.append(source) }
        if let raw = asOf?.trimmingCharacters(in: .whitespaces), !raw.isEmpty {
            if let (date, hasTime) = parseTime(raw, timeZone: calendar.timeZone) {
                stale = now.timeIntervalSince(date) > staleAfter
                let time = DateFormatter(); time.locale = Locale(identifier: "en_US"); time.calendar = calendar
                time.timeZone = calendar.timeZone
                if hasTime && calendar.isDate(date, inSameDayAs: now) {
                    time.dateFormat = "h:mm a"
                } else {
                    let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: now)
                    time.dateFormat = (sameYear ? "MMM d" : "MMM d, yyyy") + (hasTime ? ", h:mm a" : "")
                }
                parts.append("As of " + time.string(from: date))
            } else {
                // The source's own words ("Nov 6 close"), shown as given.
                parts.append("As of " + String(raw.prefix(40)))
            }
        }
        if delayed { parts.append(parts.isEmpty ? "Delayed" : "delayed") }
        return (parts.isEmpty ? nil : parts.joined(separator: " · "), stale)
    }

    static func parseTime(_ raw: String, timeZone: TimeZone = .current) -> (Date, Bool)? {
        let full = ISO8601DateFormatter()
        full.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = full.date(from: raw) { return (d, true) }
        full.formatOptions = [.withInternetDateTime]
        if let d = full.date(from: raw) { return (d, true) }
        let local = DateFormatter(); local.locale = Locale(identifier: "en_US_POSIX"); local.timeZone = timeZone
        for f in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm", "yyyy-MM-dd HH:mm:ss", "yyyy-MM-dd HH:mm"] {
            local.dateFormat = f
            if let d = local.date(from: raw) { return (d, true) }
        }
        local.dateFormat = "yyyy-MM-dd"
        if raw.count == 10, let d = local.date(from: raw) { return (d, false) }
        return nil
    }

    // MARK: - Helpers

    static func sortWord(_ raw: String?) -> String? {
        switch raw?.lowercased().replacingOccurrences(of: "-", with: "_") {
        case "high_first", "desc", "descending", "highest_first", "high_to_low": return "high"
        case "low_first", "asc", "ascending", "lowest_first", "low_to_high": return "low"
        case "as_listed", "listed", "none", "given", "as_given": return "listed"
        default: return nil
        }
    }

    static func stableSort(_ rows: [Row], _ before: (Double, Double) -> Bool) -> [Row] {
        rows.enumerated().sorted { a, b in
            let x = a.element.values.first ?? nil, y = b.element.values.first ?? nil
            switch (x, y) {
            case let (x?, y?) where x != y: return before(x, y)
            case (nil, _?): return false
            case (_?, nil): return true
            default: return a.offset < b.offset
            }
        }.map(\.element)
    }

    /// Labels that read as points in time (days, months, quarters, years, clock times, dates):
    /// their order is the time order and is kept.
    static func timeOrdered(_ labels: [String]) -> Bool {
        guard !labels.isEmpty else { return false }
        let pattern = #"^(mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?( \d{1,2}(, ?\d{4})?)?$|^q[1-4]( \d{2,4})?$|^(fy ?)?(19|20)\d{2}$|^\d{1,2}(:\d{2})? ?(am|pm|a|p)?$|^\d{1,2}:\d{2}$|^\d{4}-\d{2}(-\d{2})?$|^\d{1,2}/\d{1,2}(/\d{2,4})?$|^(week|wk|day|month|h|hour) ?\d+$|^today$|^yesterday$|^tomorrow$|^now$"#
        guard let re = try? NSRegularExpression(pattern: pattern) else { return false }
        let hits = labels.filter { l in
            let s = l.lowercased().trimmingCharacters(in: .whitespaces)
            return re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) != nil
        }
        return hits.count * 4 >= labels.count * 3
    }

    /// Decimals a set of values needs together (see `decimals`). Money with any cents shows cents
    /// on every value.
    static func decimals(_ values: [Double], unit: String?) -> Int {
        let plain = values.map(abs).filter { $0 < 1e6 }
        guard !plain.isEmpty else { return 0 }
        let cap = plain.allSatisfy { $0 < 1 } ? 3 : 2
        let need = plain.map { v -> Int in
            for d in 0..<cap { let f = pow(10, Double(d)); if abs((v * f).rounded() - v * f) < 1e-6 { return d } }
            return cap
        }.max() ?? 0
        return need > 0 && money(unit) ? max(2, need) : need
    }

    static func money(_ unit: String?) -> Bool {
        ["$", "usd", "us$", "dollars", "€", "eur", "£", "gbp"].contains(unit?.trimmingCharacters(in: .whitespaces).lowercased() ?? "")
    }

    /// A value with its unit: currencies before the number, %, ° and × right after it, words after
    /// a space. Big numbers are shortened ("3.2T", "450M"). `decimals` nil: what this value needs.
    public static func text(_ value: Double, unit: String?, decimals: Int? = nil) -> String {
        let u = unit?.trimmingCharacters(in: .whitespaces) ?? ""
        let sign = value < 0 ? "−" : ""
        let mag = abs(value)
        let number: String
        if mag >= 1e12 { number = trim(mag / 1e12) + "T" }
        else if mag >= 1e9 { number = trim(mag / 1e9) + "B" }
        else if mag >= 1e6 { number = trim(mag / 1e6) + "M" }
        else {
            let d = decimals ?? Self.decimals([mag], unit: unit)
            let f = NumberFormatter(); f.locale = Locale(identifier: "en_US"); f.numberStyle = .decimal
            f.maximumFractionDigits = d
            f.minimumFractionDigits = d
            number = f.string(from: NSNumber(value: mag)) ?? String(mag)
        }
        switch u.lowercased() {
        case "": return sign + number
        case "$", "usd", "us$", "dollars": return sign + "$" + number
        case "€", "eur": return sign + "€" + number
        case "£", "gbp": return sign + "£" + number
        case "%", "°", "°f", "x", "×": return sign + number + (u.lowercased() == "x" ? "×" : u)
        default: return sign + number + " " + u
        }
    }

    private static func trim(_ v: Double) -> String {
        let s = String(format: v >= 100 ? "%.0f" : v >= 10 ? "%.1f" : "%.2f", v)
        guard s.contains(".") else { return s }
        var t = s
        while t.hasSuffix("0") { t.removeLast() }
        if t.hasSuffix(".") { t.removeLast() }
        return t
    }
}
