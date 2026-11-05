import XCTest
@testable import SpeakeasyCore

final class SecureInputTests: XCTestCase {
    private let listJSON = """
    {"requests":[{"id":"sec_abcdefghijklmnopqrstuvwx","kind":"secret","job":"Weather for the trip","run_id":"run_1",
      "session_id":"s1","expires_at":4102444800,"seconds_left":118,"env_var":"OPENWEATHER_API_KEY",
      "label":"OpenWeather API key","prompt":"OpenWeather API key","help":"Get one free","skill":"weather",
      "stored_where":"Saved to your Hermes's private settings as OPENWEATHER_API_KEY. The AI never sees it."},
     {"id":"sec_zyxwvutsrqponmlkjihgfedc","kind":"sudo","job":"Fix SSH","command":"sudo launchctl kickstart -k system/com.openssh.sshd",
      "label":"Your Mac's password","stored_where":"Used once for this command, then forgotten. Speakeasy never stores it.",
      "expires_at":4102444800},
     {"id":"bogus","kind":"secret"}, {"id":"sec_nokindxxxxxxxxxxxxxxxxx"}]}
    """

    func testListParsesBothKindsAndDropsJunk() {
        let list = SecureInputRequest.list(Data(listJSON.utf8))
        XCTAssertEqual(list.map(\.kind), [.secret, .sudo])
        XCTAssertEqual(list[0].envVar, "OPENWEATHER_API_KEY")
        XCTAssertEqual(list[0].title, "Your agent needs your OpenWeather API key")
        XCTAssertEqual(list[0].askedBy, "Weather skill · Weather for the trip")
        XCTAssertEqual(list[0].primaryButton, "Save securely")
        XCTAssertTrue(list[0].whereItGoes.contains("OPENWEATHER_API_KEY"))
        XCTAssertEqual(list[1].command, "sudo launchctl kickstart -k system/com.openssh.sshd")
        XCTAssertEqual(list[1].primaryButton, "Allow once")
        XCTAssertEqual(list[1].skipButton, "Don't allow")
        XCTAssertTrue(list[1].title.hasPrefix("Your agent needs your Mac's password"))
    }

    func testAnswerBodyIsValidJSONForAwkwardValues() throws {
        for value in ["FAKE-key-123", "quote\"and\\slash", "tab\tand ünïcödé ✓"] {
            let body = SecureInputRequest.answerBody(value)
            let object = try JSONSerialization.jsonObject(with: Data(body)) as? [String: String]
            XCTAssertEqual(object, ["value": value])
        }
        XCTAssertEqual(String(decoding: SecureInputRequest.skipBody, as: UTF8.self), "{\"action\":\"skip\"}")
    }

    func testNotificationLineNeverCarriesAValue() {
        let list = SecureInputRequest.list(Data(listJSON.utf8))
        XCTAssertEqual(list[0].notificationLine, "Needs you: Your agent needs your OpenWeather API key")
        XCTAssertFalse(list[1].notificationLine.contains("launchctl"))
    }

    func testEventPayload() {
        let object = try! JSONSerialization.jsonObject(with: Data(listJSON.utf8)) as! [String: Any]
        XCTAssertEqual(SecureInputRequest.list(event: object["requests"]).count, 2)
        XCTAssertEqual(SecureInputRequest.list(event: nil).count, 0)
    }
}
