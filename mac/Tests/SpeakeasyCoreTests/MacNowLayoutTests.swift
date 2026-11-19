import XCTest
@testable import SpeakeasyCore

final class MacNowLayoutTests: XCTestCase {
    func testEarlierCollapsesOnlyForHomeBelowOneThousand() {
        XCTAssertFalse(MacNowLayout.showsSidebar(width: 999, nowSupported: true))
        XCTAssertTrue(MacNowLayout.showsSidebar(width: 1000, nowSupported: true))
        XCTAssertTrue(MacNowLayout.showsSidebar(width: 720, nowSupported: false))
    }

    func testNarrowHomeLeavesRoomForTheConversation() {
        // SPK-535: never under 340 (the window minimum is 760), so the composer fits beside the conversation.
        XCTAssertEqual(MacNowLayout.nowWidth(windowWidth: MacNowLayout.windowMinimum.width), 340, accuracy: 0.01)
        XCTAssertGreaterThanOrEqual(MacNowLayout.windowMinimum.width - MacNowLayout.nowWidth(windowWidth: MacNowLayout.windowMinimum.width), 400)
        XCTAssertEqual(MacNowLayout.nowWidth(windowWidth: 900), 396, accuracy: 0.01)
        XCTAssertLessThan(MacNowLayout.nowWidth(windowWidth: 720), 360)
        XCTAssertEqual(MacNowLayout.nowWidth(windowWidth: 1400), 420)
    }

    /// SPK-535: under 1,000 wide, an empty "Pick a session" pane no longer takes half the window.
    func testNarrowHomeFillsTheWindowOnlyWhenNothingIsOpenOnTheRight() {
        XCTAssertTrue(MacNowLayout.homeFillsWindow(sidebar: false, nowSupported: true, detailOpen: false))
        XCTAssertFalse(MacNowLayout.homeFillsWindow(sidebar: false, nowSupported: true, detailOpen: true))
        XCTAssertFalse(MacNowLayout.homeFillsWindow(sidebar: true, nowSupported: true, detailOpen: false))
        XCTAssertFalse(MacNowLayout.homeFillsWindow(sidebar: true, nowSupported: false, detailOpen: false))
    }

    /// SPK-535: a window saved at 620 × 400 by an older build comes back at the minimum, and the
    /// narrow top row starts after the window's close, minimise and zoom buttons.
    func testSavedWindowSmallerThanTheMinimumIsGrown() {
        let fit = MacNowLayout.clampedWindowSize(width: 620, height: 400)
        XCTAssertEqual(fit.width, 760)
        XCTAssertEqual(fit.height, 480)
        let big = MacNowLayout.clampedWindowSize(width: 1200, height: 800)
        XCTAssertEqual(big.width, 1200)
        XCTAssertEqual(big.height, 800)
        XCTAssertGreaterThanOrEqual(MacNowLayout.trafficLightInset, 72)
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
        XCTAssertEqual(MacNowLayout.menuItems(items, onCall: true).map(\.id), ["q1", "q2", "w1", "w2"])
        XCTAssertTrue(MacNowLayout.menuItems([]).isEmpty)
    }

    func testPopoverOffACallAlsoShowsTwoNewAnswers() {
        func item(_ id: String, _ kind: NowItem.Kind) -> NowItem {
            NowItem(id: id, kind: kind, title: "Example", line: "Example activity.")
        }
        let items = [item("a1", .newAnswer), item("q1", .question), item("w1", .working),
                     item("a2", .newAnswer), item("a3", .newAnswer), item("s1", .stopped)]
        XCTAssertEqual(MacNowLayout.menuItems(items).map(\.id), ["q1", "w1", "a1", "a2", "s1"])
    }

