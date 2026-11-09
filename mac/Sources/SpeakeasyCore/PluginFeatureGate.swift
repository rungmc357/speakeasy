import Foundation

// Features whose route an older plugin doesn't serve (SPK-238): reopening a saved call
// (`GET /voice/calls/{id}`, capability `saved_calls`) and Home's Recent (`GET /voice/now/recent`,
// `now_recent`). An older plugin answers both with its unknown-route 404, the same status a newer
// one gives a call it no longer has, so the app asks only a server that advertises the route. Without
// the flag it draws nothing (no Recent, no tappable saved call), and says "Update" only when a plugin
// release the user can install actually brings the route. Never "no longer saved", never an empty list.

public enum PluginFeatureGate {
    /// An installable plugin release brings the feature: updating fills the place.
    public static let updateLine = "Update Speakeasy on your Hermes to see this."
    /// The plugin has saved calls, and this one is gone.
    public static let callGoneLine = "This call is no longer saved on your Hermes."

    public enum Feature: Sendable { case savedCall, recent }

    /// The first plugin release that serves the feature's route and turns its flag on; nil while no
    /// release does (plugin `service.py` `ROUTE_CAPABILITIES`). Set it in the release that ships the route.
    public static func firstPluginVersion(_ feature: Feature) -> String? {
        switch feature {
        case .savedCall: return nil
        case .recent: return nil
        }
    }

    public enum Access: Equatable, Sendable {
        /// The server's capabilities haven't arrived (or it sent none): nothing to tell yet.
        case unknown
        case available
        /// The plugin lacks the route and no installable release brings it: draw nothing, ask nothing.
        case hidden
        /// The plugin lacks the route and the newer release the user can install has it.
        case needsUpdate
    }

    /// What the sheet shows once it has asked (or decided not to).
    public enum Outcome: Equatable, Sendable {
        case loaded
        /// The saved call's sheet: `callGoneLine`.
        case gone
        /// Nothing: no Recent section, no saved-call sheet.
        case hidden
        /// `updateLine`.
        case needsUpdate
        /// Offline, timed out, a server error: the sheet's usual plain error copy.
        case failed
    }

    /// `advertised` is the capability flag; nil when the server's capabilities aren't known.
    /// `firstVersion` is the release that brings the route; `runningVersion` the plugin's own
    /// (`/voice/status` `version`), `latestVersion` the published one (`UpdateCheck.parsePluginVersion`).
    public static func access(advertised: Bool?, firstVersion: String? = nil,
                              runningVersion: String? = nil, latestVersion: String? = nil) -> Access {
        guard let advertised else { return .unknown }
        if advertised { return .available }
        guard let firstVersion,
              let update = UpdateCheck.pluginUpdate(latestVersion: latestVersion, runningVersion: runningVersion),
              !UpdateCheck.isNewer(firstVersion, than: update) else { return .hidden }
        return .needsUpdate
    }

    public static func access(_ feature: Feature, capabilities: ServerCapabilities?,
                              runningVersion: String? = nil, latestVersion: String? = nil) -> Access {
        guard let capabilities else { return .unknown }
        let advertised: Bool
        switch feature {
        case .savedCall: advertised = capabilities.savedCalls
        case .recent: advertised = capabilities.nowRecent
        }
        return access(advertised: advertised, firstVersion: firstPluginVersion(feature),
                      runningVersion: runningVersion, latestVersion: latestVersion)
    }

    /// Ask the server at all: never a route the plugin said it doesn't have.
    public static func shouldAsk(_ access: Access) -> Bool { access == .available || access == .unknown }

    /// The plugin's answer to a route it doesn't serve (`server.py` `do_GET` fall-through), exactly
    /// `{"error": "not found"}`. A served route answers an id it lacks with its own line.
    public static func isUnknownRoute(_ body: Data) -> Bool {
        guard let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return false }
        return object.count == 1 && object["error"] as? String == "not found"
    }

    /// `status` is the HTTP status of the answer (nil: no answer, or the app didn't ask); `body` its
    /// bytes, which tell an unknown route from a call that's gone before capabilities have arrived.
    public static func outcome(_ feature: Feature, access: Access, status: Int?, body: Data? = nil) -> Outcome {
        if access == .hidden { return .hidden }
        if access == .needsUpdate { return .needsUpdate }
        guard let status else { return .failed }
        if (200..<300).contains(status) { return .loaded }
        guard status == 404 else { return .failed }
        let unknownRoute = body.map(isUnknownRoute)
        // A 404 from a server that didn't say it has the route counts as the route only when its body
        // says so; the unknown-route answer hides the place until capabilities say more.
        let fromRoute = access == .available ? unknownRoute != true : unknownRoute == false
        guard fromRoute else { return .hidden }
        return feature == .savedCall ? .gone : .failed
    }

    /// The line the sheet shows for `outcome`; nil where it shows the content, its own error copy, or nothing.
    public static func line(_ outcome: Outcome) -> String? {
        switch outcome {
        case .gone: return callGoneLine
        case .needsUpdate: return updateLine
        case .loaded, .failed, .hidden: return nil
        }
    }
}
