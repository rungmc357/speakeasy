import XCTest
@testable import SpeakeasyCore

/// The UX overhaul's client logic: voice phases, the live caption, call problems with a fix,
/// job progress, card actions, lighter pages and the capability gate.
final class UXOverhaulTests: XCTestCase {
    let now = Date(timeIntervalSince1970: 1_800_000_000)

    func live() -> VoiceState { var s = VoiceState(); s.now = now; s.connection = .live; s.interactionID = "vi"; return s }

    // MARK: Voice phases

    func testFourVoicePhases() {
        var s = live()
        XCTAssertEqual(s.voicePhase(userTalking: false), .listening)
        XCTAssertEqual(s.voicePhase(userTalking: true), .hearing)
        s.exchange = Exchange(you: "What's the weather?"); s.lastInputAt = now - 1
        XCTAssertEqual(s.voicePhase(userTalking: false), .thinking, "user stopped, reply not started")
        XCTAssertEqual(present(s).mark, .thinking)
        XCTAssertEqual(present(s).primary, "Thinking…")
        s.speech = .speaking(lastDelta: now); s.exchange.replyStarted = true
        XCTAssertEqual(s.voicePhase(userTalking: false), .speaking)
        s.mic = .muted; s.speech = .idle
        XCTAssertEqual(s.voicePhase(userTalking: false), .muted)
        XCTAssertEqual(VoiceState().voicePhase(userTalking: true), .off, "no call, no phase")
    }

    func testThinkingEndsWhenTheReplyStartsOrTimesOut() {
        var s = live()
        s.exchange = Exchange(you: "Hi"); s.lastInputAt = now - VoiceState.thinkingFor - 1
        XCTAssertFalse(s.isThinking, "an unanswered turn doesn't spin forever")
        s.lastInputAt = now
        XCTAssertTrue(s.isThinking)
        s.exchange.replyStarted = true
        XCTAssertFalse(s.isThinking)
        s.exchange = Exchange(you: "[cough]"); s.lastInputAt = now
        XCTAssertFalse(s.isThinking, "non-speech isn't a turn")
    }

    func testCaptionTailCutsOnAWord() {
        let long = "Three things tomorrow. Standup at nine thirty, then a design review at eleven that runs an hour, then a call at four."
        let t = LiveCaption.tail(long, limit: 60)
        XCTAssertLessThanOrEqual(t.count, 61)
        XCTAssertFalse(t.hasPrefix("…") && t.dropFirst().first == " ")
        XCTAssertTrue(long.hasSuffix(t.replacingOccurrences(of: "…", with: "")), "it's the end of the line")
        XCTAssertTrue(t.hasPrefix("…") ? long.contains(" " + t.dropFirst()) : true, "starts at a word")
        XCTAssertEqual(LiveCaption.tail("Short."), "Short.")
    }

    func testCaptionShowsWhoIsSpeakingNow() {
        let e = Exchange(you: "What's on tomorrow?", assistant: "Standup at 9.", replyStarted: true)
        XCTAssertEqual(LiveCaption.current(e, phase: .speaking)?.you, false)
        XCTAssertEqual(LiveCaption.current(Exchange(you: "What's on", assistant: ""), phase: .hearing)?.you, true)
        XCTAssertNil(LiveCaption.current(Exchange(), phase: .listening))
    }

    // MARK: Problems with a fix

    func testMicProblemsGetAFix() {
        var s = live()
        XCTAssertNil(CallProblem.of(s))
        s.micHealth = .broken
        XCTAssertEqual(CallProblem.of(s)?.fix, .restartCall)
        XCTAssertEqual(CallProblem.of(s)?.fixLabel, "Restart call")
        s = VoiceState(); s.connection = .failed("Microphone access is off — enable Speakeasy in System Settings")
        XCTAssertEqual(CallProblem.of(s)?.fix, .openMicSettings)
        XCTAssertEqual(CallProblem.of(s)?.fixLabel, "Open Settings")
        s.connection = .failed("Not connected to Hermes — open Settings › Connection to pair")
        XCTAssertNil(CallProblem.of(s), "pairing says where to go already")
        s.connection = .failed("Couldn't connect to Hermes. Tap to try again.")
        XCTAssertEqual(CallProblem.of(s)?.fix, .retry)
        XCTAssertFalse(CallProblem.of(s)?.detail?.contains("Tap") ?? true)
    }

    // MARK: Job progress

    func info(_ json: [String: Any]) -> WorkInfo {
        var j = json; j["run_id"] = j["run_id"] ?? "run_1"; j["status"] = j["status"] ?? "working"
        return WorkInfo(json: j)!
    }

    func testProgressFieldsDecodeAndAreOptional() {
        let i = info(["started_at": now.timeIntervalSince1970 - 240, "step_index": 3, "step_count": 5, "eta_seconds": 120])
        XCTAssertEqual(i.stepIndex, 3); XCTAssertEqual(i.stepCount, 5); XCTAssertEqual(i.etaSeconds, 120)
        XCTAssertEqual(JobProgress.line(i, now: now), "4 min · about 2 min left", "no step count")
        let old = info([:])
        XCTAssertNil(old.stepIndex); XCTAssertNil(JobProgress.eta(old)); XCTAssertNil(old.failureReason)
    }

    func testFailureReasonOnlyWhenFailedAndNeverUnknown() {
        XCTAssertNil(info(["failure_reason": "DNS check failed"]).failureReason, "only failed tasks carry one")
        let f = info(["status": "failed", "failure_reason": "DNS check failed."])
        XCTAssertEqual(JobProgress.failureReason(f), "DNS check failed.")
        XCTAssertNotEqual(JobProgress.failureReason(info(["status": "failed", "failure_reason": "unknown"])), "unknown")
    }

