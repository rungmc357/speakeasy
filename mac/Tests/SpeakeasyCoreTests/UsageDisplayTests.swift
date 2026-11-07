import XCTest
@testable import SpeakeasyCore

final class UsageDisplayTests: XCTestCase {
    func testMissingPreferenceIsOff() {
        let name = "usage-tests-\(UUID().uuidString)"
        let store = UserDefaults(suiteName: name)!
        defer { store.removePersistentDomain(forName: name) }
        XCTAssertFalse(UsageDisplay.isOn(in: store))
    }

    func testChoicePersistsAcrossInstancesAndCanBeTurnedOff() {
        let name = "usage-tests-\(UUID().uuidString)"
        let store = UserDefaults(suiteName: name)!
        defer { store.removePersistentDomain(forName: name) }
        store.set(true, forKey: UsageDisplay.key)
        XCTAssertTrue(UsageDisplay.isOn(in: UserDefaults(suiteName: name)!))
        store.set(false, forKey: UsageDisplay.key)
        XCTAssertFalse(UsageDisplay.isOn(in: UserDefaults(suiteName: name)!))
    }

    func testPreferenceDoesNotLeakToAnotherDeviceStore() {
        let first = "usage-tests-\(UUID().uuidString)"
        let second = "usage-tests-\(UUID().uuidString)"
        let a = UserDefaults(suiteName: first)!, b = UserDefaults(suiteName: second)!
        defer { a.removePersistentDomain(forName: first); b.removePersistentDomain(forName: second) }
        a.set(true, forKey: UsageDisplay.key)
        XCTAssertTrue(UsageDisplay.isOn(in: a))
        XCTAssertFalse(UsageDisplay.isOn(in: b))
    }
}
