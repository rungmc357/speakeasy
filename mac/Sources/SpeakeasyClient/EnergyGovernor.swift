import Foundation
import Combine
import SwiftUI
import SpeakeasyCore

/// The app's one view of heat and power (SPK-430): `ProcessInfo.thermalState`, Low Power Mode, and
/// whether a call is open. Views and timers read `policy` for their frame rate, decorative motion
/// and backstop waits; `policy` changes only on those three events, so observing it never re-renders
/// anything per frame.
@MainActor
public final class EnergyGovernor: ObservableObject {
    public static let shared = EnergyGovernor()

    @Published public private(set) var policy: EnergyPolicy
    /// Every heat / Low Power change, for the call's energy record.
    public var onChange: ((EnergyPolicy) -> Void)?

    private var observers: [NSObjectProtocol] = []
    /// DEBUG `-demoThermal <nominal|fair|serious|critical>` / `-demoLowPower`: the simulator always
    /// reports nominal and never Low Power, so tests and the perf harness force them.
    private let forcedHeat: EnergyPolicy.Heat?
    private let forcedLowPower: Bool

    public init(observe: Bool = true) {
        var heat: EnergyPolicy.Heat?
        var low = false
        #if DEBUG
        let args = ProcessInfo.processInfo.arguments
        if let i = args.firstIndex(of: "-demoThermal"), i + 1 < args.count { heat = EnergyPolicy.Heat(name: args[i + 1]) }
        low = args.contains("-demoLowPower")
        #endif
        forcedHeat = heat
        forcedLowPower = low
        policy = EnergyPolicy(heat: heat ?? Self.heat(ProcessInfo.processInfo.thermalState),
                              lowPower: low || ProcessInfo.processInfo.isLowPowerModeEnabled)
        guard observe else { return }
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: ProcessInfo.thermalStateDidChangeNotification, object: nil,
                                            queue: .main) { _ in
            Task { @MainActor in EnergyGovernor.shared.readDevice() }
        })
        observers.append(center.addObserver(forName: Notification.Name.NSProcessInfoPowerStateDidChange, object: nil,
                                            queue: .main) { _ in
            Task { @MainActor in EnergyGovernor.shared.readDevice() }
        })
    }

    public static func heat(_ s: ProcessInfo.ThermalState) -> EnergyPolicy.Heat {
        switch s {
        case .nominal: return .nominal
        case .fair: return .fair
        case .serious: return .serious
        case .critical: return .critical
        @unknown default: return .serious
        }
    }

    func readDevice() {
        let info = ProcessInfo.processInfo
        set(heat: forcedHeat ?? Self.heat(info.thermalState), lowPower: forcedLowPower || info.isLowPowerModeEnabled)
    }

    /// Tests (and the device notifications): a new heat / power reading.
    public func set(heat: EnergyPolicy.Heat, lowPower: Bool) {
        var next = policy
        next.heat = heat
        next.lowPower = lowPower
        guard next != policy else { return }
        policy = next
        onChange?(next)
    }

    /// A call opened or closed.
    public func setInCall(_ on: Bool) {
        guard policy.inCall != on else { return }
        policy.inCall = on
    }
}

/// A `TimelineView` schedule that never asks for more than one frame per `interval` and stops when
/// `paused`: plain timer ticks, not a display link, so a 120 Hz screen draws the animation at the
/// capped rate instead of at its own (`.animation(minimumInterval:)` leaves that to the system).
/// In low-frequency mode (Always On, the app going away) it ticks once a second.
public struct CappedSchedule: TimelineSchedule {
    public var interval: TimeInterval
    public var paused: Bool

    public init(interval: TimeInterval, paused: Bool = false) {
        self.interval = max(1.0 / 60, interval)
        self.paused = paused
    }

    public func entries(from startDate: Date, mode: TimelineScheduleMode) -> AnyIterator<Date> {
        if paused {
            var done = false
            return AnyIterator { defer { done = true }; return done ? nil : startDate }
        }
        let step = mode == .lowFrequency ? max(1, interval) : interval
        var next = startDate
        return AnyIterator {
            defer { next = next.addingTimeInterval(step) }
            return next
        }
    }
}
