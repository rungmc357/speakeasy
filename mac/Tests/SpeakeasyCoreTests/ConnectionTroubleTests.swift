import XCTest
@testable import SpeakeasyCore

final class ConnectionTroubleTests: XCTestCase {
    let tailnet = URL(string: "https://hermes.example.ts.net:8795")!
    let other = URL(string: "http://127.0.0.1:8795")!

    func err(_ code: Int) -> Error { NSError(domain: NSURLErrorDomain, code: code) }

    override func tearDown() { TailnetInterface.override = nil }

    func testHostNotFoundOnTailnetPointsAtTailscale() {
        TailnetInterface.override = false
        let m = ConnectionTrouble.message(for: err(NSURLErrorCannotFindHost), server: tailnet)
        XCTAssertTrue(ConnectionTrouble.suggestsTailscale(m), m)
    }

    /// Regression: the "turn on Tailscale" banner while Tailscale was on. A timeout is a slow Hermes,
    /// and with this device's tunnel up nothing is blamed on Tailscale.
    func testNeverBlamesTailscaleWhenItIsUpOrOnATimeout() {
        TailnetInterface.override = false
        XCTAssertFalse(ConnectionTrouble.suggestsTailscale(
            ConnectionTrouble.message(for: err(NSURLErrorTimedOut), server: tailnet)))
        TailnetInterface.override = true
        for code in [NSURLErrorCannotFindHost, NSURLErrorCannotConnectToHost, NSURLErrorNetworkConnectionLost] {
            XCTAssertFalse(ConnectionTrouble.suggestsTailscale(ConnectionTrouble.message(for: err(code), server: tailnet)))
        }
    }

    func testTailnetAddressRange() {
        XCTAssertTrue(TailnetInterface.isTailnetIPv4(0x6440_0001))    // 100.64.0.1
        XCTAssertTrue(TailnetInterface.isTailnetIPv4(0x647F_FFFE))    // 100.127.255.254
        XCTAssertFalse(TailnetInterface.isTailnetIPv4(0x6480_0001))   // 100.128.0.1
        XCTAssertFalse(TailnetInterface.isTailnetIPv4(0xC0A8_0101))   // 192.168.1.1
    }

    func testReachabilityGateNeedsASustainedOutage() {
        var gate = ReachabilityGate()
        let t0 = Date(timeIntervalSince1970: 0)
        XCTAssertFalse(gate.record(.noAnswer, at: t0))
        XCTAssertFalse(gate.record(.noAnswer, at: t0 + 5))
        XCTAssertFalse(gate.record(.noAnswer, at: t0 + 10))
        XCTAssertTrue(gate.record(.noAnswer, at: t0 + 25))
        XCTAssertFalse(gate.record(.answered, at: t0 + 26))
    }

    func testOfflineDeviceSaysOffline() {
        let m = ConnectionTrouble.message(for: err(NSURLErrorNotConnectedToInternet), server: tailnet)
        XCTAssertTrue(m.contains("offline"), m)
        XCTAssertFalse(ConnectionTrouble.suggestsTailscale(m))
    }

    func testNonTailnetServerDoesNotBlameTailscale() {
        let m = ConnectionTrouble.message(for: err(NSURLErrorCannotFindHost), server: other)
        XCTAssertFalse(ConnectionTrouble.suggestsTailscale(m), m)
    }

    func testOtherErrorsKeepTheirWording() {
        let e = NSError(domain: "Speakeasy", code: 1, userInfo: [NSLocalizedDescriptionKey: "Server said no"])
        XCTAssertEqual(ConnectionTrouble.message(for: e, server: tailnet), "Server said no")
    }
}
