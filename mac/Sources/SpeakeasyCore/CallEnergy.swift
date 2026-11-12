import Foundation

// Call energy (t_830db805, SPK-430): how hard the app may work right now, and one small record of
// what a call cost. Pure values: the client's `EnergyGovernor` feeds the policy from ProcessInfo,
// and the iPhone's call monitor fills the record and sends it to the plugin when the call ends.

/// What the device says about heat and power, and whether a call is open.
public struct EnergyPolicy: Equatable, Sendable {
    /// `ProcessInfo.ThermalState`, as a plain value (and comparable).
    public enum Heat: Int, Comparable, Sendable, CaseIterable {
        case nominal, fair, serious, critical
        public static func < (a: Heat, b: Heat) -> Bool { a.rawValue < b.rawValue }
        public var name: String {
            switch self {
            case .nominal: return "nominal"
            case .fair: return "fair"
            case .serious: return "serious"
            case .critical: return "critical"
            }
        }
        public init?(name: String) {
            guard let h = Heat.allCases.first(where: { $0.name == name }) else { return nil }
            self = h
        }
    }

    public var heat: Heat
    public var lowPower: Bool
    public var inCall: Bool

    public init(heat: Heat = .nominal, lowPower: Bool = false, inCall: Bool = false) {
        self.heat = heat
        self.lowPower = lowPower
        self.inCall = inCall
    }

    /// The phone is hot (serious or critical) or in Low Power Mode: do less until it clears.
    /// Never touches audio: only drawing, timers and backstop polls slow down.
    public var backingOff: Bool { heat >= .serious || lowPower }

    /// Every animated view's frame interval: 30 fps, 20 fps while backing off. Nothing in the app
    /// asks for more (ProMotion would otherwise run a display link at up to 120 Hz).
    public var frameInterval: TimeInterval { backingOff ? 1.0 / 20 : 1.0 / 30 }
    public var framesPerSecond: Int { Int((1 / frameInterval).rounded()) }

    /// Decorative motion (shimmer sweeps, breathing marks, the orb's outer rings): off while
    /// backing off. Behind a call (the call's own panel is what moves) shimmer is off too:
    /// see `shimmer`.
    public var decorativeMotion: Bool { !backingOff }

    /// A Working card's highlight sweep: never behind a call, never while backing off.
    public var shimmer: Bool { !backingOff && !inCall }

    /// The call's level meter (one WebRTC stats read per tick, which also feeds the mic check):
    /// 10 Hz, ~6.7 Hz while backing off.
    public var levelInterval: TimeInterval { backingOff ? 0.15 : 0.1 }

    /// The call's stage/caption driver: 10 Hz, ~6.7 Hz while backing off.
    public var stageInterval: TimeInterval { backingOff ? 0.15 : 0.1 }

    /// A caption on screen changes at most this often (≤ 4 a second), the newest words always win.
    public static let captionGap: TimeInterval = 0.25

    /// During a call with the live feed connected, backstop polls wait this long: the feed brings
    /// changes at once, and the call's own event stream carries its jobs.
    public static let callBackstop: TimeInterval = 300

    /// A backstop poll's wait: `base`, doubled while backing off; `callBackstop` during a call when
    /// the live feed is up (`feedLive`). A poll that must run (feed down) keeps its base, doubled
    /// only while backing off.
    public func backstop(_ base: TimeInterval, feedLive: Bool) -> TimeInterval {
        if inCall && feedLive { return max(base, Self.callBackstop) }
        return backingOff ? base * 2 : base
    }
}

/// Holds a caption to at most one change per `EnergyPolicy.captionGap`: a change inside the gap
/// is kept and shown when the gap ends (the newest words win); a new speaker shows at once.
public struct CaptionThrottle: Equatable, Sendable {
    public private(set) var shown: String = ""
    public private(set) var pending: String?
    private var shownAt: Date?
    public var gap: TimeInterval

    public init(gap: TimeInterval = EnergyPolicy.captionGap) { self.gap = gap }

    /// Offer the newest caption. Returns the caption to put on screen now, or nil to keep what's shown.
    public mutating func offer(_ caption: String, now: Date, immediate: Bool = false) -> String? {
        if caption == shown { pending = nil; return nil }
        if immediate || shownAt == nil || now.timeIntervalSince(shownAt!) >= gap {
            shown = caption; shownAt = now; pending = nil
            return caption
        }
        pending = caption
        return nil
    }

    /// Clear everything (a new call).
    public mutating func reset() { self = CaptionThrottle(gap: gap) }
}

/// One call's energy record, filled as the call runs and sent once when it ends (`call.energy`).
public struct CallEnergyRecord: Equatable, Sendable {
    public struct HeatChange: Equatable, Sendable {
        public var heat: EnergyPolicy.Heat
        public var at: TimeInterval
    }

