import XCTest
@testable import SpeakeasyCore

final class WelcomeJourneyTests: XCTestCase {
    func testFirstLaunchStartsWithTalk() {
        let journey = WelcomeJourney()
        XCTAssertEqual(journey.page, 0)
        XCTAssertEqual(journey.route, .intro)
        XCTAssertEqual(WelcomeJourney.headlines[0], "Talk to your agent.")
    }
    func testSeenLaunchStartsAtChoices() {
        XCTAssertEqual(WelcomeJourney(seen: true).page, 3)
        XCTAssertEqual(WelcomeJourney(seen: true).route, .intro)
    }
    func testPairedEntrySkipsWelcome() {
        XCTAssertEqual(WelcomeJourney(paired: true).route, .setup)
    }
    func testForwardVisitsEachPageBeforeSetup() {
        var journey = WelcomeJourney()
        for expected in 1...3 {
            journey.forward()
            XCTAssertEqual(journey.page, expected)
            XCTAssertEqual(journey.route, .intro)
        }
        journey.forward()
        XCTAssertEqual(journey.route, .setup)
    }
    func testBackClampsAndReturnsFromNewcomer() {
        var journey = WelcomeJourney()
        journey.back()
        XCTAssertEqual(journey.page, 0)
        journey.route = .newcomer
        journey.back()
        XCTAssertEqual(journey.page, 3)
        XCTAssertEqual(journey.route, .intro)
    }
    func testSkipAlwaysReachesChoicesNotAnInstall() {
        for page in 0..<4 {
            var journey = WelcomeJourney()
            journey.page = page
            journey.skip()
            XCTAssertEqual(journey.page, 3)
            XCTAssertEqual(journey.route, .intro)
        }
    }
    func testForwardNeverStartsSetupWhileReadingNewcomer() {
        var journey = WelcomeJourney()
        journey.route = .newcomer
        journey.forward()
        XCTAssertEqual(journey.route, .newcomer)
    }
    func testCopyIsOfficialInstallOnlyAndAllPagesHaveCopy() {
        XCTAssertEqual(WelcomeJourney.headlines.count, 4)
        XCTAssertEqual(WelcomeJourney.lines.count, 4)
        XCTAssertEqual(WelcomeJourney.installCommand, "curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash")
    }
}
