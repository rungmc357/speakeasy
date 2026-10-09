import Foundation

// Listening mode keeps a rolling transcript of the room (the "room" in code, so it never collides
// with the call's own "Listening" label). Pure and clock-injected so every rule is testable.

/// What listening mode has heard: finalized sentences plus one tail that is still being recognized.
///
/// Recognizers rewrite their latest words as they go. Storing only the whole string, replaced on
/// every update, loses text over long sessions (Buddy and `EarlyCapture` both did). So finished
/// sentences are kept as segments and only the tail is ever replaced.
///
/// Limits: the last 30 minutes of wall-clock time, and 24,000 Unicode scalars once rendered (the
/// server counts the `room` field with Python's `len()`, which counts code points, not Swift
/// `Character`s). Both trim the oldest text first.
public struct RoomTranscript: Equatable, Sendable {
    /// One finished stretch of speech (usually a sentence), stamped with when it started.
    public struct Segment: Equatable, Sendable {
        public var start: Date
        public var text: String

        public init(start: Date, text: String) {
            self.start = start
            self.text = text
        }
    }

    /// Only this much of the past is kept.
    public static let window: TimeInterval = 30 * 60
    /// The most room text a call may carry, in Unicode scalars (about 6,000 tokens).
    public static let maxScalars = 24_000
    /// "[HH:MM] " in front of every rendered line.
    static let stampScalars = 8
    /// Marks a line whose start was cut to fit the cap.
    static let cutMark = "…"

    /// Finalized segments, oldest first.
    public private(set) var segments: [Segment] = []
    /// The words still being recognized (replaced on every update; nil when there are none).
    public private(set) var volatile: Segment?

    public init() {}

    /// Nothing heard (no finished words and no words in progress).
    public var isEmpty: Bool { segments.isEmpty && volatile == nil }

    /// A finished result. It replaces the tail it grew from; blank text only clears the tail.
    public mutating func appendFinal(_ text: String, at start: Date) {
        volatile = nil
        let clean = Self.oneLine(text)
        guard !clean.isEmpty else { return }
        segments.append(Segment(start: start, text: clean))
        trimStorage()
    }

    /// A result still being recognized. It replaces the previous tail; blank text clears it (the
    /// recognizer took its guess back), so no empty line is ever rendered.
    public mutating func setVolatile(_ text: String, at start: Date) {
        let clean = Self.oneLine(text)
        volatile = clean.isEmpty ? nil : Segment(start: start, text: clean)
    }

    /// The recognizer can't finish the tail any more (it stopped or failed): keep the words as
    /// they stand, as a finished segment.
    public mutating func commitVolatile() {
        guard let tail = volatile else { return }
        volatile = nil
        segments.append(tail)
        trimStorage()
    }

    /// Drop segments that started more than `window` before `now`. The tail is being spoken right
    /// now, so it always stays.
    public mutating func prune(now: Date) {
        let cutoff = now.addingTimeInterval(-Self.window)
        if let keepFrom = segments.firstIndex(where: { $0.start >= cutoff }) {
            if keepFrom > 0 { segments.removeFirst(keepFrom) }
        } else {
            segments.removeAll()
        }
    }

    /// Forget everything (Discard, sleep, the 2-hour cap, or the call that used it has ended).
    public mutating func removeAll() {
        segments.removeAll()
        volatile = nil
    }

    /// The transcript as the server takes it: one "[HH:MM] text" line per segment, oldest first,
    /// the tail last. Keeps the newest lines that fit in `maxScalars` (counting stamps and line
    /// breaks); when even the newest line is too long on its own, it keeps that line's end.
    /// Empty when nothing was heard. `calendar` sets the time zone of the stamps.
    public func render(calendar: Calendar = .current, includeVolatile: Bool = true,
                       maxScalars: Int = RoomTranscript.maxScalars) -> String {
        var all = segments
        if includeVolatile, let volatile { all.append(volatile) }
        var lines: [String] = []   // newest first
        var used = 0
        for segment in all.reversed() {
            let stamp = Self.stamp(segment.start, calendar: calendar)
            let separator = lines.isEmpty ? 0 : 1
            let cost = Self.stampScalars + segment.text.unicodeScalars.count + separator
            if used + cost <= maxScalars {
                lines.append(stamp + segment.text)
                used += cost
                continue
            }
            if lines.isEmpty {
                // A single line longer than the cap: what was said last matters most.
                let room = maxScalars - Self.stampScalars - Self.cutMark.unicodeScalars.count
                if room > 0 {
                    lines.append(stamp + Self.cutMark + Self.suffix(of: segment.text, maxScalars: room))
                }
            }
            break   // oldest-first: never skip a line to fit an older one
        }
        return lines.reversed().joined(separator: "\n")
    }