    func testDurationsAndRelativeTimes() {
        XCTAssertEqual(JobProgress.duration(42), "42 s")
        XCTAssertEqual(JobProgress.duration(14 * 60), "14 min")
        XCTAssertEqual(JobProgress.duration(3900), "1 h 05 min")
        XCTAssertEqual(JobProgress.relative(now - 10, now: now), "just now")
        XCTAssertEqual(JobProgress.relative(now - 180, now: now), "3 min ago")
    }

    func testStepFallsBackToThePlan() {
        let i = info(["plan": ["A", "B", "C"], "events": [["kind": "milestone", "text": "A"]]])
        XCTAssertEqual(JobProgress.step(i)?.index, 2)
        XCTAssertEqual(JobProgress.step(i)?.count, 3)
    }

    // MARK: Decisions

    func testQuestionOnARunningTaskNeedsYou() {
        let q: [String: Any] = ["kind": "question", "question": "Which?", "options": ["A", "B"]]
        let t = TaskItem(json: ["task_id": "t", "run_id": "run_t", "status": "working", "updated_at": now.timeIntervalSince1970 - 5,
                                "result": ["views": [q]]])!
        let job = JobBoard.jobs([t], now: now)[0]
        XCTAssertEqual(job.stage, .needsYou(.decide), "a question never sits under a Running pill")
        XCTAssertEqual(OrbState.of(jobs: [job], userTalking: false), .needsYou(1))
        XCTAssertEqual(JobBoard.jobs([t], answered: ["t"], now: now)[0].stage, .running)
    }

    // MARK: Approvals

    func testApprovalCarriesItsContext() {
        let a = ApprovalInfo(json: ["run_id": "r", "request_id": "q", "description": "Run the deploy script",
                                    "command": "./deploy.sh staging", "reason": "Tests pass."])!
        XCTAssertEqual(a.context(task: nil).target, "./deploy.sh staging")
        XCTAssertEqual(a.context(task: nil).why, "Tests pass.")
        let bare = ApprovalInfo(json: ["run_id": "r", "request_id": "q", "description": "Delete a file"])!
        let t = TaskItem(json: ["task_id": "t", "run_id": "r", "status": "waiting_for_approval", "title": "Clean up",
                                "events": [["kind": "request", "text": "Tidy the downloads folder"]]])!
        XCTAssertEqual(bare.context(task: t).target, "Clean up")
        XCTAssertTrue(bare.context(task: t).why?.contains("Tidy the downloads folder") ?? false)
    }

    // MARK: Card actions

    func testCardActionsParseAndCap() {
        let c = ViewCard(kind: "home", number: 2, data: ["title": "Office", "actions": [
            ["id": "toggle", "label": "Turn off", "style": "primary"],
            ["id": "dim", "label": "Dim", "value": 20, "confirm": true],
            ["label": "No id"], ["id": "a"], ["id": "b", "label": "B"], ["id": "c", "label": "C"], ["id": "d", "label": "D"]]])
        XCTAssertEqual(c.actions.map(\.id), ["toggle", "dim", "b", "c"], "invalid ones dropped, at most four")
        XCTAssertEqual(c.actions[0].style, .primary)
        XCTAssertTrue(c.actions[1].confirm)
        XCTAssertEqual(c.actions[1].value.double, 20)
        XCTAssertEqual(c.viewIndex, 1, "the route addresses result.views by index")
        XCTAssertTrue(ViewCard(kind: "quote", data: [:]).actions.isEmpty)
    }

    func testMapsLinkAndCopyText() {
        let place = ViewCard(kind: "place", data: ["name": "Corner Coffee", "address": "100 Main St"])
        XCTAssertEqual(MapsLink.url(place)?.host, "maps.apple.com")
        XCTAssertTrue(MapsLink.url(place)?.absoluteString.contains("100%20Main%20St") ?? false)
        let route = ViewCard(kind: "route", data: ["to": "Airport"])
        XCTAssertTrue(MapsLink.url(route)?.absoluteString.contains("daddr=Airport") ?? false)
        let chart = ViewCard(kind: "chart", data: ["title": "Latency", "labels": ["Mon", "Tue"],
                                                   "series": [["name": "p95", "values": [420, 180.5]]]])
        XCTAssertEqual(CardText.plain(chart), "Latency\np95\nMon: 420.0\nTue: 180.5")
    }

    // MARK: Capabilities and lighter pages

    func testCapabilitiesGateNewControls() throws {
        let new = try JSONDecoder().decode(ServerStatus.self, from: Data(#"{"capabilities":{"card_actions":true,"job_progress":true}}"#.utf8))
        XCTAssertEqual(new.capabilities?.cardActions, true)
        XCTAssertEqual(new.capabilities?.spokenChoices, false, "a missing flag is off")
        let old = try JSONDecoder().decode(ServerStatus.self, from: Data("{}".utf8))
        XCTAssertNil(old.capabilities, "an older plugin: new controls hidden")
    }

    func testSummaryPointsDecodeCapped() {
        let i = info(["status": "completed", "result": ["spoken": "Done.", "full": "Long text",
                                                        "summary_points": ["a", "b", "", "c", "d", "e"]]])
        XCTAssertEqual(i.result?.summaryPoints, ["a", "b", "c", "d"])
    }
}