    public let startedAt: Date
    public let cpuAtStart: Double
    public private(set) var layout: String
    private var layoutSince: Date
    public private(set) var layoutSeconds: [String: TimeInterval] = [:]
    public let heatAtStart: EnergyPolicy.Heat
    public private(set) var heatChanges: [HeatChange] = []
    public private(set) var heat: EnergyPolicy.Heat
    public private(set) var lowPower: Bool
    public private(set) var lowPowerEver: Bool
    private var lowPowerSince: Date?
    public private(set) var lowPowerSeconds: TimeInterval = 0
    private var backoffSince: Date?
    public private(set) var backoffSeconds: TimeInterval = 0
    /// Frames the call's orb drew (the cheap fps estimate) and the seconds it was on screen.
    public private(set) var frames: Int = 0

    /// Layout names: where the call was seen from.
    public static let layouts = ["full", "home", "transcript", "other", "background"]

    public init(at now: Date, cpuSeconds: Double, layout: String, heat: EnergyPolicy.Heat, lowPower: Bool) {
        startedAt = now
        cpuAtStart = cpuSeconds
        self.layout = layout
        layoutSince = now
        heatAtStart = heat
        self.heat = heat
        self.lowPower = lowPower
        lowPowerEver = lowPower
        lowPowerSince = lowPower ? now : nil
        backoffSince = EnergyPolicy(heat: heat, lowPower: lowPower).backingOff ? now : nil
    }

    public mutating func setLayout(_ name: String, at now: Date) {
        guard name != layout else { return }
        layoutSeconds[layout, default: 0] += max(0, now.timeIntervalSince(layoutSince))
        layout = name
        layoutSince = now
    }

    public mutating func setHeat(_ h: EnergyPolicy.Heat, at now: Date) {
        guard h != heat else { return }
        heat = h
        heatChanges.append(HeatChange(heat: h, at: max(0, now.timeIntervalSince(startedAt))))
        updateBackoff(at: now)
    }

    public mutating func setLowPower(_ on: Bool, at now: Date) {
        guard on != lowPower else { return }
        lowPower = on
        if on { lowPowerEver = true; lowPowerSince = now }
        else if let since = lowPowerSince { lowPowerSeconds += max(0, now.timeIntervalSince(since)); lowPowerSince = nil }
        updateBackoff(at: now)
    }

    public mutating func countFrames(_ n: Int = 1) { frames += n }

    private mutating func updateBackoff(at now: Date) {
        let hot = EnergyPolicy(heat: heat, lowPower: lowPower).backingOff
        if hot, backoffSince == nil { backoffSince = now }
        if !hot, let since = backoffSince { backoffSeconds += max(0, now.timeIntervalSince(since)); backoffSince = nil }
    }

    /// The hottest state seen on this call.
    public var heatMax: EnergyPolicy.Heat { heatChanges.map(\.heat).reduce(heatAtStart, max) }

    /// The record as sent to `POST /voice/telemetry/call-energy`: numbers and short names only
    /// (no words, no audio). `cpuSeconds` = the app's CPU time now (user + system, all threads).
    public func payload(endedAt now: Date, cpuSeconds: Double, interactionID: String?,
                        app: String, device: String) -> [String: Any] {
        var record = self
        record.setLayout("_end", at: now)
        record.setLowPower(false, at: now)
        record.updateBackoffEnd(at: now)
        let duration = max(0, now.timeIntervalSince(startedAt))
        let cpu = max(0, cpuSeconds - cpuAtStart)
        let foreground = duration - (record.layoutSeconds["background"] ?? 0)
        var body: [String: Any] = [
            "duration_s": round1(duration),
            "cpu_s": round1(cpu),
            "cpu_pct": duration > 0 ? round1(cpu / duration * 100) : 0,
            "layout_s": record.layoutSeconds.filter { $0.key != "_end" }.mapValues(round1),
            "thermal_start": heatAtStart.name,
            "thermal_max": heatMax.name,
            "thermal": heatChanges.suffix(24).map { ["state": $0.heat.name, "at_s": round1($0.at)] as [String: Any] },
            "low_power": lowPowerEver,
            "low_power_s": round1(record.lowPowerSeconds),
            "backoff_s": round1(record.backoffSeconds),
            "app": app,
            "device": device,
        ]
        if foreground >= 1, frames > 0 { body["orb_fps"] = round1(Double(frames) / foreground) }
        if let interactionID { body["interaction_id"] = interactionID }
        return body
    }

    private mutating func updateBackoffEnd(at now: Date) {
        if let since = backoffSince { backoffSeconds += max(0, now.timeIntervalSince(since)); backoffSince = nil }
    }

    private func round1(_ x: Double) -> Double { (x * 10).rounded() / 10 }
}
