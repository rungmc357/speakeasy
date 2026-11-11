import Foundation

/// 74.1 (F4(c)): when each step of a call's connect happened, measured from the tap, so the next slow
/// connect is a number instead of a feeling ("up to ten seconds of connecting before it hears me").
/// Pure and clock-driven; the client marks the stages and logs `summary` on the device (and in the
/// call's mic-check note) once the first transcript arrives, or when the call ends before that.
public struct ConnectTimeline: Equatable, Sendable {
    public enum Stage: String, CaseIterable, Sendable {
        case sessionCreated = "session created"
        case channelOpen = "channel open"
        case live = "call live"
        case firstMicPacket = "first mic packet"
        case firstTranscript = "first transcript"
    }

    public let tap: Date
    /// The first time each stage was reached (a reconnect doesn't move an earlier mark).
    public private(set) var marks: [Stage: Date] = [:]
    /// Repairs and retries in between, in order ("mic restarted in place", "reconnect: no sound…").
    public private(set) var events: [(String, Date)] = []
    /// The summary went out (once per call).
    public private(set) var reported = false

    public init(tap: Date) { self.tap = tap }

    /// Marks `stage` the first time it's reached; true when this was the first time.
    @discardableResult
    public mutating func mark(_ stage: Stage, at now: Date) -> Bool {
        guard marks[stage] == nil, now >= tap else { return false }
        marks[stage] = now
        return true
    }

    public mutating func note(_ event: String, at now: Date) {
        events.append((event, now))
    }

    /// Seconds from the tap to `stage`, nil if not reached.
    public func seconds(to stage: Stage) -> TimeInterval? { marks[stage].map { $0.timeIntervalSince(tap) } }

    /// The summary is due: the first transcript arrived (or `ended`), and it hasn't gone out yet.
    public func due(ended: Bool = false) -> Bool { !reported && (marks[.firstTranscript] != nil || ended) }

    /// "tap → session created +1.2s, channel open +2.0s, …; mic restarted in place +9.0s". Stages not
    /// reached read "never".
    public func summary() -> String {
        func f(_ t: TimeInterval) -> String { String(format: "+%.1fs", t) }
        let stages = Stage.allCases.map { stage in
            "\(stage.rawValue) " + (seconds(to: stage).map(f) ?? "never")
        }
        var line = "connect timeline: tap → " + stages.joined(separator: ", ")
        if !events.isEmpty {
            line += "; " + events.map { "\($0.0) " + f($0.1.timeIntervalSince(tap)) }.joined(separator: ", ")
        }
        return line
    }

    public mutating func markReported() { reported = true }

    public static func == (a: ConnectTimeline, b: ConnectTimeline) -> Bool {
        a.tap == b.tap && a.marks == b.marks && a.reported == b.reported
            && a.events.map(\.0) == b.events.map(\.0) && a.events.map(\.1) == b.events.map(\.1)
    }
}
