import Foundation

/// Resolves executable panel-smoke cases before the native client is created.
public enum PanelSmokeSelection {
    private static let cases: Set<String> = [
        "work-scroll", "work-status", "transcript-scroll", "drag", "ended",
        "look-at-this", "overflow", "snapshots",
    ]

    public enum ValidationError: Error, CustomStringConvertible {
        case emptySelection
        case unknownCases(Set<String>)
        case snapshotsRequireDirectory

        public var description: String {
            switch self {
            case .emptySelection:
                return "SPEAKEASY_PANEL_SMOKE_ONLY must select at least one case"
            case .unknownCases(let names):
                return "Unknown panel-smoke cases: " + names.sorted().joined(separator: ", ")
            case .snapshotsRequireDirectory:
                return "The snapshots case requires --snapshot-dir DIR"
            }
        }
    }

    public static func resolve(only: String?, snapshotDir: String?) throws -> Set<String> {
        let hasDirectory = snapshotDir.map { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty } ?? false
        guard let only else {
            return snapshotDir != nil ? cases : cases.subtracting(["snapshots"])
        }
        let selected = Set(only.split(separator: ",").map(String.init))
        guard !selected.isEmpty else { throw ValidationError.emptySelection }
        let unknown = selected.subtracting(cases)
        guard unknown.isEmpty else { throw ValidationError.unknownCases(unknown) }
        guard !selected.contains("snapshots") || hasDirectory else {
            throw ValidationError.snapshotsRequireDirectory
        }
        return selected
    }
}
