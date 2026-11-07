import XCTest
@testable import SpeakeasyCore

final class MacNowLayoutTests: XCTestCase {
    func testEarlierCollapsesOnlyForHomeBelowOneThousand() {
        XCTAssertFalse(MacNowLayout.showsSidebar(width: 999, nowSupported: true))
        XCTAssertTrue(MacNowLayout.showsSidebar(width: 1000, nowSupported: true))
        XCTAssertTrue(MacNowLayout.showsSidebar(width: 720, nowSupported: false))
    }

    func testNarrowHomeLeavesRoomForTheConversation() {
        XCTAssertEqual(MacNowLayout.nowWidth(windowWidth: 720), 316.8, accuracy: 0.01)
        XCTAssertLessThan(MacNowLayout.nowWidth(windowWidth: 720), 360)
        XCTAssertEqual(MacNowLayout.nowWidth(windowWidth: 1400), 420)
    }

    func testFloatingBarRequiresOpenCallAndHomeNotFrontmost() {
        for enabled in [true, false] {
            for open in [true, false] {
                for front in [true, false] {
                    XCTAssertEqual(MacNowLayout.floats(nowSupported: enabled, callOpen: open, homeFrontmost: front),
                                   enabled && open && !front)
                }
            }
        }
    }

    func testPopoverTakesTwoQuestionsAndTwoWorkingInServerOrder() {
        func item(_ id: String, _ kind: NowItem.Kind) -> NowItem {
            NowItem(id: id, kind: kind, title: "Example", line: "Example activity.")
        }
        let items = [item("finished", .newAnswer), item("q1", .question), item("w1", .working),
                     item("q2", .question), item("q3", .question), item("w2", .waiting),
                     item("w3", .working), item("stopped", .stopped)]
        XCTAssertEqual(MacNowLayout.menuItems(items).map(\.id), ["q1", "q2", "w1", "w2"])
        XCTAssertTrue(MacNowLayout.menuItems([]).isEmpty)
    }
}
