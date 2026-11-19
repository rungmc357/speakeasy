import XCTest
@testable import SpeakeasyCore

final class ThreadFollowTests: XCTestCase {
    func testAtBottomFollowsWhatIsAddedBelow() {
        var f = ThreadFollow()
        XCTAssertEqual(f.report(content: 2000, viewport: 600, distance: 0), .none)
        // A sent message / working card / reply makes the thread taller while the view holds still.
        XCTAssertEqual(f.report(content: 2140, viewport: 600, distance: 140), .scrollToEnd)
        XCTAssertTrue(f.pinned)
    }

    func testScrolledUpStaysPut() {
        var f = ThreadFollow()
        _ = f.report(content: 2000, viewport: 600, distance: 0)
        XCTAssertEqual(f.report(content: 2000, viewport: 600, distance: 900), .none)  // person scrolls up
        XCTAssertFalse(f.pinned)
        XCTAssertEqual(f.report(content: 2200, viewport: 600, distance: 1100), .none)  // a reply arrives
        XCTAssertFalse(f.pinned)
    }

    func testScrollingBackDownFollowsAgain() {
        var f = ThreadFollow()
        _ = f.report(content: 2000, viewport: 600, distance: 0)
        _ = f.report(content: 2000, viewport: 600, distance: 900)
        _ = f.report(content: 2000, viewport: 600, distance: 20)  // within the slack
        XCTAssertTrue(f.pinned)
        XCTAssertEqual(f.report(content: 2100, viewport: 600, distance: 120), .scrollToEnd)
    }

    func testSendingAlwaysShowsTheNewMessage() {
        var f = ThreadFollow()
        _ = f.report(content: 2000, viewport: 600, distance: 0)
        _ = f.report(content: 2000, viewport: 600, distance: 900)
        XCTAssertEqual(f.sent(), .scrollToEnd)
        XCTAssertTrue(f.pinned)
        XCTAssertEqual(f.report(content: 2300, viewport: 600, distance: 300), .scrollToEnd)
    }

    func testOwnScrollAnimationIsNotReadAsScrollingUp() {
        var f = ThreadFollow()
        _ = f.report(content: 2000, viewport: 600, distance: 0)
        _ = f.sent()
        XCTAssertEqual(f.report(content: 2000, viewport: 600, distance: 400, programmatic: true), .none)
        XCTAssertTrue(f.pinned)
    }

    func testWindowResizeAtBottomKeepsTheNewestInView() {
        var f = ThreadFollow()
        _ = f.report(content: 2000, viewport: 600, distance: 0)
        XCTAssertEqual(f.report(content: 2000, viewport: 500, distance: 100), .scrollToEnd)
    }

    func testShortThreadNeedsNoScroll() {
        var f = ThreadFollow()
        XCTAssertEqual(f.report(content: 300, viewport: 600, distance: -300), .none)
        XCTAssertEqual(f.report(content: 400, viewport: 600, distance: -200), .none)
        XCTAssertTrue(f.pinned)
    }
}