    func testPopoverOffACallShowsOneStoppedRowAloneOrMixed() {
        func item(_ id: String, _ kind: NowItem.Kind) -> NowItem {
            NowItem(id: id, kind: kind, title: "Example", line: "Example activity.")
        }
        XCTAssertEqual(MacNowLayout.menuItems([item("s1", .stopped), item("s2", .stopped)]).map(\.id), ["s1"])
        XCTAssertEqual(MacNowLayout.menuItems([item("w1", .working), item("s1", .stopped)]).map(\.id), ["w1", "s1"])
        XCTAssertTrue(MacNowLayout.menuItems([item("s1", .stopped)], onCall: true).isEmpty)
    }

    func testItemPathKeepsThePrefixColonAndEncodesTheRest() {
        XCTAssertEqual(NowItemPath.segment("s:s_a"), "s:s_a")
        XCTAssertEqual(NowItemPath.segment("t:run 1/x?y"), "t:run%201%2Fx%3Fy")
        XCTAssertEqual(NowItemPath.segment("c:a:b"), "c:a%3Ab")
        XCTAssertEqual(NowItemPath.segment("weird:id"), "weird%3Aid")
    }

    func testCallClock() {
        XCTAssertEqual(MacNowLayout.callClock(seconds: 42), "0:42")
        XCTAssertEqual(MacNowLayout.callClock(seconds: 725), "12:05")
        XCTAssertEqual(MacNowLayout.callClock(seconds: 3729), "1:02:09")
        XCTAssertEqual(MacNowLayout.callClock(seconds: -3), "0:00")
    }

    func testCallBarAndPopoverMatchTheDesign() {
        XCTAssertEqual(MacNowLayout.callBarSize.width, 320)
        XCTAssertEqual(MacNowLayout.callBarSize.height, 56)
        XCTAssertEqual(MacNowLayout.popoverWidth, 360)
    }

    // MARK: B73-22: the Mac against a build 73 plugin

    /// The Mac ignores build 73's additive Home state (`stream.current`, `stack`, `recommendations`,
    /// an entry's `current`/`answer` marks): a b73 /voice/now reply gives the Mac exactly what the
    /// same reply without those keys gives it (items, stream, counts, pill, popover rows).
    func testB73HomeStateLeavesWhatTheMacReadsUnchanged() throws {
        let url = try XCTUnwrap(Bundle.module.url(forResource: "now_b73", withExtension: "json", subdirectory: "Contract"))
        let full = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        XCTAssertNotNil(full["stack"]); XCTAssertNotNil(full["recommendations"])
        var older = full
        older.removeValue(forKey: "stack")
        older.removeValue(forKey: "recommendations")
        var stream = try XCTUnwrap(older["stream"] as? [String: Any])
        XCTAssertNotNil(stream["current"])
        stream.removeValue(forKey: "current")
        stream["entries"] = (stream["entries"] as? [[String: Any]] ?? []).map { entry in
            var e = entry; e.removeValue(forKey: "current"); e.removeValue(forKey: "answer"); return e
        }
        older["stream"] = stream

        var b73 = NowHome(json: full)
        let old = NowHome(json: older)
        XCTAssertFalse(b73.items.isEmpty)
        XCTAssertFalse((b73.stream?.entries ?? []).isEmpty)
        XCTAssertTrue(b73.stream?.entries.contains { $0.current } ?? false, "the fixture marks a current exchange")
        // Clear only the b73 fields the Mac never reads; everything else must already match.
        b73.stack = nil
        b73.recommendations = nil
        b73.stream?.current = nil
        if let entries = b73.stream?.entries {
            b73.stream?.entries = entries.map { var e = $0; e.current = false; e.answer = false; return e }
        }
        XCTAssertEqual(b73, old)
        XCTAssertEqual(MacNowLayout.menuItems(NowHome(json: full).items, onCall: false).map(\.id),
                       MacNowLayout.menuItems(old.items, onCall: false).map(\.id))
        XCTAssertEqual(MacNowLayout.menuItems(NowHome(json: full).items, onCall: true).map(\.id),
                       MacNowLayout.menuItems(old.items, onCall: true).map(\.id))
    }
}
