import XCTest
@testable import SpeakeasyCore

/// SPK-304: an email address spelled out on a call reads as the address, not letter by letter.
final class SpokenEmailTests: XCTestCase {
    func testSpelledLettersBecomeTheAddress() {
        XCTAssertEqual(SpokenEmail.collapsed("Send it to R-I-V K A L O S A at gmail dot com please."),
                       "Send it to rivkalosa@gmail.com please.")
        XCTAssertEqual(SpokenEmail.collapsed("It's J O at example.org"), "It's jo@example.org")
        XCTAssertEqual(SpokenEmail.collapsed("send a K A B at fastmail dot com"), "send a kab@fastmail.com")
    }

    func testSpokenWordsAndJoiners() {
        XCTAssertEqual(SpokenEmail.collapsed("email sam dot lee at example dot co dot uk"),
                       "email sam.lee@example.co.uk")
        XCTAssertEqual(SpokenEmail.collapsed("mira underscore 42 at mail dot example dot com"),
                       "mira_42@mail.example.com")
        XCTAssertEqual(SpokenEmail.collapsed("Pat at proton dot me works."), "pat@proton.me works.")
    }

    func testOrdinarySentencesStayAsSaid() {
        for said in ["Look at google dot com for it.", "I work at example.com now.", "Meet me at noon.",
                     "We'll stay at the hotel dot com rate.", "Check it at 5 or so.", "No address here",
                     "already sam@example.com"] {
            XCTAssertEqual(SpokenEmail.collapsed(said), said, said)
        }
    }

    func testCallTranscriptShowsTheAddressButKeepsTypedTextAndRawWords() {
        let said = TranscriptTurn(id: 1, speaker: .you, text: "it's T E S at example dot com")
        XCTAssertEqual(said.cleanedText, "it's tes@example.com")
        XCTAssertEqual(said.text, "it's T E S at example dot com", "what the voice heard is unchanged")
        let typed = TranscriptTurn(typed: "T E S at example dot com", id: 2)
        XCTAssertEqual(typed.cleanedText, "T E S at example dot com", "typed words are shown as typed")
    }
}
