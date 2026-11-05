import Foundation
import XCTest
@testable import SpeakeasyCore

final class ClientIdentityTests: XCTestCase {
    private let ios27 = OperatingSystemVersion(majorVersion: 27, minorVersion: 0, patchVersion: 0)

    func testHeaderNamesPlatformOSBuildAndDeviceClass() {
        XCTAssertEqual(ClientIdentity.header(platform: "iOS", osVersion: ios27, appVersion: "1.4", build: "60",
                                             deviceClass: "iPhone"), "iOS 27.0; app 1.4 (60); iPhone")
        let mac = OperatingSystemVersion(majorVersion: 27, minorVersion: 1, patchVersion: 2)
        XCTAssertEqual(ClientIdentity.header(platform: "macOS", osVersion: mac, appVersion: "0.3.15", build: "52",
                                             deviceClass: "Mac"), "macOS 27.1.2; app 0.3.15 (52); Mac")
        XCTAssertEqual(ClientIdentity.header(platform: "visionOS", osVersion: ios27, appVersion: "1.4", build: "60",
                                             deviceClass: "Apple Vision Pro"), "visionOS 27.0; app 1.4 (60); Apple Vision Pro")
    }

    func testOddFieldsAreCleanedOrDropped() {
        XCTAssertNil(ClientIdentity.header(platform: "Android", osVersion: ios27, appVersion: "1", build: "1", deviceClass: "x"))
        XCTAssertNil(ClientIdentity.header(platform: "iOS", osVersion: ios27, appVersion: "", build: "60", deviceClass: "iPhone"))
        XCTAssertEqual(ClientIdentity.header(platform: "iOS", osVersion: ios27, appVersion: "1.4; evil", build: "6 0",
                                             deviceClass: "Geo's iPhone 17,1"), "iOS 27.0; app 1.4evil (60); Geos iPhone")
        XCTAssertEqual(ClientIdentity.header(platform: "iOS", osVersion: ios27, appVersion: "1.4", build: "60",
                                             deviceClass: "17"), "iOS 27.0; app 1.4 (60)")
    }

    func testDeviceClassKeepsOnlyTheGenericWord() {
        XCTAssertEqual(ClientIdentity.deviceClass(fromMachine: "iPhone17,1"), "iPhone")
        XCTAssertEqual(ClientIdentity.deviceClass(fromMachine: "iPad14,3"), "iPad")
        XCTAssertEqual(ClientIdentity.deviceClass(fromMachine: "arm64"), "arm")
    }
}
