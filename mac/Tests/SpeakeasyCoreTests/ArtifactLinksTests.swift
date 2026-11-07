import XCTest
@testable import SpeakeasyCore

/// Artifact cards v1: decoding, links built from ids (never a scheme from the server), the one-time
/// open-in choice, and what the job view and Home draw. Invented data.
final class ArtifactLinksTests: XCTestCase {
    private func item(_ raw: [String: Any]) -> JSONValue { JSONValue(any: raw) }

    func testArtifactKindsDecodeAndSummarize() throws {
        let explore = try XCTUnwrap(ViewCard(json: ["kind": "explore", "title": "Weekend", "sections": [
            ["label": "Hotels", "entity": "stay", "items": [["name": "Casa do Rio"], ["name": "Hotel Lume"]]]]], number: 1))
        XCTAssertEqual(explore.summary, "Weekend, Casa do Rio, Hotel Lume")
        let compare = try XCTUnwrap(ViewCard(json: ["kind": "compare", "title": "Desks", "winner": 1, "why": "Steadiest",
                                                    "options": [["name": "Riser One"], ["name": "Lift Pro"]]], number: 2))
        XCTAssertEqual(compare.summary, "Desks, My pick: Lift Pro, Steadiest")
        let status = try XCTUnwrap(ViewCard(json: ["kind": "status", "scope": "Servers", "items": [
            ["name": "runner-a", "state": "ok", "value": "41"], ["name": "runner-b", "state": "warn"]]], number: 3))
        XCTAssertEqual(status.summary, "Servers, runner-a 41, runner-b warn")
        let draft = try XCTUnwrap(ViewCard(json: ["kind": "draft", "channel": "email", "to": "Sam Rivera",
                                                  "subject": "Thursday", "body": "Hi Sam"], number: 4))
        XCTAssertEqual(draft.summary, "Draft to Sam Rivera, Thursday")
        XCTAssertEqual(CardText.plain(draft), "Thursday\n\nHi Sam")
    }

    func testMusicLinksUseIdsElseASearchAndNeverGuess() {
        let album = item(["name": "Night Garden", "creator": "The Paper Kites", "media_kind": "album",
                          "ids": ["apple_music": "1440857781", "spotify": "4aawyAB9vmqN3uQ7FjRGTy"]])
        XCTAssertEqual(ArtifactLinks.music(album, app: .appleMusic)?.absoluteString, "https://music.apple.com/us/album/1440857781")
        XCTAssertEqual(ArtifactLinks.music(album, app: .spotify)?.absoluteString, "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy")
        let bare = item(["name": "Night Garden", "creator": "The Paper Kites", "media_kind": "song"])
        XCTAssertEqual(ArtifactLinks.music(bare, app: .appleMusic)?.absoluteString,
                       "https://music.apple.com/us/search?term=Night%20Garden%20The%20Paper%20Kites")
        XCTAssertEqual(ArtifactLinks.music(bare, app: .spotify)?.absoluteString,
                       "https://open.spotify.com/search/Night%20Garden%20The%20Paper%20Kites")
        XCTAssertTrue(ArtifactLinks.isMusic(bare, entity: "media"))
        XCTAssertFalse(ArtifactLinks.isMusic(item(["name": "A talk", "media_kind": "podcast"]), entity: "media"))
    }

    func testMapsNeedsAPlaceNotABareName() {
        XCTAssertNil(ArtifactLinks.maps(item(["name": "Taberna Azul"]), app: .apple))
        let pinned = item(["name": "Casa do Rio", "lat": 38.71, "lon": -9.14])
        XCTAssertEqual(ArtifactLinks.maps(pinned, app: .apple)?.absoluteString, "https://maps.apple.com/?ll=38.71,-9.14&q=Casa%20do%20Rio")
        XCTAssertEqual(ArtifactLinks.maps(pinned, app: .google)?.absoluteString,
                       "https://www.google.com/maps/search/?api=1&query=38.71,-9.14")
        let addressed = item(["name": "Taberna Azul", "address": "12 Rua Nova, Lisbon"])
        XCTAssertEqual(ArtifactLinks.maps(addressed, app: .google)?.absoluteString,
                       "https://www.google.com/maps/search/?api=1&query=Taberna%20Azul,%2012%20Rua%20Nova,%20Lisbon")
        XCTAssertEqual(ArtifactLinks.maps(item(["name": "Bean Bar", "city": "Austin"]), app: .apple)?.absoluteString,
                       "https://maps.apple.com/?q=Bean%20Bar,%20Austin")
    }

    func testSearchTermsCannotBreakTheQuery() {
        let odd = item(["name": "Salt & Pepper", "city": "Austin"])
        let url = ArtifactLinks.maps(odd, app: .google)?.absoluteString ?? ""
        XCTAssertTrue(url.hasSuffix("query=Salt%20%26%20Pepper,%20Austin"), url)
    }

