import XCTest
@testable import SpeakeasyCore

final class PluginUpdateFlowTests: XCTestCase {
    let t0 = Date(timeIntervalSince1970: 1_800_000_000)

    func testNoticeShowsOncePerVersionAndANewerOneShowsAgain() {
        var r = PluginUpdateRecord()
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.0", record: r, note: "Faster", inCall: false),
                       .available(version: "0.9.1", note: "Faster"))
        r = PluginUpdateFlow.dismiss(r, version: "0.9.1")
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.0", record: r, inCall: false), .none)
        // A newer version is a new notice.
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.2", running: "0.9.0", record: r, inCall: false),
                       .available(version: "0.9.2", note: nil))
        // Up to date: nothing.
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.0", running: "0.9.0", record: PluginUpdateRecord(), inCall: false), .none)
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: nil, record: PluginUpdateRecord(), inCall: false), .none)
    }

    func testRecordSurvivesARoundTrip() throws {
        let r = PluginUpdateRecord(dismissed: "0.9.1", target: "0.9.2", runID: "run_1", startedAt: t0, lastRunning: "0.9.0")
        let back = try JSONDecoder().decode(PluginUpdateRecord.self, from: JSONEncoder().encode(r))
        XCTAssertEqual(back, r)
    }

    func testUpdateThenTheNewVersionIsConfirmedAndCleared() {
        var r = PluginUpdateFlow.started(PluginUpdateRecord(lastRunning: "0.9.0"), version: "0.9.1", runID: "run_7", now: t0)
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.0", record: r, inCall: false, now: t0),
                       .updating(version: "0.9.1", runID: "run_7"))
        r = PluginUpdateFlow.observe(r, running: "0.9.1", jobSettled: true, now: t0.addingTimeInterval(5))
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.1", record: r, inCall: false), .updated(version: "0.9.1"))
        r = PluginUpdateFlow.acknowledge(r)
        XCTAssertNil(r.target)
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.1", record: r, inCall: false), .none)
    }

    func testAJobThatFinishedWithoutTheVersionMovingSaysItDidntTakeAfterTheSettle() {
        var r = PluginUpdateFlow.started(PluginUpdateRecord(), version: "0.9.1", runID: "run_7", now: t0)
        r = PluginUpdateFlow.observe(r, running: "0.9.0", jobSettled: true, now: t0)
        XCTAssertEqual(r.jobEndedAt, t0)
        let soon = t0.addingTimeInterval(PluginUpdateFlow.settleSeconds - 1)
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.0", record: r, inCall: false, now: soon),
                       .updating(version: "0.9.1", runID: "run_7"))
        let later = t0.addingTimeInterval(PluginUpdateFlow.settleSeconds + 1)
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.0", record: r, inCall: false, now: later),
                       .didntTake(version: "0.9.1", runID: "run_7"))
        // Dismissed: quiet for that version.
        r = PluginUpdateFlow.dismiss(r, version: "0.9.1")
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.0", record: r, inCall: false, now: later), .none)
    }

    func testNoVerdictWhileACallIsOpenOrJustEnded() {
        // The new files load after hang-up, so the 30 s count starts when the call ends.
        var r = PluginUpdateFlow.started(PluginUpdateRecord(), version: "0.9.1", runID: "run_7", now: t0)
        r = PluginUpdateFlow.observe(r, running: "0.9.0", jobSettled: true, now: t0)
        let later = t0.addingTimeInterval(600)
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.0", record: r, inCall: true, now: later),
                       .updating(version: "0.9.1", runID: "run_7"))
        let hungUp = later.addingTimeInterval(-10)
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.0", record: r, inCall: false,
                                              lastCallEnded: hungUp, now: later),
                       .updating(version: "0.9.1", runID: "run_7"))
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.0", record: r, inCall: false,
                                              lastCallEnded: hungUp, now: later.addingTimeInterval(25)),
                       .didntTake(version: "0.9.1", runID: "run_7"))
    }

    func testAnUpdateFromElsewhereIsConfirmedToo() {
        var r = PluginUpdateFlow.observe(PluginUpdateRecord(), running: "0.9.0", jobSettled: false)
        XCTAssertNil(r.target)
        r = PluginUpdateFlow.observe(r, running: "0.9.1", jobSettled: false)    // updated by voice mid-call
        XCTAssertEqual(PluginUpdateFlow.phase(latest: "0.9.1", running: "0.9.1", record: r, inCall: false), .updated(version: "0.9.1"))
    }

    func testTheRequestUsesHermesOwnUpdaterAndNeverForcesAReload() {
        let text = PluginUpdateFlow.requestText(version: "0.9.1")
        XCTAssertTrue(text.contains("`hermes plugins update speakeasy`"))
        XCTAssertTrue(text.contains("without --force"))
        XCTAssertTrue(text.contains("Do not run `hermes voice reload`"))
        XCTAssertTrue(text.contains("to 0.9.1"))
        XCTAssertLessThanOrEqual(text.count, 4000)
    }

    func testChangelogHeadline() {
        let md = """
        # What's new
        ## Mac 0.3.11 — Watch the work
        ## Plugin 0.9.1 — Calls pick up faster on a slow connection
        - stuff
        ## Plugin 0.9.0 - Older
        ## Plugin 0.8.9
        ## Plugin 0.8.8 —
        """
        XCTAssertEqual(PluginUpdateFlow.changelogHeadline(md, version: "0.9.1"), "Calls pick up faster on a slow connection")
        XCTAssertEqual(PluginUpdateFlow.changelogHeadline(md, version: "0.9.0"), "Older")
        XCTAssertNil(PluginUpdateFlow.changelogHeadline(md, version: "0.3.11"))
        XCTAssertNil(PluginUpdateFlow.changelogHeadline(md, version: "0.9"))
        // An entry with no title: no what's-new line at all (the card then omits line 2).
        XCTAssertNil(PluginUpdateFlow.changelogHeadline(md, version: "0.8.9"))
        XCTAssertNil(PluginUpdateFlow.changelogHeadline(md, version: "0.8.8"))
    }

    func testStartJobsCapabilityDecodes() throws {
        let caps = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"start_jobs": true, "task_messages": true}"#.utf8))
        XCTAssertTrue(caps.startJobs)
        let old = try JSONDecoder().decode(ServerCapabilities.self, from: Data(#"{"task_messages": true}"#.utf8))
        XCTAssertFalse(old.startJobs)
    }
}
