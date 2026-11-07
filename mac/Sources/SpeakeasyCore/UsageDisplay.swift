import Foundation

/// A display preference on this device only. It never changes server reporting or billing.
public enum UsageDisplay {
    public static let key = "showUsage"

    /// Missing preferences intentionally read as false, including on existing installs.
    public static func isOn(in defaults: UserDefaults = .standard) -> Bool {
        defaults.bool(forKey: key)
    }
}
