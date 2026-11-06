import Foundation

/// The plugin-update notice, shared by the Mac and iPhone apps: "Speakeasy plugin X is available"
/// with one-tap **Update** (a normal job on the user's Hermes, `POST /voice/work`), then "Updated to X"
/// once the server reports that version, or "Update didn't take" when the job ended and the version
/// never moved. Pure state: the apps persist `PluginUpdateRecord` and feed it what they see.
public struct PluginUpdateRecord: Codable, Equatable, Sendable {
    /// The version whose notice was dismissed: it never shows again (a newer one does).
    public var dismissed: String?
    /// The version an update is under way for (from Update, or seen arriving on its own).
    public var target: String?
    /// The update job's run id (openable on the board), when the app started it.
    public var runID: String?
    public var startedAt: Date?
    /// When the app first saw the update job finished (done or failed).
    public var jobEndedAt: Date?
    /// The plugin version last reported by the server: a jump shows "Updated to …" even when the
    /// update came from somewhere else (a call, the terminal).
    public var lastRunning: String?

    public init(dismissed: String? = nil, target: String? = nil, runID: String? = nil, startedAt: Date? = nil,
                jobEndedAt: Date? = nil, lastRunning: String? = nil) {
        self.dismissed = dismissed; self.target = target; self.runID = runID; self.startedAt = startedAt
        self.jobEndedAt = jobEndedAt; self.lastRunning = lastRunning
    }
}

public enum PluginUpdatePhase: Equatable, Sendable {
    case none
    case available(version: String, note: String?)
    case updating(version: String, runID: String?)
    case updated(version: String)
    case didntTake(version: String, runID: String?)

    public var version: String? {
        switch self {
        case .none: return nil
        case .available(let v, _), .updating(let v, _), .updated(let v), .didntTake(let v, _): return v
        }
    }

    /// A newer plugin is offered and nothing is updating yet.
    public var offersUpdate: Bool {
        if case .available = self { return true }
        return false
    }
}

public enum PluginUpdateFlow {
    /// After the job ends (and any call has hung up), how long the new version may take to load.
    public static let settleSeconds: TimeInterval = 30
    /// How long "Updated to …" stays before the notice clears.
    public static let confirmSeconds: TimeInterval = 6
    public static let changelogURL = URL(string: "https://raw.githubusercontent.com/\(UpdateCheck.repo)/main/CHANGELOG.md")!

    /// What the notice shows now.
    public static func phase(latest: String?, running: String?, record: PluginUpdateRecord, note: String? = nil,
                             inCall: Bool, lastCallEnded: Date? = nil, now: Date = Date()) -> PluginUpdatePhase {
        if let target = record.target {
            if let running, !UpdateCheck.isNewer(target, than: running) { return .updated(version: target) }
            if let ended = record.jobEndedAt, !inCall,
               now.timeIntervalSince(max(ended, lastCallEnded ?? .distantPast)) >= settleSeconds {
                return .didntTake(version: target, runID: record.runID)
            }
            return .updating(version: target, runID: record.runID)
        }
        guard let version = UpdateCheck.pluginUpdate(latestVersion: latest, runningVersion: running),
              record.dismissed != version else { return .none }
        return .available(version: version, note: note)
    }

    /// The user closed the notice (or "Update didn't take"): this version stays quiet.
    public static func dismiss(_ record: PluginUpdateRecord, version: String) -> PluginUpdateRecord {
        var r = record
        r.dismissed = version
        if r.target == version { r.target = nil; r.runID = nil; r.startedAt = nil; r.jobEndedAt = nil }
        return r
    }

    /// Update was tapped and the job started.
    public static func started(_ record: PluginUpdateRecord, version: String, runID: String?, now: Date = Date()) -> PluginUpdateRecord {
        var r = record
        r.target = version; r.runID = runID; r.startedAt = now; r.jobEndedAt = nil
        return r
    }

    /// Feed what the server reports: its plugin version, and the update job's status when known.
    public static func observe(_ record: PluginUpdateRecord, running: String?, jobSettled: Bool, now: Date = Date()) -> PluginUpdateRecord {
        var r = record
        if let running {
            if r.target == nil, let before = r.lastRunning, UpdateCheck.isNewer(running, than: before) {
                r.target = running  // updated elsewhere (by voice, in a terminal): confirm it all the same
            }
            r.lastRunning = running
        }
        if r.target != nil, r.runID != nil, jobSettled, r.jobEndedAt == nil { r.jobEndedAt = now }
        return r
    }

    /// "Updated to …" has been shown: clear the notice.
    public static func acknowledge(_ record: PluginUpdateRecord) -> PluginUpdateRecord {
        var r = record
        if let target = r.target { r.dismissed = target }
        r.target = nil; r.runID = nil; r.startedAt = nil; r.jobEndedAt = nil
        return r
    }

    /// The job the Update button starts. Works on any Hermes (no Speakeasy-only command): the agent
    /// runs Hermes' own updater and never forces a reload, so a live call is never cut off.
    public static func requestText(version: String?) -> String {
        let to = version.map { " to \($0)" } ?? ""
        return "Update my Speakeasy plugin\(to). Run `hermes plugins update speakeasy` on this Hermes machine, "
            + "without --force, then check with `hermes plugins list` that speakeasy is enabled and tell me the "
            + "version it now has. Do not run `hermes voice reload` and do not restart Hermes or its gateway: "
            + "Speakeasy loads the new version by itself once no call or task is running. If the update is "
            + "refused (for example by the security scan), stop and show me the reason and the findings."
    }

    /// One line of what's new: the title of the CHANGELOG entry "## Plugin <version> — <title>".
    public static func changelogHeadline(_ markdown: String, version: String) -> String? {
        for line in markdown.components(separatedBy: .newlines) where line.hasPrefix("## ") {
            let heading = line.dropFirst(3).trimmingCharacters(in: .whitespaces)
            guard heading.hasPrefix("Plugin \(version) ") else { continue }
            let rest = heading.dropFirst("Plugin \(version)".count).trimmingCharacters(in: .whitespaces)
            for dash in ["—", "–", "-"] where rest.hasPrefix(dash) {
                let title = rest.dropFirst(dash.count).trimmingCharacters(in: .whitespaces)
                return title.isEmpty ? nil : String(title.prefix(90))
            }
            return nil
        }
        return nil
    }
}
