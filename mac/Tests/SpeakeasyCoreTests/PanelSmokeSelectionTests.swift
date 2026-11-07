import XCTest
@testable import SpeakeasyCore

final class PanelSmokeSelectionTests: XCTestCase {
    private let regular: Set<String> = ["work-scroll", "work-status", "transcript-scroll", "drag",
                                         "ended", "look-at-this", "overflow"]

    func testUnsetPreservesDefaultCasesWithoutSnapshots() throws {
        XCTAssertEqual(try PanelSmokeSelection.resolve(only: nil, snapshotDir: nil), regular)
    }

    func testUnsetIncludesSnapshotsWithDirectory() throws {
        XCTAssertEqual(try PanelSmokeSelection.resolve(only: nil, snapshotDir: "out"), regular.union(["snapshots"]))
    }

    func testExplicitEmptySelectionIsRejected() {
        for value in ["", ",", ",,,", "  \n"] {
            XCTAssertThrowsError(try PanelSmokeSelection.resolve(only: value, snapshotDir: nil), value)
        }
    }

    func testUnknownOnlySelectionIsRejected() {
        XCTAssertThrowsError(try PanelSmokeSelection.resolve(only: "bogus", snapshotDir: nil))
    }

    func testMixedUnknownSelectionIsRejected() {
        XCTAssertThrowsError(try PanelSmokeSelection.resolve(only: "overflow,bogus", snapshotDir: nil))
    }

    func testExplicitSnapshotsRequiresDirectory() {
        for directory: String? in [nil, "", "  \n"] {
            XCTAssertThrowsError(try PanelSmokeSelection.resolve(only: "snapshots", snapshotDir: directory))
            XCTAssertThrowsError(try PanelSmokeSelection.resolve(only: "overflow,snapshots", snapshotDir: directory))
        }
    }

    func testSnapshotsWithDirectoryIsExecutable() throws {
        XCTAssertEqual(try PanelSmokeSelection.resolve(only: "snapshots", snapshotDir: "out"), ["snapshots"])
    }

    func testExplicitSubsetAndDuplicates() throws {
        XCTAssertEqual(try PanelSmokeSelection.resolve(only: "overflow,drag,overflow", snapshotDir: nil), ["overflow", "drag"])
    }

    func testEachRegularCaseIsExecutableWithoutDirectory() throws {
        for name in regular {
            XCTAssertEqual(try PanelSmokeSelection.resolve(only: name, snapshotDir: nil), [name])
        }
    }

    func testUnsetPreservesDirectoryPresenceRule() throws {
        for directory in ["", "  "] {
            XCTAssertEqual(try PanelSmokeSelection.resolve(only: nil, snapshotDir: directory), regular.union(["snapshots"]))
        }
    }
}