    func testCallNeedsARealNumber() {
        XCTAssertEqual(ArtifactLinks.call(item(["phone": "+1 (512) 555-0134"]))?.absoluteString, "tel:+15125550134")
        XCTAssertNil(ArtifactLinks.call(item(["phone": "ask"])))
    }

    func testOpenInChoiceIsRememberedAndClearable() throws {
        let d = try XCTUnwrap(UserDefaults(suiteName: "artifact-tests-\(UUID().uuidString)"))
        XCTAssertNil(OpenInPrefs.music(d))
        OpenInPrefs.setMusic(.spotify, d)
        XCTAssertEqual(OpenInPrefs.music(d), .spotify)
        OpenInPrefs.setMusic(nil, d)
        XCTAssertNil(OpenInPrefs.music(d))
        OpenInPrefs.setMaps(.google, d)
        XCTAssertEqual(OpenInPrefs.maps(d), .google)
        d.set("waze", forKey: OpenInPrefs.mapsKey)
        XCTAssertNil(OpenInPrefs.maps(d))   // an unknown value asks again
    }

    func testIconTilesHaveASymbolForEveryEntity() {
        for entity in ["place", "stay", "reservation", "flight", "event", "product", "media", "person", "article", "pr",
                       "listing", "generation", "task"] {
            XCTAssertFalse(ArtifactLinks.symbol(entity).isEmpty, entity)
        }
        XCTAssertEqual(ArtifactLinks.symbol("media", item: item(["media_kind": "podcast"])), "mic.fill")
    }

    func testHomeGlanceAndJobViewKnowArtifacts() {
        let compare = ViewCard(kind: "compare", data: ["winner": 0, "why": "x", "options": [["name": "A"], ["name": "B"]]])
        let badCompare = ViewCard(kind: "compare", data: ["winner": 4, "options": [["name": "A"]]])
        XCTAssertTrue(JobAnswer.hasGlance(compare))
        XCTAssertFalse(JobAnswer.hasGlance(badCompare))
        XCTAssertTrue(JobAnswer.hasGlance(ViewCard(kind: "status", data: ["items": [["name": "a", "state": "ok"]]])))
        XCTAssertTrue(JobAnswer.artifactKinds.isSuperset(of: ["explore", "compare", "status", "draft"]))
        XCTAssertTrue(ViewCard.drawn.isSuperset(of: JobAnswer.artifactKinds))
    }

    func testTheOtherFourKindsSummariseAndCopyInPlainWords() {
        let tl = ViewCard(kind: "timeline", data: ["title": "Porto trip", "days": [["label": "Fri 12", "items": [["when": "9:40 AM", "what": "Land"]]]]])
        XCTAssertEqual(tl.summary, "Porto trip, Fri 12: 9:40 AM Land")
        XCTAssertTrue(CardText.plain(tl).contains("9:40 AM · Land"))
        let rows = ViewCard(kind: "rows", data: ["items": [["ref": "#41", "name": "Fix the login timeout", "state": "CI passing"]]])
        XCTAssertEqual(rows.summary, "#41 Fix the login timeout CI passing")
        let metric = ViewCard(kind: "metric", data: ["label": "Cloud spend", "value": "$1,260", "delta": "+18%"])
        XCTAssertEqual(metric.summary, "Cloud spend, $1,260, +18%")
        XCTAssertTrue(JobAnswer.hasGlance(metric))
        XCTAssertFalse(JobAnswer.hasGlance(ViewCard(kind: "metric", data: ["label": "x"])))
        let album = ViewCard(kind: "preview", data: ["entity": "media", "media_kind": "album", "title": "Night Garden",
                                                     "creator": "The Paper Kites", "list": ["Opening"]])
        XCTAssertEqual(album.summary, "Night Garden, by The Paper Kites")
        XCTAssertTrue(CardText.plain(album).contains("1. Opening"))
        for kind in ["timeline", "rows", "metric", "preview"] { XCTAssertTrue(JobAnswer.artifactKinds.contains(kind)) }
    }

    func testPreviewPlaysByItsTitleAndCreator() throws {
        let card = ViewCard(kind: "preview", data: ["entity": "media", "media_kind": "album", "title": "Night Garden",
                                                    "creator": "The Paper Kites", "ids": ["spotify": "4aawyAB9vmqN3uQ7FjRGTy"]])
        let it = ArtifactLinks.previewItem(card.data)
        XCTAssertTrue(ArtifactLinks.isMusic(it, entity: "media"))
        XCTAssertEqual(ArtifactLinks.music(it, app: .spotify)?.absoluteString, "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy")
        let search = try XCTUnwrap(ArtifactLinks.music(it, app: .appleMusic)?.absoluteString)
        XCTAssertTrue(search.hasPrefix("https://music.apple.com/us/search?term=Night%20Garden%20The%20Paper%20Kites"), search)
        for entity in ["paper", "contract", "file", "issue", "email", "bill", "alert", "day"] {
            XCTAssertNotEqual(ArtifactLinks.symbol(entity), "mappin.and.ellipse", entity)
        }
    }
}
