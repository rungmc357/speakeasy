import Foundation

// Features whose route an older plugin doesn't serve (SPK-238): reopening a saved call
// (`GET /voice/calls/{id}`, capability `saved_calls`) and Home's Recent (`GET /voice/now/recent`,
// `now_recent`). An older plugin answers both with its unknown-route 404, the same status a newer
// one gives a call it no longer has, so the app asks only a server that advertises the route. Without
// the flag it says what an update brings, never "no longer saved", and never an empty list.

public enum PluginFeatureGate {
    /// The plugin is older than the feature: updating it fills the place.
    public static let updateLine = "Update Speakeasy on your Hermes to see this."
    /// The plugin has saved calls, and this one is gone.
    public static let callGoneLine = "This call is no longer saved on your Hermes."

    public enum Feature: Sendable { case savedCall, recent }

    public enum Access: Equatable, Sendable {
        /// The server's capabilities haven't arrived (or it sent none): nothing to tell yet.
        case unknown
        case available
        case needsUpdate
    }

    /// What the sheet shows once it has asked (or decided not to).
    public enum Outcome: Equatable, Sendable {
        case loaded
        /// The saved call's sheet: `callGoneLine`.
        case gone
        /// `updateLine`.
        case needsUpdate
        /// Offline, timed out, a server error: the sheet's usual plain error copy.
        case failed
    }

    /// `advertised` is the capability flag; nil when the server's capabilities aren't known.
    public static func access(advertised: Bool?) -> Access {
        guard let advertised else { return .unknown }
        return advertised ? .available : .needsUpdate
    }

    public static func access(_ feature: Feature, capabilities: ServerCapabilities?) -> Access {
        guard let capabilities else { return .unknown }
        switch feature {
        case .savedCall: return access(advertised: capabilities.savedCalls)
        case .recent: return access(advertised: capabilities.nowRecent)
        }
    }

    /// Ask the server at all: never a route the plugin said it doesn't have.
    public static func shouldAsk(_ access: Access) -> Bool { access != .needsUpdate }

    /// `status` is the HTTP status of the answer (nil: no answer, or the app didn't ask).
    public static func outcome(_ feature: Feature, access: Access, status: Int?) -> Outcome {
        if access == .needsUpdate { return .needsUpdate }
        guard let status else { return .failed }
        if (200..<300).contains(status) { return .loaded }
        guard status == 404 else { return .failed }
        // A 404 from a server that didn't say it has the route can't mean the call is gone.
        guard access == .available else { return .needsUpdate }
        return feature == .savedCall ? .gone : .failed
    }

    /// The line the sheet shows for `outcome`; nil where it shows the content or its own error copy.
    public static func line(_ outcome: Outcome) -> String? {
        switch outcome {
        case .gone: return callGoneLine
        case .needsUpdate: return updateLine
        case .loaded, .failed: return nil
        }
    }
}
