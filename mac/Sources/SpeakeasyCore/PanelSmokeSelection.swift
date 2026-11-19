import Foundation

/// Resolves executable panel-smoke cases before the native client is created.
public enum PanelSmokeSelection {
    /// Every case `--panel-smoke` can run, in run order. `PanelSmoke.run` switches over these, so a
    /// new case can't be accepted here and then run nothing.
    public enum Case: String, CaseIterable, Sendable {
        case workScroll = "work-scroll"
        case workStatus = "work-status"
        case transcriptScroll = "transcript-scroll"
        case drag
        case ended
        case lookAtThis = "look-at-this"
        case snapshots
    }

    public enum ValidationError: Error, Equatable, CustomStringConvertible {
        case emptySelection
        case unknownCases(Set<String>)
        case snapshotsRequireDirectory

        public var description: String {
            switch self {
            case .emptySelection:
                return "SPEAKEASY_PANEL_SMOKE_ONLY must select at least one case"
            case .unknownCases(let names):
                return "Unknown panel-smoke cases: " + names.sorted().joined(separator: ", ")
                    + " (known: " + Case.allCases.map(\.rawValue).joined(separator: ", ") + ")"
            case .snapshotsRequireDirectory:
                return "The snapshots case requires --snapshot-dir DIR"
            }
        }
    }

    /// The snapshot directory, or nil when it's missing or blank (a blank one would write to `/`).
    public static func directory(_ snapshotDir: String?) -> String? {
        guard let snapshotDir, !snapshotDir.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return snapshotDir
    }

    /// The cases to run, in run order. Unset runs everything (`snapshots` only with a directory);
    /// a comma-separated list runs just those, ignoring spaces and letter case around each name.
    public static func resolve(only: String?, snapshotDir: String?) throws -> [Case] {
        let hasDirectory = directory(snapshotDir) != nil
        guard let only else {
            return Case.allCases.filter { $0 != .snapshots || hasDirectory }
        }
        let names = Set(only.split(separator: ",")
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
            .filter { !$0.isEmpty })
        guard !names.isEmpty else { throw ValidationError.emptySelection }
        let unknown = names.filter { Case(rawValue: $0) == nil }
        guard unknown.isEmpty else { throw ValidationError.unknownCases(unknown) }
        let selected = Case.allCases.filter { names.contains($0.rawValue) }
        guard !selected.contains(.snapshots) || hasDirectory else {
            throw ValidationError.snapshotsRequireDirectory
        }
        return selected
    }
}