    /// A frozen copy for the call that listening mode is turned off into: pruned to the window at
    /// `now`, then rendered within the cap.
    public func snapshot(now: Date, calendar: Calendar = .current) -> RoomSnapshot {
        var pruned = self
        pruned.prune(now: now)
        let text = pruned.render(calendar: calendar)
        return RoomSnapshot(text: text, takenAt: now,
                            lineCount: text.isEmpty ? 0 : text.split(separator: "\n").count,
                            firstHeardAt: pruned.segments.first?.start ?? pruned.volatile?.start)
    }

    // MARK: Helpers

    /// "[14:05] " in the calendar's time zone (24-hour clock).
    static func stamp(_ date: Date, calendar: Calendar) -> String {
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        return String(format: "[%02d:%02d] ", parts.hour ?? 0, parts.minute ?? 0)
    }

    /// Runs of whitespace and line breaks become one space, so each segment renders as one line.
    static func oneLine(_ text: String) -> String {
        text.split(whereSeparator: { $0.isWhitespace || $0.isNewline }).joined(separator: " ")
    }

    /// The end of `text` within `maxScalars` scalars, cut on a character boundary (a combining
    /// mark never loses its letter), without leading spaces.
    static func suffix(of text: String, maxScalars: Int) -> String {
        var count = 0
        var start = text.endIndex
        for index in text.indices.reversed() {
            let size = text[index].unicodeScalars.count
            if count + size > maxScalars { break }
            count += size
            start = index
        }
        return String(text[start...].drop(while: { $0 == " " }))
    }

    /// Keep memory bounded on long sessions: drop the oldest segments once their text alone is
    /// over the cap (rendering applies the exact cap, stamps included).
    private mutating func trimStorage() {
        var total = segments.reduce(0) { $0 + $1.text.unicodeScalars.count }
        var drop = 0
        while total > Self.maxScalars, drop < segments.count - 1 {
            total -= segments[drop].text.unicodeScalars.count
            drop += 1
        }
        if drop > 0 { segments.removeFirst(drop) }
    }
}

/// What a call started from listening mode carries: the rendered room text, frozen at take-off.
public struct RoomSnapshot: Equatable, Sendable {
    /// "[HH:MM] text" lines, at most `RoomTranscript.maxScalars` scalars ("" when nothing was heard).
    public let text: String
    public let takenAt: Date
    public let lineCount: Int
    /// When the oldest line kept was said (nil when nothing was heard).
    public let firstHeardAt: Date?

    public init(text: String, takenAt: Date, lineCount: Int, firstHeardAt: Date?) {
        self.text = text
        self.takenAt = takenAt
        self.lineCount = lineCount
        self.firstHeardAt = firstHeardAt
    }

    public var isEmpty: Bool { text.isEmpty }
}

/// Maps the recognizer's audio time (seconds of audio it has been fed) to wall-clock time.
///
/// The mic can pause (device switch, engine restart), so audio time drifts behind the clock.
/// Each time audio starts flowing again an anchor pins audio time to the clock; a result's time is
/// read from the latest anchor at or before it. One timeline per recognizer (a rebuilt recognizer
/// starts again at zero).
public struct RoomTimeline: Equatable, Sendable {
    public struct Anchor: Equatable, Sendable {
        public var audioSeconds: Double
        public var wallClock: Date
    }

    /// Older anchors are dropped past this many (restarts are rare; results are near the end).
    static let maxAnchors = 64
    public private(set) var anchors: [Anchor] = []

    public init() {}

    /// Audio fed from `audioSeconds` on started at `wallClock`.
    public mutating func anchor(audioSeconds: Double, wallClock: Date) {
        anchors.append(Anchor(audioSeconds: audioSeconds, wallClock: wallClock))
        if anchors.count > Self.maxAnchors { anchors.removeFirst(anchors.count - Self.maxAnchors) }
    }

    /// When the audio at `audioSeconds` was heard (nil before any anchor).
    public func wallClock(atAudioSeconds seconds: Double) -> Date? {
        guard let first = anchors.first else { return nil }
        let anchor = anchors.last(where: { $0.audioSeconds <= seconds }) ?? first
        return anchor.wallClock.addingTimeInterval(seconds - anchor.audioSeconds)
    }
}
