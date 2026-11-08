import XCTest
@testable import SpeakeasyCore

/// Home's chip is the work model (the model jobs from Home run on); the coordinator's model is a
/// separate, secondary choice. Invented model ids.
final class HomeWorkModelTests: XCTestCase {
    func testStatusReadsWorkAndCoordinatorSeparately() {
        let json: [String: Any] = [
            "model": ["id": "acme/fast-1", "name": "Fast 1"], "source": "auto",
            "coordinator": ["model": ["id": "acme/fast-1", "name": "Fast 1"], "source": "auto",
                            "auto": ["id": "acme/fast-1", "name": "Fast 1"], "options": [["id": "acme/fast-1", "name": "Fast 1"]]],
            "work": ["model": ["id": "acme/big-3", "name": "Big 3"], "source": "default",
                     "default": ["id": "acme/big-3", "name": "Big 3"],
                     "options": [["id": "acme/big-3", "name": "Big 3"], ["id": "other/huge-2", "name": "Huge 2"]]],
        ]
        let status = HomeModelStatus(json: json)
        XCTAssertEqual(status.model?.id, "acme/fast-1")
        XCTAssertFalse(status.chosen)
        let work = try? XCTUnwrap(status.work)
        XCTAssertEqual(work?.model?.name, "Big 3")
        XCTAssertEqual(work?.defaultModel?.id, "acme/big-3")
        XCTAssertEqual(work?.options.map(\.id), ["acme/big-3", "other/huge-2"])
        XCTAssertEqual(work?.chosen, false)
    }

    func testOlderPluginHasNoWorkModel() {
        let status = HomeModelStatus(json: ["model": ["id": "acme/fast-1", "name": "Fast 1"], "source": "chosen"])
        XCTAssertNil(status.work)
        XCTAssertTrue(status.chosen)
    }

    func testJobCardCarriesTheModelThatRan() throws {
        let item = try XCTUnwrap(NowItem(json: ["id": "r1", "kind": "working", "title": "Compare two desks",
                                                "model": ["id": "acme/big-3", "name": "Big 3", "provider": "acme"]]))
        XCTAssertEqual(item.workModel?.label, "Big 3")
        let bare = try XCTUnwrap(NowItem(json: ["id": "r2", "kind": "working", "title": "Compare two desks"]))
        XCTAssertNil(bare.workModel)
    }
}
