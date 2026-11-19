import XCTest
@testable import SpeakeasyCore

/// The ready screen's folded row says what it holds and what it's set to, so pressing Start a call
/// never acts on a choice the user can't see.
final class ReadyCopyTests: XCTestCase {
    func testTitleNamesWhatsInside() {
        XCTAssertEqual(ReadyCopy.moreTitle(homeOffered: true), "Finished work and home control")
        XCTAssertEqual(ReadyCopy.moreTitle(homeOffered: false), "Where finished work goes")
    }

    func testSummaryNoDestinationIsThisMacOnly() {
        XCTAssertEqual(ReadyCopy.moreSummary(destination: nil, homeOn: nil), "This Mac only")
        XCTAssertEqual(ReadyCopy.moreSummary(destination: nil, homeOn: true), "This Mac only, home control on")
    }

    func testSummaryHomeNotOfferedSaysNothingAboutHome() {
        let s = ReadyCopy.moreSummary(destination: "Discord · #updates", homeOn: nil)
        XCTAssertEqual(s, "Discord · #updates")
        XCTAssertFalse(s.lowercased().contains("home"))
    }

    func testSummaryHomeOnAndOff() {
        XCTAssertEqual(ReadyCopy.moreSummary(destination: "Discord · #updates", homeOn: true),
                       "Discord · #updates, home control on")
        XCTAssertEqual(ReadyCopy.moreSummary(destination: "Telegram · Notes", homeOn: false),
                       "Telegram · Notes, home control off")
    }

    func testDestinationLabelMatchesThePicker() {
        let ds = [Destination(target: "discord:1001", label: "Discord · #updates"),
                  Destination(target: "telegram:2002", label: "Telegram · Notes")]
        XCTAssertNil(ReadyCopy.destinationLabel(target: nil, destinations: ds))
        XCTAssertEqual(ReadyCopy.destinationLabel(target: "telegram:2002", destinations: ds), "Telegram · Notes")
        XCTAssertEqual(ReadyCopy.destinationLabel(target: "slack:9", destinations: ds), "slack:9",
                       "a target the server no longer lists shows as the picker shows it")
    }

    func testHomeDetailCountsDevices() {
        XCTAssertEqual(ReadyCopy.homeDetail(devices: 0), "No devices picked yet · choose which in Settings › Home")
        XCTAssertEqual(ReadyCopy.homeDetail(devices: 1), "1 device · choose which in Settings › Home")
        XCTAssertEqual(ReadyCopy.homeDetail(devices: 3), "3 devices · choose which in Settings › Home")
    }
}
