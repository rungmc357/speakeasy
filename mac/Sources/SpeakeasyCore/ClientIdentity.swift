import Foundation

/// The `X-Speakeasy-Client` header every request to the plugin carries, e.g.
/// `iOS 27.0; app 1.4 (60); iPhone`, so the server knows each paired device's platform, OS and app
/// build (shown in `hermes voice devices` and kept with each call in the call log). Platform, versions
/// and a generic device class only: never a hardware identifier, a device name or anything personal.
public enum ClientIdentity {
    public static let headerName = "X-Speakeasy-Client"
    public static let platforms: Set<String> = ["iOS", "iPadOS", "macOS", "visionOS"]

    /// The header value, or nil when a field can't be said safely (the server ignores a missing header).
    public static func header(platform: String, osVersion: OperatingSystemVersion, appVersion: String,
                              build: String, deviceClass: String) -> String? {
        guard platforms.contains(platform) else { return nil }
        let version = clean(appVersion, allowed: "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.+-", limit: 24)
        let buildNumber = clean(build, allowed: "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.", limit: 16)
        guard !version.isEmpty, !buildNumber.isEmpty, version.first?.isLetter == true || version.first?.isNumber == true
        else { return nil }
        var os = "\(osVersion.majorVersion).\(osVersion.minorVersion)"
        if osVersion.patchVersion > 0 { os += ".\(osVersion.patchVersion)" }
        var value = "\(platform) \(os); app \(version) (\(buildNumber))"
        let model = clean(deviceClass, allowed: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ ", limit: 24)
            .trimmingCharacters(in: .whitespaces)
        if model.first?.isLetter == true { value += "; \(model)" }
        return value
    }

    /// A generic device class from a hardware model string (`iPhone17,1` → `iPhone`, `iPad14,3` →
    /// `iPad`): the letters before the first digit, so the model number itself never leaves the device.
    public static func deviceClass(fromMachine machine: String) -> String {
        String(machine.prefix { $0.isLetter })
    }

    private static func clean(_ text: String, allowed: String, limit: Int) -> String {
        let set = Set(allowed)
        return String(text.filter { set.contains($0) }.prefix(limit))
    }
}
