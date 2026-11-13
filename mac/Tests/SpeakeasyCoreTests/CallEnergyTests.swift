import XCTest
@testable import SpeakeasyCore

/// Call energy (t_830db805) and the thermal back-off (SPK-430): the policy, the caption throttle,
/// and the call's energy record.
final class CallEnergyTests: XCTestCase {
    func testCoolPhoneRunsAtThirtyFramesAndKeepsDecorativeMotion() {
        let p = EnergyPolicy()
        XCTAssertFalse(p.backingOff)
        XCTAssertEqual(p.framesPerSecond, 30)
        XCTAssertTrue(p.decorativeMotion)
        XCTAssertTrue(p.shimmer)
        XCTAssertEqual(p.levelInterval, 0.1, accuracy: 1e-9)   // 10 Hz ≤ 15 Hz
        XCTAssertEqual(p.backstop(30, feedLive: true), 30)
    }

    func testFairIsNotHotYet() {
        XCTAssertFalse(EnergyPolicy(heat: .fair).backingOff)
        XCTAssertEqual(EnergyPolicy(heat: .fair).framesPerSecond, 30)
    }

    func testSeriousOrCriticalOrLowPowerBacksOff() {
        for p in [EnergyPolicy(heat: .serious), EnergyPolicy(heat: .critical), EnergyPolicy(lowPower: true)] {
            XCTAssertTrue(p.backingOff)
            XCTAssertEqual(p.framesPerSecond, 20)
            XCTAssertFalse(p.decorativeMotion)
            XCTAssertFalse(p.shimmer)
            XCTAssertGreaterThan(p.levelInterval, EnergyPolicy().levelInterval)
            XCTAssertGreaterThan(p.stageInterval, EnergyPolicy().stageInterval)
            XCTAssertEqual(p.backstop(5, feedLive: false), 10)   // polls stretch, never stop when they must run
        }
    }

    func testTheStateClearingRestoresEverything() {
        var p = EnergyPolicy(heat: .serious)
        p.heat = .nominal
        XCTAssertEqual(p, EnergyPolicy())
        XCTAssertEqual(p.framesPerSecond, 30)
    }

    func testNoAnimationIsEverAboveThirtyFrames() {
        for heat in EnergyPolicy.Heat.allCases {
            for low in [false, true] {
                for call in [false, true] {
                    XCTAssertLessThanOrEqual(EnergyPolicy(heat: heat, lowPower: low, inCall: call).framesPerSecond, 30)
                    XCTAssertLessThanOrEqual(1 / EnergyPolicy(heat: heat, lowPower: low, inCall: call).levelInterval, 15)
                }
            }
        }
    }

    func testDuringACallWithTheFeedUpBackstopPollsWaitMinutes() {
        let p = EnergyPolicy(inCall: true)
        XCTAssertEqual(p.backstop(5, feedLive: true), EnergyPolicy.callBackstop)
        XCTAssertEqual(p.backstop(30, feedLive: true), EnergyPolicy.callBackstop)
        XCTAssertEqual(p.backstop(5, feedLive: false), 5)   // feed down: the poll is all there is
        XCTAssertFalse(p.shimmer)                           // nothing sweeps behind a call
        XCTAssertTrue(p.decorativeMotion)
    }

    func testCaptionChangesAtMostFourTimesASecondAndTheNewestWordsWin() {
        var t = CaptionThrottle()
        let t0 = Date(timeIntervalSinceReferenceDate: 1000)
        XCTAssertEqual(t.offer("On", now: t0), "On")
        XCTAssertNil(t.offer("On it.", now: t0.addingTimeInterval(0.05)))
        XCTAssertNil(t.offer("On it. I'll", now: t0.addingTimeInterval(0.1)))
        XCTAssertEqual(t.pending, "On it. I'll")
        XCTAssertEqual(t.offer("On it. I'll line", now: t0.addingTimeInterval(0.26)), "On it. I'll line")
        XCTAssertNil(t.pending)
        // 20 offers a second for 2 s: at most 4 a second reach the screen.
        var shown = 0
        for i in 0..<40 {
            if t.offer("w\(i)", now: t0.addingTimeInterval(1 + Double(i) * 0.05)) != nil { shown += 1 }
        }
        XCTAssertLessThanOrEqual(shown, 9)
        // A new speaker's words show at once.
        XCTAssertEqual(t.offer("Hello", now: t0.addingTimeInterval(2.96), immediate: true), "Hello")
        // The same words again: nothing to do.
        XCTAssertNil(t.offer("Hello", now: t0.addingTimeInterval(5)))
    }

