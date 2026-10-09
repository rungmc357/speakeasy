import Foundation
import XCTest
@testable import SpeakeasyCore

/// Listening mode's rolling room transcript: finished sentences plus one replaceable tail, the last
/// 30 minutes, at most 24,000 Unicode scalars once rendered.
final class RoomTranscriptTests: XCTestCase {
    /// 2026-10-08 14:00:00 UTC.
    let t0 = Date(timeIntervalSince1970: 1_791_468_000)
    var utc: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC")!
        return calendar
    }()

    func testFinalsRenderInOrderWithTimestampsAndTheTailComesLast() {
        var room = RoomTranscript()
        room.appendFinal("Sam says the deadline is Friday the fourteenth.", at: t0)
        room.appendFinal("Priya will send the deck.", at: t0 + 65)
        room.appendFinal("Let's book a table for Thursday.", at: t0 + 130)
        room.setVolatile("and ask Hermes to", at: t0 + 190)
        XCTAssertEqual(room.render(calendar: utc), """
        [14:00] Sam says the deadline is Friday the fourteenth.
        [14:01] Priya will send the deck.
        [14:02] Let's book a table for Thursday.
        [14:03] and ask Hermes to
        """)
        XCTAssertEqual(room.render(calendar: utc, includeVolatile: false).components(separatedBy: "\n").count, 3)
    }

    func testTheNextFinalReplacesTheVolatileTail() {
        var room = RoomTranscript()
        room.appendFinal("First.", at: t0)
        room.setVolatile("we should", at: t0 + 5)
        room.setVolatile("we should ask Hermes", at: t0 + 5)
        XCTAssertEqual(room.volatile?.text, "we should ask Hermes")
        room.appendFinal("We should ask Hermes to book the table.", at: t0 + 5)
        XCTAssertNil(room.volatile)
        XCTAssertEqual(room.render(calendar: utc), "[14:00] First.\n[14:00] We should ask Hermes to book the table.")
    }

    func testAnEmptiedTailLeavesNoBlankLine() {
        var room = RoomTranscript()
        room.appendFinal("Hello there.", at: t0)
        room.setVolatile("uh", at: t0 + 3)
        room.setVolatile("   ", at: t0 + 3)   // the recognizer took its guess back
        XCTAssertNil(room.volatile)
        XCTAssertEqual(room.render(calendar: utc), "[14:00] Hello there.")
        XCTAssertFalse(room.render(calendar: utc).hasSuffix("\n"))
    }

    func testBlankFinalOnlyClearsTheTail() {
        var room = RoomTranscript()
        room.setVolatile("mm", at: t0)
        room.appendFinal(" \n ", at: t0)
        XCTAssertTrue(room.isEmpty)
        XCTAssertEqual(room.render(calendar: utc), "")
    }

    func testEmptyTranscriptRendersNothing() {
        let room = RoomTranscript()
        XCTAssertTrue(room.isEmpty)
        XCTAssertEqual(room.render(calendar: utc), "")
        let snapshot = room.snapshot(now: t0, calendar: utc)
        XCTAssertTrue(snapshot.isEmpty)
        XCTAssertEqual(snapshot.lineCount, 0)
        XCTAssertNil(snapshot.firstHeardAt)
    }

    func testLineBreaksInsideResultsBecomeSpaces() {
        var room = RoomTranscript()
        room.appendFinal("one\ntwo\r\n  three", at: t0)
        XCTAssertEqual(room.render(calendar: utc), "[14:00] one two three")
    }

    func testPruneDropsWhatIsOlderThanThirtyMinutes() {
        var room = RoomTranscript()
        let now = t0 + 3_600
        room.appendFinal("Thirty-one minutes ago.", at: now - 31 * 60)
        room.appendFinal("Twenty-nine minutes ago.", at: now - 29 * 60)
        room.setVolatile("still talking", at: now - 40 * 60)   // the tail is being spoken now; it stays
        room.prune(now: now)
        XCTAssertEqual(room.segments.map(\.text), ["Twenty-nine minutes ago."])
        XCTAssertEqual(room.volatile?.text, "still talking")
    }

    func testPruneCanEmptyEverything() {
        var room = RoomTranscript()
        room.appendFinal("Long ago.", at: t0)
        room.prune(now: t0 + 2 * 3_600)
        XCTAssertTrue(room.segments.isEmpty)
    }

    func testSnapshotAfterFortyFiveMinutesCarriesOnlyTheLastThirty() {
        var room = RoomTranscript()
        for minute in 0..<45 { room.appendFinal("Minute \(minute).", at: t0 + Double(minute) * 60) }
        let snapshot = room.snapshot(now: t0 + 45 * 60, calendar: utc)
        XCTAssertFalse(snapshot.text.contains("Minute 14."))
        XCTAssertTrue(snapshot.text.hasPrefix("[14:15] Minute 15."))
        XCTAssertTrue(snapshot.text.hasSuffix("[14:44] Minute 44."))
        XCTAssertEqual(snapshot.lineCount, 30)
        XCTAssertEqual(snapshot.firstHeardAt, t0 + 15 * 60)
        // Taking a snapshot doesn't change the transcript itself.
        XCTAssertEqual(room.segments.count, 45)
    }

    func testFortyThousandCharactersRenderWithinTheCapKeepingTheNewest() {
        var room = RoomTranscript()
        // 400 lines of 100 characters = 40,000 characters of finals.
        for i in 0..<400 {
            let label = String(format: "%03d ", i)
            room.appendFinal(label + String(repeating: "x", count: 96), at: t0 + Double(i))
        }
        let text = room.render(calendar: utc)
        XCTAssertLessThanOrEqual(text.unicodeScalars.count, RoomTranscript.maxScalars)
        XCTAssertTrue(text.hasSuffix("399 " + String(repeating: "x", count: 96)))
        // Whole lines are dropped from the oldest end; nothing in between goes missing.
        let lines = text.components(separatedBy: "\n")
        let numbers = lines.compactMap { Int($0.dropFirst(8).prefix(3)) }
        XCTAssertEqual(numbers, Array((400 - lines.count)..<400))
        XCTAssertGreaterThan(lines.count, 200)
    }

    func testOneOversizedSegmentIsCutFromTheFront() {
        var room = RoomTranscript()
        let long = "START " + String(repeating: "a", count: 30_000) + " END"
        room.appendFinal(long, at: t0)
        let text = room.render(calendar: utc)
        XCTAssertLessThanOrEqual(text.unicodeScalars.count, RoomTranscript.maxScalars)
        XCTAssertTrue(text.hasPrefix("[14:00] …"))
        XCTAssertTrue(text.hasSuffix(" END"))
        XCTAssertFalse(text.contains("START"))
    }

    func testTheCapCountsUnicodeScalarsNotCharacters() {
        // "é" written as e + combining acute: one Character, two scalars (Python's len() says 2).
        let accented = String(repeating: "e\u{301}", count: 7_000)
        XCTAssertEqual(accented.count, 7_000)
        XCTAssertEqual(accented.unicodeScalars.count, 14_000)
        var room = RoomTranscript()
        room.appendFinal("older " + accented, at: t0)
        room.appendFinal("newer " + accented, at: t0 + 60)
        let text = room.render(calendar: utc)
        // By Characters both lines would fit (about 14,000); by scalars (about 28,000) only one does.
        XCTAssertLessThanOrEqual(text.unicodeScalars.count, RoomTranscript.maxScalars)
        XCTAssertEqual(text.components(separatedBy: "\n").count, 1)
        XCTAssertTrue(text.hasPrefix("[14:01] newer "))
    }

    func testCuttingNeverSplitsALetterFromItsMark() {
        let accented = String(repeating: "e\u{301}", count: 20_000)   // 40,000 scalars
        var room = RoomTranscript()
        room.appendFinal(accented, at: t0)
        let text = room.render(calendar: utc)
        XCTAssertLessThanOrEqual(text.unicodeScalars.count, RoomTranscript.maxScalars)
        let body = text.dropFirst("[14:00] …".count)
        XCTAssertEqual(body.unicodeScalars.first, "e")   // not a dangling combining mark
        XCTAssertTrue(body.allSatisfy { $0 == "e\u{301}" })
    }

    func testStorageStaysBoundedOnLongSessions() {
        var room = RoomTranscript()
        for i in 0..<2_000 { room.appendFinal(String(repeating: "y", count: 99), at: t0 + Double(i)) }
        let stored = room.segments.reduce(0) { $0 + $1.text.unicodeScalars.count }
        XCTAssertLessThanOrEqual(stored, RoomTranscript.maxScalars)
        XCTAssertEqual(room.segments.last?.start, t0 + 1_999)
    }

    func testCommitVolatileKeepsTheWordsAsFinished() {
        var room = RoomTranscript()
        room.setVolatile("book the table for eight", at: t0)
        room.commitVolatile()
        XCTAssertNil(room.volatile)
        XCTAssertEqual(room.segments.map(\.text), ["book the table for eight"])
        room.commitVolatile()   // nothing pending: no-op
        XCTAssertEqual(room.segments.count, 1)
    }

    func testRemoveAllForgetsEverything() {
        var room = RoomTranscript()
        room.appendFinal("Something private.", at: t0)
        room.setVolatile("more", at: t0 + 1)
        room.removeAll()
        XCTAssertTrue(room.isEmpty)
        XCTAssertEqual(room.render(calendar: utc), "")
    }

    func testStampsFollowTheCalendarTimeZone() {
        var room = RoomTranscript()
        room.appendFinal("Hi.", at: t0)
        var tokyo = Calendar(identifier: .gregorian)
        tokyo.timeZone = TimeZone(identifier: "Asia/Tokyo")!
        XCTAssertEqual(room.render(calendar: tokyo), "[23:00] Hi.")
    }

    // MARK: Timeline

    func testTimelineMapsAudioTimeToTheClock() {
        var timeline = RoomTimeline()
        XCTAssertNil(timeline.wallClock(atAudioSeconds: 1))
        timeline.anchor(audioSeconds: 0, wallClock: t0)
        XCTAssertEqual(timeline.wallClock(atAudioSeconds: 12.5), t0 + 12.5)
    }

    func testTimelineSkipsTheGapAfterAMicRestart() {
        var timeline = RoomTimeline()
        timeline.anchor(audioSeconds: 0, wallClock: t0)
        // 60 s of audio, then the mic was gone for 20 s and came back.
        timeline.anchor(audioSeconds: 60, wallClock: t0 + 80)
        XCTAssertEqual(timeline.wallClock(atAudioSeconds: 30), t0 + 30)
        XCTAssertEqual(timeline.wallClock(atAudioSeconds: 65), t0 + 85)
    }
}
