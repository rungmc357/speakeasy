import XCTest
@testable import SpeakeasyCore

/// SPK-139: "paired with Hermes on Studio Mac", never an address or a host name.
final class ComputerLabelTests: XCTestCase {
    func testNamesTheComputer() {
        XCTAssertEqual(ServerStatus.computerLabel("Studio Mac"), "Studio Mac")
        XCTAssertEqual(ServerStatus.computerLabel("  Office   iMac "), "Office iMac")
    }

    func testNeverAnAddressOrHostName() {
        for raw in ["127.0.0.1", "::1", "192.168.1.20", "localhost", "hermes.example-tailnet.ts.net",
                    "calm-river-sound-demo.trycloudflare.com", "", "   "] {
            XCTAssertEqual(ServerStatus.computerLabel(raw), "your Hermes computer", raw)
        }
        XCTAssertEqual(ServerStatus.computerLabel(nil), "your Hermes computer")
        XCTAssertNil(ServerStatus.plainComputerName("127.0.0.1"))
        XCTAssertEqual(ServerStatus.plainComputerName("Studio Mac"), "Studio Mac")
    }

    func testDecodesFromStatus() throws {
        let named = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"computer_name": "Studio Mac"}"#.utf8))
        XCTAssertEqual(named.computerLabel, "Studio Mac")
        let older = try JSONDecoder().decode(ServerStatus.self, from: Data("{}".utf8))
        XCTAssertNil(older.computerName)
        XCTAssertEqual(older.computerLabel, "your Hermes computer")
    }
}