    func testTheRecordSplitsLayoutTimeAndTracksHeat() {
        let t0 = Date(timeIntervalSinceReferenceDate: 5000)
        var r = CallEnergyRecord(at: t0, cpuSeconds: 10, layout: "home", heat: .nominal, lowPower: false)
        r.setLayout("transcript", at: t0.addingTimeInterval(100))
        r.setHeat(.fair, at: t0.addingTimeInterval(120))
        r.setLayout("background", at: t0.addingTimeInterval(150))
        r.setHeat(.serious, at: t0.addingTimeInterval(200))
        r.setLayout("home", at: t0.addingTimeInterval(240))
        r.setHeat(.fair, at: t0.addingTimeInterval(250))
        r.countFrames(6000)
        let body = r.payload(endedAt: t0.addingTimeInterval(300), cpuSeconds: 40, interactionID: "int_1",
                             app: "77.0 (1)", device: "iPhone18,1")
        XCTAssertEqual(body["duration_s"] as? Double, 300)
        XCTAssertEqual(body["cpu_s"] as? Double, 30)
        XCTAssertEqual(body["cpu_pct"] as? Double, 10)
        let layout = body["layout_s"] as? [String: Double]
        XCTAssertEqual(layout, ["home": 160, "transcript": 50, "background": 90])
        XCTAssertEqual(body["thermal_start"] as? String, "nominal")
        XCTAssertEqual(body["thermal_max"] as? String, "serious")
        let changes = body["thermal"] as? [[String: Any]]
        XCTAssertEqual(changes?.map { $0["state"] as? String }, ["fair", "serious", "fair"])
        XCTAssertEqual(changes?.map { $0["at_s"] as? Double }, [120, 200, 250])
        XCTAssertEqual(body["backoff_s"] as? Double, 50)
        XCTAssertEqual(body["low_power"] as? Bool, false)
        XCTAssertEqual(body["orb_fps"] as? Double, 28.6)   // 6000 frames / 210 s in front
        XCTAssertEqual(body["interaction_id"] as? String, "int_1")
        XCTAssertNil(body["words"])
    }

    func testLowPowerCountsAsBackingOffInTheRecord() {
        let t0 = Date(timeIntervalSinceReferenceDate: 0)
        var r = CallEnergyRecord(at: t0, cpuSeconds: 0, layout: "home", heat: .nominal, lowPower: true)
        r.setLowPower(false, at: t0.addingTimeInterval(30))
        let body = r.payload(endedAt: t0.addingTimeInterval(60), cpuSeconds: 3, interactionID: nil, app: "a", device: "d")
        XCTAssertEqual(body["low_power"] as? Bool, true)
        XCTAssertEqual(body["low_power_s"] as? Double, 30)
        XCTAssertEqual(body["backoff_s"] as? Double, 30)
        XCTAssertNil(body["interaction_id"])
        XCTAssertNil(body["orb_fps"])
    }

    func testTheRecordIsSmallAndSerializable() throws {
        let t0 = Date()
        var r = CallEnergyRecord(at: t0, cpuSeconds: 0, layout: "home", heat: .nominal, lowPower: false)
        for i in 0..<20 { r.setHeat(i % 2 == 0 ? .fair : .nominal, at: t0.addingTimeInterval(Double(i))) }
        let body = r.payload(endedAt: t0.addingTimeInterval(30), cpuSeconds: 1, interactionID: "x", app: "a", device: "d")
        let data = try JSONSerialization.data(withJSONObject: body)
        XCTAssertLessThan(data.count, 2048)
    }
}
