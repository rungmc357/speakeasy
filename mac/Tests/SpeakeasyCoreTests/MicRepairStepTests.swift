import XCTest
@testable import SpeakeasyCore

/// 74.1 (F4(a)): a mic that gives nothing at the start of a call is first restarted in place on the
/// same connection; the connection is reopened only if that can't run or doesn't help.
final class MicRepairStepTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_000)

    private func run(_ check: inout MicCheck, from: TimeInterval, to: TimeInterval, level: Double?,
                     packets: (TimeInterval) -> Int? = { Int($0 * 50) }) -> MicFault? {
        var t = from
        while t <= to {
            if let fault = check.sample(level: level, packetsSent: packets(t), listening: true,
                                        now: t0.addingTimeInterval(t)) { return fault }
            t += 0.25
        }
        return nil
    }

    private func handheld() -> MicCheck {
        var c = MicCheck(firstSoundGrace: MicCheck.firstSoundWithinHandheld, canRestartAudio: true)
        c.connectionOpened()
        return c
    }

    func testNoSoundAtTheStartRestartsTheAudioInPlaceFirst() {
        var c = handheld()
        XCTAssertEqual(run(&c, from: 0, to: 8, level: 0), .noSound)
        XCTAssertEqual(c.repairStep(for: .noSound), .restartAudio, "local restart before any reconnect")
        c.audioRestarted()
        XCTAssertEqual(c.repairs, 0, "an in-place restart is not a reopen")
        XCTAssertEqual(c.health, .checking)
        // The restart brought the sound back: no reconnect at all.
        XCTAssertNil(run(&c, from: 8.25, to: 20, level: 0.003))
        XCTAssertEqual(c.health, .ok)
    }

    func testARestartThatDidntHelpReconnectsSoon() {
        var c = handheld()
        XCTAssertEqual(run(&c, from: 0, to: 8, level: 0), .noSound)
        c.audioRestarted()
        // Still flat zero: judged after the short restart grace (not the whole first-sound window again).
        var at: TimeInterval = 8.25
        var fault: MicFault?
        while at <= 20, fault == nil {
            fault = c.sample(level: 0, packetsSent: Int(at * 50), listening: true, now: t0.addingTimeInterval(at))
            if fault == nil { at += 0.25 }
        }
        XCTAssertEqual(fault, .noSound)
        XCTAssertLessThanOrEqual(at - 8.25, MicCheck.restartGrace + 0.3)
        XCTAssertEqual(c.repairStep(for: .noSound), .reconnect, "one in-place restart per connection")
        c.repairStarted()
        XCTAssertEqual(c.repairs, 1)
        // The reopened connection gets its own in-place restart first again.
        c.connectionOpened()
        XCTAssertEqual(run(&c, from: 30, to: 38, level: 0), .noSound)
        XCTAssertEqual(c.repairStep(for: .noSound), .restartAudio)
    }

    func testOtherFaultsAndMidCallDeathReconnect() {
        var c = handheld()
        XCTAssertEqual(run(&c, from: 0, to: 9, level: 0.01, packets: { _ in 12 }), .notSending)
        XCTAssertEqual(c.repairStep(for: .notSending), .reconnect, "audio not leaving the device: reopen")
        var d = handheld()
        XCTAssertNil(run(&d, from: 0, to: 10, level: 0.01))
        XCTAssertEqual(run(&d, from: 10.25, to: 20, level: 0), .noSound)
        XCTAssertEqual(d.repairStep(for: .noSound), .reconnect, "only the first-sound window restarts in place")
    }

    func testTheMacAlwaysReconnects() {
        var c = MicCheck(); c.connectionOpened()
        XCTAssertEqual(run(&c, from: 0, to: 5, level: 0), .noSound)
        XCTAssertEqual(c.repairStep(for: .noSound), .reconnect)
    }

    func testARestartThatCantRunFallsBackToReconnectOrBroken() {
        var c = handheld()
        XCTAssertEqual(run(&c, from: 0, to: 8, level: 0), .noSound)
        c.audioRestartFailed()
        XCTAssertEqual(c.repairStep(for: .noSound), .reconnect)
        XCTAssertNotEqual(c.health, .broken)
        // Reopens used up: a failed in-place restart leaves the mic broken (no third reopen).
        var d = handheld()
        for _ in 0..<MicCheck.maxRepairs { d.repairStarted(); d.connectionOpened() }
        XCTAssertEqual(run(&d, from: 0, to: 8, level: 0), .noSound, "the in-place restart is still on offer")
        d.audioRestartFailed()
        XCTAssertEqual(d.health, .broken)
    }

    func testFirstSoundWindowIsUnchanged() {
        XCTAssertEqual(MicCheck.firstSoundWithinHandheld, 7)
        XCTAssertEqual(MicCheck.firstSoundWithin, 2.5)
        XCTAssertEqual(MicCheck.maxRepairs, 2)
    }
}

final class ConnectTimelineTests: XCTestCase {
    let tap = Date(timeIntervalSince1970: 2_000)

    func testStagesAreMeasuredFromTheTapOnce() {
        var t = ConnectTimeline(tap: tap)
        XCTAssertTrue(t.mark(.sessionCreated, at: tap.addingTimeInterval(1.2)))
        XCTAssertTrue(t.mark(.channelOpen, at: tap.addingTimeInterval(2.0)))
        XCTAssertTrue(t.mark(.live, at: tap.addingTimeInterval(2.4)))
        t.note("mic restarted in place", at: tap.addingTimeInterval(9.4))
        XCTAssertTrue(t.mark(.firstMicPacket, at: tap.addingTimeInterval(9.6)))
        // A reconnect's second admission doesn't move the first mark.
        XCTAssertFalse(t.mark(.sessionCreated, at: tap.addingTimeInterval(12)))
        XCTAssertFalse(t.due())
        t.mark(.firstTranscript, at: tap.addingTimeInterval(11.1))
        XCTAssertTrue(t.due())
        XCTAssertEqual(t.summary(), "connect timeline: tap → session created +1.2s, channel open +2.0s, call live +2.4s, "
                       + "first mic packet +9.6s, first transcript +11.1s; mic restarted in place +9.4s")
        t.markReported()
        XCTAssertFalse(t.due(ended: true), "one summary per call")
    }

    func testACallThatEndsBeforeAnyTranscriptStillReports() {
        var t = ConnectTimeline(tap: tap)
        t.mark(.sessionCreated, at: tap.addingTimeInterval(3))
        XCTAssertFalse(t.due())
        XCTAssertTrue(t.due(ended: true))
        XCTAssertTrue(t.summary().contains("first transcript never"))
    }
}
