import Foundation
import XCTest
@testable import SpeakeasyCore

/// Listening mode's states, wording, dead-mic monitor, rebuild budget and take-off timing.
final class RoomListeningTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_791_468_000)

    // MARK: Take-off

    func testWordsHeardWhileConnectingAreHandedOver() {
        XCTAssertEqual(RoomTakeoffPolicy.decide(takeoffAt: t0, liveAt: t0 + 0.5, now: t0 + 0.5,
                                                heardWords: true, speechSinceLive: false), .handOverWords)
        // Even before the call is live, and even if the user keeps talking after.
        XCTAssertEqual(RoomTakeoffPolicy.decide(takeoffAt: t0, liveAt: nil, now: t0 + 0.3,
                                                heardWords: true, speechSinceLive: false), .handOverWords)
        XCTAssertEqual(RoomTakeoffPolicy.decide(takeoffAt: t0, liveAt: t0 + 1, now: t0 + 1,
                                                heardWords: true, speechSinceLive: true), .handOverWords)
    }

    func testQuietCallLiveEarlyWaitsUntilTwoSecondsAfterTakeoff() {
        XCTAssertEqual(RoomTakeoffPolicy.decide(takeoffAt: t0, liveAt: t0 + 0.5, now: t0 + 0.5,
                                                heardWords: false, speechSinceLive: false), .waitUntil(t0 + 2))
        XCTAssertEqual(RoomTakeoffPolicy.decide(takeoffAt: t0, liveAt: t0 + 0.5, now: t0 + 2,
                                                heardWords: false, speechSinceLive: false), .nudgeNow)
    }

    func testQuietCallLiveLateNudgesRightAway() {
        XCTAssertEqual(RoomTakeoffPolicy.decide(takeoffAt: t0, liveAt: t0 + 3, now: t0 + 3,
                                                heardWords: false, speechSinceLive: false), .nudgeNow)
    }

    func testSpeechOnTheLiveCallMeansNoNudge() {
        // AE3: the user started talking at 1.5 s; no context response.
        XCTAssertEqual(RoomTakeoffPolicy.decide(takeoffAt: t0, liveAt: t0 + 0.5, now: t0 + 2,
                                                heardWords: false, speechSinceLive: true), .skip)
        XCTAssertEqual(RoomTakeoffPolicy.decide(takeoffAt: t0, liveAt: t0 + 3, now: t0 + 3,
                                                heardWords: false, speechSinceLive: true), .skip)
    }

    func testNotLiveYetWaitsForLive() {
        XCTAssertEqual(RoomTakeoffPolicy.decide(takeoffAt: t0, liveAt: nil, now: t0 + 5,
                                                heardWords: false, speechSinceLive: false), .waitForLive)
    }

    func testTakeoffSpeechLevelMatchesTheMicCheck() {
        XCTAssertEqual(RoomTakeoffPolicy.speechLevel, MicCheck.speechLevel)
        XCTAssertEqual(RoomTakeoffPolicy.quietWindow, 2)
    }

    // MARK: Dead-mic monitor

    private func feed(_ monitor: inout RoomSignalMonitor, from: TimeInterval, to: TimeInterval,
                      step: TimeInterval = 0.1, level: Double) -> RoomSignalMonitor.Reading {
        var reading = monitor.reading
        var t = from
        while t <= to + 0.000_1 {
            reading = monitor.sample(level: level, now: t0 + t)
            t += step
        }
        return reading
    }

    func testTenSecondsOfZerosIsNotHearing() {
        var monitor = RoomSignalMonitor()
        XCTAssertEqual(feed(&monitor, from: 0, to: 9.5, level: 0), .waiting)
        XCTAssertEqual(feed(&monitor, from: 9.6, to: 10.2, level: 0), .silent)
    }

    func testAQuietRoomIsStillHearing() {
        var monitor = RoomSignalMonitor()
        // A silent room on a working mic: tiny but never exactly zero.
        XCTAssertEqual(feed(&monitor, from: 0, to: 60, level: 0.000_2), .hearing)
    }

    func testDeadMicMidSessionThenSignalReturns() {
        var monitor = RoomSignalMonitor()
        XCTAssertEqual(feed(&monitor, from: 0, to: 5, level: 0.01), .hearing)
        XCTAssertEqual(feed(&monitor, from: 5.1, to: 14.9, level: 0), .hearing)   // under 10 s of zeros
        XCTAssertEqual(feed(&monitor, from: 15, to: 16, level: 0), .silent)
        XCTAssertEqual(monitor.sample(level: 0.003, now: t0 + 16.1), .hearing)
    }

    func testBuffersThatStopArrivingCountAsSilence() {
        var monitor = RoomSignalMonitor()
        XCTAssertEqual(feed(&monitor, from: 0, to: 2, level: 0.02), .hearing)
        XCTAssertEqual(monitor.tick(now: t0 + 11), .hearing)
        XCTAssertEqual(monitor.tick(now: t0 + 12.1), .silent)
    }

    func testReopeningTheMicJudgesItAfresh() {
        var monitor = RoomSignalMonitor()
        XCTAssertEqual(feed(&monitor, from: 0, to: 11, level: 0), .silent)
        monitor.reopened(now: t0 + 20)
        XCTAssertEqual(monitor.reading, .waiting)
        XCTAssertEqual(monitor.tick(now: t0 + 25), .waiting)
        XCTAssertEqual(monitor.sample(level: 0.01, now: t0 + 26), .hearing)
        // A mic that was working keeps showing as working across a reopen.
        monitor.reopened(now: t0 + 40)
        XCTAssertEqual(monitor.reading, .hearing)
    }

    func testMonitorReadingsMapToStates() {
        XCTAssertEqual(RoomListeningState.capturing(.waiting, since: t0), .preparing(.waitingForSound))
        XCTAssertEqual(RoomListeningState.capturing(.hearing, since: t0), .listening(since: t0))
        XCTAssertEqual(RoomListeningState.capturing(.silent, since: t0), .notHearing(since: t0))
    }

    // MARK: Rebuild budget

    func testThirdFailureWithinAMinuteGivesUp() {
        var budget = RoomRebuildBudget()
        XCTAssertTrue(budget.recordFailure(now: t0))
        XCTAssertTrue(budget.recordFailure(now: t0 + 20))
        XCTAssertFalse(budget.recordFailure(now: t0 + 40))
    }

    func testFailuresSpreadOutKeepRestarting() {
        var budget = RoomRebuildBudget()
        XCTAssertTrue(budget.recordFailure(now: t0))
        XCTAssertTrue(budget.recordFailure(now: t0 + 50))
        XCTAssertTrue(budget.recordFailure(now: t0 + 70))   // the first one is over a minute old
        XCTAssertEqual(budget.retryDelay, 1.0)
    }

    // MARK: State

    func testOnMeansTheMicIsHeldForListening() {
        XCTAssertTrue(RoomListeningState.preparing(.starting).isOn)
        XCTAssertTrue(RoomListeningState.listening(since: t0).isOn)
        XCTAssertTrue(RoomListeningState.notHearing(since: t0).isOn)
        XCTAssertFalse(RoomListeningState.off.isOn)
        XCTAssertFalse(RoomListeningState.failed(.micStopped).isOn)
        XCTAssertFalse(RoomListeningState.unavailable(.systemTooOld).isOn)
    }

    // MARK: Presentation

    func testOff() {
        let p = presentRoom(.off, now: t0, heardWords: false, warnings: [.bluetoothInput])
        XCTAssertEqual(p.title, "Listening mode off")
        XCTAssertEqual(p.detail, "")
        XCTAssertNil(p.hint)
        XCTAssertFalse(p.isOn)
        XCTAssertEqual(p.warnings, [])   // warnings only while on
    }

    func testPreparingNeverSaysItIsListening() {
        let starting = presentRoom(.preparing(.starting), now: t0, heardWords: false)
        XCTAssertEqual(starting.title, "Starting listening mode…")
        XCTAssertEqual(starting.detail, "Getting ready · not hearing yet")
        XCTAssertEqual(starting.hint, "Won't answer until you turn it off")
        XCTAssertTrue(starting.isOn)
        XCTAssertEqual(presentRoom(.preparing(.downloadingModel(fraction: 0.4)), now: t0, heardWords: false).detail,
                       "Downloading the speech model · 40%")
        XCTAssertEqual(presentRoom(.preparing(.downloadingModel(fraction: nil)), now: t0, heardWords: false).detail,
                       "Downloading the speech model")
        XCTAssertEqual(presentRoom(.preparing(.waitingForSound), now: t0, heardWords: false).detail,
                       "Waiting for sound from the mic")
        for step in [RoomPreparation.starting, .downloadingModel(fraction: 0.5), .waitingForSound] {
            let p = presentRoom(.preparing(step), now: t0, heardWords: false)
            XCTAssertNotEqual(p.title, "Listening mode on")
            XCTAssertNotEqual(p.title, "Listening")
        }
    }

    func testListeningShowsElapsedTime() {
        let p = presentRoom(.listening(since: t0), now: t0 + 724, heardWords: true)
        XCTAssertEqual(p.title, "Listening mode on")
        XCTAssertEqual(p.detail, "12m 04s")
        XCTAssertEqual(p.hint, "Won't answer until you turn it off")
        XCTAssertEqual(p.tone, .plain)
        XCTAssertTrue(p.isOn)
    }

    func testListeningWithNothingHeardYetSaysSo() {
        let p = presentRoom(.listening(since: t0), now: t0 + 42, heardWords: false)
        XCTAssertEqual(p.detail, "42s · nothing heard yet")
    }

    func testNotHearingIsAnHonestWarning() {
        let p = presentRoom(.notHearing(since: t0), now: t0 + 600, heardWords: true)
        XCTAssertEqual(p.title, "Listening mode can't hear anything")
        XCTAssertEqual(p.detail, "No sound from the mic · check the input in Sound settings")
        XCTAssertEqual(p.tone, .warning)
        XCTAssertTrue(p.isOn)
    }

    func testFailedSaysWhatStopped() {
        let transcriber = presentRoom(.failed(.transcriberStopped), now: t0, heardWords: true)
        XCTAssertEqual(transcriber.title, "Listening mode stopped")
        XCTAssertEqual(transcriber.detail, "Transcription kept stopping · turn it on again")
        XCTAssertEqual(transcriber.tone, .error)
        XCTAssertFalse(transcriber.isOn)
        XCTAssertEqual(presentRoom(.failed(.micStopped), now: t0, heardWords: true).detail,
                       "Lost the microphone · turn it on again")
    }

    func testUnavailableGivesTheReason() {
        let cases: [(RoomUnavailability, String)] = [
            (.systemTooOld, "Needs macOS 26 or later"),
            (.pluginTooOld, "Your Hermes runs an older Speakeasy plugin: update it, then run hermes voice reload"),
            (.noTranscriber, "On-device transcription isn't available on this Mac"),
            (.languageNotSupported(language: "Klingon"), "On-device transcription doesn't support Klingon yet"),
            (.modelDownloadFailed, "Couldn't download the speech model · check the connection and try again"),
            (.micDenied, "Allow Speakeasy to use the microphone in System Settings › Privacy & Security › Microphone"),
            (.noMicrophone, "No microphone found"),
        ]
        for (reason, detail) in cases {
            let p = presentRoom(.unavailable(reason), now: t0, heardWords: false)
            XCTAssertEqual(p.title, "Listening mode unavailable")
            XCTAssertEqual(p.detail, detail)
            XCTAssertEqual(p.tone, .warning)
            XCTAssertFalse(p.isOn)
        }
    }

    func testWarningsShowWhileOnInAFixedOrder() {
        let p = presentRoom(.listening(since: t0), now: t0 + 5, heardWords: true,
                            warnings: [.speechRecognitionDenied, .bluetoothInput])
        XCTAssertEqual(p.warnings, [
            "Bluetooth mics like AirPods mostly hear the wearer, not the room, and switch to call audio",
            "Speech recognition is off for Speakeasy, so words said right after turning it off won't be caught",
        ])
        XCTAssertEqual(presentRoom(.preparing(.starting), now: t0, heardWords: false, warnings: [.bluetoothInput]).warnings.count, 1)
        XCTAssertEqual(presentRoom(.failed(.micStopped), now: t0, heardWords: false, warnings: [.bluetoothInput]).warnings, [])
    }

    func testNoStateUsesTheBareCallLabel() {
        let states: [RoomListeningState] = [
            .off, .preparing(.starting), .preparing(.waitingForSound), .listening(since: t0), .notHearing(since: t0),
            .failed(.transcriberStopped), .unavailable(.micDenied),
        ]
        for state in states {
            XCTAssertNotEqual(presentRoom(state, now: t0 + 1, heardWords: true).title, "Listening")
            XCTAssertTrue(presentRoom(state, now: t0 + 1, heardWords: true).title.lowercased().contains("listening mode"))
        }
    }
}
