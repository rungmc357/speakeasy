import XCTest
@testable import SpeakeasyCore

final class PanelSmokeSelectionTests: XCTestCase {
    private typealias Case = PanelSmokeSelection.Case
    private let regular: [Case] = [.workScroll, .workStatus, .transcriptScroll, .drag, .ended, .lookAtThis]

    private func assertRejects(_ only: String?, snapshotDir: String? = nil,
                               _ expected: PanelSmokeSelection.ValidationError,
                               file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertThrowsError(try PanelSmokeSelection.resolve(only: only, snapshotDir: snapshotDir),
                             "\(String(describing: only)), \(String(describing: snapshotDir))", file: file, line: line) {
            XCTAssertEqual($0 as? PanelSmokeSelection.ValidationError, expected, file: file, line: line)
        }
    }

    func testUnsetPreservesDefaultCasesWithoutSnapshots() throws {
        XCTAssertEqual(try PanelSmokeSelection.resolve(only: nil, snapshotDir: nil), regular)
    }

    func testUnsetIncludesSnapshotsWithDirectory() throws {
        XCTAssertEqual(try PanelSmokeSelection.resolve(only: nil, snapshotDir: "out"), regular + [.snapshots])
    }

    func testUnsetSkipsSnapshotsWithBlankDirectory() throws {
        for directory in ["", "  "] {
            XCTAssertEqual(try PanelSmokeSelection.resolve(only: nil, snapshotDir: directory), regular)
        }
    }

    func testExplicitEmptySelectionIsRejected() {
        for value in ["", ",", ",,,", "  \n", " , "] {
            assertRejects(value, .emptySelection)
        }
    }

    func testUnknownOnlySelectionIsRejected() {
        assertRejects("bogus", .unknownCases(["bogus"]))
    }

    func testMixedUnknownSelectionIsRejected() {
        assertRejects("drag,bogus", .unknownCases(["bogus"]))
    }

    func testRemovedOverflowCaseIsRejected() {
        assertRejects("overflow", .unknownCases(["overflow"]))
    }

    func testUnknownMessageListsKnownCases() {
        let message = PanelSmokeSelection.ValidationError.unknownCases(["bogus"]).description
        XCTAssertTrue(message.contains("bogus"))
        for smokeCase in Case.allCases { XCTAssertTrue(message.contains(smokeCase.rawValue), smokeCase.rawValue) }
    }

    func testExplicitSnapshotsRequiresDirectory() {
        for directory: String? in [nil, "", "  \n"] {
            assertRejects("snapshots", snapshotDir: directory, .snapshotsRequireDirectory)
            assertRejects("drag,snapshots", snapshotDir: directory, .snapshotsRequireDirectory)
        }
    }

    func testSnapshotsWithDirectoryIsExecutable() throws {
        XCTAssertEqual(try PanelSmokeSelection.resolve(only: "snapshots", snapshotDir: "out"), [.snapshots])
    }

    func testExplicitSubsetRunsInRunOrderWithoutDuplicates() throws {
        XCTAssertEqual(try PanelSmokeSelection.resolve(only: "ended,drag,ended", snapshotDir: nil), [.drag, .ended])
    }

    func testSpacesAndLetterCaseAreIgnored() throws {
        XCTAssertEqual(try PanelSmokeSelection.resolve(only: " Look-At-This, drag ,", snapshotDir: nil), [.drag, .lookAtThis])
    }

    func testEachRegularCaseIsExecutableWithoutDirectory() throws {
        for smokeCase in regular {
            XCTAssertEqual(try PanelSmokeSelection.resolve(only: smokeCase.rawValue, snapshotDir: nil), [smokeCase])
        }
    }

    func testBlankDirectoryIsNoDirectory() {
        XCTAssertNil(PanelSmokeSelection.directory(nil))
        XCTAssertNil(PanelSmokeSelection.directory(""))
        XCTAssertNil(PanelSmokeSelection.directory(" \n"))
        XCTAssertEqual(PanelSmokeSelection.directory("out"), "out")
    }
}
